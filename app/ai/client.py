from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
)
from openai.types.chat.chat_completion import Choice
from openai.types.completion_usage import CompletionUsage
from pydantic import BaseModel, ValidationError

from app.core.config import LLMSettings

if TYPE_CHECKING:
    from httpx import Client

logger = logging.getLogger(__name__)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
TOKENS_PER_MILLION = Decimal(1_000_000)


class LLMErrorCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    INVALID_RESPONSE = "invalid_response"
    AUTHENTICATION = "authentication"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    QUOTA = "quota"
    UNAVAILABLE = "unavailable"
    REQUEST_REJECTED = "request_rejected"
    REFUSED = "refused"
    OUTPUT_LIMIT = "output_limit"
    INCOMPLETE = "incomplete"


class LLMOperation(StrEnum):
    CLASSIFICATION = "classification"
    SUMMARY = "summary"


@dataclass(frozen=True, slots=True)
class LLMRequestSettings:
    model: str
    max_output_tokens: int
    max_input_chars: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    total_tokens: int
    estimated_cost_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class LLMResult[OutputT: BaseModel]:
    output: OutputT
    model: str
    response_id: str
    request_id: str | None
    usage: LLMUsage | None

    def with_output[ResultT: BaseModel](self, output: ResultT) -> LLMResult[ResultT]:
        return LLMResult(
            output, self.model, self.response_id, self.request_id, self.usage
        )

    def invalid_response(self, message: str) -> LLMError:
        return LLMError(
            LLMErrorCode.INVALID_RESPONSE,
            message,
            request_id=self.request_id,
            usage=self.usage,
            model=self.model,
        )


class LLMError(Exception):
    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        usage: LLMUsage | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.usage = usage
        self.model = model


def _status_error(error: APIStatusError) -> LLMError:
    if error.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        code, message = (
            LLMErrorCode.AUTHENTICATION,
            "Check the API key and model access.",
        )
    elif error.status_code == HTTPStatus.REQUEST_TIMEOUT:
        code, message = LLMErrorCode.TIMEOUT, "The provider request timed out."
    elif error.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        if error.code in ("insufficient_quota", "billing_hard_limit_reached"):
            code, message = LLMErrorCode.QUOTA, "The provider quota is exhausted."
        else:
            code, message = (
                LLMErrorCode.RATE_LIMIT,
                "The provider rate limit was reached.",
            )
    elif error.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        code, message = (
            LLMErrorCode.UNAVAILABLE,
            "The provider is temporarily unavailable.",
        )
    else:
        code, message = (
            LLMErrorCode.REQUEST_REJECTED,
            "The provider rejected the request.",
        )
    return LLMError(code, message, request_id=error.request_id)


class _GroqMetadata(BaseModel):
    id: str | None = None


class _GroqCompletion(BaseModel):
    object: Literal["chat.completion"]
    id: str
    model: str
    choices: tuple[Choice]
    usage: CompletionUsage | None = None
    x_groq: _GroqMetadata | None = None


def _response_failure(choice: Choice) -> tuple[LLMErrorCode, str] | None:
    if choice.finish_reason == "length":
        return LLMErrorCode.OUTPUT_LIMIT, "The output reached its token limit."
    if choice.finish_reason == "content_filter" or choice.message.refusal is not None:
        return LLMErrorCode.REFUSED, "The provider filtered or refused the response."
    if choice.finish_reason != "stop":
        return LLMErrorCode.INCOMPLETE, "The provider returned an incomplete response."
    return None


class LLMClient:
    def __init__(
        self, settings: LLMSettings, *, http_client: Client | None = None
    ) -> None:
        self._settings = settings
        timeout = settings.timeout_seconds
        if http_client is None:
            http_client = DefaultHttpxClient(timeout=timeout)
        http_client.follow_redirects = False
        self._client = OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=GROQ_BASE_URL,
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._client.close()

    def request_settings(
        self, operation: LLMOperation | None = None
    ) -> LLMRequestSettings:
        settings = self._settings
        limits = {
            LLMOperation.CLASSIFICATION: settings.classification_max_output_tokens,
            LLMOperation.SUMMARY: settings.summary_max_output_tokens,
        }
        return LLMRequestSettings(
            model=settings.model,
            max_output_tokens=limits.get(operation) or settings.max_output_tokens,
            max_input_chars=settings.max_input_chars,
            timeout_seconds=settings.timeout_seconds,
        )

    def generate[OutputT: BaseModel](
        self,
        *,
        instructions: str,
        prompt: str,
        schema: type[OutputT],
        operation: LLMOperation | None = None,
    ) -> LLMResult[OutputT]:
        if not instructions.strip() or not prompt.strip():
            raise LLMError(
                LLMErrorCode.INVALID_INPUT, "Instructions and prompt must be non-empty."
            )
        settings = self.request_settings(operation)
        if len(instructions) + len(prompt) > settings.max_input_chars:
            raise LLMError(
                LLMErrorCode.INVALID_INPUT,
                "The input exceeds the configured character limit.",
            )

        try:
            # Defer content parsing until finish_reason and usage have been read.
            raw = self._client.chat.completions.with_raw_response.parse(
                model=settings.model,
                messages=[
                    {"role": "system", "content": instructions},
                    {"role": "user", "content": prompt},
                ],
                response_format=schema,
                max_completion_tokens=settings.max_output_tokens,
            )
        except APITimeoutError:
            raise LLMError(
                LLMErrorCode.TIMEOUT, "The provider request timed out."
            ) from None
        except APIConnectionError:
            raise LLMError(
                LLMErrorCode.UNAVAILABLE, "Could not connect to the provider."
            ) from None
        except APIStatusError as error:
            raise _status_error(error) from None

        request_id = raw.headers.get("x-request-id")
        try:
            response = _GroqCompletion.model_validate_json(raw.content, strict=True)
            if request_id is None and response.x_groq is not None:
                request_id = response.x_groq.id
            usage = self._usage(response.usage)
        except ValueError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The provider returned invalid response metadata.",
                request_id=request_id,
            ) from None
        (choice,) = response.choices
        logger.info(
            "LLM response model=%s request_id=%s status=%s usage=%s",
            response.model,
            request_id,
            choice.finish_reason,
            usage,
        )
        if failure := _response_failure(choice):
            raise LLMError(
                *failure, request_id=request_id, usage=usage, model=response.model
            )
        try:
            output = schema.model_validate_json(
                choice.message.content or "", strict=True, extra="forbid"
            )
        except ValidationError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The generated output does not match the requested schema.",
                request_id=request_id,
                usage=usage,
                model=response.model,
            ) from None
        return LLMResult(output, response.model, response.id, request_id, usage)

    def _usage(self, data: CompletionUsage | None) -> LLMUsage | None:
        if data is None:
            return None
        details = data.prompt_tokens_details
        cached = 0
        if details is not None and details.cached_tokens is not None:
            cached = details.cached_tokens
        input_tokens = data.prompt_tokens
        output_tokens = data.completion_tokens
        if (
            not 0 <= cached <= input_tokens
            or output_tokens < 0
            or data.total_tokens != input_tokens + output_tokens
        ):
            raise ValueError("Invalid token counts.")

        input_price = self._settings.input_usd_per_million
        output_price = self._settings.output_usd_per_million
        cached_price = self._settings.cached_input_usd_per_million
        cost = None
        if input_price is not None and output_price is not None:
            cost = (
                (input_tokens - cached) * input_price
                + cached * (input_price if cached_price is None else cached_price)
                + output_tokens * output_price
            ) / TOKENS_PER_MILLION
        return LLMUsage(input_tokens, cached, output_tokens, data.total_tokens, cost)

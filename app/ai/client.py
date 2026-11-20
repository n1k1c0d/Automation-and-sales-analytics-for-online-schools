from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http import HTTPStatus
from math import isfinite
from random import uniform
from time import sleep
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
    from openai.types.chat import ChatCompletionMessageParam

logger = logging.getLogger(__name__)
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
TOKENS_PER_MILLION = Decimal(1_000_000)
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY_SECONDS = 30
TRANSIENT_SERVER_ERRORS = (500, 502, 503, 504)
QUOTA_CODES = (
    "insufficient_quota",
    "billing_hard_limit_reached",
    "quota_exceeded",
    "spend_limit_reached",
)


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


type LLMResponseMode = Literal["json_schema", "json_object"]


@dataclass(frozen=True, slots=True)
class LLMRequestSettings:
    model: str
    max_output_tokens: int
    max_input_chars: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int
    cached_input_tokens: int | None
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
    retry_usage: tuple[LLMUsage | None, ...] = ()
    response_mode: LLMResponseMode = "json_schema"

    @property
    def attempts(self) -> int:
        return 1 + len(self.retry_usage)

    def with_output[ResultT: BaseModel](self, output: ResultT) -> LLMResult[ResultT]:
        return LLMResult(
            output,
            self.model,
            self.response_id,
            self.request_id,
            self.usage,
            self.retry_usage,
            self.response_mode,
        )

    def invalid_response(self, message: str) -> LLMError:
        return LLMError(
            LLMErrorCode.INVALID_RESPONSE,
            message,
            request_id=self.request_id,
            usage=self.usage,
            model=self.model,
            retry_usage=self.retry_usage,
            response_mode=self.response_mode,
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
        retry_usage: tuple[LLMUsage | None, ...] = (),
        response_mode: LLMResponseMode | None = "json_schema",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.usage = usage
        self.model = model
        self.retry_usage = retry_usage
        self.response_mode = response_mode

    @property
    def attempts(self) -> int:
        return 1 + len(self.retry_usage) if self.response_mode is not None else 0


def _status_error(
    error: APIStatusError,
    usage: LLMUsage | None,
    retry_usage: tuple[LLMUsage | None, ...],
    response_mode: LLMResponseMode,
) -> LLMError:
    if error.status_code in {HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN}:
        code, message = (
            LLMErrorCode.AUTHENTICATION,
            "Check the API key and model access.",
        )
    elif error.status_code == HTTPStatus.REQUEST_TIMEOUT:
        code, message = LLMErrorCode.TIMEOUT, "The provider request timed out."
    elif (
        error.status_code == HTTPStatus.PAYMENT_REQUIRED
        or error.code in QUOTA_CODES
        or error.type in QUOTA_CODES
    ):
        code, message = LLMErrorCode.QUOTA, "The provider quota is exhausted."
    elif (
        error.status_code == HTTPStatus.BAD_REQUEST
        and error.code == "json_validate_failed"
    ):
        code, message = (
            LLMErrorCode.INVALID_RESPONSE,
            "Groq rejected generated JSON (HTTP 400, json_validate_failed).",
        )
    elif error.status_code == HTTPStatus.TOO_MANY_REQUESTS:
        code, message = LLMErrorCode.RATE_LIMIT, "The provider rate limit was reached."
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
    return LLMError(
        code,
        message,
        request_id=error.request_id,
        usage=usage,
        retry_usage=retry_usage,
        response_mode=response_mode,
    )


def _retry_delay(retry_after: str | None, previous_retries: int) -> float | None:
    minimum = 0.0
    if retry_after:
        try:
            minimum = float(retry_after)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(retry_after)
                minimum = (retry_at - datetime.now(UTC)).total_seconds()
            except (ValueError, TypeError, OverflowError):
                pass
    if minimum > MAX_RETRY_DELAY_SECONDS:
        return None
    backoff = 2**previous_retries
    delay = max(backoff, minimum) if isfinite(minimum) else backoff
    return min(MAX_RETRY_DELAY_SECONDS, delay + uniform(0, 0.5))


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
                LLMErrorCode.INVALID_INPUT,
                "Instructions and prompt must be non-empty.",
                response_mode=None,
            )
        settings = self.request_settings(operation)
        if len(instructions) + len(prompt) > settings.max_input_chars:
            raise LLMError(
                LLMErrorCode.INVALID_INPUT,
                "The input exceeds the configured character limit.",
                response_mode=None,
            )

        retry_usage: list[LLMUsage | None] = []
        response_mode: LLMResponseMode = "json_schema"
        previous_schema_failure = False
        while True:
            try:
                content, request_id = self._request_completion(
                    settings, instructions, prompt, schema, response_mode
                )
                break
            except APITimeoutError:
                raise LLMError(
                    LLMErrorCode.TIMEOUT,
                    "The provider request timed out.",
                    retry_usage=tuple(retry_usage),
                    response_mode=response_mode,
                ) from None
            except APIConnectionError:
                raise LLMError(
                    LLMErrorCode.UNAVAILABLE,
                    "Could not connect to the provider.",
                    retry_usage=tuple(retry_usage),
                    response_mode=response_mode,
                ) from None
            except APIStatusError as error:
                usage = self._error_usage(error)
                failure = _status_error(error, usage, tuple(retry_usage), response_mode)
                provider_schema_failure = failure.code is LLMErrorCode.INVALID_RESPONSE
                temporary = (
                    provider_schema_failure
                    or failure.code is LLMErrorCode.RATE_LIMIT
                    or (
                        failure.code is LLMErrorCode.UNAVAILABLE
                        and error.status_code in TRANSIENT_SERVER_ERRORS
                    )
                )
                if (
                    response_mode == "json_object"
                    or not temporary
                    or failure.attempts >= MAX_ATTEMPTS
                ):
                    raise failure from None
                delay = _retry_delay(
                    error.response.headers.get("retry-after"), len(retry_usage)
                )
                if delay is None:
                    raise failure from None
                if (
                    operation is LLMOperation.CLASSIFICATION
                    and provider_schema_failure
                    and previous_schema_failure
                ):
                    json_schema = json.dumps(
                        schema.model_json_schema(),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    fallback_instructions = f"{instructions}\nВерни только JSON-объект, соответствующий схеме:\n{json_schema}"
                    if (
                        len(fallback_instructions) + len(prompt)
                        > settings.max_input_chars
                    ):
                        raise failure from None
                    response_mode = "json_object"
                    instructions = fallback_instructions
                previous_schema_failure = provider_schema_failure
                retry_usage.append(usage)
                sleep(delay)

        return self._parse_completion(
            content, request_id, schema, tuple(retry_usage), response_mode
        )

    def _request_completion(
        self,
        settings: LLMRequestSettings,
        instructions: str,
        prompt: str,
        schema: type[BaseModel],
        response_mode: LLMResponseMode,
    ) -> tuple[bytes, str | None]:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": instructions},
            {"role": "user", "content": prompt},
        ]
        api = self._client.chat.completions.with_raw_response
        if response_mode == "json_object":
            raw = api.create(
                model=settings.model,
                messages=messages,
                response_format={"type": "json_object"},
                max_completion_tokens=settings.max_output_tokens,
            )
        else:
            raw = api.parse(
                model=settings.model,
                messages=messages,
                response_format=schema,
                max_completion_tokens=settings.max_output_tokens,
            )
        return raw.content, raw.headers.get("x-request-id")

    def _parse_completion[OutputT: BaseModel](
        self,
        content: bytes,
        request_id: str | None,
        schema: type[OutputT],
        retry_usage: tuple[LLMUsage | None, ...],
        response_mode: LLMResponseMode,
    ) -> LLMResult[OutputT]:
        try:
            response = _GroqCompletion.model_validate_json(content, strict=True)
            if request_id is None and response.x_groq is not None:
                request_id = response.x_groq.id
            usage = self._usage(response.usage)
        except ValueError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The provider returned invalid response metadata.",
                request_id=request_id,
                retry_usage=retry_usage,
                response_mode=response_mode,
            ) from None
        (choice,) = response.choices
        logger.info(
            "LLM response model=%s request_id=%s mode=%s status=%s usage=%s",
            response.model,
            request_id,
            response_mode,
            choice.finish_reason,
            usage,
        )
        if failure := _response_failure(choice):
            raise LLMError(
                *failure,
                request_id=request_id,
                usage=usage,
                model=response.model,
                retry_usage=retry_usage,
                response_mode=response_mode,
            )
        try:
            output = schema.model_validate_json(
                choice.message.content or "", strict=True, extra="forbid"
            )
        except ValidationError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The returned JSON failed local Pydantic validation.",
                request_id=request_id,
                usage=usage,
                model=response.model,
                retry_usage=retry_usage,
                response_mode=response_mode,
            ) from None
        return LLMResult(
            output,
            response.model,
            response.id,
            request_id,
            usage,
            retry_usage,
            response_mode,
        )

    def _error_usage(self, error: APIStatusError) -> LLMUsage | None:
        try:
            body = error.response.json()
            if not isinstance(body, dict):
                return None
            usage = body.get("usage")
            if usage is None and isinstance(error.body, dict):
                usage = error.body.get("usage")
            if usage is not None:
                return self._usage(CompletionUsage.model_validate(usage, strict=True))
        except ValueError:
            # An unusable meter must not mask the original HTTP error.
            pass
        return None

    def _usage(self, data: CompletionUsage | None) -> LLMUsage | None:
        if data is None:
            return None
        details = data.prompt_tokens_details
        cached = details.cached_tokens if details is not None else None
        discounted_tokens = cached or 0
        input_tokens = data.prompt_tokens
        output_tokens = data.completion_tokens
        if (
            not 0 <= discounted_tokens <= input_tokens
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
                (input_tokens - discounted_tokens) * input_price
                + discounted_tokens
                * (input_price if cached_price is None else cached_price)
                + output_tokens * output_price
            ) / TOKENS_PER_MILLION
        return LLMUsage(input_tokens, cached, output_tokens, data.total_tokens, cost)

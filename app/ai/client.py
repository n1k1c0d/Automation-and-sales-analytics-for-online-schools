import logging
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from http import HTTPStatus
from types import TracebackType
from typing import TYPE_CHECKING, Self

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    DefaultHttpxClient,
    OpenAI,
)
from openai.types.responses import Response, ResponseUsage
from pydantic import BaseModel, ValidationError

from app.core.config import LLMSettings

if TYPE_CHECKING:
    from httpx import Client

logger = logging.getLogger(__name__)
OPENAI_BASE_URL = "https://api.openai.com/v1"
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


class LLMError(Exception):
    def __init__(
        self,
        code: LLMErrorCode,
        message: str,
        *,
        request_id: str | None = None,
        usage: LLMUsage | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.usage = usage


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


def _response_failure(response: Response) -> tuple[LLMErrorCode, str] | None:
    if response.status == "failed":
        return LLMErrorCode.UNAVAILABLE, "The provider could not generate a response."
    if response.status == "incomplete" and response.incomplete_details is not None:
        match response.incomplete_details.reason:
            case "max_output_tokens":
                return LLMErrorCode.OUTPUT_LIMIT, "The output reached its token limit."
            case "content_filter":
                return LLMErrorCode.REFUSED, "The provider filtered the response."
    if response.status != "completed":
        return LLMErrorCode.INCOMPLETE, "The provider returned an incomplete response."
    if any(
        part.type == "refusal"
        for item in response.output
        if item.type == "message"
        for part in item.content
    ):
        return LLMErrorCode.REFUSED, "The model refused the request."
    return None


class LLMClient:
    def __init__(
        self, settings: LLMSettings, *, http_client: "Client | None" = None
    ) -> None:
        self._settings = settings
        timeout = settings.timeout_seconds
        self._client = OpenAI(
            api_key=settings.api_key.get_secret_value(),
            base_url=OPENAI_BASE_URL,
            timeout=timeout,
            max_retries=0,
            http_client=http_client
            or DefaultHttpxClient(timeout=timeout, follow_redirects=False),
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

    def generate[OutputT: BaseModel](
        self,
        *,
        instructions: str,
        prompt: str,
        schema: type[OutputT],
    ) -> LLMResult[OutputT]:
        if not instructions.strip() or not prompt.strip():
            raise LLMError(
                LLMErrorCode.INVALID_INPUT, "Instructions and prompt must be non-empty."
            )
        if len(instructions) + len(prompt) > self._settings.max_input_chars:
            raise LLMError(
                LLMErrorCode.INVALID_INPUT,
                "The input exceeds the configured character limit.",
            )

        try:
            # Read the envelope before parsing model text, including partial JSON on truncation.
            raw = self._client.responses.with_raw_response.parse(
                model=self._settings.model,
                instructions=instructions,
                input=prompt,
                text_format=schema,
                max_output_tokens=self._settings.max_output_tokens,
                service_tier="default",
                store=False,
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
            response = Response.model_validate_json(raw.content)
            usage = self._usage(response.usage)
        except ValueError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The provider returned invalid response metadata.",
                request_id=request_id,
            ) from None
        logger.info(
            "LLM response model=%s request_id=%s status=%s usage=%s",
            response.model,
            request_id,
            response.status,
            usage,
        )
        if failure := _response_failure(response):
            raise LLMError(*failure, request_id=request_id, usage=usage)
        try:
            output = schema.model_validate_json(
                response.output_text, strict=True, extra="forbid"
            )
        except ValidationError:
            raise LLMError(
                LLMErrorCode.INVALID_RESPONSE,
                "The generated output does not match the requested schema.",
                request_id=request_id,
                usage=usage,
            ) from None
        return LLMResult(output, response.model, response.id, request_id, usage)

    def _usage(self, data: ResponseUsage | None) -> LLMUsage | None:
        if data is None:
            return None
        cached = data.input_tokens_details.cached_tokens
        if (
            not 0 <= cached <= data.input_tokens
            or data.output_tokens < 0
            or data.total_tokens != data.input_tokens + data.output_tokens
        ):
            raise ValueError("Invalid token counts.")

        input_price = self._settings.input_usd_per_million
        output_price = self._settings.output_usd_per_million
        cached_price = self._settings.cached_input_usd_per_million
        cost = None
        if input_price is not None and output_price is not None:
            cost = (
                (data.input_tokens - cached) * input_price
                + cached * (input_price if cached_price is None else cached_price)
                + data.output_tokens * output_price
            ) / TOKENS_PER_MILLION
        return LLMUsage(
            data.input_tokens, cached, data.output_tokens, data.total_tokens, cost
        )

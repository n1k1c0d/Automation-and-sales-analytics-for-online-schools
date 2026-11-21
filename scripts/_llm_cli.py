import json
import sys
from dataclasses import asdict

from pydantic import ValidationError

from app.ai.client import LLMError, LLMResult, LLMUsage
from app.core.config import LLMSettings


def settings_error_message(error: ValidationError) -> str:
    prefix = LLMSettings.model_config["env_prefix"]
    fields = {
        f"{prefix}{str(item['loc'][0]).upper()}" if item["loc"] else f"{prefix}*"
        for item in error.errors(include_input=False)
    }
    return f"Check Groq settings in .env: {', '.join(sorted(fields))}."


def _retry_payload(attempts: tuple[LLMUsage | None, ...]) -> list[dict | None]:
    return [asdict(usage) if usage is not None else None for usage in attempts]


def print_error_usage(error: LLMError) -> None:
    if error.response_mode is not None:
        metadata: dict[str, object] = {
            "response_mode": error.response_mode,
            "attempts": error.attempts,
        }
        if error.retry_usage:
            metadata["retry_usage"] = _retry_payload(error.retry_usage)
        if error.usage is None:
            metadata["usage"] = None
        print(json.dumps(metadata, default=str), file=sys.stderr)
    if error.usage is not None:
        print(json.dumps(asdict(error.usage), default=str), file=sys.stderr)


def result_payload(result: LLMResult) -> dict[str, object]:
    payload = {
        "output": result.output.model_dump(mode="json"),
        "model": result.model,
        "response_id": result.response_id,
        "request_id": result.request_id,
        "response_mode": result.response_mode,
        "attempts": result.attempts,
        "usage": asdict(result.usage) if result.usage is not None else None,
    }
    if result.retry_usage:
        payload["retry_usage"] = _retry_payload(result.retry_usage)
    return payload

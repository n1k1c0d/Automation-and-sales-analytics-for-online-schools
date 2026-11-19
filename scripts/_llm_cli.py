from dataclasses import asdict

from pydantic import ValidationError

from app.ai.client import LLMResult
from app.core.config import LLMSettings


def settings_error_message(error: ValidationError) -> str:
    prefix = LLMSettings.model_config["env_prefix"]
    fields = {
        f"{prefix}{str(item['loc'][0]).upper()}" if item["loc"] else f"{prefix}*"
        for item in error.errors(include_input=False)
    }
    return f"Check Groq settings in .env: {', '.join(sorted(fields))}."


def result_payload(result: LLMResult) -> dict[str, object]:
    return {
        "output": result.output.model_dump(mode="json"),
        "model": result.model,
        "response_id": result.response_id,
        "request_id": result.request_id,
        "usage": asdict(result.usage) if result.usage is not None else None,
    }

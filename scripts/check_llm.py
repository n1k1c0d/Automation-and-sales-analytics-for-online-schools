import json
import sys
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.ai.client import LLMClient, LLMError, LLMErrorCode
from app.core.config import LLMSettings
from scripts._llm_cli import result_payload, settings_error_message


class ConnectionCheck(BaseModel):
    status: Literal["ok"]


def main() -> int:
    try:
        settings = LLMSettings()
    except ValidationError as exc:
        print(settings_error_message(exc), file=sys.stderr)
        return 2

    try:
        with LLMClient(settings) as client:
            result = client.generate(
                instructions='Return the JSON object {"status":"ok"} for this check.',
                prompt="Run a synthetic connectivity check for EduFlow AI.",
                schema=ConnectionCheck,
            )
    except LLMError as exc:
        print(f"Groq check failed ({exc.code}): {exc}", file=sys.stderr)
        if exc.code is LLMErrorCode.OUTPUT_LIMIT:
            print(
                "Adjust GROQ_MAX_OUTPUT_TOKENS before running again.", file=sys.stderr
            )
        if exc.request_id is not None:
            print(f"Request ID: {exc.request_id}", file=sys.stderr)
        if exc.usage is not None:
            print(json.dumps(asdict(exc.usage), default=str), file=sys.stderr)
        return 2

    print(json.dumps(result_payload(result), default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

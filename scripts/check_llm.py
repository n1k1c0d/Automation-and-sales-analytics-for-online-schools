import json
import sys
from dataclasses import asdict
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.ai.client import LLMClient, LLMError
from app.core.config import LLMSettings


class ConnectionCheck(BaseModel):
    status: Literal["ok"]


def main() -> int:
    try:
        settings = LLMSettings()
    except ValidationError as exc:
        fields = sorted(
            {
                f"GROQ_{str(item['loc'][0]).upper()}" if item["loc"] else "GROQ_*"
                for item in exc.errors(include_input=False)
            }
        )
        print(f"Check Groq settings in .env: {', '.join(fields)}.", file=sys.stderr)
        return 2

    try:
        with LLMClient(settings) as client:
            result = client.generate(
                instructions="Set the status field to ok.",
                prompt="Run a synthetic connectivity check for EduFlow AI.",
                schema=ConnectionCheck,
            )
    except LLMError as exc:
        print(f"LLM check failed ({exc.code}): {exc}", file=sys.stderr)
        if exc.request_id is not None:
            print(f"Request ID: {exc.request_id}", file=sys.stderr)
        if exc.usage is not None:
            print(json.dumps(asdict(exc.usage), default=str), file=sys.stderr)
        return 2

    print(
        json.dumps(
            {
                "output": result.output.model_dump(mode="json"),
                "model": result.model,
                "response_id": result.response_id,
                "request_id": result.request_id,
                "usage": asdict(result.usage) if result.usage is not None else None,
            },
            default=str,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

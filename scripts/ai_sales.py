import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from app.ai.client import LLMClient, LLMError, LLMErrorCode, LLMResult
from app.ai.rejections import (
    MAX_COMMENTS,
    ClassificationEvaluation,
    LabeledComment,
    classify_rejections,
    evaluate_rejections,
)
from app.ai.sales_analysis import Report, answer_sales_question, summarize_sales
from app.core.config import LLMSettings
from scripts._llm_cli import result_payload, settings_error_message

LABELED_FIXTURE = Path("data/synthetic/ai/rejection_cases.json")
OUTPUT_LIMIT_HINTS = {
    "classify": "Try fewer comments per request or set GROQ_CLASSIFICATION_MAX_OUTPUT_TOKENS.",
    "evaluate": "Reduce --batch-size or set GROQ_CLASSIFICATION_MAX_OUTPUT_TOKENS.",
    "summarize": "Adjust GROQ_SUMMARY_MAX_OUTPUT_TOKENS before running again.",
    "ask": "Adjust GROQ_MAX_OUTPUT_TOKENS before running again.",
}


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze anonymized sales data with GroqCloud."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    classify = commands.add_parser(
        "classify", help="Classify a JSON array of comments."
    )
    classify.add_argument("file", type=Path)
    evaluate = commands.add_parser(
        "evaluate", help="Score classification on labeled examples."
    )
    evaluate.add_argument("file", type=Path, nargs="?", default=LABELED_FIXTURE)
    evaluate.add_argument(
        "--batch-size",
        type=int,
        choices=range(1, MAX_COMMENTS + 1),
        default=MAX_COMMENTS,
    )
    summarize = commands.add_parser(
        "summarize", help="Summarize a sales_report JSON file."
    )
    summarize.add_argument("file", type=Path)
    ask = commands.add_parser(
        "ask", help="Ask a question about a sales_report JSON file."
    )
    ask.add_argument("file", type=Path)
    ask.add_argument("question")
    return parser.parse_args(argv)


def _run(client: LLMClient, args: argparse.Namespace) -> LLMResult | BaseModel:
    data = args.file.read_text(encoding="utf-8-sig")
    if args.command == "classify":
        comments = TypeAdapter(list[str]).validate_json(data)
        return classify_rejections(client, comments)
    if args.command == "evaluate":
        examples = TypeAdapter(list[LabeledComment]).validate_json(data)
        return evaluate_rejections(client, examples, batch_size=args.batch_size)

    report = TypeAdapter(Report).validate_json(data)
    if args.command == "summarize":
        return summarize_sales(client, report)
    return answer_sales_question(client, report, args.question)


def main(argv: Sequence[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        settings = LLMSettings()
    except ValidationError as exc:
        print(settings_error_message(exc), file=sys.stderr)
        return 2
    try:
        with LLMClient(settings) as client:
            result = _run(client, args)
    except (OSError, UnicodeError):
        print("Could not read the input file as UTF-8.", file=sys.stderr)
        return 2
    except ValidationError:
        print("Input JSON does not match the command's format.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except LLMError as exc:
        print(f"Groq analysis failed ({exc.code}) — {exc}", file=sys.stderr)
        if exc.code is LLMErrorCode.OUTPUT_LIMIT:
            print(OUTPUT_LIMIT_HINTS[args.command], file=sys.stderr)
        if exc.request_id is not None:
            print(f"Request ID {exc.request_id}", file=sys.stderr)
        if exc.usage is not None:
            print(json.dumps(asdict(exc.usage), default=str), file=sys.stderr)
        return 2

    if isinstance(result, LLMResult):
        output = result_payload(result)
    else:
        output = result.model_dump(mode="json")
    print(json.dumps(output, default=str, indent=2))
    if isinstance(result, ClassificationEvaluation) and result.output_limit_batches:
        print(OUTPUT_LIMIT_HINTS["evaluate"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

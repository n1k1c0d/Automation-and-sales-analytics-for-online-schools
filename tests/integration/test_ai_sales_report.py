import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

from app.ai import client as llm
from app.analytics.sales import read_sales_report
from app.core.config import LLMSettings
from app.schemas.sales_report import ReportPeriod
from app.services.sales_import import import_sales_data
from scripts import ai_sales
from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset
from tests.integration.test_standard_sales_kpis import EXPECTED_STANDARD_KPIS
from tests.unit.test_llm_client import _payload


def test_ai_cli_summarizes_sql_report_without_changing_kpis(
    isolated_postgres_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = tmp_path / "reference"
    write_csv_dataset(generate_dataset(), data)
    assert not import_sales_data(
        isolated_postgres_engine, data, source_system="synthetic_42"
    ).issues
    period = ReportPeriod(
        start_date=date(2026, 6, 1),
        end_date=date(2026, 8, 31),
        cutoff_date=date(2026, 10, 31),
    )
    report = read_sales_report(isolated_postgres_engine, period)
    report_file = tmp_path / "report.json"
    report_file.write_text(report.model_dump_json(), encoding="utf-8-sig")

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_output_tokens"] == 3072
        facts = json.loads(body["input"])
        assert facts["scopes"]["total"]["current"] == EXPECTED_STANDARD_KPIS
        assert "daily" not in facts
        assert (
            len(
                [
                    scope
                    for scope in facts["scopes"].values()
                    if scope["dimension"] == "manager"
                ]
            )
            == 8
        )
        references = [
            {"scope": "total", "metric": metric}
            for metric in ("leads", "paid_amount_rub", "lesson_to_payment_pct")
        ]
        plan = {
            "metrics": references,
            "hypotheses": [{"kind": "payment_follow_up", "evidence": references[-1]}],
        }
        return httpx.Response(200, json=_payload(json.dumps(plan)))

    for field in LLMSettings.model_fields:
        monkeypatch.delenv(f"OPENAI_{field.upper()}", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_SUMMARY_MAX_OUTPUT_TOKENS", "3072")
    monkeypatch.setattr(
        llm,
        "DefaultHttpxClient",
        lambda **_: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    assert ai_sales.main(["summarize", str(report_file)]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert [row["current"] for row in parsed["output"]["observations"]] == [
        500,
        "8506500.00",
        "39.33",
    ]
    assert (
        parsed["output"]["hypotheses"][0]["evidence"]["metric"]
        == "lesson_to_payment_pct"
    )
    assert parsed["usage"]["total_tokens"] == 1000

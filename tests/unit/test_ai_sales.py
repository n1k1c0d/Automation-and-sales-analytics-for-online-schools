import json
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import TypeAdapter

from app.ai.client import LLMClient, LLMError, LLMErrorCode
from app.ai.rejections import (
    MAX_COMMENTS,
    LabeledComment,
    RejectionReason,
    classify_rejections,
    evaluate_rejections,
)
from app.ai.sales_analysis import answer_sales_question, summarize_sales
from app.core.config import LLMSettings
from app.schemas.sales_report import (
    ReportPeriod,
    SalesMetrics,
    SalesReport,
    SalesReportComparison,
    SourceSalesMetrics,
)
from tests.unit.test_llm_client import REQUEST_HEADERS, _payload

FIXTURE = Path(__file__).resolve().parents[2] / "data/synthetic/ai/rejection_cases.json"


@pytest.fixture(autouse=True)
def isolate_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in LLMSettings.model_fields:
        monkeypatch.delenv(f"OPENAI_{field.upper()}", raising=False)


def _client(plans: Iterable[dict], requests: list[dict] | None = None) -> LLMClient:
    replies = iter(plans)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"]["format"]["strict"] is True
        if requests is not None:
            requests.append(json.loads(body["input"]))
        return httpx.Response(
            200,
            headers=REQUEST_HEADERS,
            json=_payload(json.dumps(next(replies), ensure_ascii=False)),
        )

    return LLMClient(
        LLMSettings(_env_file=None, api_key="test-key"),
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )


def _classification(comment_id: int, reason: str, evidence: str | None) -> dict:
    return {"comment_id": comment_id, "reason": reason, "evidence": evidence}


def _classification_reply(*items: dict) -> dict:
    return {"classifications": items}


def test_classification_preserves_input_order_and_calculates_counts() -> None:
    plans = [
        _classification_reply(
            _classification(2, "unclear", None),
            _classification(1, "price", "дорого"),
        )
    ]
    sent = []
    with _client(plans, sent) as client:
        result = classify_rejections(
            client, ["Для нас слишком дорого.", "Причину не сообщили."]
        )
    assert [item.comment_id for item in result.output.classifications] == [1, 2]
    assert result.output.counts[RejectionReason.PRICE] == 1
    assert result.output.counts[RejectionReason.UNCLEAR] == 1
    assert sum(result.output.counts.values()) == 2
    assert sent == [
        [
            {"comment_id": 1, "comment": "Для нас слишком дорого."},
            {"comment_id": 2, "comment": "Причину не сообщили."},
        ]
    ]


@pytest.mark.parametrize(
    "items",
    [
        (),
        (_classification(2, "price", "дорого"),),
        (_classification(1, "price", "дорого"), _classification(1, "price", "дорого")),
        (_classification(1, "price", "70 тысяч"),),
        (_classification(1, "unclear", "дорого"),),
        (_classification(1, "price", None),),
    ],
    ids=[
        "missing-id",
        "unknown-id",
        "duplicate-id",
        "invented-evidence",
        "unclear-with-quote",
        "missing-evidence",
    ],
)
def test_rejects_unverifiable_classifications(items: tuple[dict, ...]) -> None:
    with (
        _client([_classification_reply(*items)]) as client,
        pytest.raises(LLMError) as caught,
    ):
        classify_rejections(client, ["Для нас слишком дорого."])
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.usage is not None


def test_evaluation_scores_labeled_examples_without_sending_the_labels() -> None:
    examples = TypeAdapter(list[LabeledComment]).validate_json(FIXTURE.read_bytes())
    assert len(examples) == 18 and {item.expected for item in examples} == set(
        RejectionReason
    )
    sample = [*examples[:MAX_COMMENTS], examples[-1]]
    predictions = [
        _classification_reply(
            _classification(1, "price", "общая стоимость выше нашего бюджета"),
            _classification(2, "financing_terms", "цена программы слишком высокая"),
            _classification(3, "bank_declined", "отказал в рассрочке"),
            _classification(4, "bank_declined", "заявку не одобрили"),
            _classification(5, "financing_terms", "не устраивают проценты"),
            _classification(6, "financing_terms", "ежемесячный платёж"),
            _classification(7, "schedule", "совпадают с рабочей сменой"),
            _classification(8, "schedule", "группа учится по будням"),
        ),
        _classification_reply(_classification(1, "unclear", None)),
    ]
    sent = []
    with _client(predictions, sent) as client:
        evaluation = evaluate_rejections(client, sample)
    assert (evaluation.total, evaluation.correct, evaluation.accuracy_pct) == (
        9,
        8,
        Decimal("88.89"),
    )
    assert [
        (item.comment_id, item.expected, item.actual) for item in evaluation.mismatches
    ] == [(2, RejectionReason.PRICE, RejectionReason.FINANCING_TERMS)]
    assert [len(batch) for batch in sent] == [8, 1]
    assert all(set(row) == {"comment_id", "comment"} for batch in sent for row in batch)
    assert len(evaluation.usage_by_request) == 2


def _metrics(**values) -> SalesMetrics:
    return SalesMetrics.model_validate(
        dict.fromkeys(SalesMetrics.model_fields, 0)
        | {"average_paid_check_rub": None, "lesson_to_payment_pct": None}
        | values
    )


def _report(
    month: int, total: SalesMetrics, *, sources: list[SourceSalesMetrics] | None = None
) -> SalesReport:
    return SalesReport(
        period=ReportPeriod(
            start_date=date(2026, month, 1),
            end_date=date(2026, month, 30),
            cutoff_date=date(2026, 7, 31),
        ),
        manager_id=None,
        source=None,
        total=total,
        daily=[],
        by_manager=[],
        by_source=sources or [],
    )


def _reference(metric: str, scope: str = "total") -> dict:
    return {"scope": scope, "metric": metric}


def test_summary_values_and_hypothesis_evidence_come_from_report() -> None:
    report = SalesReportComparison(
        current=_report(
            6, _metrics(paid_amount_rub="180000.01", lesson_to_payment_pct="40.00")
        ),
        comparison=_report(
            5, _metrics(paid_amount_rub="150000.00", lesson_to_payment_pct="35.50")
        ),
    )
    amount = _reference("paid_amount_rub")
    conversion = _reference("lesson_to_payment_pct")
    plan = {
        "metrics": [amount, conversion],
        "hypotheses": [{"kind": "payment_follow_up", "evidence": conversion}],
    }
    sent = []
    with _client([plan], sent) as client:
        result = summarize_sales(client, report)
    amount_value, conversion_value = result.output.observations
    assert amount_value.current == Decimal("180000.01")
    assert amount_value.difference == Decimal("30000.01")
    assert conversion_value.difference == Decimal("4.50")
    assert "+4,50 п. п." in result.output.lines[1]
    assert result.output.hypotheses[0].evidence == conversion_value.reference
    assert sent[0]["scopes"]["total"]["current"]["paid_amount_rub"] == "180000.01"
    assert "daily" not in sent[0]


@pytest.mark.parametrize(
    "plan",
    [
        {"metrics": [_reference("imaginary_kpi")], "hypotheses": []},
        {"metrics": [_reference("leads", "source:unknown")], "hypotheses": []},
        {"metrics": [_reference("leads") | {"value": 999}], "hypotheses": []},
        {
            "metrics": [_reference("leads")],
            "hypotheses": [
                {"kind": "payment_follow_up", "evidence": _reference("leads")}
            ],
        },
    ],
)
def test_summary_rejects_invented_values_and_unrelated_hypotheses(plan: dict) -> None:
    with _client([plan]) as client, pytest.raises(LLMError) as caught:
        summarize_sales(client, _report(6, _metrics(leads=500)))
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.usage is not None


def test_ranked_answer_calculates_the_maximum_and_keeps_ties() -> None:
    sources = [
        SourceSalesMetrics(
            source=source, **_metrics(paid_amount_rub=value).model_dump()
        )
        for source, value in (
            ("social", "480.10"),
            ("webinar", "900.20"),
            ("referral", "900.20"),
        )
    ]
    plan = {
        "status": "answered",
        "metrics": [],
        "ranking": {
            "dimension": "source",
            "metric": "paid_amount_rub",
            "period": "current",
            "order": "highest",
        },
    }
    with _client([plan]) as client:
        result = answer_sales_question(
            client,
            _report(6, _metrics(), sources=sources),
            "У какого канала больше всего оплат?",
        )
    assert [item.scope_label for item in result.output.observations] == [
        "webinar",
        "referral",
    ]
    assert {item.current for item in result.output.observations} == {Decimal("900.20")}
    assert result.output.lines[0].startswith("Максимум")


def test_question_selects_values_for_a_comparison() -> None:
    report = SalesReportComparison(
        current=_report(6, _metrics(leads=165)),
        comparison=_report(5, _metrics(leads=167)),
    )
    plan = {"status": "answered", "metrics": [_reference("leads")], "ranking": None}
    sent = []
    with _client([plan], sent) as client:
        result = answer_sales_question(
            client, report, "Как изменилось число новых лидов?"
        )
    (observation,) = result.output.observations
    assert (observation.current, observation.comparison, observation.difference) == (
        165,
        167,
        -2,
    )
    assert sent[0]["question"] == "Как изменилось число новых лидов?"


@pytest.mark.parametrize(
    "plan",
    [
        {"status": "insufficient_data", "metrics": [], "ranking": None},
        {
            "status": "answered",
            "metrics": [_reference("average_paid_check_rub")],
            "ranking": None,
        },
    ],
)
def test_question_reports_insufficient_data(plan: dict) -> None:
    with _client([plan]) as client:
        result = answer_sales_question(
            client, _report(6, _metrics()), "Какой средний чек?"
        )
    assert result.output.status == "insufficient_data"
    assert not result.output.observations

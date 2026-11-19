import json
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from pydantic import TypeAdapter

from app.ai import client as llm
from app.ai.client import LLMClient, LLMError, LLMErrorCode
from app.ai.comment_privacy import CommentPrivacyError, check_comment_privacy
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
    ManagerSalesMetrics,
    ReportPeriod,
    SalesMetrics,
    SalesReport,
    SalesReportComparison,
    SourceSalesMetrics,
)
from scripts.ai_sales import main as ai_sales_main
from scripts.check_llm import main as check_llm
from tests.unit.test_llm_client import REQUEST_HEADERS, _payload

FIXTURE = Path(__file__).resolve().parents[2] / "data/synthetic/ai/rejection_cases.json"
CHALLENGE = FIXTURE.with_name("rejection_challenge_cases.json")


@pytest.fixture(autouse=True)
def isolate_llm_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in LLMSettings.model_fields:
        monkeypatch.delenv(f"GROQ_{field.upper()}", raising=False)


def _client(plans: Iterable[dict], requests: list[dict] | None = None) -> LLMClient:
    replies = iter(plans)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["response_format"]["json_schema"]["strict"] is True
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        if requests is not None:
            requests.append(json.loads(body["messages"][1]["content"]))
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
    ("comment", "category"),
    [
        ("Перезвонить на +7 (900) 000-00-00.", "phone"),
        ("Номер для связи 89000000000.", "phone"),
        ("Позвонить на (202) 555-0149.", "phone"),
        ("Ответить на learner@example.test.", "email"),
        ("Почта learner＠example.test", "email"),
        ("Клиент Иван Тестов отказался от курса.", "name"),
        ("ФИО: учбов условный", "name"),
        ("Курс не подошёл Сергею. Заявитель Б. Б. Примеров.", "name"),
    ],
)
def test_classification_blocks_personal_data_before_http(
    comment: str, category: str
) -> None:
    sent = []
    with _client([], sent) as client, pytest.raises(CommentPrivacyError) as caught:
        classify_rejections(client, ["Цена не подошла.", comment])
    (finding,) = caught.value.findings
    assert finding.comment_id == 2
    assert finding.categories == (category,)
    assert not sent
    assert comment not in str(caught.value)


def test_normalized_obscured_email_is_blocked() -> None:
    with pytest.raises(CommentPrivacyError, match=r"1 \(email\)"):
        check_comment_privacy(["Контакт learner\u200b@example.test."])


def test_single_string_cannot_bypass_batch_privacy_check() -> None:
    sent = []
    with _client([], sent) as client, pytest.raises(ValueError, match="sequence"):
        classify_rejections(client, "a@b.co")
    assert not sent


def test_oversized_batch_is_rejected_before_sending_any_comments() -> None:
    sent = []
    with _client([], sent) as client, pytest.raises(ValueError, match="between"):
        classify_rejections(client, ["Курс слишком дорогой."] * (MAX_COMMENTS + 1))
    assert not sent


def test_evaluation_blocks_whole_file_before_first_batch() -> None:
    safe = LabeledComment(comment="Курс слишком дорогой.", expected="price")
    sensitive = LabeledComment(
        comment="Отказ по почте learner@example.test.", expected="unclear"
    )
    sent = []
    with (
        _client([], sent) as client,
        pytest.raises(CommentPrivacyError, match=r"9 \(email\)"),
    ):
        evaluate_rejections(client, [safe] * MAX_COMMENTS + [sensitive])
    assert not sent


def test_privacy_filter_allows_synthetic_business_data() -> None:
    adapter = TypeAdapter(list[LabeledComment])
    standard = adapter.validate_json(FIXTURE.read_bytes())
    challenge = adapter.validate_json(CHALLENGE.read_bytes())
    assert len(standard) == 18 and len(challenge) == 24
    check_comment_privacy(
        [case.comment for case in standard + challenge]
        + ["Банк Открытие спросил про цену 120 000 ₽. Встреча 2026-06-01 12:00."]
    )
    labels = {case.expected for case in challenge}
    assert {
        RejectionReason.UNCLEAR,
        RejectionReason.BANK_DECLINED,
        RejectionReason.FINANCING_TERMS,
        RejectionReason.PRICE,
        RejectionReason.POSTPONED,
    } <= labels


@pytest.mark.parametrize(
    "items",
    [
        (),
        (_classification(2, "price", "дорого"),),
        (_classification(1, "price", "дорого"), _classification(2, "unclear", None)),
        (_classification(1, "price", "дорого"), _classification(1, "price", "дорого")),
        (_classification(1, "price", "70 тысяч"),),
        (_classification(1, "unclear", "дорого"),),
        (_classification(1, "price", None),),
        (_classification(1, "refund", "дорого"),),
    ],
    ids=[
        "missing-id",
        "unknown-id",
        "extra-id",
        "duplicate-id",
        "invented-evidence",
        "unclear-with-quote",
        "missing-evidence",
        "unknown-category",
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


def test_evidence_must_belong_to_the_same_comment() -> None:
    comments = ["Банк отказал в рассрочке.", "Условия рассрочки не подходят."]
    plan = _classification_reply(
        _classification(1, "financing_terms", comments[1]),
        _classification(2, "bank_declined", comments[0]),
    )
    with _client([plan]) as client, pytest.raises(LLMError) as caught:
        classify_rejections(client, comments)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE


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
    assert evaluation.output_limit_rate_pct == 0
    assert evaluation.by_category[RejectionReason.PRICE].wrong == 1
    assert evaluation.settings.max_output_tokens == 2048


def test_evaluation_tracks_attempts_without_counting_comments_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = []
    waits = []
    monkeypatch.setattr(llm, "sleep", waits.append)

    def respond(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["max_completion_tokens"] == 2048
        sent.append(json.loads(body["messages"][1]["content"]))
        if len(sent) == 1:
            return httpx.Response(
                503,
                json={
                    "error": {"type": "server_error"},
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    },
                },
            )
        if len(sent) == 2:
            payload = _payload('{"classifications":', finish_reason="length")
        elif len(sent) == 3:
            return httpx.Response(
                429,
                headers={"Retry-After": "2"},
                json={"error": {"code": "rate_limit_exceeded"}},
            )
        else:
            plan = _classification_reply(_classification(1, "price", "дорого"))
            payload = _payload(json.dumps(plan))
        return httpx.Response(200, headers=REQUEST_HEADERS, json=payload)

    options = LLMSettings(
        _env_file=None, api_key="test-key", classification_max_output_tokens=2048
    )
    examples = [
        LabeledComment(comment="Банк отказал.", expected="bank_declined"),
        LabeledComment(comment="Причину не сообщили.", expected="unclear"),
        LabeledComment(comment="Слишком дорого.", expected="price"),
    ]
    with LLMClient(
        options, http_client=httpx.Client(transport=httpx.MockTransport(respond))
    ) as client:
        report = evaluate_rejections(client, examples, batch_size=2)
    assert [len(batch) for batch in sent] == [2, 2, 1, 1]
    assert sent[0] == sent[1] and sent[2] == sent[3]
    assert len(waits) == 2 and waits[1] >= 2
    assert report.settings.model == "qwen/qwen3.8-27b"
    assert report.settings.max_output_tokens == 2048
    assert report.correct == 1 and report.accuracy_pct == Decimal("33.33")
    assert report.output_limit_batches == 1 and report.output_limit_rate_pct == Decimal(
        "50.00"
    )
    assert [batch.status for batch in report.batches] == ["output_limit", "completed"]
    assert [batch.attempts for batch in report.batches] == [2, 2]
    assert report.batches[0].model == "qwen-test"
    assert report.batches[1].first_comment_id == 3
    assert report.by_category[RejectionReason.BANK_DECLINED].unclassified == 1
    assert report.by_category[RejectionReason.PRICE].correct == 1
    assert [
        usage.total_tokens if usage else None for usage in report.usage_by_request
    ] == [24, 1000, None, 1000]


@pytest.mark.parametrize(
    ("args", "schema", "limit_env", "expected_limit"),
    [
        ([], "ConnectionCheck", "GROQ_MAX_OUTPUT_TOKENS", 256),
        (
            ["classify", "comments.json"],
            "ClassificationPlan",
            "GROQ_CLASSIFICATION_MAX_OUTPUT_TOKENS",
            512,
        ),
        (
            ["evaluate", "cases.json", "--batch-size", "4"],
            "ClassificationPlan",
            "GROQ_CLASSIFICATION_MAX_OUTPUT_TOKENS",
            512,
        ),
        (
            ["summarize", "report.json"],
            "SummaryPlan",
            "GROQ_SUMMARY_MAX_OUTPUT_TOKENS",
            1024,
        ),
        (
            ["ask", "report.json", "Сколько лидов?"],
            "QuestionPlan",
            "GROQ_MAX_OUTPUT_TOKENS",
            256,
        ),
    ],
)
def test_cli_uses_groq_and_reports_the_effective_limit(
    args: list[str],
    schema: str,
    limit_env: str,
    expected_limit: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GROQ_MAX_OUTPUT_TOKENS", "256")
    monkeypatch.setenv("GROQ_CLASSIFICATION_MAX_OUTPUT_TOKENS", "512")
    monkeypatch.setenv("GROQ_SUMMARY_MAX_OUTPUT_TOKENS", "1024")

    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "qwen/qwen3.8-27b"
        assert body["response_format"]["json_schema"]["name"] == schema
        assert body["max_completion_tokens"] == expected_limit
        return httpx.Response(200, json=_payload(finish_reason="length"))

    monkeypatch.setattr(
        llm,
        "DefaultHttpxClient",
        lambda **_: httpx.Client(transport=httpx.MockTransport(respond)),
    )
    comment = "Причину не указали."
    inputs = {
        "comments.json": [comment],
        "cases.json": [{"comment": comment, "expected": "unclear"}],
        "report.json": _report(6, _metrics(leads=3)).model_dump(mode="json"),
    }
    for filename, data in inputs.items():
        (tmp_path / filename).write_text(json.dumps(data), encoding="utf-8-sig")
    exit_code = ai_sales_main(args) if args else check_llm()

    output = capsys.readouterr()
    assert limit_env in output.err
    assert "test-key" not in output.err
    if args[:1] == ["evaluate"]:
        assert exit_code == 1
        assert "--batch-size" in output.err
        result = json.loads(output.out)
        assert result["batch_size"] == 4
        assert result["output_limit_batches"] == 1
        assert result["by_category"]["unclear"]["unclassified"] == 1
    else:
        assert exit_code == 2
        assert "output_limit" in output.err
        assert not output.out


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


@pytest.mark.parametrize("command", ["summary", "question"])
@pytest.mark.parametrize("field", ["manager", "comparison_source", "source_filter"])
def test_sensitive_report_text_is_blocked_before_http(command: str, field: str) -> None:
    current, prior = _report(6, _metrics()), _report(5, _metrics())
    if field == "manager":
        sensitive = "ФИО: учебный пример"
        current.by_manager.append(
            ManagerSalesMetrics(
                manager_id=UUID(int=1),
                manager_name=sensitive,
                **_metrics().model_dump(),
            )
        )
    elif field == "comparison_source":
        sensitive = "demo@example.test"
        prior.by_source.append(
            SourceSalesMetrics(source=sensitive, **_metrics().model_dump())
        )
    else:
        sensitive = "demo@example.test"
        current.source = prior.source = sensitive
    report = SalesReportComparison(current=current, comparison=prior)
    sent = []
    with (
        _client([], sent) as client,
        pytest.raises(ValueError, match="Potential personal data") as caught,
    ):
        if command == "summary":
            summarize_sales(client, report)
        else:
            answer_sales_question(client, report, "Сколько лидов?")
    assert not sent and sensitive not in str(caught.value)


@pytest.mark.parametrize(
    "marker",
    [
        "demo＠example.test",
        "+7 (900) 000-00-00",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
    ],
)
def test_sensitive_question_is_blocked_before_http(marker: str) -> None:
    sent = []
    with (
        _client([], sent) as client,
        pytest.raises(ValueError, match="sales question") as caught,
    ):
        answer_sales_question(
            client, _report(6, _metrics()), f"Сколько оплат у {marker}?"
        )
    assert not sent and marker not in str(caught.value)


def test_manager_name_and_uuid_stay_local_in_both_operations() -> None:
    metrics = _metrics(leads=3)
    report = _report(6, metrics)
    manager_id = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    report.manager_id = manager_id
    report.by_manager.append(
        ManagerSalesMetrics(
            manager_id=manager_id, manager_name="Demo Agent A", **metrics.model_dump()
        )
    )
    ref = _reference("leads", "manager:1")
    plans = [
        {"metrics": [ref], "hypotheses": []},
        {"status": "answered", "metrics": [ref], "ranking": None},
    ]
    sent = []
    with _client(plans, sent) as client:
        summary = summarize_sales(client, report)
        answer = answer_sales_question(client, report, "Сколько лидов у Demo Agent A?")
    for payload, result in zip(sent, (summary, answer), strict=True):
        prompt = json.dumps(payload, ensure_ascii=False)
        assert "Demo Agent A" not in prompt and str(manager_id) not in prompt
        assert payload["filters"]["manager_filtered"] is True
        (row,) = result.output.observations
        assert (row.scope_label, row.current) == ("Demo Agent A", 3)
        assert result.output.filters["manager_id"] == str(manager_id)
    assert sent[1]["question"] == "Сколько лидов у manager:1?"
    assert all(
        set(scope) == {"dimension", "label"} for scope in sent[1]["scopes"].values()
    )


@pytest.mark.parametrize("with_hypothesis", [False, True])
def test_summary_without_values_is_insufficient(with_hypothesis: bool) -> None:
    report = _report(6, _metrics())
    ref = _reference(
        "lesson_to_payment_pct" if with_hypothesis else "average_paid_check_rub"
    )
    plan = {
        "metrics": [ref],
        "hypotheses": [{"kind": "payment_follow_up", "evidence": ref}]
        if with_hypothesis
        else [],
    }
    with _client([plan]) as client:
        result = summarize_sales(client, report)
    assert result.output.status == "insufficient_data"
    assert result.output.current_period == report.period
    assert result.output.lines == ["Недостаточно данных для ответа."]
    assert not result.output.observations and not result.output.hypotheses


def test_summary_with_a_value_preserves_partial_results() -> None:
    report = _report(6, _metrics(leads=0))
    plan = {
        "metrics": [_reference("average_paid_check_rub"), _reference("leads")],
        "hypotheses": [],
    }
    with _client([plan]) as client:
        result = summarize_sales(client, report)
    assert result.output.status == "answered"
    assert [item.current for item in result.output.observations] == [None, 0]
    assert "нет данных" in result.output.lines[0]


def test_summary_uses_comparison_when_current_is_missing() -> None:
    report = SalesReportComparison(
        current=_report(6, _metrics()),
        comparison=_report(5, _metrics(average_paid_check_rub="750.00")),
    )
    plan = {"metrics": [_reference("average_paid_check_rub")], "hypotheses": []}
    with _client([plan]) as client:
        result = summarize_sales(client, report)
    assert result.output.status == "answered"
    (observation,) = result.output.observations
    assert observation.current is None
    assert observation.comparison == Decimal("750.00")
    assert observation.difference is None


@pytest.mark.parametrize(
    "question",
    [
        "Как изменилась эффективность?",
        "Какой менеджер лучший?",
        "Как мы выступили относительно прошлого периода?",
    ],
)
def test_ambiguous_questions_do_not_receive_unsupported_metrics(question: str) -> None:
    plan = {"status": "insufficient_data", "metrics": [], "ranking": None}
    sent = []
    with _client([plan], sent) as client:
        result = answer_sales_question(
            client, _report(6, _metrics(leads=500)), question
        )
    assert sent[0]["question"] == question
    assert result.output.status == "insufficient_data"
    assert result.output.lines == ["Недостаточно данных для ответа."]
    assert not result.output.observations


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
            "Какой источник принёс больше всего денег?",
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
            "status": "insufficient_data",
            "metrics": [_reference("average_paid_check_rub")],
            "ranking": None,
        },
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


@pytest.mark.parametrize(
    ("period", "status", "winners"),
    [("current", "answered", ["social"]), ("comparison", "insufficient_data", [])],
)
def test_ranking_treats_zero_and_missing_values_by_period(
    period: str, status: str, winners: list[str]
) -> None:
    def source(label: str, value: str | None) -> SourceSalesMetrics:
        return SourceSalesMetrics(
            source=label, **_metrics(average_paid_check_rub=value).model_dump()
        )

    report = SalesReportComparison(
        current=_report(
            6, _metrics(), sources=[source("social", "0.00"), source("webinar", None)]
        ),
        comparison=_report(5, _metrics(), sources=[source("social", None)]),
    )
    ranking = {
        "dimension": "source",
        "metric": "average_paid_check_rub",
        "period": period,
        "order": "highest",
    }
    with _client([{"status": "answered", "metrics": [], "ranking": ranking}]) as client:
        answer = answer_sales_question(
            client, report, "Какой канал лидирует по среднему чеку?"
        )
    assert answer.output.status == status
    assert [row.scope_label for row in answer.output.observations] == winners
    if winners:
        (row,) = answer.output.observations
        assert (
            row.current == Decimal("0.00")
            and row.comparison is None
            and row.difference is None
        )


@pytest.mark.parametrize("change", [{"metric": "imaginary_kpi"}, {"value": "99999.99"}])
def test_ranking_rejects_invented_kpi_or_amount(change: dict) -> None:
    ranking = {
        "dimension": "source",
        "metric": "paid_amount_rub",
        "period": "current",
        "order": "highest",
    } | change
    with (
        _client([{"status": "answered", "metrics": [], "ranking": ranking}]) as client,
        pytest.raises(LLMError) as caught,
    ):
        answer_sales_question(
            client, _report(6, _metrics()), "Какой источник принёс больше всего денег?"
        )
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE

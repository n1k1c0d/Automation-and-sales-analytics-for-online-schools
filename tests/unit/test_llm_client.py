import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai import client as llm
from app.ai.client import LLMClient, LLMError, LLMErrorCode, LLMOperation, LLMUsage
from app.ai.rejections import GroqClassificationResponse, RejectionReason
from app.ai.sales_analysis import QuestionPlan, SummaryPlan
from app.core.config import LLMSettings, PostgresSettings
from app.schemas.sales_report import SalesMetrics
from scripts.ai_sales import main as ai_sales
from scripts.check_llm import ConnectionCheck
from scripts.check_llm import main as check_llm

KEY = "unit-test-token"
PROVIDER_MESSAGE = "private provider message"
REQUEST_ID = "req_test"
REQUEST_HEADERS = {"x-request-id": REQUEST_ID}


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in LLMSettings.model_fields:
        monkeypatch.delenv(f"GROQ_{field.upper()}", raising=False)


def _settings(**overrides) -> LLMSettings:
    return LLMSettings(_env_file=None, api_key=KEY, **overrides)


def _payload(
    output: str | None = '{"status":"ok"}',
    *,
    finish_reason: str = "stop",
    refusal: bool = False,
) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "qwen-test",
        "service_tier": "on_demand",
        "x_groq": {"id": "req_body"},
        "choices": [
            {
                "index": 0,
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": output,
                    "refusal": PROVIDER_MESSAGE if refusal else None,
                },
            },
        ],
        "usage": {
            "prompt_tokens": 900,
            "prompt_tokens_details": {"cached_tokens": 400},
            "completion_tokens": 100,
            "completion_tokens_details": {"reasoning_tokens": 40},
            "total_tokens": 1000,
        },
    }


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _generate(
    client: LLMClient,
    *,
    prompt: str = "A synthetic prompt.",
    schema: type[BaseModel] = ConnectionCheck,
    operation: LLMOperation | None = None,
):
    return client.generate(
        instructions="Return the requested status.",
        prompt=prompt,
        schema=schema,
        operation=operation,
    )


def test_uses_strict_structured_outputs_and_accounts_for_usage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.invalid/v1")
    monkeypatch.setenv("GROQ_BASE_URL", "https://elsewhere.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "unused-openai-key")
    options = _settings(
        timeout_seconds=1.25,
        max_output_tokens=256,
        input_usd_per_million="2",
        cached_input_usd_per_million="0.5",
        output_usd_per_million="8",
    )
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=REQUEST_HEADERS, json=_payload())

    with (
        caplog.at_level(logging.INFO, logger=llm.__name__),
        LLMClient(options, http_client=_http_client(respond)) as client,
    ):
        result = _generate(client)
    (request,) = requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.groq.com/openai/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert set(request.extensions["timeout"].values()) == {1.25}
    body = json.loads(request.content)
    assert body["model"] == "qwen/qwen3.8-27b"
    assert body["messages"] == [
        {"role": "system", "content": "Return the requested status."},
        {"role": "user", "content": "A synthetic prompt."},
    ]
    assert body["max_completion_tokens"] == 256
    assert body.get("stream", False) is False
    assert {
        "store",
        "service_tier",
        "max_output_tokens",
        "instructions",
        "input",
        "text",
    }.isdisjoint(body)
    assert body["response_format"]["type"] == "json_schema"
    json_schema = body["response_format"]["json_schema"]
    assert json_schema["strict"] is True
    assert json_schema["schema"]["additionalProperties"] is False
    assert json_schema["schema"]["required"] == ["status"]
    assert result.output == ConnectionCheck(status="ok")
    assert result.model == "qwen-test" and result.response_id == "chatcmpl-test"
    assert result.response_mode == "json_schema" and result.attempts == 1
    assert result.request_id == REQUEST_ID
    assert result.usage == LLMUsage(900, 400, 100, 1000, Decimal("0.002"))
    assert "total_tokens=1000" in caplog.text
    assert "A synthetic prompt." not in caplog.text and KEY not in caplog.text


@pytest.mark.parametrize(
    ("rates", "estimated_cost"),
    [
        ({}, None),
        (
            {"input_usd_per_million": "2", "output_usd_per_million": "8"},
            Decimal("0.0026"),
        ),
    ],
)
def test_estimated_cost_with_optional_rates(
    rates: dict, estimated_cost: Decimal | None
) -> None:
    http_client = _http_client(lambda _: httpx.Response(200, json=_payload()))
    with LLMClient(_settings(**rates), http_client=http_client) as client:
        result = _generate(client)
    assert result.usage is not None
    assert result.usage.estimated_cost_usd == estimated_cost


@pytest.mark.parametrize("usage_fields", [{}, {"usage": None}], ids=["absent", "null"])
def test_missing_provider_usage_is_reported_as_unknown(usage_fields: dict) -> None:
    payload = _payload()
    del payload["usage"]
    payload.update(usage_fields)
    http_client = _http_client(lambda _: httpx.Response(200, json=payload))
    with LLMClient(_settings(), http_client=http_client) as client:
        result = _generate(client)
    assert result.output == ConnectionCheck(status="ok")
    assert result.usage is None
    assert result.request_id == "req_body"


@pytest.mark.parametrize("details", [None, {}, {"cached_tokens": 0}])
def test_groq_usage_without_cache_discount(details: dict | None) -> None:
    payload = _payload()
    if details is None:
        del payload["usage"]["prompt_tokens_details"]
    else:
        payload["usage"]["prompt_tokens_details"] = details
    http_client = _http_client(lambda _: httpx.Response(200, json=payload))
    rates = _settings(
        input_usd_per_million="2",
        cached_input_usd_per_million="0.5",
        output_usd_per_million="8",
    )
    with LLMClient(rates, http_client=http_client) as client:
        result = _generate(client)
    cached = (details or {}).get("cached_tokens")
    assert result.usage == LLMUsage(900, cached, 100, 1000, Decimal("0.0026"))


def _assert_closed_objects(node: object) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert set(node["required"]) == set(node["properties"])
        for child in node.values():
            _assert_closed_objects(child)
    elif isinstance(node, list):
        for child in node:
            _assert_closed_objects(child)


METRIC = {"scope": "total", "metric": "leads"}
RANKING = {
    "dimension": "source",
    "metric": "leads",
    "period": "current",
    "order": "highest",
}
UNCLEAR = {"comment_id": 1, "reason": "unclear", "evidence": None}


@pytest.mark.parametrize(
    ("schema", "example"),
    [
        (ConnectionCheck, {"status": "ok"}),
        (GroqClassificationResponse, {"classifications": [UNCLEAR]}),
        (
            SummaryPlan,
            {
                "metrics": [METRIC],
                "hypotheses": [{"kind": "source_mix", "evidence": METRIC}],
            },
        ),
        (QuestionPlan, {"status": "answered", "metrics": [METRIC], "ranking": None}),
        (QuestionPlan, {"status": "answered", "metrics": [], "ranking": RANKING}),
    ],
    ids=[
        "connection",
        "classification",
        "summary",
        "question-value",
        "question-ranking",
    ],
)
def test_production_structured_output_contract(
    schema: type[BaseModel], example: dict
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        response_format = json.loads(request.content)["response_format"]
        assert response_format["type"] == "json_schema"
        descriptor = response_format["json_schema"]
        assert descriptor["name"] == schema.__name__
        assert descriptor["strict"] is True
        definition = descriptor["schema"]
        _assert_closed_objects(definition)
        if schema is GroqClassificationResponse:
            batch = definition["properties"]["classifications"]
            assert batch["type"] == "array"
            assert {"minItems", "maxItems"}.isdisjoint(batch)
            assert batch["items"]["$ref"] == "#/$defs/GroqClassificationItem"
            fields = definition["$defs"]["GroqClassificationItem"]["properties"]
            identifier = fields["comment_id"]
            assert identifier["type"] == "integer"
            assert {"minimum", "maximum"}.isdisjoint(identifier)
            assert {"type": "string"} in fields["evidence"]["anyOf"]
            assert {"type": "null"} in fields["evidence"]["anyOf"]
            assert fields["reason"]["$ref"] == "#/$defs/RejectionReason"
            assert set(definition["$defs"]["RejectionReason"]["enum"]) == set(
                RejectionReason
            )
        if schema is QuestionPlan:
            assert {"type": "null"} in definition["properties"]["ranking"]["anyOf"]
        if schema in (SummaryPlan, QuestionPlan):
            assert set(definition["$defs"]["MetricCode"]["enum"]) == set(
                SalesMetrics.model_fields
            )
        return httpx.Response(200, json=_payload(json.dumps(example)))

    with LLMClient(_settings(), http_client=_http_client(respond)) as client:
        result = _generate(client, schema=schema)
    assert type(result.output) is schema
    assert result.output.model_dump(mode="json") == example


@pytest.mark.parametrize(
    ("schema", "invalid"),
    [
        (ConnectionCheck, {"status": "ok", "detail": True}),
        (ConnectionCheck, {"status": 1}),
        (
            GroqClassificationResponse,
            {"classifications": [UNCLEAR | {"comment_id": "1"}]},
        ),
        (
            GroqClassificationResponse,
            {"classifications": [UNCLEAR | {"note": "extra"}]},
        ),
        (
            GroqClassificationResponse,
            {"classifications": [{"comment_id": 1, "reason": "unclear"}]},
        ),
        (SummaryPlan, {"metrics": [METRIC | {"metric": 1}], "hypotheses": []}),
        (
            SummaryPlan,
            {
                "metrics": [METRIC],
                "hypotheses": [
                    {"kind": "source_mix", "evidence": METRIC | {"value": 3}}
                ],
            },
        ),
        (QuestionPlan, {"status": "answered", "metrics": [METRIC]}),
        (QuestionPlan, {"status": "answered", "metrics": [], "ranking": False}),
        (
            QuestionPlan,
            {"status": "answered", "metrics": [], "ranking": RANKING | {"limit": 1}},
        ),
    ],
    ids=[
        "connection-extra",
        "connection-type",
        "classification-type",
        "classification-extra",
        "missing-evidence",
        "metric-type",
        "nested-hypothesis-extra",
        "missing-ranking",
        "ranking-type",
        "ranking-extra",
    ],
)
def test_local_pydantic_validation_rejects_invalid_output_without_retry(
    schema: type[BaseModel], invalid: dict
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_payload(json.dumps(invalid)))

    with (
        LLMClient(_settings(), http_client=_http_client(respond)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, schema=schema)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert str(caught.value) == "The returned JSON failed local Pydantic validation."
    assert len(requests) == 1 and caught.value.retry_usage == ()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_payload(PROVIDER_MESSAGE), LLMErrorCode.INVALID_RESPONSE),
        (_payload(refusal=True), LLMErrorCode.REFUSED),
        (
            _payload('{"status":', finish_reason="length"),
            LLMErrorCode.OUTPUT_LIMIT,
        ),
        (_payload(None, finish_reason="content_filter"), LLMErrorCode.REFUSED),
        (_payload(None, finish_reason="tool_calls"), LLMErrorCode.INCOMPLETE),
        (_payload(None), LLMErrorCode.INVALID_RESPONSE),
        (_payload(""), LLMErrorCode.INVALID_RESPONSE),
    ],
    ids=[
        "invalid-json",
        "refusal",
        "truncated-json",
        "filtered",
        "unexpected-tool-call",
        "null-content",
        "empty-content",
    ],
)
@pytest.mark.parametrize(
    "schema", [ConnectionCheck, GroqClassificationResponse, SummaryPlan, QuestionPlan]
)
def test_unsuccessful_generation_preserves_usage(
    schema: type[BaseModel],
    payload: dict,
    expected: LLMErrorCode,
    caplog: pytest.LogCaptureFixture,
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers=REQUEST_HEADERS, json=payload)

    with (
        caplog.at_level(logging.INFO, logger=llm.__name__),
        LLMClient(_settings(), http_client=_http_client(respond)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, schema=schema)
    error = caught.value
    assert error.code == expected
    assert len(requests) == 1 and error.retry_usage == ()
    assert error.model == "qwen-test"
    assert error.request_id == REQUEST_ID
    assert error.usage == LLMUsage(900, 400, 100, 1000, None)
    assert PROVIDER_MESSAGE not in str(error) and PROVIDER_MESSAGE not in caplog.text


@pytest.mark.parametrize(
    ("status", "provider_code", "expected", "attempts"),
    [
        (400, "invalid_request", LLMErrorCode.REQUEST_REJECTED, 1),
        (400, "json_validate_failed", LLMErrorCode.INVALID_RESPONSE, 3),
        (401, "invalid_api_key", LLMErrorCode.AUTHENTICATION, 1),
        (403, "permission_denied", LLMErrorCode.AUTHENTICATION, 1),
        (408, "timeout", LLMErrorCode.TIMEOUT, 1),
        (429, "rate_limit_exceeded", LLMErrorCode.RATE_LIMIT, 3),
        (429, "insufficient_quota", LLMErrorCode.QUOTA, 1),
        (429, "billing_hard_limit_reached", LLMErrorCode.QUOTA, 1),
        (500, "server_error", LLMErrorCode.UNAVAILABLE, 3),
        (502, "server_error", LLMErrorCode.UNAVAILABLE, 3),
        (503, "server_error", LLMErrorCode.UNAVAILABLE, 3),
        (504, "server_error", LLMErrorCode.UNAVAILABLE, 3),
        (501, "not_implemented", LLMErrorCode.UNAVAILABLE, 1),
    ],
)
def test_provider_status_and_retry_bound(
    status: int,
    provider_code: str,
    expected: LLMErrorCode,
    attempts: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []
    waits = []
    monkeypatch.setattr(llm, "sleep", waits.append)
    monkeypatch.setattr(llm, "uniform", lambda low, high: 0.25)

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            status,
            headers=REQUEST_HEADERS,
            json={"error": {"code": provider_code, "message": PROVIDER_MESSAGE}},
        )

    with (
        LLMClient(_settings(), http_client=_http_client(respond)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(
            client,
            schema=GroqClassificationResponse,
            operation=LLMOperation.CLASSIFICATION,
        )
    assert caught.value.code == expected
    assert caught.value.request_id == REQUEST_ID and caught.value.usage is None
    assert PROVIDER_MESSAGE not in str(caught.value)
    assert len(calls) == attempts
    assert waits == [1.25, 2.25][: attempts - 1]
    assert caught.value.retry_usage == (None,) * (attempts - 1)
    modes = [json.loads(call.content)["response_format"]["type"] for call in calls]
    expected_modes = ["json_schema"] * attempts
    if provider_code == "json_validate_failed":
        expected_modes[-1] = "json_object"
        assert (
            str(caught.value)
            == "Groq rejected generated JSON (HTTP 400, json_validate_failed)."
        )
    assert modes == expected_modes
    assert caught.value.response_mode == modes[-1] and caught.value.attempts == attempts


@pytest.mark.parametrize(
    ("schema", "operation"),
    [
        (ConnectionCheck, None),
        (QuestionPlan, None),
        (SummaryPlan, LLMOperation.SUMMARY),
    ],
)
def test_other_operations_keep_strict_schema_after_repeated_generation_failure(
    schema: type[BaseModel],
    operation: LLMOperation | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    modes = []

    def respond(request: httpx.Request) -> httpx.Response:
        modes.append(json.loads(request.content)["response_format"]["type"])
        return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})

    monkeypatch.setattr(llm, "sleep", lambda _: None)
    with (
        LLMClient(_settings(), http_client=_http_client(respond)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, schema=schema, operation=operation)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert modes == ["json_schema"] * 3


@pytest.mark.parametrize("statuses", [(400, 503), (429, 400)])
def test_classification_keeps_strict_mode_after_mixed_errors(
    statuses: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    modes = []

    def respond(request: httpx.Request) -> httpx.Response:
        modes.append(json.loads(request.content)["response_format"]["type"])
        if len(modes) <= len(statuses):
            status = statuses[len(modes) - 1]
            code = "json_validate_failed" if status == 400 else "temporary"
            return httpx.Response(status, json={"error": {"code": code}})
        return httpx.Response(
            200, json=_payload(json.dumps({"classifications": [UNCLEAR]}))
        )

    monkeypatch.setattr(llm, "sleep", lambda _: None)
    with LLMClient(_settings(), http_client=_http_client(respond)) as client:
        result = _generate(
            client,
            schema=GroqClassificationResponse,
            operation=LLMOperation.CLASSIFICATION,
        )
    assert modes == ["json_schema"] * 3
    assert result.response_mode == "json_schema" and result.attempts == 3


def test_json_fallback_respects_input_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    modes = []

    def respond(request: httpx.Request) -> httpx.Response:
        modes.append(json.loads(request.content)["response_format"]["type"])
        return httpx.Response(400, json={"error": {"code": "json_validate_failed"}})

    monkeypatch.setattr(llm, "sleep", lambda _: None)
    with (
        LLMClient(
            _settings(max_input_chars=100), http_client=_http_client(respond)
        ) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(
            client,
            schema=GroqClassificationResponse,
            operation=LLMOperation.CLASSIFICATION,
        )
    assert modes == ["json_schema", "json_schema"]
    assert caught.value.response_mode == "json_schema" and caught.value.attempts == 2


def test_provider_json_validation_failure_recovers_on_second_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []
    waits = []
    monkeypatch.setattr(llm, "sleep", waits.append)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "code": "json_validate_failed",
                        "message": PROVIDER_MESSAGE,
                    },
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": 4,
                        "total_tokens": 24,
                    },
                },
            )
        return httpx.Response(200, headers=REQUEST_HEADERS, json=_payload())

    with LLMClient(_settings(), http_client=_http_client(respond)) as client:
        result = _generate(client)
    first, second = requests
    assert first.content == second.content and len(waits) == 1
    assert isinstance(result, llm.LLMResult) and result.output == ConnectionCheck(
        status="ok"
    )
    assert result.request_id == REQUEST_ID
    assert result.retry_usage == (LLMUsage(20, None, 4, 24, None),)
    assert result.usage == LLMUsage(900, 400, 100, 1000, None)


@pytest.mark.parametrize(
    ("retry_after", "minimum"), [("4", 4), ("2.5", 2.5), ("bad", 1), ("-3", 1)]
)
def test_retry_after_and_jitter_before_success(
    retry_after: str, minimum: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    requests = []
    waits = []
    monkeypatch.setattr(llm, "sleep", waits.append)

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                json={"error": {"type": "rate_limit_exceeded"}},
            )
        return httpx.Response(200, headers=REQUEST_HEADERS, json=_payload())

    with LLMClient(_settings(), http_client=_http_client(respond)) as client:
        result = _generate(client)
    first, second = requests
    assert first.url == second.url and first.content == second.content
    assert len(waits) == 1 and minimum <= waits[0] <= minimum + 0.5
    assert result.output.status == "ok" and result.retry_usage == (None,)
    assert result.usage == LLMUsage(900, 400, 100, 1000, None)


@pytest.mark.parametrize("retry_after", ["86400", "http-date"])
def test_long_retry_after_returns_control_without_retry(retry_after: str) -> None:
    if retry_after == "http-date":
        retry_after = format_datetime(
            datetime.now(UTC) + timedelta(days=1), usegmt=True
        )
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": retry_after})

    with (
        LLMClient(_settings(), http_client=_http_client(respond)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert caught.value.code == LLMErrorCode.RATE_LIMIT
    assert len(calls) == 1 and caught.value.retry_usage == ()


def test_quota_in_error_type_stops_after_a_temporary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [
            httpx.Response(503, json={"error": {"usage": _payload()["usage"]}}),
            httpx.Response(
                429,
                json={"error": {"type": "insufficient_quota"}},
                headers=REQUEST_HEADERS,
            ),
        ]
    )
    waits = []
    monkeypatch.setattr(llm, "sleep", waits.append)
    with (
        LLMClient(
            _settings(), http_client=_http_client(lambda _: next(replies))
        ) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert len(waits) == 1
    assert caught.value.code == LLMErrorCode.QUOTA
    assert caught.value.request_id == REQUEST_ID
    assert caught.value.usage is None
    assert caught.value.retry_usage == (LLMUsage(900, 400, 100, 1000, None),)


@pytest.mark.parametrize(
    ("network_error", "expected"),
    [
        (httpx.ReadTimeout, LLMErrorCode.TIMEOUT),
        (httpx.ConnectError, LLMErrorCode.UNAVAILABLE),
    ],
)
def test_transport_errors_are_normalized_without_retry(
    network_error: type[httpx.RequestError], expected: LLMErrorCode
) -> None:
    calls = []

    def fail(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise network_error(PROVIDER_MESSAGE, request=request)

    with (
        LLMClient(_settings(), http_client=_http_client(fail)) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert caught.value.code == expected
    assert PROVIDER_MESSAGE not in str(caught.value)
    assert len(calls) == 1


def test_rejects_oversized_input_before_request() -> None:
    def unexpected(_: httpx.Request) -> httpx.Response:
        pytest.fail("The provider must not receive oversized input.")

    with (
        LLMClient(
            _settings(max_input_chars=40), http_client=_http_client(unexpected)
        ) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, prompt="x" * 40)
    assert caught.value.code == LLMErrorCode.INVALID_INPUT


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        json.dumps(_payload() | {"choices": []}),
        json.dumps(
            _payload()
            | {"usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 6}}
        ),
        json.dumps(
            _payload()
            | {"usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7}}
        ),
    ],
    ids=[
        "malformed-json",
        "missing-choice",
        "inconsistent-tokens",
        "responses-api-usage",
    ],
)
def test_malformed_provider_envelope_is_an_error(body: str) -> None:
    http_client = _http_client(
        lambda _: httpx.Response(200, headers=REQUEST_HEADERS, text=body)
    )
    with (
        LLMClient(_settings(), http_client=http_client) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.request_id == REQUEST_ID and caught.value.usage is None


def test_redirect_cannot_send_chat_to_another_host() -> None:
    urls = []

    def redirect(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://elsewhere.invalid/"})

    http_client = httpx.Client(
        transport=httpx.MockTransport(redirect), follow_redirects=True
    )
    with (
        LLMClient(_settings(), http_client=http_client) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert caught.value.code == LLMErrorCode.REQUEST_REJECTED
    assert urls == ["https://api.groq.com/openai/v1/chat/completions"]


def test_settings_load_from_dotenv_and_mask_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        f"GROQ_API_KEY={KEY}\nGROQ_MODEL=llm-test\n"
        "POSTGRES_USER=test-user\nPOSTGRES_DB=test-db\nPOSTGRES_PASSWORD=test-password\n",
        encoding="utf-8",
    )
    for field in PostgresSettings.model_fields:
        monkeypatch.delenv(f"POSTGRES_{field.upper()}", raising=False)
    settings = LLMSettings()
    assert settings.model == "llm-test" and settings.api_key.get_secret_value() == KEY
    assert KEY not in repr(settings)
    postgres = PostgresSettings()
    assert postgres.db == "test-db" and postgres.user == "test-user"


def test_groq_limits_and_independent_operation_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "unused-model")
    monkeypatch.setenv("OPENAI_MAX_OUTPUT_TOKENS", "8192")
    with LLMClient(_settings()) as client:
        for operation in (None, LLMOperation.CLASSIFICATION, LLMOperation.SUMMARY):
            settings = client.request_settings(operation)
            assert settings.model == "qwen/qwen3.8-27b"
            assert settings.timeout_seconds == 60
            assert settings.max_input_chars == 24_000
            assert settings.max_output_tokens == 2_048

    monkeypatch.setenv("GROQ_MAX_OUTPUT_TOKENS", "3072")
    monkeypatch.setenv("GROQ_SUMMARY_MAX_OUTPUT_TOKENS", "4096")
    with LLMClient(_settings()) as client:
        assert (
            client.request_settings(LLMOperation.CLASSIFICATION).max_output_tokens
            == 3072
        )
        assert client.request_settings(LLMOperation.SUMMARY).max_output_tokens == 4096


def test_invalid_groq_key_is_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    invalid_key = "test key with spaces"
    monkeypatch.setenv("GROQ_API_KEY", invalid_key)
    with pytest.raises(ValidationError, match="GROQ_API_KEY") as caught:
        LLMSettings(_env_file=None)
    assert invalid_key not in str(caught.value)


def test_incomplete_price_config_is_rejected() -> None:
    with pytest.raises(
        ValidationError, match="Set both input and output prices"
    ) as caught:
        _settings(input_usd_per_million="2")
    assert KEY not in str(caught.value)


def test_check_command_reads_environment_and_prints_usage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setattr(
        llm,
        "DefaultHttpxClient",
        lambda **_: _http_client(
            lambda __: httpx.Response(200, headers=REQUEST_HEADERS, json=_payload())
        ),
    )
    assert check_llm() == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["output"] == {"status": "ok"}
    assert (
        result["usage"]["total_tokens"] == 1000 and result["request_id"] == REQUEST_ID
    )
    assert KEY not in output.out and not output.err


@pytest.mark.parametrize("legacy_key", [False, True])
def test_ai_commands_require_groq_key(
    legacy_key: bool,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    if legacy_key:
        monkeypatch.setenv("OPENAI_API_KEY", KEY)
        Path(".env").write_text(f"OPENAI_API_KEY={KEY}\n", encoding="utf-8")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValidationError) as caught:
        LLMSettings()
    assert caught.value.errors(include_input=False)[0]["type"] == "missing"
    assert check_llm() == 2
    assert ai_sales(["evaluate"]) == 2
    output = capsys.readouterr()
    assert not output.out and output.err.count("GROQ_API_KEY") == 2
    assert KEY not in output.err


def test_cli_identifies_invalid_groq_setting_without_printing_its_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", KEY)
    monkeypatch.setenv("GROQ_SUMMARY_MAX_OUTPUT_TOKENS", "private-invalid-value")
    assert check_llm() == 2
    assert ai_sales(["evaluate"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert (
        output.err.splitlines()
        == ["Check Groq settings in .env: GROQ_SUMMARY_MAX_OUTPUT_TOKENS."] * 2
    )

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai import client as llm
from app.ai.client import LLMClient, LLMError, LLMErrorCode, LLMOperation, LLMUsage
from app.ai.rejections import MAX_COMMENTS, MAX_EVIDENCE_CHARS, ClassificationPlan
from app.ai.sales_analysis import QuestionPlan, SummaryPlan
from app.core.config import LLMSettings, PostgresSettings
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
):
    return client.generate(
        instructions="Return the requested status.",
        prompt=prompt,
        schema=schema,
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


def test_missing_provider_usage_is_reported_as_unknown() -> None:
    payload = _payload() | {"usage": None}
    http_client = _http_client(lambda _: httpx.Response(200, json=payload))
    with LLMClient(_settings(), http_client=http_client) as client:
        result = _generate(client)
    assert result.output == ConnectionCheck(status="ok")
    assert result.usage is None
    assert result.request_id == "req_body"


def test_groq_usage_without_cache_details() -> None:
    payload = _payload()
    del payload["usage"]["prompt_tokens_details"]
    http_client = _http_client(lambda _: httpx.Response(200, json=payload))
    with LLMClient(_settings(), http_client=http_client) as client:
        result = _generate(client)
    assert result.usage == LLMUsage(900, 0, 100, 1000, None)


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
        (ClassificationPlan, {"classifications": [UNCLEAR]}),
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
        if schema is ClassificationPlan:
            batch = definition["properties"]["classifications"]
            assert (batch["minItems"], batch["maxItems"]) == (1, MAX_COMMENTS)
            item = definition["$defs"]["RejectionClassification"]
            identifier = item["properties"]["comment_id"]
            assert (identifier["minimum"], identifier["maximum"]) == (1, MAX_COMMENTS)
            evidence = item["properties"]["evidence"]["anyOf"]
            assert {"type": "null"} in evidence
            assert {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_EVIDENCE_CHARS,
            } in evidence
        if schema is QuestionPlan:
            assert {"type": "null"} in definition["properties"]["ranking"]["anyOf"]
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
        (ClassificationPlan, {"classifications": [UNCLEAR | {"comment_id": "1"}]}),
        (ClassificationPlan, {"classifications": [UNCLEAR | {"note": "extra"}]}),
        (
            ClassificationPlan,
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
def test_production_schema_rejects_invalid_output(
    schema: type[BaseModel], invalid: dict
) -> None:
    transport = _http_client(
        lambda _: httpx.Response(200, json=_payload(json.dumps(invalid)))
    )
    with (
        LLMClient(_settings(), http_client=transport) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, schema=schema)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE


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
    "schema", [ConnectionCheck, ClassificationPlan, SummaryPlan, QuestionPlan]
)
def test_unsuccessful_generation_preserves_usage(
    schema: type[BaseModel],
    payload: dict,
    expected: LLMErrorCode,
    caplog: pytest.LogCaptureFixture,
) -> None:
    http_client = _http_client(
        lambda _: httpx.Response(200, headers=REQUEST_HEADERS, json=payload)
    )
    with (
        caplog.at_level(logging.INFO, logger=llm.__name__),
        LLMClient(_settings(), http_client=http_client) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client, schema=schema)
    error = caught.value
    assert error.code == expected
    assert error.model == "qwen-test"
    assert error.request_id == REQUEST_ID
    assert error.usage == LLMUsage(900, 400, 100, 1000, None)
    assert PROVIDER_MESSAGE not in str(error) and PROVIDER_MESSAGE not in caplog.text


@pytest.mark.parametrize(
    ("status", "provider_code", "expected"),
    [
        (400, "invalid_request", LLMErrorCode.REQUEST_REJECTED),
        (400, "json_validate_failed", LLMErrorCode.INVALID_RESPONSE),
        (401, "invalid_api_key", LLMErrorCode.AUTHENTICATION),
        (403, "permission_denied", LLMErrorCode.AUTHENTICATION),
        (408, "timeout", LLMErrorCode.TIMEOUT),
        (429, "rate_limit_exceeded", LLMErrorCode.RATE_LIMIT),
        (429, "insufficient_quota", LLMErrorCode.QUOTA),
        (503, "server_error", LLMErrorCode.UNAVAILABLE),
    ],
)
def test_provider_status_is_normalized_without_retry(
    status: int, provider_code: str, expected: LLMErrorCode
) -> None:
    calls = []

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
        _generate(client)
    assert caught.value.code == expected
    assert caught.value.request_id == REQUEST_ID and caught.value.usage is None
    assert PROVIDER_MESSAGE not in str(caught.value)
    assert len(calls) == 1


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
    ],
    ids=["malformed-json", "missing-choice", "inconsistent-tokens"],
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

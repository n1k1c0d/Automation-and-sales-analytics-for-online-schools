import json
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from app.ai import client as llm
from app.ai.client import LLMClient, LLMError, LLMErrorCode, LLMUsage
from app.core.config import LLMSettings
from scripts.check_llm import main as check_llm

KEY = "unit-test-token"
PROVIDER_MESSAGE = "private provider message"
REQUEST_ID = "req_test"
REQUEST_HEADERS = {"x-request-id": REQUEST_ID}


class StructuredStatus(BaseModel):
    status: Literal["ok"]


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for field in LLMSettings.model_fields:
        monkeypatch.delenv(f"OPENAI_{field.upper()}", raising=False)


def _settings(**overrides) -> LLMSettings:
    return LLMSettings(_env_file=None, api_key=KEY, **overrides)


def _payload(
    output: str = '{"status":"ok"}',
    *,
    status: str = "completed",
    reason: str | None = None,
    refusal: bool = False,
) -> dict:
    part = (
        {"type": "refusal", "refusal": PROVIDER_MESSAGE}
        if refusal
        else {"type": "output_text", "text": output, "annotations": []}
    )
    return {
        "id": "resp_test",
        "object": "response",
        "created_at": 0,
        "status": status,
        "model": "gpt-5-mini-test",
        "incomplete_details": {"reason": reason} if reason else None,
        "output": [
            {"id": "rs_test", "type": "reasoning", "summary": []},
            {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "status": "completed" if status == "completed" else "incomplete",
                "content": [part],
            },
        ],
        "parallel_tool_calls": False,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": 900,
            "input_tokens_details": {"cached_tokens": 400, "cache_write_tokens": 0},
            "output_tokens": 100,
            "output_tokens_details": {"reasoning_tokens": 40},
            "total_tokens": 1000,
        },
    }


def _http_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _generate(client: LLMClient, *, prompt: str = "A synthetic prompt."):
    return client.generate(
        instructions="Return the requested status.",
        prompt=prompt,
        schema=StructuredStatus,
    )


def test_uses_strict_structured_outputs_and_accounts_for_usage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://elsewhere.invalid/v1")
    options = _settings(
        model="gpt-5-mini",
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
    assert str(request.url) == "https://api.openai.com/v1/responses"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert set(request.extensions["timeout"].values()) == {1.25}
    body = json.loads(request.content)
    assert body["model"] == "gpt-5-mini"
    assert body["input"] == "A synthetic prompt."
    assert body["instructions"] == "Return the requested status."
    assert body["max_output_tokens"] == 256
    assert body["store"] is False
    assert body["service_tier"] == "default"
    output_format = body["text"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["strict"] is True
    assert output_format["schema"]["additionalProperties"] is False
    assert output_format["schema"]["required"] == ["status"]
    assert result.output == StructuredStatus(status="ok")
    assert result.request_id == REQUEST_ID and result.response_id == "resp_test"
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
    assert result.output == StructuredStatus(status="ok")
    assert result.usage is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (_payload(PROVIDER_MESSAGE), LLMErrorCode.INVALID_RESPONSE),
        (_payload('{"status":42}'), LLMErrorCode.INVALID_RESPONSE),
        (_payload('{"status":"ok","extra":true}'), LLMErrorCode.INVALID_RESPONSE),
        (_payload(refusal=True), LLMErrorCode.REFUSED),
        (
            _payload('{"status":', status="incomplete", reason="max_output_tokens"),
            LLMErrorCode.OUTPUT_LIMIT,
        ),
        (_payload(status="incomplete", reason="content_filter"), LLMErrorCode.REFUSED),
        (_payload(status="cancelled"), LLMErrorCode.INCOMPLETE),
        (_payload(status="failed"), LLMErrorCode.UNAVAILABLE),
    ],
    ids=[
        "invalid-json",
        "wrong-type",
        "extra-field",
        "refusal",
        "truncated-json",
        "filtered",
        "cancelled",
        "failed",
    ],
)
def test_unsuccessful_generation_preserves_usage(
    payload: dict, expected: LLMErrorCode, caplog: pytest.LogCaptureFixture
) -> None:
    http_client = _http_client(
        lambda _: httpx.Response(200, headers=REQUEST_HEADERS, json=payload)
    )
    with (
        caplog.at_level(logging.INFO, logger=llm.__name__),
        LLMClient(_settings(), http_client=http_client) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    error = caught.value
    assert error.code == expected
    assert error.request_id == REQUEST_ID
    assert error.usage == LLMUsage(900, 400, 100, 1000, None)
    assert PROVIDER_MESSAGE not in str(error) and PROVIDER_MESSAGE not in caplog.text


@pytest.mark.parametrize(
    ("status", "provider_code", "expected"),
    [
        (400, "invalid_request", LLMErrorCode.REQUEST_REJECTED),
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


def test_malformed_provider_envelope_is_an_error() -> None:
    http_client = _http_client(
        lambda _: httpx.Response(200, headers=REQUEST_HEADERS, text="not json")
    )
    with (
        LLMClient(_settings(), http_client=http_client) as client,
        pytest.raises(LLMError) as caught,
    ):
        _generate(client)
    assert caught.value.code == LLMErrorCode.INVALID_RESPONSE
    assert caught.value.request_id == REQUEST_ID and caught.value.usage is None


def test_settings_load_from_dotenv_and_mask_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        f"OPENAI_API_KEY={KEY}\nOPENAI_MODEL=llm-test\n", encoding="utf-8"
    )
    settings = LLMSettings()
    assert settings.model == "llm-test" and settings.api_key.get_secret_value() == KEY
    assert KEY not in repr(settings)


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
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
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


def test_check_command_reports_missing_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert check_llm() == 2
    output = capsys.readouterr()
    assert not output.out and "OPENAI_API_KEY" in output.err

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_live_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected_request(*_args, **_kwargs):
        pytest.fail("Use a mock HTTP transport in unit tests.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unexpected_request)
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", unexpected_request
    )

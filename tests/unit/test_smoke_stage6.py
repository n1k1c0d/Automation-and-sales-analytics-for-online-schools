import json
from functools import partial

import httpx2
import pytest

from scripts import smoke_stage6 as smoke


@pytest.mark.parametrize("offline", [False, True], ids=["invalid-kpi", "api-offline"])
def test_smoke_reports_failure_and_continues_to_readiness(monkeypatch, capsys, offline):
    paths = []

    def respond(request):
        path = request.url.path
        paths.append(path)
        if offline and request.url.host == "api.test":
            raise httpx2.ConnectError("private-test-detail", request=request)
        if path == "/api/v1/metrics":
            return httpx2.Response(200, json={"total": "private-test-detail"})
        if path == "/api/v1/dimensions":
            return httpx2.Response(200, json={"sources": [], "managers": []})
        if path == "/_stcore/health":
            return httpx2.Response(200, text="ok")
        return httpx2.Response(200, json={"status": "ok"})

    client = partial(httpx2.Client, transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(smoke.httpx2, "Client", client)
    monkeypatch.setattr(smoke.check_db, "main", lambda: 0)
    monkeypatch.setattr(
        smoke.check_llm, "main", lambda: pytest.fail("unexpected Groq call")
    )
    assert (
        smoke.main(["--api-url", "http://api.test", "--n8n-url", "http://n8n.test"])
        == 1
    )
    output = capsys.readouterr().out
    assert "FAIL FastAPI sales report" in output
    assert "PASS n8n readiness" in output
    assert "private-test-detail" not in output
    assert paths[-2:] == ["/_stcore/health", "/healthz/readiness"]


def test_changed_workflow_timezone_fails_preflight(tmp_path):
    export = tmp_path / "workflow.json"
    export.write_text(json.dumps({"settings": {"timezone": "UTC"}}))
    with pytest.raises(smoke.SmokeFailure, match="Europe/Moscow"):
        smoke.validate_workflow(export)

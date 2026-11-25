from datetime import date
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from app.api.routes import sales_metrics
from workflows.report_api import app


def test_report_projection_forwards_filters_and_omits_private_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager_id = UUID("ac862450-6244-4e78-bc10-871539841eae")
    period = {"start_date": "2026-06-01", "end_date": "2026-06-01"}
    private_label = "learner@example.test"
    kpis = {
        "paid_amount_rub": "100.04",
        "paying_leads": 1,
        "average_paid_check_rub": "100.04",
        "lesson_to_payment_pct": None,
    }

    def report(engine, current, *, comparison_period, **filters):
        assert filters == {"source": "webinar", "manager_id": manager_id}
        assert current.cutoff_date == date(2026, 6, 1) and comparison_period is None
        return {
            "period": current,
            "total": kpis,
            "by_manager": [
                dict(kpis, manager_name=private_label, manager_id=manager_id)
            ],
            "by_source": [dict(kpis, source=s) for s in ["webinar", private_label]],
        }

    monkeypatch.setattr(sales_metrics, "read_sales_report", report)
    monkeypatch.setattr(sales_metrics, "get_db_engine", lambda _: object())
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/metrics",
            params=period | {"source": "webinar", "manager_id": str(manager_id)},
        )
    assert response.status_code == 200
    assert private_label not in response.text
    body = response.json()
    assert body["total"] == kpis
    assert body["by_manager"] == [dict(kpis, manager_id=str(manager_id))]
    assert body["by_source"][0]["source"] == "webinar"
    assert body["by_source"][1]["source"].startswith("source-")


def test_report_api_errors_omit_input_and_import_route() -> None:
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/metrics", params={"manager_id": "private@example.test"}
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "Invalid report parameters."}
        assert client.post("/api/v1/import").status_code == 404

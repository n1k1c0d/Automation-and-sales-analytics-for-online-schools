from collections.abc import Iterator
from datetime import UTC, datetime
from http import HTTPStatus
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import Engine, select

from app.database.models import Lead
from app.main import app

MANAGERS = b"""external_id,name,team,created_at
M1,First Manager,Demo,2026-05-01T00:00:00Z
M2,Second Manager,Demo,2026-05-01T00:00:00Z
"""
LEADS = b"""external_id,manager_external_id,source,created_at,status
L1,M1,webinar,2026-06-01T10:00:00Z,won
L2,M2,referral,2026-06-01T12:00:00Z,new
L3,M1,webinar,2026-05-31T10:00:00Z,new
L4,M1,webinar,2026-06-01T12:00:00Z,unknown
"""
LESSONS = b"""external_id,lead_external_id,scheduled_at,held_at,status
A1,L1,2026-06-02T10:00:00Z,2026-06-02T10:00:00Z,completed
"""
PAYMENTS = b"""external_id,lead_external_id,amount_rub,paid_at,status
P1,L1,150.25,2026-06-05T10:00:00Z,paid
"""


@pytest.fixture
def api_client(
    isolated_postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    monkeypatch.setenv("POSTGRES_DB", str(isolated_postgres_engine.url.database))
    with TestClient(app) as client:
        yield client


def test_csv_import_and_metrics_round_trip(api_client: TestClient) -> None:
    data = {"source_system": "api_csv"}
    rejected = api_client.post(
        "/api/v1/import",
        data=data,
        files=[
            ("files", ("managers.csv", MANAGERS)),
            ("files", ("leads.csv", b"external_id,bad_header\n")),
        ],
    )
    assert rejected.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "missing columns" in rejected.json()["detail"]

    files = [
        ("files", ("payments.csv", PAYMENTS, "text/csv")),
        ("files", ("leads.csv", LEADS, "text/csv")),
        ("files", ("managers.csv", MANAGERS, "text/csv")),
        ("files", ("lessons.csv", LESSONS, "text/csv")),
    ]
    imported = api_client.post("/api/v1/import", data=data, files=files)
    assert imported.status_code == HTTPStatus.OK
    report = imported.json()
    assert report["source_system"] == "api_csv"
    assert {name: report["tables"][name]["inserted"] for name in report["tables"]} == {
        "managers": 2,
        "leads": 3,
        "lessons": 1,
        "payments": 1,
    }
    assert report["tables"]["leads"]["invalid"] == 1
    assert [
        (issue["table"], issue["row"], issue["outcome"]) for issue in report["issues"]
    ] == [("leads", 5, "invalid")]

    repeated = api_client.post("/api/v1/import", data=data, files=files)
    assert repeated.status_code == HTTPStatus.OK
    assert repeated.json()["tables"]["leads"] == {
        "inserted": 0,
        "duplicate": 3,
        "invalid": 1,
        "conflict": 0,
    }

    period = {"start_date": "2026-06-01", "end_date": "2026-06-03"}
    metrics = api_client.get("/api/v1/metrics", params=period)
    assert metrics.status_code == HTTPStatus.OK
    current = metrics.json()
    assert current["period"]["cutoff_date"] == "2026-06-03"
    assert current["total"]["leads"] == 2
    assert current["total"]["paid_amount_rub"] == "0.00"
    assert current["total"]["average_paid_check_rub"] is None
    assert current["total"]["lesson_to_payment_pct"] == "0.00"
    assert [day["day"] for day in current["daily"]] == [
        "2026-06-01",
        "2026-06-02",
        "2026-06-03",
    ]
    first_manager = next(
        row["manager_id"]
        for row in current["by_manager"]
        if row["manager_name"] == "First Manager"
    )

    comparison = api_client.get(
        "/api/v1/metrics",
        params=period
        | {
            "cutoff_date": "2026-06-05",
            "source": " webinar ",
            "manager_id": first_manager,
            "compare_start_date": "2026-05-31",
            "compare_end_date": "2026-05-31",
        },
    )
    assert comparison.status_code == HTTPStatus.OK
    compared = comparison.json()
    assert compared["current"]["source"] == "webinar"
    assert compared["current"]["manager_id"] == first_manager
    assert compared["current"]["total"]["leads"] == 1
    assert compared["current"]["total"]["completed_lessons"] == 1
    assert compared["current"]["total"]["paid_amount_rub"] == "0.00"
    assert compared["current"]["total"]["lesson_to_payment_pct"] == "100.00"
    assert compared["comparison"]["total"]["leads"] == 1
    assert compared["comparison"]["total"]["lesson_to_payment_pct"] is None

    empty = api_client.get(
        "/api/v1/metrics",
        params=period | {"manager_id": first_manager, "source": "referral"},
    )
    assert empty.status_code == HTTPStatus.OK
    assert empty.json()["total"]["leads"] == 0

    with_payment = api_client.get(
        "/api/v1/metrics", params=period | {"end_date": "2026-06-05"}
    )
    assert with_payment.status_code == HTTPStatus.OK
    assert with_payment.json()["total"]["paid_amount_rub"] == "150.25"
    assert with_payment.json()["total"]["average_paid_check_rub"] == "150.25"


def test_excel_import_uses_explicit_time_zone(
    api_client: TestClient, isolated_postgres_engine: Engine
) -> None:
    workbook = Workbook()
    workbook.active.title = "leads"
    workbook.active.append(
        ("external_id", "manager_external_id", "source", "created_at", "status")
    )
    workbook.active.append(
        ("0002", "0001", "referral", datetime(2026, 6, 2, 10), "new")
    )
    managers = workbook.create_sheet("managers")
    managers.append(("external_id", "name", "team", "created_at"))
    managers.append(("0001", "Excel Manager", "Demo", datetime(2026, 6, 1, 10)))
    body = BytesIO()
    workbook.save(body)
    workbook.close()
    response = api_client.post(
        "/api/v1/import",
        data={"source_system": "api_xlsx", "excel_timezone": "Europe/Moscow"},
        files={"files": ("sales.xlsx", body.getvalue())},
    )
    assert response.status_code == HTTPStatus.OK
    report = response.json()
    assert not report["issues"]
    assert report["tables"]["managers"]["inserted"] == 1
    assert report["tables"]["leads"]["inserted"] == 1
    with isolated_postgres_engine.connect() as connection:
        stored = connection.execute(
            select(Lead.external_id, Lead.created_at).where(
                Lead.source_system == "api_xlsx"
            )
        ).one()
    assert tuple(stored) == ("0002", datetime(2026, 6, 2, 7, tzinfo=UTC))

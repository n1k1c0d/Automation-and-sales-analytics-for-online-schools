from http import HTTPStatus
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app.api.routes import sales_import
from app.main import app

PERIOD = {"start_date": "2026-06-01", "end_date": "2026-06-30"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("POSTGRES_DB", "")
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": "2026-06-31"},
        {"start_date": "2026-06-01T00:00:00"},
        {"start_date": "0001-01-01"},
        {"start_date": "2026-07-01"},
        {"end_date": "2027-06-03"},
        {"manager_id": "not-a-uuid"},
        {"source": "  "},
        {"source": "web\x00inar"},
        {"compare_start_date": "2026-05-01"},
        {"compare_cutoff_date": "2026-05-31"},
        {"compare_start_date": "2026-05-31", "compare_end_date": "2026-05-01"},
        {"unexpected_filter": "value"},
    ],
)
def test_metrics_reject_invalid_filters_before_accessing_database(
    client: TestClient, changes: dict[str, str]
) -> None:
    response = client.get("/api/v1/metrics", params=PERIOD | changes)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"]


@pytest.mark.parametrize(
    "changes",
    [
        {"cutoff_date": "2026-05-01"},
        {
            "compare_start_date": "2026-05-01",
            "compare_end_date": "2026-05-31",
            "compare_cutoff_date": "2026-05-30",
        },
    ],
    ids=["current-cutoff", "comparison-cutoff"],
)
def test_metrics_reject_cutoff_before_period_end(
    client: TestClient, changes: dict[str, str]
) -> None:
    response = client.get("/api/v1/metrics", params=PERIOD | changes)
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"][0]["type"] == "cutoff_before_period_end"


@pytest.mark.parametrize(
    "names",
    [
        [],
        ["other.csv"],
        ["../managers.csv"],
        ["managers.csv", "managers.csv"],
        ["sales.xlsx", "managers.csv"],
    ],
)
def test_import_rejects_invalid_file_selection(
    client: TestClient, names: list[str]
) -> None:
    response = client.post(
        "/api/v1/import",
        data={"source_system": "api_test"},
        files=[("files", (name, b"example")) for name in names],
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_import_rejects_invalid_source_code(client: TestClient) -> None:
    response = client.post(
        "/api/v1/import",
        data={"source_system": "invalid source"},
        files={"files": ("managers.csv", b"example")},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"][0]["loc"] == ["body", "source_system"]


def test_bad_workbook_is_reported_as_input_error(client: TestClient) -> None:
    response = client.post(
        "/api/v1/import",
        data={"source_system": "api_test"},
        files={"files": ("sales.xlsx", b"not a workbook")},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json() == {"detail": "Could not read the Excel workbook."}


def test_import_rejects_oversized_workbook_before_unpacking(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sales_import, "MAX_WORKBOOK_BYTES", 64)
    workbook = BytesIO()
    with ZipFile(workbook, "w") as archive:
        archive.writestr("workbook.xml", b"x" * 65)
    response = client.post(
        "/api/v1/import",
        data={"source_system": "api_test"},
        files={"files": ("sales.xlsx", workbook.getvalue())},
    )
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


@pytest.mark.parametrize("streamed", [False, True])
def test_request_limit_also_applies_without_content_length(
    client: TestClient, streamed: bool
) -> None:
    start = b'--boundary\r\nContent-Disposition: form-data; name="files"; filename="managers.csv"\r\n\r\n'
    end = b"x" * sales_import.MAX_REQUEST_BYTES + b"\r\n--boundary--\r\n"
    response = client.post(
        "/api/v1/import",
        headers={"Content-Type": "multipart/form-data; boundary=boundary"},
        content=iter((start, end)) if streamed else start + end,
    )
    assert response.status_code == HTTPStatus.REQUEST_ENTITY_TOO_LARGE


def test_missing_database_settings_keep_health_available(client: TestClient) -> None:
    assert client.get("/health").json() == {"status": "ok"}
    response = client.get("/api/v1/metrics", params=PERIOD)
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert (
        client.get("/api/v1/dimensions").status_code == HTTPStatus.SERVICE_UNAVAILABLE
    )


def test_database_errors_do_not_expose_sql_or_credentials(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine("sqlite://")
    monkeypatch.setattr(app.state, "db_engine", engine)
    try:
        response = client.get("/api/v1/metrics", params=PERIOD)
    finally:
        engine.dispose()
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.json() == {
        "detail": "PostgreSQL is unavailable. Check the database and migrations."
    }


def test_openapi_describes_sales_endpoints(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "get" in paths["/api/v1/dimensions"]
    assert (
        "multipart/form-data"
        in paths["/api/v1/import"]["post"]["requestBody"]["content"]
    )
    assert {
        parameter["name"] for parameter in paths["/api/v1/metrics"]["get"]["parameters"]
    } == {
        "start_date",
        "end_date",
        "cutoff_date",
        "manager_id",
        "source",
        "compare_start_date",
        "compare_end_date",
        "compare_cutoff_date",
    }

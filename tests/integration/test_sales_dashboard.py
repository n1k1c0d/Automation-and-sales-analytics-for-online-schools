from datetime import date
from functools import partial
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from streamlit.testing.v1 import AppTest

from app.main import app
from app.services.sales_import import import_sales_data
from dashboard import api
from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset

ENTRYPOINT = Path(__file__).resolve().parents[2] / "dashboard/main.py"


def test_dashboard_filters_and_compares_the_standard_dataset(
    isolated_postgres_engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = tmp_path / "reference"
    write_csv_dataset(generate_dataset(), data)
    assert not import_sales_data(
        isolated_postgres_engine, data, source_system="synthetic_42"
    ).issues
    monkeypatch.setenv("POSTGRES_DB", str(isolated_postgres_engine.url.database))
    monkeypatch.setenv("EDUFLOW_API_URL", "http://dashboard-test")

    requests = []
    with TestClient(app) as backend:

        def forward(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/api/v1/metrics"
            response = backend.get(request.url.path, params=request.url.params)
            requests.append((dict(request.url.params), response.json()))
            return response

        client = partial(httpx2.Client, transport=httpx2.MockTransport(forward))
        monkeypatch.setattr(api.httpx2, "Client", client)
        dashboard = AppTest.from_file(str(ENTRYPOINT), default_timeout=20).run()
        assert not dashboard.exception
        assert [(card.label, card.value) for card in dashboard.metric][:4] == [
            ("Входящие лиды", "500"),
            ("Проведённые уроки", "335"),
            ("Оплатили, лидов", "105"),
            ("Сумма оплат", "8\u202f506\u202f500 ₽"),
        ]
        assert len(dashboard.get("vega_lite_chart")) == 2
        assert len(dashboard.dataframe) == 2

        dashboard.date_input(key="period").set_value(
            (date(2026, 7, 1), date(2026, 7, 31))
        )
        dashboard.toggle(key="compare").set_value(True).run()
        dashboard.date_input(key="compare_period").set_value(
            (date(2026, 6, 1), date(2026, 6, 30))
        )
        dashboard.date_input(key="compare_cutoff").set_value(date(2026, 8, 31)).run()
        assert not dashboard.exception
        assert [(card.value, card.delta) for card in dashboard.metric][:4] == [
            ("165", "-2"),
            ("112", "+10"),
            ("42", "+27"),
            ("3\u202f210\u202f000 ₽", "+1\u202f959\u202f500 ₽"),
        ]
        assert dashboard.metric[5].delta == "+10,94 п. п."

        manager_id = requests[-1][1]["current"]["by_manager"][0]["manager_id"]
        dashboard.selectbox(key="source").select("webinar")
        dashboard.selectbox(key="manager").select(manager_id).run()
        assert not dashboard.exception
        params, report = requests[-1]
        assert params == {
            "start_date": "2026-07-01",
            "end_date": "2026-07-31",
            "cutoff_date": "2026-10-31",
            "manager_id": manager_id,
            "source": "webinar",
            "compare_start_date": "2026-06-01",
            "compare_end_date": "2026-06-30",
            "compare_cutoff_date": "2026-08-31",
        }
        assert dashboard.metric[0].value == str(report["current"]["total"]["leads"])

        dashboard.toggle(key="compare").set_value(False)
        dashboard.date_input(key="period").set_value(
            (date(2027, 1, 1), date(2027, 1, 31))
        ).run()
        assert not dashboard.exception
        assert dashboard.metric[0].value == "0"
        assert dashboard.info[0].value == "За выбранные периоды и фильтры данных нет."

import os
from http import HTTPStatus

import httpx2
import streamlit as st
from pydantic import ValidationError

from app.schemas.sales_metrics import SalesMetricsQuery
from app.schemas.sales_report import SalesReport, SalesReportComparison


class DashboardAPIError(Exception):
    pass


def get_api_url() -> str:
    return os.getenv("EDUFLOW_API_URL", "http://127.0.0.1:8000").rstrip("/")


@st.cache_data(ttl=30, max_entries=64, show_spinner=False)
def get_report(
    base_url: str, query: SalesMetricsQuery
) -> SalesReport | SalesReportComparison:
    params = query.model_dump(mode="json", exclude_none=True)
    try:
        with httpx2.Client(timeout=5, trust_env=False) as client:
            response = client.get(f"{base_url}/api/v1/metrics", params=params)
            response.raise_for_status()
    except httpx2.TimeoutException as exc:
        raise DashboardAPIError(
            "API не ответил за 5 секунд. Повторите запрос."
        ) from exc
    except httpx2.HTTPStatusError as exc:
        if exc.response.status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            message = (
                "API доступен, но PostgreSQL не отвечает. Проверьте базу и миграции."
            )
        else:
            message = f"Не удалось получить отчёт. API вернул HTTP {exc.response.status_code}."
        raise DashboardAPIError(message) from exc
    except (httpx2.RequestError, httpx2.InvalidURL) as exc:
        raise DashboardAPIError(
            "Не удалось подключиться к API. Проверьте, запущен ли FastAPI и верен ли его адрес."
        ) from exc

    schema = SalesReport if query.compare_start_date is None else SalesReportComparison
    try:
        return schema.model_validate_json(response.content)
    except ValidationError as exc:
        raise DashboardAPIError("Ответ API не соответствует формату отчёта.") from exc

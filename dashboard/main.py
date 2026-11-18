from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import streamlit as st
from pydantic import ValidationError

from app.schemas.sales_metrics import (
    MAX_REPORT_DAYS,
    SalesDimensions,
    SalesMetricsQuery,
)
from app.schemas.sales_report import (
    CUTOFF_BEFORE_PERIOD_END,
    SalesReport,
    SalesReportComparison,
)
from dashboard.api import DashboardAPIError, get_api_url, get_dimensions, get_report
from dashboard.views import (
    breakdowns,
    funnel_chart,
    headline_metrics,
    period_label,
    source_label,
    trend_chart,
)

DEMO_PERIOD = (date(2026, 6, 1), date(2026, 8, 31))
DEMO_CUTOFF = date(2026, 10, 31)
MIN_DATE = date.min + timedelta(days=1)
MAX_DATE = date.max - timedelta(days=1)
CUTOFF_HELP = "Выберите дату не раньше конца периода. Пустое поле использует последний день периода."


def _date_input(label: str, value, *, key: str, help_text: str | None = None):
    return st.date_input(
        label,
        value,
        min_value=MIN_DATE,
        max_value=MAX_DATE,
        format="DD.MM.YYYY",
        key=key,
        help=help_text,
    )


def _period_query() -> SalesMetricsQuery:
    days = _date_input("Текущий период", DEMO_PERIOD, key="period")
    cutoff = _date_input(
        "Срез когорты", DEMO_CUTOFF, key="cutoff", help_text=CUTOFF_HELP
    )
    compare_enabled = st.toggle("Сравнить периоды", key="compare")
    if len(days) != 2:
        raise ValueError("Выберите начало и конец текущего периода.")
    start, end = days
    values = {"start_date": start, "end_date": end, "cutoff_date": cutoff}
    if compare_enabled:
        compare_end = max(MIN_DATE, start - timedelta(days=1))
        span = max(timedelta(), min(end - start, compare_end - MIN_DATE))
        default_start = compare_end - span
        compare = _date_input(
            "Период сравнения", (default_start, compare_end), key="compare_period"
        )
        comparison_cutoff = _date_input(
            "Срез сравнения", None, key="compare_cutoff", help_text=CUTOFF_HELP
        )
        if len(compare) != 2:
            raise ValueError("Выберите начало и конец периода сравнения.")
        values.update(
            compare_start_date=compare[0],
            compare_end_date=compare[1],
            compare_cutoff_date=comparison_cutoff,
        )
    try:
        return SalesMetricsQuery.model_validate(values)
    except ValidationError as exc:
        if exc.errors()[0]["type"] == CUTOFF_BEFORE_PERIOD_END:
            raise ValueError(
                "Дата среза должна совпадать с последним днём своего периода или быть позже."
            ) from exc
        raise ValueError(
            f"Проверьте даты. Каждый период должен содержать от 1 до {MAX_REPORT_DAYS} дней."
        ) from exc


def _split_report(
    report: SalesReport | SalesReportComparison,
) -> tuple[SalesReport, SalesReport | None]:
    if isinstance(report, SalesReportComparison):
        return report.current, report.comparison
    return report, None


def _dimension_filters(dimensions: SalesDimensions) -> tuple[str | None, str | None]:
    managers = {str(row.manager_id): row.manager_name for row in dimensions.managers}
    duplicates = Counter(managers.values())
    manager_labels = {
        identifier: f"{name} · {identifier[:8]}" if duplicates[name] > 1 else name
        for identifier, name in managers.items()
    }
    manager = st.selectbox(
        "Менеджер",
        [
            None,
            *sorted(
                managers,
                key=lambda identifier: (manager_labels[identifier], identifier),
            ),
        ],
        format_func=lambda identifier: (
            manager_labels[identifier] if identifier else "Все менеджеры"
        ),
        key="manager",
    )
    source = st.selectbox(
        "Канал привлечения",
        [None, *sorted(dimensions.sources, key=source_label)],
        format_func=lambda name: source_label(name) if name else "Все каналы",
        key="source",
    )
    return manager, source


def main() -> None:
    st.set_page_config(page_title="Аналитика продаж · EduFlow AI", layout="wide")
    st.html(Path(__file__).with_name("style.css"))
    st.title("Аналитика продаж")

    with st.sidebar:
        st.title("EduFlow AI")
        st.header("Фильтры")
        try:
            query = _period_query()
        except ValueError as exc:
            st.warning(str(exc))
            st.stop()

        if st.button("Обновить данные", icon=":material/refresh:", width="stretch"):
            get_report.clear()
            get_dimensions.clear()

        base_url = get_api_url()
        try:
            with st.spinner("Получаем справочники…"):
                dimensions = get_dimensions(base_url)
        except DashboardAPIError as exc:
            st.error(str(exc))
            st.stop()
        manager, source = _dimension_filters(dimensions)

    filters = query.model_dump() | {"manager_id": manager, "source": source}
    try:
        with st.spinner("Получаем отчёт…"):
            current, comparison = _split_report(
                get_report(base_url, SalesMetricsQuery.model_validate(filters))
            )
    except DashboardAPIError as exc:
        st.error(str(exc))
        st.stop()

    caption = (
        f"{period_label(current.period)} · Срез {current.period.cutoff_date:%d.%m.%Y}"
    )
    if comparison is not None:
        caption += (
            f"  |  Сравнение {period_label(comparison.period)}"
            f" · Срез {comparison.period.cutoff_date:%d.%m.%Y}"
        )
    st.caption(caption)
    headline_metrics(current, comparison)
    if not current.by_manager and not (comparison and comparison.by_manager):
        st.info("За выбранные периоды и фильтры данных нет.")
        return

    trends, funnel = st.columns([1.55, 1], gap="medium")
    with trends:
        trend_chart(current, comparison)
    with funnel:
        funnel_chart(current, comparison)
    breakdowns(current, comparison)


main()

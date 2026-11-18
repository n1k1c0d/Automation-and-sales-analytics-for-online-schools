from datetime import date
from decimal import Decimal

import altair as alt
import streamlit as st

from app.schemas.sales_report import ReportPeriod, SalesMetrics, SalesReport

COLORS = ["#3155cc", "#9b4d8c"]
PERIOD_NAMES = ["Текущий", "Сравнение"]
SOURCE_NAMES = {
    "organic_search": "Органический поиск",
    "paid_search": "Платный поиск",
    "social": "Соцсети",
    "referral": "Рекомендации",
    "webinar": "Вебинары",
}
METRICS = {
    "leads": ("Входящие лиды", ""),
    "scheduled_lessons": ("Назначенные уроки", ""),
    "completed_lessons": ("Проведённые уроки", ""),
    "bank_application_leads": ("Подали заявку, лидов", ""),
    "paying_leads": ("Оплатили, лидов", ""),
    "paid_amount_rub": ("Сумма оплат", "₽"),
    "average_paid_check_rub": ("Средний чек", "₽"),
    "cohort_leads": ("Лиды с первым уроком", ""),
    "cohort_bank_application_leads": ("Лиды когорты с заявкой", ""),
    "cohort_paying_leads": ("Лиды когорты с оплатой", ""),
    "lesson_to_bank_application_pct": ("Урок → заявка", "%"),
    "lesson_to_payment_pct": ("Урок → оплата", "%"),
}
CHART_METRICS = {
    "Лиды": "leads",
    "Уроки": "completed_lessons",
    "Оплаты": "paying_leads",
    "Сумма": "paid_amount_rub",
}


def source_label(source: str) -> str:
    return SOURCE_NAMES.get(source, source)


def _date_label(day: date) -> str:
    return day.strftime("%d.%m.%Y")


def period_label(period: ReportPeriod) -> str:
    return f"{_date_label(period.start_date)} – {_date_label(period.end_date)}"


def _format_value(
    value: int | Decimal | None, unit: str, *, signed: bool = False
) -> str:
    if value is None:
        return "—"
    places = 2 if unit in {"%", "п. п."} or value % 1 else 0
    sign = "+" if signed and value else ""
    formatted = (
        format(value, f"{sign},.{places}f").replace(",", "\u202f").replace(".", ",")
    )
    return f"{formatted} {unit}" if unit else formatted


def _card(
    field: str, current: SalesMetrics, comparison: SalesMetrics | None, *, border: bool
) -> None:
    label, unit = METRICS[field]
    value = getattr(current, field)
    before = getattr(comparison, field) if comparison is not None else None
    delta = None
    if value is not None and before is not None:
        delta = _format_value(
            value - before, "п. п." if unit == "%" else unit, signed=True
        )
    st.metric(label, _format_value(value, unit), delta=delta, border=border)


def headline_metrics(current: SalesReport, comparison: SalesReport | None) -> None:
    previous = comparison.total if comparison is not None else None
    fields = ("leads", "completed_lessons", "paying_leads", "paid_amount_rub")
    with st.container(key="headline_cards"):
        for column, field in zip(st.columns(len(fields)), fields, strict=True):
            with column:
                _card(field, current.total, previous, border=True)


def _style_chart(chart):
    return (
        chart.configure_view(strokeWidth=0)
        .configure_axis(labelColor="#47577a", titleColor="#334460", gridColor="#e8edf6")
        .configure_legend(labelColor="#334460", orient="top")
    )


def trend_chart(current: SalesReport, comparison: SalesReport | None) -> None:
    with st.container(key="trend_panel", border=True):
        st.subheader("Динамика")
        choice = st.segmented_control(
            "Показатель",
            list(CHART_METRICS),
            default="Лиды",
            label_visibility="collapsed",
        )
        field = CHART_METRICS[choice or "Лиды"]
        label, unit = METRICS[field]

        data = []
        for name, report in zip(PERIOD_NAMES, (current, comparison), strict=True):
            if report is None:
                continue
            for index, row in enumerate(report.daily, start=1):
                value = getattr(row, field)
                data.append(
                    {
                        "period": name,
                        "calendar": row.day.isoformat(),
                        "day_index": index,
                        "date_label": _date_label(row.day),
                        "value": float(value) if unit else value,
                        "shown": _format_value(value, unit),
                    }
                )
        if comparison is None:
            horizontal = alt.X(
                "calendar:T",
                title=None,
                axis=alt.Axis(format="%d.%m", tickCount=6),
                scale=alt.Scale(type="utc"),
            )
        else:
            horizontal = alt.X(
                "day_index:Q", title="День периода", axis=alt.Axis(tickMinStep=1)
            )

        chart = (
            alt.Chart(alt.Data(values=data))
            .mark_line(strokeWidth=2.5, point=alt.OverlayMarkDef(size=18))
            .encode(
                x=horizontal,
                y=alt.Y(
                    "value:Q",
                    title=f"{label}, {unit}" if unit else label,
                    axis=alt.Axis(format="~s", tickMinStep=1),
                ),
                color=alt.Color(
                    "period:N",
                    title=None,
                    scale=alt.Scale(domain=PERIOD_NAMES, range=COLORS),
                    legend=alt.Legend(values=PERIOD_NAMES) if comparison else None,
                ),
                tooltip=[
                    alt.Tooltip("period:N", title="Период"),
                    alt.Tooltip("date_label:N", title="Дата"),
                    alt.Tooltip("shown:N", title=label),
                ],
            )
            .properties(height=265)
        )
        st.altair_chart(_style_chart(chart), width="stretch", theme=None)


def funnel_chart(current: SalesReport, comparison: SalesReport | None) -> None:
    with st.container(key="funnel_panel", border=True):
        st.subheader("Когорта после урока")
        fields = {
            "cohort_leads": "Первый урок",
            "cohort_bank_application_leads": "Заявка в банк",
            "cohort_paying_leads": "Оплата",
        }
        data = [
            {"period": name, "stage": label, "count": getattr(report.total, field)}
            for name, report in zip(PERIOD_NAMES, (current, comparison), strict=True)
            if report is not None
            for field, label in fields.items()
        ]
        chart = alt.Chart(alt.Data(values=data)).encode(
            x=alt.X(
                "count:Q",
                title=None,
                scale=alt.Scale(
                    domain=[0, max(1, max(row["count"] for row in data)) * 1.19]
                ),
                axis=alt.Axis(tickMinStep=1, tickCount=4),
            ),
            y=alt.Y("stage:N", title=None, sort=list(fields.values())),
            color=alt.Color(
                "period:N",
                scale=alt.Scale(domain=PERIOD_NAMES, range=COLORS),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("period:N", title="Период"),
                alt.Tooltip("stage:N", title="Этап"),
                alt.Tooltip("count:Q", title="Лиды", format="d"),
            ],
        )
        if comparison is not None:
            chart = chart.encode(yOffset=alt.YOffset("period:N", sort=PERIOD_NAMES))
        bars = chart.mark_bar(cornerRadiusEnd=3)
        counts = chart.mark_text(align="left", dx=5, color="#233252").encode(
            text="count:Q"
        )
        st.altair_chart(
            _style_chart((bars + counts).properties(height=148)),
            width="stretch",
            theme=None,
        )
        previous = comparison.total if comparison else None
        for column, field in zip(
            st.columns(2),
            ("lesson_to_bank_application_pct", "lesson_to_payment_pct"),
            strict=True,
        ):
            with column:
                _card(field, current.total, previous, border=False)
        st.caption("Оба исхода считаются от первого урока и учитываются независимо.")


def breakdowns(current: SalesReport, comparison: SalesReport | None) -> None:
    st.subheader("Срезы")
    report = current
    if comparison is not None:
        selected = st.segmented_control(
            "Период срезов", PERIOD_NAMES, default=PERIOD_NAMES[0]
        )
        if selected == PERIOD_NAMES[1]:
            report = comparison
    managers, sources = st.tabs(["Менеджеры", "Каналы"])
    with managers:
        _table("Менеджер", [(row.manager_name, row) for row in report.by_manager])
    with sources:
        _table("Канал", [(source_label(row.source), row) for row in report.by_source])


def _table(dimension: str, rows: list[tuple[str, SalesMetrics]]) -> None:
    if not rows:
        st.caption("В этом срезе нет данных.")
        return
    columns = {}
    for field, (label, unit) in METRICS.items():
        if unit:
            columns[field] = st.column_config.NumberColumn(
                label, format=f"%.2f {unit.replace('%', '%%')}"
            )
        else:
            columns[field] = st.column_config.NumberColumn(label, format="%d")
    table = [
        {
            dimension: label,
            **{
                key: float(value) if isinstance(value, Decimal) else value
                for key, value in metric.model_dump(include=set(METRICS)).items()
            },
        }
        for label, metric in rows
    ]
    st.dataframe(table, column_config=columns, hide_index=True, width="stretch")

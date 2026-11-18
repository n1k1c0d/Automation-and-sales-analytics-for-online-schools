from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Self
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import Connection, Engine, text

BUSINESS_ZONE = ZoneInfo("Europe/Moscow")
REPORT_SQL = text(
    (Path(__file__).resolve().parents[2] / "sql/queries/sales_report.sql").read_text(
        encoding="utf-8"
    )
)


class ReportPeriod(BaseModel):
    model_config = ConfigDict(frozen=True)

    start_date: date
    end_date: date
    cutoff_date: date

    @model_validator(mode="after")
    def check_period(self) -> Self:
        if self.start_date > self.end_date:
            raise ValueError("start_date must be on or before end_date.")
        return self


class SalesMetrics(BaseModel):
    leads: int
    scheduled_lessons: int
    completed_lessons: int
    bank_application_leads: int
    paying_leads: int
    paid_amount_rub: Decimal
    average_paid_check_rub: Decimal | None
    cohort_leads: int
    cohort_bank_application_leads: int
    cohort_paying_leads: int
    lesson_to_bank_application_pct: Decimal | None
    lesson_to_payment_pct: Decimal | None


class DailySalesMetrics(SalesMetrics):
    day: date


class ManagerSalesMetrics(SalesMetrics):
    manager_id: UUID
    manager_name: str


class SourceSalesMetrics(SalesMetrics):
    source: str


class SalesReport(BaseModel):
    period: ReportPeriod
    manager_id: UUID | None
    source: str | None
    total: SalesMetrics
    daily: list[DailySalesMetrics]
    by_manager: list[ManagerSalesMetrics]
    by_source: list[SourceSalesMetrics]


class SalesReportComparison(BaseModel):
    current: SalesReport
    comparison: SalesReport


def _business_midnight(day: date, *, next_day: bool = False) -> datetime:
    try:
        if next_day:
            day += timedelta(days=1)
        return datetime.combine(day, time.min, tzinfo=BUSINESS_ZONE).astimezone(UTC)
    except OverflowError as exc:
        raise ValueError(
            "Report dates must allow conversion to UTC day boundaries."
        ) from exc


def load_sales_report(
    connection: Connection,
    period: ReportPeriod,
    *,
    manager_id: UUID | None = None,
    source: str | None = None,
) -> SalesReport:
    if source is not None:
        source = source.strip()
        if not source:
            raise ValueError("source must not be blank.")
    rows = connection.execute(
        REPORT_SQL,
        {
            "start_date": period.start_date,
            "end_date": period.end_date,
            "period_start_utc": _business_midnight(period.start_date),
            "period_end_utc": _business_midnight(period.end_date, next_day=True),
            "cutoff_end_utc": _business_midnight(period.cutoff_date, next_day=True),
            "business_zone": BUSINESS_ZONE.key,
            "manager_id": manager_id,
            "source": source,
        },
    ).mappings()
    by_grain = {"total": [], "day": [], "manager": [], "source": []}
    for row in rows:
        by_grain[row["grain"]].append(row)
    (total,) = by_grain["total"]
    return SalesReport(
        period=period,
        manager_id=manager_id,
        source=source,
        total=SalesMetrics.model_validate(total),
        daily=[DailySalesMetrics.model_validate(row) for row in by_grain["day"]],
        by_manager=[
            ManagerSalesMetrics.model_validate(row) for row in by_grain["manager"]
        ],
        by_source=[
            SourceSalesMetrics.model_validate(row) for row in by_grain["source"]
        ],
    )


def read_sales_report(
    engine: Engine,
    period: ReportPeriod,
    *,
    comparison_period: ReportPeriod | None = None,
    manager_id: UUID | None = None,
    source: str | None = None,
) -> SalesReport | SalesReportComparison:
    with (
        engine.connect().execution_options(
            isolation_level="REPEATABLE READ"
        ) as connection,
        connection.begin(),
    ):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
        current = load_sales_report(
            connection, period, manager_id=manager_id, source=source
        )
        if comparison_period is None:
            return current
        comparison = load_sales_report(
            connection, comparison_period, manager_id=manager_id, source=source
        )
        return SalesReportComparison(current=current, comparison=comparison)

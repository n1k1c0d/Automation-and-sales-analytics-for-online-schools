from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Connection, Engine, select, text

from app.database.models import Lead, Manager
from app.schemas.sales_metrics import ManagerDimension, SalesDimensions
from app.schemas.sales_report import (
    DailySalesMetrics,
    ManagerSalesMetrics,
    ReportPeriod,
    SalesMetrics,
    SalesReport,
    SalesReportComparison,
    SourceSalesMetrics,
)

BUSINESS_ZONE = ZoneInfo("Europe/Moscow")
REPORT_SQL = text(
    (Path(__file__).resolve().parents[2] / "sql/queries/sales_report.sql").read_text(
        encoding="utf-8"
    )
)


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


def read_sales_dimensions(engine: Engine) -> SalesDimensions:
    with engine.connect() as connection:
        managers = connection.execute(
            select(Manager.id, Manager.name).order_by(Manager.name, Manager.id)
        )
        manager_options = [
            ManagerDimension(manager_id=manager_id, manager_name=name)
            for manager_id, name in managers
        ]
        sources = connection.scalars(
            select(Lead.source).distinct().order_by(Lead.source)
        ).all()
    return SalesDimensions(managers=manager_options, sources=list(sources))

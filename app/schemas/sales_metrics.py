from datetime import date
from typing import Annotated, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from app.schemas.sales_report import ReportPeriod

MAX_REPORT_DAYS = 366


def _calendar_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Use a calendar date in YYYY-MM-DD format.") from exc
    if parsed.isoformat() != value:
        raise ValueError("Use a calendar date in YYYY-MM-DD format.")
    return parsed


type ReportDate = Annotated[
    date, BeforeValidator(_calendar_date), Field(gt=date.min, lt=date.max)
]
type LeadSource = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[^\x00]+$")
]


class SalesMetricsQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_date: ReportDate
    end_date: ReportDate
    cutoff_date: ReportDate | None = None
    manager_id: UUID | None = None
    source: LeadSource | None = None
    compare_start_date: ReportDate | None = None
    compare_end_date: ReportDate | None = None
    compare_cutoff_date: ReportDate | None = None

    @model_validator(mode="after")
    def check_periods(self) -> Self:
        if (self.compare_start_date is None) != (self.compare_end_date is None):
            raise ValueError("Provide both comparison dates.")
        if self.compare_cutoff_date is not None and self.compare_end_date is None:
            raise ValueError("Comparison cutoff requires a comparison period.")
        for period in self.periods():
            if (
                period is not None
                and (period.end_date - period.start_date).days >= MAX_REPORT_DAYS
            ):
                raise ValueError(
                    f"Each period can contain at most {MAX_REPORT_DAYS} days."
                )
        return self

    def periods(self) -> tuple[ReportPeriod, ReportPeriod | None]:
        current = ReportPeriod(
            start_date=self.start_date,
            end_date=self.end_date,
            cutoff_date=self.cutoff_date or self.end_date,
        )
        comparison = None
        if self.compare_start_date is not None and self.compare_end_date is not None:
            comparison = ReportPeriod(
                start_date=self.compare_start_date,
                end_date=self.compare_end_date,
                cutoff_date=self.compare_cutoff_date or self.compare_end_date,
            )
        return current, comparison


class ManagerDimension(BaseModel):
    manager_id: UUID
    manager_name: str


class SalesDimensions(BaseModel):
    managers: list[ManagerDimension]
    sources: list[str]

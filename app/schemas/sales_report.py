from datetime import date
from decimal import Decimal
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator


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

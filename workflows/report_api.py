"""Read-only deployment of the metrics handler for the local n8n execution log."""

from decimal import Decimal
from hashlib import sha256
from http import HTTPStatus
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_serializer
from sqlalchemy.exc import SQLAlchemyError

from app.api.database import database_lifespan
from app.api.routes.sales_metrics import sales_metrics
from app.main import database_error
from app.schemas.sales_report import ReportPeriod


class ReportKpis(BaseModel):
    paid_amount_rub: Decimal
    paying_leads: int
    average_paid_check_rub: Decimal | None
    lesson_to_payment_pct: Decimal | None


class SourceKpis(ReportKpis):
    source: str

    @field_serializer("source")
    def report_label(self, value: str) -> str:
        # Imported source labels are free text. Only the published demo labels
        # may appear verbatim in n8n's persisted HTTP response.
        if value in {"organic_search", "paid_search", "social", "referral", "webinar"}:
            return value
        return "source-" + sha256(value.encode()).hexdigest()[:16]


class ManagerKpis(ReportKpis):
    manager_id: UUID


class Report(BaseModel):
    period: ReportPeriod
    total: ReportKpis
    by_source: list[SourceKpis]
    by_manager: list[ManagerKpis]


class Comparison(BaseModel):
    current: Report
    comparison: Report


app = FastAPI(title="EduFlow AI · Report API", lifespan=database_lifespan)
app.add_exception_handler(SQLAlchemyError, database_error)
app.add_api_route(
    "/api/v1/metrics",
    sales_metrics,
    methods=["GET"],
    response_model=Report | Comparison,
)


@app.exception_handler(RequestValidationError)
async def invalid_query(
    _request: Request, _error: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        content={"detail": "Invalid report parameters."},
    )

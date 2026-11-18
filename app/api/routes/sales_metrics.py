from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Query, Request

from app.analytics.sales import read_sales_dimensions, read_sales_report
from app.api.database import get_db_engine
from app.schemas.sales_metrics import SalesDimensions, SalesMetricsQuery
from app.schemas.sales_report import SalesReport, SalesReportComparison

router = APIRouter(prefix="/api/v1", tags=["sales"])


@router.get(
    "/metrics",
    summary="Get sales metrics for one or two periods",
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"description": "Database unavailable"}},
)
def sales_metrics(
    request: Request, query: Annotated[SalesMetricsQuery, Query()]
) -> SalesReport | SalesReportComparison:
    current, comparison = query.periods()
    return read_sales_report(
        get_db_engine(request),
        current,
        comparison_period=comparison,
        manager_id=query.manager_id,
        source=query.source,
    )


@router.get(
    "/dimensions",
    summary="List managers and lead sources for report filters",
    responses={HTTPStatus.SERVICE_UNAVAILABLE: {"description": "Database unavailable"}},
)
def sales_dimensions(request: Request) -> SalesDimensions:
    return read_sales_dimensions(get_db_engine(request))

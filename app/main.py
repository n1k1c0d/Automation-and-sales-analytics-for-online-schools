import logging
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.body_limit import RequestBodyLimitMiddleware

from app.api.database import DATABASE_UNAVAILABLE, database_lifespan
from app.api.routes.health import router as health_router
from app.api.routes.sales_import import MAX_REQUEST_BYTES
from app.api.routes.sales_import import router as sales_import_router
from app.api.routes.sales_metrics import router as sales_metrics_router

logger = logging.getLogger(__name__)

app = FastAPI(title="EduFlow AI", lifespan=database_lifespan)
app.add_middleware(RequestBodyLimitMiddleware, max_body_size=MAX_REQUEST_BYTES)
app.include_router(health_router)
app.include_router(sales_import_router)
app.include_router(sales_metrics_router)


@app.exception_handler(SQLAlchemyError)
async def database_error(_request: Request, error: SQLAlchemyError) -> JSONResponse:
    logger.error("Database request failed (%s).", type(error).__name__)
    return JSONResponse(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        content={"detail": DATABASE_UNAVAILABLE},
    )

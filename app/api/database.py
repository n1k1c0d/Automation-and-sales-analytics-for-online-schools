from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from http import HTTPStatus

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import Engine

from app.core.config import PostgresSettings
from app.database.connection import create_db_engine

DATABASE_UNAVAILABLE = "PostgreSQL is unavailable. Check the database and migrations."


@asynccontextmanager
async def database_lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        settings = PostgresSettings()
    except ValidationError:
        engine = None
    else:
        engine = create_db_engine(settings)
    app.state.db_engine = engine
    try:
        yield
    finally:
        if engine is not None:
            engine.dispose()


def get_db_engine(request: Request) -> Engine:
    if (engine := getattr(request.app.state, "db_engine", None)) is None:
        raise HTTPException(HTTPStatus.SERVICE_UNAVAILABLE, DATABASE_UNAVAILABLE)
    return engine

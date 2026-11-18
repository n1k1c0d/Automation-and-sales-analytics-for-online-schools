import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack
from uuid import uuid4

import pytest
from sqlalchemy import Engine

from app.core.config import PostgresSettings
from app.database.connection import create_db_engine


def _migrated_database(request: pytest.FixtureRequest) -> Iterator[Engine]:
    if not request.config.getoption("--integration"):
        pytest.skip("Pass --integration to run PostgreSQL tests.")

    settings = PostgresSettings()
    test_db = f"eduflow_test_{uuid4().hex}"
    admin_engine = create_db_engine(settings.model_copy(update={"db": "postgres"}))

    with ExitStack() as cleanup:
        cleanup.callback(admin_engine.dispose)
        admin = cleanup.enter_context(
            admin_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        )
        admin.exec_driver_sql(f'CREATE DATABASE "{test_db}"')
        cleanup.callback(admin.exec_driver_sql, f'DROP DATABASE "{test_db}"')

        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env={**os.environ, "POSTGRES_DB": test_db},
            check=True,
        )
        engine = create_db_engine(settings.model_copy(update={"db": test_db}))
        cleanup.callback(engine.dispose)
        yield engine


@pytest.fixture(scope="session")
def postgres_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    yield from _migrated_database(request)


@pytest.fixture
def isolated_postgres_engine(request: pytest.FixtureRequest) -> Iterator[Engine]:
    yield from _migrated_database(request)

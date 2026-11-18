from sqlalchemy import URL, Engine, create_engine

from app.core.config import PostgresSettings


def create_db_engine(settings: PostgresSettings) -> Engine:
    url = URL.create(
        "postgresql+psycopg",
        username=settings.user,
        password=settings.password.get_secret_value(),
        host=settings.host,
        port=settings.port,
        database=settings.db,
    )
    return create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 5})

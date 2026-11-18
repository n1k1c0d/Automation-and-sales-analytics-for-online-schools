import logging

from pydantic import ValidationError
from sqlalchemy.exc import OperationalError

from app.core.config import PostgresSettings
from app.database.connection import create_db_engine

logger = logging.getLogger(__name__)


def main() -> int:
    try:
        settings = PostgresSettings()
    except ValidationError as exc:
        fields = ", ".join(
            f"POSTGRES_{error['loc'][0]}".upper()
            for error in exc.errors(include_input=False, include_context=False)
        )
        logger.error("Check PostgreSQL settings in .env: %s.", fields)
        return 1

    engine = create_db_engine(settings)
    try:
        with engine.connect() as connection:
            version = connection.exec_driver_sql("SHOW server_version").scalar_one()
    except OperationalError:
        logger.error(
            "Could not connect to PostgreSQL. Check that the postgres service is "
            "healthy and the host, port and credentials in .env match the database."
        )
        return 1
    finally:
        engine.dispose()

    logger.info("PostgreSQL %s is reachable.", version)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    raise SystemExit(main())

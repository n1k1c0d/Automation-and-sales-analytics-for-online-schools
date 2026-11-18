import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import PostgresSettings
from app.database.connection import create_db_engine
from app.services.sales_import import import_sales_data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import synthetic EduFlow sales records."
    )
    parser.add_argument(
        "path", type=Path, help="CSV directory, named CSV, or .xlsx workbook."
    )
    parser.add_argument(
        "--source-system", required=True, help="Permanent code of the source."
    )
    parser.add_argument(
        "--excel-timezone", help="IANA time zone for native Excel date cells."
    )
    args = parser.parse_args()

    try:
        settings = PostgresSettings()
    except ValidationError:
        print("Check PostgreSQL settings in .env.", file=sys.stderr)
        return 2

    engine = create_db_engine(settings)
    try:
        report = import_sales_data(
            engine,
            args.path,
            source_system=args.source_system,
            excel_timezone=args.excel_timezone,
        )
    except (OSError, ValueError) as error:
        print(f"Import failed: {error}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Import failed. Check the PostgreSQL connection and apply Alembic migrations.",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    print(json.dumps(asdict(report), indent=2))
    return 1 if report.issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

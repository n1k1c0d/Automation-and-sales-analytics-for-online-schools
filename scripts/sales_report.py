import argparse
import json
import sys
from datetime import date
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.analytics.sales import ReportPeriod, read_sales_report
from app.core.config import PostgresSettings
from app.database.connection import create_db_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Read sales KPIs from PostgreSQL.")
    parser.add_argument("--start-date", required=True, type=date.fromisoformat)
    parser.add_argument("--end-date", required=True, type=date.fromisoformat)
    parser.add_argument("--cutoff-date", type=date.fromisoformat)
    parser.add_argument("--manager-id", type=UUID)
    parser.add_argument("--source")
    parser.add_argument(
        "--compare", nargs=2, type=date.fromisoformat, metavar=("START", "END")
    )
    parser.add_argument("--compare-cutoff-date", type=date.fromisoformat)
    args = parser.parse_args()
    if args.compare_cutoff_date is not None and args.compare is None:
        parser.error("--compare-cutoff-date requires --compare.")

    try:
        period = ReportPeriod(
            start_date=args.start_date,
            end_date=args.end_date,
            cutoff_date=args.cutoff_date or args.end_date,
        )
        comparison_period = None
        if args.compare:
            start, end = args.compare
            comparison_period = ReportPeriod(
                start_date=start,
                end_date=end,
                cutoff_date=args.compare_cutoff_date or end,
            )
    except ValidationError:
        parser.error("Each start date must be on or before its end date.")

    try:
        settings = PostgresSettings()
    except ValidationError:
        print("Check PostgreSQL settings in .env.", file=sys.stderr)
        return 2

    engine = create_db_engine(settings)
    try:
        output = read_sales_report(
            engine,
            period,
            comparison_period=comparison_period,
            manager_id=args.manager_id,
            source=args.source,
        ).model_dump(mode="json")
    except ValueError as exc:
        print(f"Report failed: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print(
            "Report failed. Check PostgreSQL and the applied migrations.",
            file=sys.stderr,
        )
        return 2
    finally:
        engine.dispose()

    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

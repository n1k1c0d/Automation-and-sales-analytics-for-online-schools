"""Read-only checks for the local EduFlow demonstration."""

import argparse
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx2
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.exc import SQLAlchemyError

from app.analytics.sales import BUSINESS_ZONE, read_sales_report
from app.core.config import PostgresSettings
from app.database.connection import create_db_engine
from app.schemas.sales_metrics import SalesDimensions
from app.schemas.sales_report import ReportPeriod, SalesReport
from scripts import check_db, check_llm

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/daily_sales_report.json"
PERIOD = ReportPeriod(
    start_date=date(2026, 6, 1),
    end_date=date(2026, 8, 31),
    cutoff_date=date(2026, 10, 31),
)


class N8nSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    n8n_local_port: int = 5678


class SmokeFailure(Exception):
    pass


@contextmanager
def check(name: str, failures: list[str]) -> Iterator[None]:
    try:
        yield
    except SmokeFailure as exc:
        reason = str(exc)
    except httpx2.HTTPStatusError as exc:
        reason = f"HTTP {exc.response.status_code}"
    except httpx2.TimeoutException:
        reason = "HTTP request timed out"
    except (httpx2.RequestError, httpx2.InvalidURL):
        reason = "connection failed or invalid URL"
    except (ValueError, OSError, SQLAlchemyError):
        reason = "invalid configuration, response or unavailable database"
    else:
        print(f"PASS {name}")
        return
    failures.append(name)
    print(f"FAIL {name}: {reason}")


def get(client: httpx2.Client, url: str, **kwargs) -> httpx2.Response:
    response = client.get(url, **kwargs)
    response.raise_for_status()
    return response


def validate_workflow(path: Path) -> None:
    workflow = json.loads(path.read_bytes())
    settings = workflow.get("settings") if isinstance(workflow, dict) else None
    if not isinstance(settings, dict) or settings.get("timezone") != BUSINESS_ZONE.key:
        raise SmokeFailure(f"workflow timezone must be {BUSINESS_ZONE.key}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--api-url", default=os.getenv("EDUFLOW_API_URL", "http://127.0.0.1:8000")
    )
    parser.add_argument("--streamlit-url", default="http://127.0.0.1:8501")
    parser.add_argument(
        "--n8n-url", help="Defaults to loopback and the N8N_LOCAL_PORT in .env."
    )
    parser.add_argument("--skip-streamlit", action="store_true")
    parser.add_argument("--skip-n8n", action="store_true")
    parser.add_argument(
        "--with-groq",
        action="store_true",
        help="Run a real Groq connection check (uses quota).",
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=WORKFLOW,
        help="n8n export to check before publication.",
    )
    parser.add_argument(
        "--expect-leads",
        type=int,
        help="Expected lead count for the fixed demo period.",
    )
    args = parser.parse_args(argv)
    failures = []
    api = args.api_url.rstrip("/")
    with check("PostgreSQL", failures):
        if check_db.main():
            raise SmokeFailure("check the database and its connection settings")
    with check("Workflow export timezone", failures):
        validate_workflow(args.workflow)
    # Allow FastAPI to report a PostgreSQL connection timeout (five seconds).
    with httpx2.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
        with check("FastAPI /health", failures):
            if get(client, f"{api}/health").json() != {"status": "ok"}:
                raise SmokeFailure("expected status ok")
        with check("FastAPI dimensions", failures):
            SalesDimensions.model_validate_json(
                get(client, f"{api}/api/v1/dimensions").content, strict=True
            )
        with check("FastAPI sales report and SQL totals", failures):
            response = get(
                client, f"{api}/api/v1/metrics", params=PERIOD.model_dump(mode="json")
            )
            report = SalesReport.model_validate_json(response.content, strict=True)
            if (
                report.period != PERIOD
                or report.source is not None
                or report.manager_id is not None
            ):
                raise SmokeFailure("unexpected report period or filters")
            engine = create_db_engine(PostgresSettings())
            try:
                reference = read_sales_report(engine, PERIOD)
            finally:
                engine.dispose()
            if report.total != reference.total:
                raise SmokeFailure("API totals differ from SQL")
            if (
                args.expect_leads is not None
                and report.total.leads != args.expect_leads
            ):
                raise SmokeFailure(
                    f"expected {args.expect_leads} leads, got {report.total.leads}"
                )
        if args.skip_streamlit:
            print("SKIP Streamlit")
        else:
            with check("Streamlit readiness", failures):
                if (
                    get(
                        client, args.streamlit_url.rstrip("/") + "/_stcore/health"
                    ).text.strip()
                    != "ok"
                ):
                    raise SmokeFailure("unexpected readiness response")
        if args.skip_n8n:
            print("SKIP n8n")
        else:
            with check("n8n readiness", failures):
                base = (
                    args.n8n_url or f"http://127.0.0.1:{N8nSettings().n8n_local_port}"
                )
                if get(client, base.rstrip("/") + "/healthz/readiness").json() != {
                    "status": "ok"
                }:
                    raise SmokeFailure("expected status ok")
    if args.with_groq:
        with check("Groq structured response", failures):
            if check_llm.main():
                raise SmokeFailure("Groq connection check failed")
    else:
        print("SKIP Groq (opt in with --with-groq)")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())

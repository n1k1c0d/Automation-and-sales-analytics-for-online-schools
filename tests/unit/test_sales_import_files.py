from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.schemas.sales_import import ManagerRow
from app.services.sales_import_files import read_import_files


@pytest.mark.parametrize(
    ("header", "message"),
    [
        ("external_id,name,created_at", "missing columns: team"),
        ("external_id,name,name,team,created_at", "header names must be unique"),
        ("external_id,name,team,created_at,phone", "unexpected columns: phone"),
    ],
)
def test_rejects_invalid_csv_headers(tmp_path: Path, header: str, message: str) -> None:
    path = tmp_path / "managers.csv"
    path.write_text(header + "\n", encoding="utf-8-sig")
    with pytest.raises(ValueError, match=message):
        read_import_files(path)


def test_ambiguous_excel_time_requires_an_offset() -> None:
    row = {
        "external_id": "M01",
        "name": "Synthetic Manager",
        "team": "Demo",
        "created_at": datetime(2026, 10, 25, 2, 30),
    }
    with pytest.raises(ValidationError, match="Ambiguous or nonexistent local time"):
        ManagerRow.model_validate(
            row, context={"excel_timezone": ZoneInfo("Europe/Berlin")}
        )

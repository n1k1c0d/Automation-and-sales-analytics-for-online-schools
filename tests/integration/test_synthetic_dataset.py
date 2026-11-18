import csv
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, func, select

from app.database.models import Base
from scripts.generate_synthetic_data import generate_dataset, write_csv_dataset


def _read_column(value: str, python_type: type) -> object:
    if value == "":
        return None
    if python_type is datetime:
        return datetime.fromisoformat(value)
    return python_type(value)


def test_generated_csvs_fit_the_migrated_schema(
    postgres_engine: Engine, tmp_path: Path
) -> None:
    dataset = generate_dataset()
    export = tmp_path / "sales"
    write_csv_dataset(dataset, export)

    with postgres_engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            count = select(func.count()).select_from(table)
            before = connection.execute(count).scalar_one()
            with (export / f"{table.name}.csv").open(
                encoding="utf-8", newline=""
            ) as file:
                rows = [
                    {
                        column.name: _read_column(
                            record[column.name], column.type.python_type
                        )
                        for column in table.columns
                    }
                    for record in csv.DictReader(file)
                ]
            assert rows
            connection.execute(table.insert(), rows)
            assert connection.execute(count).scalar_one() - before == len(
                dataset[table.name]
            )

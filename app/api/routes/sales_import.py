from http import HTTPStatus
from pathlib import Path
from shutil import copyfileobj
from tempfile import TemporaryDirectory
from typing import Annotated
from zipfile import BadZipFile, ZipFile

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.database import get_db_engine
from app.schemas.sales_import import ROW_MODELS, SourceSystem
from app.services.sales_import import ImportReport, import_sales_data

MAX_REQUEST_BYTES = 5 * 1024 * 1024
MAX_WORKBOOK_BYTES = 50 * 1024 * 1024
CSV_FILENAMES = frozenset(f"{name}.csv" for name in ROW_MODELS)

router = APIRouter(prefix="/api/v1", tags=["sales"])


def _validate_workbook(path: Path) -> None:
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if any(entry.flag_bits & 1 for entry in entries):
                raise ValueError("Encrypted workbooks are unsupported.")
            if sum(entry.file_size for entry in entries) > MAX_WORKBOOK_BYTES:
                raise HTTPException(
                    HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    "Uncompressed workbook content exceeds 50 MiB.",
                )
    except BadZipFile as exc:
        raise ValueError("Could not read the Excel workbook.") from exc


@router.post(
    "/import",
    summary="Import named sales CSV files or an Excel workbook",
    responses={
        HTTPStatus.BAD_REQUEST: {"description": "Malformed multipart request"},
        HTTPStatus.REQUEST_ENTITY_TOO_LARGE: {
            "description": "Upload exceeds size limit"
        },
        HTTPStatus.SERVICE_UNAVAILABLE: {"description": "Database unavailable"},
    },
)
def upload_sales_data(
    request: Request,
    files: Annotated[list[UploadFile], File(description="Named CSVs or one .xlsx")],
    source_system: Annotated[SourceSystem, Form()],
    excel_timezone: Annotated[str | None, Form()] = None,
) -> ImportReport:
    names = [upload.filename or "" for upload in files]
    workbook = len(names) == 1 and names[0].lower().endswith(".xlsx")
    if workbook and ("/" in names[0] or "\\" in names[0]):
        raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, "Invalid workbook name.")
    if not workbook and (
        not names
        or len(names) != len(set(names))
        or not CSV_FILENAMES.issuperset(names)
    ):
        raise HTTPException(
            HTTPStatus.UNPROCESSABLE_ENTITY,
            "Upload distinct, named sales CSV files or one .xlsx workbook.",
        )

    with TemporaryDirectory(prefix="eduflow-import-") as directory:
        root = Path(directory)
        path = root / "sales.xlsx" if workbook else root
        for name, upload in zip(names, files, strict=True):
            target = path if workbook else root / name
            with target.open("wb") as output:
                copyfileobj(upload.file, output)
        try:
            if workbook:
                _validate_workbook(path)
            return import_sales_data(
                get_db_engine(request),
                path,
                source_system=source_system,
                excel_timezone=excel_timezone,
            )
        except ValueError as exc:
            raise HTTPException(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc)) from exc

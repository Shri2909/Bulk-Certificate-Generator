"""Job archive (§11.3): a ZIP of every generated PDF plus manifest.csv with one line per submitted row.

Built from rows the caller already loaded, so no DB connection is held while files are read.
"""

from __future__ import annotations

import csv
import io
import json
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import IO

from certgen.errors import ProblemCode
from certgen.storage import Storage

SPOOL_MAX_MEMORY = 8 * 1024 * 1024  # larger archives spill to a temp file instead of RAM
STREAM_CHUNK = 64 * 1024
MANIFEST_HEADER = ("row_index", "name", "email", "status", "certificate_id", "error_codes", "error_messages")
# Spreadsheet apps treat these leading characters as a formula (incl. full-width variants).
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r", "\n", "\uff1d", "\uff0b", "\uff0d", "\uff20")
MISSING_FILE_MESSAGE = "The certificate file is missing on the server."


@dataclass(frozen=True)
class ArchiveRow:
    job_id: str
    row_index: int
    name: str | None
    email: str | None
    status: str
    certificate_id: str | None
    errors: str | None  # items.errors JSON


def neutralize(cell: str) -> str:
    """CSV-injection guard: a leading apostrophe makes spreadsheets show the text instead of running it."""
    return "'" + cell if cell.startswith(FORMULA_PREFIXES) else cell


def build_archive(rows: Sequence[ArchiveRow], storage: Storage) -> IO[bytes]:
    """Return a rewound file holding the ZIP; the caller streams and closes it."""
    # Returned open on purpose: stream() closes it after the last chunk.
    spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_MEMORY)  # noqa: SIM115
    manifest = io.StringIO(newline="")
    writer = csv.writer(manifest)
    writer.writerow(MANIFEST_HEADER)
    with zipfile.ZipFile(spool, "w") as zf:
        for row in rows:
            errors = json.loads(row.errors) if row.errors else []
            codes = [e["code"] for e in errors]
            messages = [e["message"] for e in errors]
            if row.status == "succeeded" and row.certificate_id is not None:
                pdf = storage.read(row.job_id, row.certificate_id)
                if pdf is None:
                    codes, messages = [ProblemCode.CERTIFICATE_FILE_MISSING.value], [MISSING_FILE_MESSAGE]
                else:
                    name = f"certificates/{row.row_index:05d}_{row.certificate_id}.pdf"
                    zf.writestr(name, pdf, compress_type=zipfile.ZIP_STORED)  # PDFs are already compressed
            writer.writerow(
                [
                    row.row_index,
                    neutralize(row.name or ""),
                    neutralize(row.email or ""),
                    row.status,
                    row.certificate_id or "",
                    "; ".join(codes),
                    neutralize("; ".join(messages)),
                ]
            )
        zf.writestr("manifest.csv", manifest.getvalue().encode("utf-8"), compress_type=zipfile.ZIP_DEFLATED)
    spool.seek(0)
    return spool


def stream(file: IO[bytes]) -> Iterable[bytes]:
    """Yield the file in chunks and close it afterwards, even if the client disconnects mid-download."""

    def chunks() -> Iterator[bytes]:
        try:
            while chunk := file.read(STREAM_CHUNK):
                yield chunk
        finally:
            file.close()

    return chunks()

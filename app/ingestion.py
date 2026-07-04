import csv
import io
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from app import db

SAMPLE_SIZE = 1000

_SQL_TYPES = {"integer": "INTEGER", "real": "REAL", "date": "TEXT", "text": "TEXT"}
_INT_RE = re.compile(r"^[+-]?\d+$")
_REAL_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_LEADING_DIGITS_RE = re.compile(r"^[+-]?(\d*)")


class CsvError(ValueError):
    """Malformed upload; routers translate this into a 400 response."""


def dataset_slug(filename: str) -> str:
    slug = _sanitize_identifier(Path(filename).stem)
    if not slug:
        raise CsvError(f"cannot derive a dataset name from filename {filename!r}")
    return slug


def _sanitize_identifier(raw: str) -> str:
    slug = re.sub(r"[^a-z0-9_]+", "_", raw.strip().lower())
    return re.sub(r"_+", "_", slug).strip("_")


def sanitize_headers(raw_headers: list[str]) -> list[str]:
    # _row_id is reserved for the synthetic primary key; seeding it here
    # guarantees no CSV column can ever shadow it.
    used = {"_row_id"}
    names: list[str] = []
    for position, raw in enumerate(raw_headers, start=1):
        name = _sanitize_identifier(raw) or f"column_{position}"
        candidate, n = name, 2
        while candidate in used:
            candidate = f"{name}_{n}"
            n += 1
        used.add(candidate)
        names.append(candidate)
    return names


def _is_integer(value: str) -> bool:
    v = value.strip()
    if not _INT_RE.match(v):
        return False
    digits = v.lstrip("+-")
    return len(digits) == 1 or not digits.startswith("0")


def _is_real(value: str) -> bool:
    v = value.strip()
    if not _REAL_RE.match(v):
        return False
    # Leading-zero numerics (ZIP codes, phone extensions) carry meaning in
    # the zeros, so they must not be demoted to a numeric type.
    match = _LEADING_DIGITS_RE.match(v)
    integer_digits = match.group(1) if match else ""
    return len(integer_digits) <= 1 or not integer_digits.startswith("0")


def _is_date(value: str) -> bool:
    try:
        datetime.fromisoformat(value.strip())
    except ValueError:
        return False
    return True


def infer_column_type(values: Iterable[str | None]) -> str:
    present = [v for v in values if v is not None and v != ""]
    if not present:
        return "text"
    for logical, conforms in (("integer", _is_integer), ("real", _is_real), ("date", _is_date)):
        if all(conforms(v) for v in present):
            return logical
    return "text"


def _coerce(value: str | None, logical: str) -> int | float | str | None:
    if value is None or value == "":
        return None
    # Inference only samples the first SAMPLE_SIZE rows, so later rows may
    # not conform; store those verbatim rather than failing the ingest.
    if logical == "integer":
        try:
            return int(value)
        except ValueError:
            return value
    if logical == "real":
        try:
            return float(value)
        except ValueError:
            return value
    return value


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _parse(text: str) -> tuple[list[str], list[list[str]]]:
    try:
        dialect: csv.Dialect | type[csv.Dialect] = csv.Sniffer().sniff(
            text[:65536], delimiters=",;\t|"
        )
    except csv.Error:
        dialect = csv.excel
    rows = [row for row in csv.reader(io.StringIO(text), dialect) if row]
    if not rows:
        raise CsvError("CSV file is empty")
    if len(rows) == 1:
        raise CsvError("CSV contains a header row but no data rows")
    return rows[0], rows[1:]


def _normalize(rows: list[list[str]], width: int) -> list[list[str | None]]:
    normalized: list[list[str | None]] = []
    for number, row in enumerate(rows, start=1):
        if len(row) > width:
            raise CsvError(f"data row {number} has {len(row)} fields, expected {width}")
        normalized.append(list(row) + [None] * (width - len(row)))
    return normalized


def ingest_csv(conn: sqlite3.Connection, name: str, data: bytes) -> db.Dataset:
    header, raw_rows = _parse(_decode(data))
    column_names = sanitize_headers(header)
    rows = _normalize(raw_rows, len(column_names))

    sample = rows[:SAMPLE_SIZE]
    columns = [
        db.Column(column_name, infer_column_type(row[i] for row in sample))
        for i, column_name in enumerate(column_names)
    ]

    # Identifiers below are sanitized to [a-z0-9_] above and double-quoted;
    # all values go through placeholders. The caller owns the commit so the
    # table, its rows, and the registry entry land atomically.
    table_name = f"ds_{name}"
    column_ddl = ", ".join(f'"{c.name}" {_SQL_TYPES[c.type]}' for c in columns)
    conn.execute(
        f'CREATE TABLE "{table_name}" ("_row_id" INTEGER PRIMARY KEY AUTOINCREMENT, {column_ddl})'
    )

    column_list = ", ".join(f'"{c.name}"' for c in columns)
    placeholders = ", ".join("?" for _ in columns)
    conn.executemany(
        f'INSERT INTO "{table_name}" ({column_list}) VALUES ({placeholders})',
        (
            [_coerce(value, column.type) for value, column in zip(row, columns, strict=True)]
            for row in rows
        ),
    )

    dataset = db.Dataset(
        name=name,
        table_name=table_name,
        columns=columns,
        row_count=len(rows),
        created_at=db.utc_now(),
    )
    db.register_dataset(conn, dataset)
    return dataset

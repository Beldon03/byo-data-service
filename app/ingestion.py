import csv
import io
import math
import re
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from app import db

SAMPLE_SIZE = 1000
MAX_COLUMNS = 2000  # SQLite's default SQLITE_MAX_COLUMN

_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1

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
    if len(digits) > 1 and digits.startswith("0"):
        return False
    # Python ints are unbounded but SQLite INTEGER is 64-bit; anything wider
    # must demote (to real via the next inference step) or stay text.
    return _SQLITE_INT_MIN <= int(v) <= _SQLITE_INT_MAX


def _is_real(value: str) -> bool:
    v = value.strip()
    if not _REAL_RE.match(v):
        return False
    # Leading-zero numerics (ZIP codes, phone extensions) carry meaning in
    # the zeros, so they must not be demoted to a numeric type.
    match = _LEADING_DIGITS_RE.match(v)
    integer_digits = match.group(1) if match else ""
    if len(integer_digits) > 1 and integer_digits.startswith("0"):
        return False
    # Values like 1e400 overflow float() to inf, which JSON cannot represent.
    return not math.isinf(float(v))


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
    # Conversion is gated on the same validators inference used, so the two
    # can never disagree (int() alone would also accept '1_0' or overflow
    # SQLite's 64-bit range). Rows beyond the sampled prefix may not conform;
    # those are stored verbatim rather than failing the ingest.
    if logical == "integer" and _is_integer(value):
        return int(value)
    if logical == "real" and _is_real(value):
        return float(value)
    if logical == "date" and _is_date(value):
        return value.strip()
    return value


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _detect_dialect(text: str) -> csv.Dialect | type[csv.Dialect]:
    try:
        dialect = csv.Sniffer().sniff(text[:65536], delimiters=",;\t|")
    except csv.Error:
        return csv.excel
    # A text column that consistently contains ';' or '|' can fool the
    # sniffer; only trust its verdict if the delimiter occurs in the header.
    if dialect.delimiter not in text.split("\n", 1)[0]:
        return csv.excel
    return dialect


def _parse(text: str) -> tuple[list[str], list[list[str]]]:
    try:
        rows = [row for row in csv.reader(io.StringIO(text), _detect_dialect(text)) if row]
    except csv.Error as exc:
        raise CsvError(f"malformed CSV: {exc}") from exc
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
    # The name is interpolated into DDL below, so enforce the slug invariant
    # here rather than trusting every caller to have used dataset_slug().
    if not name or name != _sanitize_identifier(name):
        raise ValueError(f"dataset name {name!r} must be a sanitized slug")

    header, raw_rows = _parse(_decode(data))
    column_names = sanitize_headers(header)
    if len(column_names) > MAX_COLUMNS:
        raise CsvError(f"CSV has {len(column_names)} columns; the limit is {MAX_COLUMNS}")
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

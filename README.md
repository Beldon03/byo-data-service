# Bring Your Own Data Service

A containerized FastAPI service that ingests arbitrary CSV (and XLSX) files as independent datasets. One SQLite table per file, schema inferred from the data and exposes a REST API for schema inspection, row CRUD, and read-only SQL queries. A minimal single-page UI is served at the root.

## Requirements

Docker with the Compose plugin. Nothing else is installed on the host.

## Run

```bash
docker compose up --build
```

- UI: http://localhost:8000
- Interactive API docs (Swagger): http://localhost:8000/docs
- Ingested data lives on the named volume `byod-data` and survives
  `docker compose down` / `up`. Use `docker compose down -v` to wipe it.

## Run the tests

```bash
docker compose run --rm api pytest
```

## API walkthrough

Create a sample file:

```powershell
@'
order_id,amount,ordered_on,note
1,9.99,2026-01-15,first
2,12.50,2026-01-16,
'@ | Set-Content -Path sales.csv -Encoding utf8
```

### Upload a file (dataset name = slugified filename)

```powershell
curl.exe -F "file=@sales.csv" http://localhost:8000/datasets
```

```json
{"name":"sales","table":"ds_sales","columns":[{"name":"order_id","type":"integer"},{"name":"amount","type":"real"},{"name":"ordered_on","type":"date"},{"name":"note","type":"text"}],"row_count":2}
```

XLSX works the same way (`curl.exe -F "file=@report.xlsx" ...`); the active sheet
is ingested through the same pipeline.

### List datasets

```powershell
curl.exe http://localhost:8000/datasets
```

```json
[{"name":"sales","row_count":2,"created_at":"2026-07-04T08:00:00.000000+00:00"}]
```

### Show a dataset's schema

```powershell
curl.exe http://localhost:8000/datasets/sales/schema
```

```json
{"columns":[{"name":"order_id","type":"integer"},{"name":"amount","type":"real"},{"name":"ordered_on","type":"date"},{"name":"note","type":"text"}]}
```

### Browse rows (paginated)

```powershell
curl.exe "http://localhost:8000/datasets/sales/rows?limit=1&offset=1"
```

```json
{"rows":[{"_row_id":2,"order_id":2,"amount":12.5,"ordered_on":"2026-01-16","note":null}],"total":2,"limit":1,"offset":1}
```

Every row carries `_row_id`, the synthetic primary key used for addressing.
A single row is available at `GET /datasets/sales/rows/2`.

### Insert a row (missing columns become NULL)

```powershell
curl.exe -X POST -H "Content-Type: application/json" `
  -d '{"order_id":3,"amount":7.5,"ordered_on":"2026-01-17"}' `
  http://localhost:8000/datasets/sales/rows
```

```json
{"_row_id":3,"order_id":3,"amount":7.5,"ordered_on":"2026-01-17","note":null}
```

### Update a row (partial; returns the full updated row)

```powershell
curl.exe -X PATCH -H "Content-Type: application/json" `
  -d '{"amount":8.0}' http://localhost:8000/datasets/sales/rows/3
```

```json
{"_row_id":3,"order_id":3,"amount":8.0,"ordered_on":"2026-01-17","note":null}
```

### Delete a row / delete a dataset

```powershell
curl.exe -X DELETE http://localhost:8000/datasets/sales/rows/3   # 204
curl.exe -X DELETE http://localhost:8000/datasets/sales          # 204, drops the table
```

### Read-only SQL across datasets

```powershell
curl.exe -X POST -H "Content-Type: application/json" `
  -d '{"sql":"SELECT ordered_on, SUM(amount) AS total FROM ds_sales GROUP BY ordered_on"}' `
  http://localhost:8000/query
```

```json
{"columns":["ordered_on","total"],"rows":[["2026-01-15",9.99],["2026-01-16",12.5]]}
```

Joins across `ds_*` tables work. Writes, DDL, `PRAGMA`, `ATTACH`, and any
access to the internal `_registry` table are rejected with 400.

### Error semantics

| Status | Meaning |
| --- | --- |
| 400 | Malformed upload (bad CSV/XLSX, binary file, empty or header-only file, oversized row) or rejected SQL |
| 404 | Unknown dataset or row |
| 409 | Dataset name already exists |
| 422 | Unknown column, `_row_id` in a payload, or a type-invalid value |

Every error body is `{"detail": "<human-readable message>"}`.

## Design decisions

**SQLite over Postgres.** The brief asks for a system an evaluator runs with
one command and explicitly waives concurrency guarantees. SQLite keeps the
whole system in a single container with zero configuration, and a file on a
named volume gives durable persistence. The service uses one shared connection and async handlers, which serializes
request handling on the event loop.

**One table per dataset, created dynamically.** Arbitrary unknown schemas rule
out any fixed data model (and an EAV design would make row CRUD and SQL
queries miserable). Each upload becomes `ds_<slug>` with columns typed by
inference.

**Synthetic `_row_id` primary key.** A CSV cannot be trusted to contain a
usable key (duplicates, NULLs, no column at all), so every table gets
`_row_id INTEGER PRIMARY KEY AUTOINCREMENT` and all row addressing uses it.
A CSV column literally named `_row_id` is renamed during sanitization.

**`_registry` metadata table.** Dataset name, table name, column names,
logical types, row count, and creation time live in one registry table. All
schema reads and column validation go through it. The service never
introspects `sqlite_master` at request time. It is also the backbone of
identifier safety: table and column names cannot be bound parameters, so only
registry-validated (or sanitizer-produced `[a-z0-9_]`) identifiers are ever
interpolated into SQL, always double-quoted; all values are bound parameters.

**Type inference is best-effort by design.** Up to 1,000 data rows per column
are sampled; candidate types are tried in order INTEGER → REAL → ISO-8601
date/datetime (stored as TEXT with logical type `date`) → TEXT. Empty strings
are NULL and do not vote; a single non-conforming value demotes the column;
leading-zero numerics (ZIP codes) stay TEXT; integers beyond SQLite's 64-bit
range demote to REAL. Rows beyond the sample that do not conform to the
inferred type are stored verbatim (SQLite's dynamic typing allows this) rather
than failing the upload.

**Read-only `/query` protected by mechanism, not string inspection.** The
endpoint uses a separate connection opened with `PRAGMA query_only = ON` plus
an authorizer callback that permits only read operations and denies any access
to `_registry`. SQL strings are never regex-filtered. Results are capped at
10,000 rows and 5 seconds of execution so one runaway query cannot hang the
single-threaded service.

**Parsing choices.** CSV parsing is stdlib `csv` with `Sniffer` dialect
detection (its verdict is only trusted when the detected delimiter appears in
the header line, so free text containing `;` or `|` cannot garble a comma
file). Encodings: UTF-8 (with or without BOM), UTF-16/UTF-32 with BOM, and a
latin-1 fallback; files containing NUL bytes are rejected as binary. Ragged
rows: too few fields are padded with NULL, too many fields reject the upload
with 400 where a short row is usually a trailing-comma artifact, extra fields
signal real corruption. XLSX support is an `openpyxl` branch that converts
the active worksheet into the same header-plus-string-rows shape and feeds the
shared pipeline, so both formats get identical inference and validation.

## Assumptions and limitations

- **Single writer, no concurrency guarantees** (per the brief). One shared
  SQLite connection; handlers run sequentially on the event loop.
- **Uploads are processed in memory**; there is no file-size cap. Very large
  files are bounded by container memory.
- **Type inference samples the first 1,000 rows**; later non-conforming
  values are stored verbatim rather than re-typing the column.
- **Dataset names come from the uploaded filename**, slugified to
  `[a-z0-9_]`. A name collision returns 409 when deleting the existing dataset or
  renaming the file; there is no overwrite.
- **Dates are stored as ISO-8601 TEXT** with a `date` logical type; SQLite
  has no native date storage class.
- **XLSX ingests the active worksheet only**; formulas are read as their
  cached values; workbooks whose chart handling trips openpyxl are rejected
  with 400.
- `/query` returns at most 10,000 rows and aborts after 5 seconds.
- The UI refuses to edit integers beyond JavaScript's exact range
  (±2^53); use the API for those.
- No authentication or permissions (explicitly out of scope).

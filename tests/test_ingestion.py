import sqlite3

import pytest

from app import db, ingestion


def ingest(conn: sqlite3.Connection, name: str, text: str, encoding: str = "utf-8") -> db.Dataset:
    dataset = ingestion.ingest_csv(conn, name, text.encode(encoding))
    conn.commit()
    return dataset


def fetch_all(conn: sqlite3.Connection, table: str) -> list[sqlite3.Row]:
    return conn.execute(f'SELECT * FROM "{table}" ORDER BY _row_id').fetchall()


def test_happy_path_infers_types_and_inserts_rows(conn: sqlite3.Connection) -> None:
    dataset = ingest(
        conn,
        "sales",
        "order_id,amount,ordered_on,note\n1,9.99,2026-01-15,first\n2,12.50,2026-01-16,\n",
    )

    assert dataset.table_name == "ds_sales"
    assert [(c.name, c.type) for c in dataset.columns] == [
        ("order_id", "integer"),
        ("amount", "real"),
        ("ordered_on", "date"),
        ("note", "text"),
    ]
    assert dataset.row_count == 2

    rows = fetch_all(conn, "ds_sales")
    assert rows[0]["_row_id"] == 1
    assert rows[0]["order_id"] == 1
    assert rows[0]["amount"] == 9.99
    assert rows[0]["ordered_on"] == "2026-01-15"
    assert rows[1]["note"] is None


def test_dataset_is_registered(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "sales", "a\n1\n")

    assert db.get_dataset(conn, "sales") == dataset


def test_duplicate_headers_get_suffixes(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "amount,amount,amount\n1,2,3\n")

    assert [c.name for c in dataset.columns] == ["amount", "amount_2", "amount_3"]


def test_blank_headers_get_positional_names(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "id,,   \n1,2,3\n")

    assert [c.name for c in dataset.columns] == ["id", "column_2", "column_3"]


def test_utf8_bom_is_stripped_from_first_header(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "id,name\n1,ann\n", encoding="utf-8-sig")

    assert dataset.columns[0].name == "id"


def test_row_id_header_cannot_shadow_primary_key(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "_row_id,x\n9,1\n")

    assert [c.name for c in dataset.columns] == ["row_id", "x"]
    assert fetch_all(conn, "ds_d")[0]["_row_id"] == 1


def test_short_rows_are_padded_with_null(conn: sqlite3.Connection) -> None:
    ingest(conn, "d", "a,b,c\n1,2\n")

    row = fetch_all(conn, "ds_d")[0]
    assert (row["a"], row["b"], row["c"]) == (1, 2, None)


def test_row_with_too_many_fields_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ingestion.CsvError, match="data row 2 has 3 fields, expected 2"):
        ingest(conn, "d", "a,b\n1,2\n1,2,3\n")


def test_empty_file_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ingestion.CsvError, match="empty"):
        ingest(conn, "d", "")


def test_header_only_file_is_rejected(conn: sqlite3.Connection) -> None:
    with pytest.raises(ingestion.CsvError, match="no data rows"):
        ingest(conn, "d", "a,b,c\n")


def test_mixed_type_column_demotes_to_text(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "x\n1\nabc\n")

    assert dataset.columns[0].type == "text"


def test_leading_zero_numerics_stay_text(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "zip,code\n01234,007\n98101,42\n")

    assert [c.type for c in dataset.columns] == ["text", "text"]
    assert fetch_all(conn, "ds_d")[0]["zip"] == "01234"


def test_integers_mixed_with_reals_promote_to_real(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "x\n1\n2.5\n")

    assert dataset.columns[0].type == "real"


def test_dates_and_datetimes_share_the_date_type(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "at\n2026-01-01\n2026-01-02T10:30:00\n")

    assert dataset.columns[0].type == "date"
    assert fetch_all(conn, "ds_d")[0]["at"] == "2026-01-01"


def test_empty_strings_are_null_and_do_not_vote(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "a,b\n,1\n2,3\n")

    assert dataset.columns[0].type == "integer"
    assert fetch_all(conn, "ds_d")[0]["a"] is None


def test_all_empty_column_falls_back_to_text(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "a,b\n1,\n2,\n")

    assert dataset.columns[1].type == "text"


def test_semicolon_delimiter_is_sniffed(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "a;b\n1;2\n")

    assert [c.name for c in dataset.columns] == ["a", "b"]
    assert fetch_all(conn, "ds_d")[0]["a"] == 1


def test_nonconforming_value_beyond_sample_is_stored_verbatim(conn: sqlite3.Connection) -> None:
    lines = "x\n" + "\n".join(str(i) for i in range(ingestion.SAMPLE_SIZE)) + "\nabc\n1_0\n"
    dataset = ingest(conn, "d", lines)

    assert dataset.columns[0].type == "integer"
    rows = fetch_all(conn, "ds_d")
    assert rows[-2]["x"] == "abc"
    assert rows[-1]["x"] == "1_0"


def test_integers_beyond_64_bits_demote_to_real(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "x\n99999999999999999999\n1\n")

    assert dataset.columns[0].type == "real"
    assert fetch_all(conn, "ds_d")[0]["x"] == 1e20


def test_float_overflow_values_stay_text(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "x\n1e400\n2.5\n")

    assert dataset.columns[0].type == "text"
    assert fetch_all(conn, "ds_d")[0]["x"] == "1e400"


def test_oversized_field_is_rejected_as_malformed(conn: sqlite3.Connection) -> None:
    huge = "a" * 200_000
    with pytest.raises(ingestion.CsvError, match="malformed CSV"):
        ingest(conn, "d", f'x\n"{huge}"\n')


def test_semicolons_inside_text_do_not_fool_the_sniffer(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "name,notes\nDoe; John,a\nRoe; Jane,b\n")

    assert [c.name for c in dataset.columns] == ["name", "notes"]
    assert fetch_all(conn, "ds_d")[0]["name"] == "Doe; John"


def test_too_many_columns_rejected(conn: sqlite3.Connection) -> None:
    header = ",".join(f"c{i}" for i in range(ingestion.MAX_COLUMNS + 1))
    row = ",".join("1" for _ in range(ingestion.MAX_COLUMNS + 1))
    with pytest.raises(ingestion.CsvError, match="limit is 2000"):
        ingest(conn, "d", f"{header}\n{row}\n")


def test_date_values_are_stored_stripped(conn: sqlite3.Connection) -> None:
    dataset = ingest(conn, "d", "at\n 2026-01-01 \n2026-01-02\n")

    assert dataset.columns[0].type == "date"
    assert fetch_all(conn, "ds_d")[0]["at"] == "2026-01-01"


def test_unsanitized_dataset_name_is_a_caller_error(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="sanitized slug"):
        ingestion.ingest_csv(conn, 'x"; DROP TABLE _registry;--', b"a\n1\n")


def test_binary_upload_is_rejected(conn: sqlite3.Connection) -> None:
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    with pytest.raises(ingestion.CsvError, match="binary"):
        ingestion.ingest_csv(conn, "d", png_bytes)


def test_dataset_slug_from_filename() -> None:
    assert ingestion.dataset_slug("Sales Report.csv") == "sales_report"
    assert ingestion.dataset_slug("data.v2.csv") == "data_v2"
    with pytest.raises(ingestion.CsvError, match="cannot derive"):
        ingestion.dataset_slug("###.csv")

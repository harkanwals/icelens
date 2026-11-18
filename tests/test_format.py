from __future__ import annotations

import datetime as dt

import pyarrow as pa

from icelens import format as fmt


def test_null_is_never_blank() -> None:
    assert fmt.cell(None) is fmt.NULL
    assert str(fmt.NULL) == "NULL"


def test_truncation_says_so() -> None:
    assert str(fmt.cell("x" * 100, width=10)) == "x" * 9 + "…"
    assert str(fmt.cell("short", width=10)) == "short"


def test_a_cell_is_never_parsed_as_markup() -> None:
    """Cell text comes out of data files and catalogs, so it must never be read as markup.

    `cell` returns a `Text`, which Rich and Textual render verbatim; a `str` would be parsed,
    and a column value of `[red]` would either restyle the grid or raise on a bad tag.
    """
    for hostile in ("[red]boom[/red]", "[/]", "[not a tag", r"\[escaped]"):
        assert str(fmt.cell(hostile, width=100)) == hostile


def test_sizes_and_counts_read_like_prose() -> None:
    assert fmt.size(512) == "512 B"
    assert fmt.size(1536) == "1.5 KB"
    assert fmt.size(None) == "-"
    assert fmt.count(1234567) == "1,234,567"


def test_timestamps_lose_the_noise() -> None:
    assert fmt.when(1789703960570).startswith("2026-")
    assert fmt.when(dt.datetime(2026, 3, 2, 9, 30, 15, 123456)) == "2026-03-02 09:30:15"


def test_maps_and_structs_stay_on_one_line() -> None:
    assert fmt.scalar({"a": 1, "b": None}) == "a=1, b=NULL"
    assert fmt.scalar([("added-records", "3"), ("total-records", "57")]) == "added-records=3, total-records=57"
    assert fmt.scalar([1, 2, 3]) == "[1, 2, 3]"
    assert fmt.scalar(b"\x00\x01") == "0x0001 (2 B)"


def test_grid_renders_every_column() -> None:
    table = pa.table({"a": [1, None], "b": ["x" * 80, "y"]})
    columns, rows = fmt.grid(table, width=8)
    assert columns == ["a", "b"]
    assert rows[1][0] is fmt.NULL
    assert str(rows[0][1]).endswith("…")


def test_a_nanosecond_timestamp_shows_all_nine_digits_instead_of_raising() -> None:
    # pyarrow raises on `as_py()` of a timestamp with a nanosecond digit set, unless pandas is
    # installed. Iceberg v3's timestamp_ns columns are made of them.
    ns = 1_767_225_600_123_456_789
    table = pa.table({
        "t": pa.array([ns, None], pa.timestamp("ns")),
        "tz": pa.array([ns, None], pa.timestamp("ns", tz="UTC")),
        "us": pa.array([ns // 1000, None], pa.timestamp("us")),
    })
    assert fmt.pylist(table) == [
        {"t": "2026-01-01 00:00:00.123456789", "tz": "2026-01-01 00:00:00.123456789Z",
         "us": dt.datetime(2026, 1, 1, 0, 0, 0, 123456)},
        {"t": None, "tz": None, "us": None},
    ]
    _, rows = fmt.grid(table)
    assert str(rows[0][0]) == "2026-01-01 00:00:00.123456789"
    assert rows[1][0] is fmt.NULL


def test_csv_export_keeps_the_untruncated_value() -> None:
    table = pa.table({"path": ["s3://bucket/" + "x" * 100]})
    assert b"x" * 100 in fmt.csv_bytes(table)


def test_a_size_past_a_terabyte_stays_in_terabytes() -> None:
    assert fmt.size(5 * 1024**5) == "5,120.0 TB"
    assert fmt.size(1023) == "1,023 B"
    assert fmt.size(1024**3) == "1.0 GB"


def test_when_names_what_it_cannot_read_rather_than_guessing() -> None:
    assert fmt.when(None) == "-"
    assert fmt.when("yesterday") == "yesterday"
    aware = dt.datetime(2026, 3, 2, 9, 30, 15, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    assert fmt.when(aware) == "2026-03-02 09:30:15"  # the wall clock as recorded, no offset noise


def test_every_scalar_type_has_one_spelling() -> None:
    from decimal import Decimal

    assert fmt.scalar(Decimal("73.40")) == "73.40"  # the decimal's own digits, trailing zero kept
    assert fmt.scalar(dt.date(2026, 3, 2)) == "2026-03-02"
    assert fmt.scalar(dt.time(9, 30)) == "09:30:00"
    assert fmt.scalar(dt.datetime(2026, 3, 2, 9, 30)) == "2026-03-02 09:30:00.000"
    assert fmt.scalar(True) == "true" and fmt.scalar(False) == "false"
    assert fmt.scalar(bytes(range(20))) == "0x000102030405060708090a0b… (20 B)"
    assert fmt.scalar([1, [2, None]]) == "[1, [2, NULL]]"
    assert fmt.scalar(3.5) == "3.5"


def test_a_key_value_pane_keeps_a_missing_value_missing() -> None:
    table = fmt.kv([("present", 1), ("absent", None)])
    assert table.column("value").to_pylist() == ["1", None]
    _, rows = fmt.grid(table)
    assert rows[1][1] is fmt.NULL


def test_pack_formats_only_what_it_is_told_to_and_leaves_none_as_null() -> None:
    table = fmt.pack(
        [{"size": 2048, "parent": None, "name": None}, {"size": None, "parent": 7, "name": "x"}],
        ["size", "parent", "name"],
        display={"size": fmt.size, "parent": fmt.blank},
    )
    assert table.column("size").to_pylist() == ["2.0 KB", "-"]
    assert table.column("parent").to_pylist() == ["", "7"]  # "no parent" is an ordinary answer
    assert table.column("name").to_pylist() == [None, "x"]  # no formatter: absent stays absent


def test_csv_writes_null_as_an_empty_field_and_keeps_the_header() -> None:
    out = fmt.csv_bytes(pa.table({"a": [1, None], "b": ["x", None]})).decode()
    assert out.splitlines() == ['"a","b"', '1,"x"', ","]

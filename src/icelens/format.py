"""Turning Arrow values into strings a terminal can show without lying about them.

Three rules run through this module. NULL is never rendered as empty space - an empty cell and
an absent value are different facts, and a lakehouse browser that blurs them is worse than no
browser. A truncated value always says so with an ellipsis, so nobody copies half an S3 path
believing it was the whole one. And **nothing that came out of a catalog, a Parquet file or a
server error is ever handed to the terminal as markup or as control codes**: every value
becomes a `rich.text.Text` with its escapes neutralised first.

That last rule is not theoretical. Rich renders a bare `str` cell through `Text.from_markup`,
so a value of `[/]` raises `MarkupError` and takes the render down, `[red]x` silently restyles
the row, and `[link=http://…]click[/link]` emits an OSC 8 hyperlink - a cell that displays one
thing and opens another. Rich's own `strip_control_codes` removes BEL, CR and backspace but
**not** ESC, so a string in a data file can still drive the terminal. `safe()` closes both.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from typing import Any

import pyarrow as pa
from rich.text import Text

#: how wide a cell may get before it is cut; the app overrides this from --max-width
DEFAULT_WIDTH = 44

NULL = Text("NULL", style="dim italic")

#: C0 and C1 control characters, plus the bidirectional and invisible formatting characters
#: that let a file path read as something it is not. Tab and newline are handled before this
#: runs (values are flattened to one line), so nothing here is legitimate display text.
_CONTROL = re.compile(
    # \x0d as well as the rest: a carriage return puts the cursor back at column zero, which
    # lets one cell overwrite the line that was printed before it. \n and \t are left alone -
    # `scalar` already collapses them, and the guide rail writes real multi-line text.
    # U+061C is the Arabic letter mark: a bidi mark like U+200F beside it, and as able to
    # reorder the neutral characters - the slashes and dots of a path - around it.
    "[\x00-\x08\x0b-\x0d\x0e-\x1f\x7f-\x9f"
    "\u061c\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

#: what a stripped control character leaves behind. Visible on purpose: the value was not
#: what it appeared to be, and silently cleaning it would be its own kind of lie.
REPLACEMENT = "\ufffd"


def safe(text: str) -> str:
    """A string with every terminal-driving character replaced, for display anywhere.

    Applied to values, to notes, to notifications and to error messages, because all four
    carry text this tool did not write: column values out of Parquet, table and namespace
    names out of a catalog, and exception strings out of a remote server.
    """
    return _CONTROL.sub(REPLACEMENT, text)


def size(value: Any) -> str:
    """Bytes as a human reads them. 1 KB is 1024 bytes here, as the file listings mean it."""
    if value is None:
        return "-"
    number = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(number) < 1024 or unit == "TB":
            return f"{number:,.0f} {unit}" if unit == "B" else f"{number:,.1f} {unit}"
        number /= 1024
    return f"{number:,.1f} TB"


def count(value: Any) -> str:
    return "-" if value is None else f"{int(value):,}"


def when(value: Any) -> str:
    """A timestamp at second resolution; milliseconds are noise when reading history."""
    if value is None:
        return "-"
    if isinstance(value, (int, float)):  # Iceberg stores milliseconds since the epoch
        value = dt.datetime.fromtimestamp(value / 1000, tz=dt.timezone.utc)
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0, tzinfo=None).isoformat(sep=" ")
    return str(value)


def scalar(value: Any) -> str:
    """One value, flattened to a single line, with no width limit applied yet."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="milliseconds")
    if isinstance(value, (dt.date, dt.time, Decimal)):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return f"0x{raw[:12].hex()}{'…' if len(raw) > 12 else ''} ({len(raw)} B)"
    # Keys go through `scalar` as well as values: a map key is data out of a Parquet file and a
    # struct field name is a schema somebody else wrote, so either can carry ESC or a newline.
    if isinstance(value, dict):
        return ", ".join(f"{scalar(k)}={scalar(v)}" for k, v in value.items())
    if isinstance(value, (list, tuple)):
        # Arrow hands a map column back as a list of (key, value) pairs
        if value and all(isinstance(item, tuple) and len(item) == 2 for item in value):
            return ", ".join(f"{scalar(k)}={scalar(v)}" for k, v in value)
        return "[" + ", ".join(scalar(item) for item in value) + "]"
    text = str(value)
    if "\n" in text or "\t" in text or "\r" in text:
        text = " ".join(text.split())
    return safe(text)


def cell(value: Any, width: int = DEFAULT_WIDTH) -> Text:
    """A value as a DataTable cell: styled NULL, or the value cut to `width`.

    Always a `Text`, never a `str`. Rich and Textual both parse a `str` cell as markup, and
    these strings come from data files and catalogs. A `Text` is rendered verbatim.
    """
    if value is None:
        return NULL
    width = max(4, int(width))
    text = scalar(value)
    return Text(text if len(text) <= width else text[: width - 1] + "…")


def pylist(table: pa.Table) -> list[dict[str, Any]]:
    """`table.to_pylist()`, except that a nanosecond timestamp arrives as its text.

    pyarrow hands a timestamp back as a `datetime`, which stops at microseconds, and raises on
    one with a nanosecond digit set unless pandas is installed - it is not a dependency. Iceberg
    v3's `timestamp_ns` and `timestamptz_ns` are exactly those columns: measured, one DuckDB
    wrote took the Data pane down with a traceback. The text keeps all nine digits, and the
    table's own schema, which `--json` reports, still says the column is a timestamp.
    """
    for index, field in enumerate(table.schema):
        if pa.types.is_timestamp(field.type) and field.type.unit == "ns":
            table = table.set_column(index, field.name, table.column(index).cast(pa.string()))
    return table.to_pylist()


def grid(table: pa.Table, width: int = DEFAULT_WIDTH, limit: int | None = None) -> tuple[list[str], list[list[Any]]]:
    """An Arrow table as (column names, rendered rows), ready for a DataTable."""
    columns = list(table.column_names)
    source = table.slice(0, limit) if limit is not None else table
    rows = [[cell(row[name], width) for name in columns] for row in pylist(source)]
    return columns, rows


def pack(
    rows: list[dict[str, Any]],
    columns: list[str],
    display: dict[str, Any] | None = None,
) -> pa.Table:
    """Typed rows as the all-string table a terminal shows.

    Display formatting happens here and only here. The panes keep their values typed - an
    int is an int, a byte count is a byte count, a timestamp is epoch milliseconds - so the
    same rows can leave as JSON without a consumer having to parse `"13.0 KB"` back into a
    number. A formatter handles its own None (`count(None)` is `"-"`, which is a real answer);
    a column with no formatter shows NULL for None, because an absent value and an empty cell
    are different facts.
    """
    display = display or {}

    def render(column: str, value: Any) -> str | None:
        formatter = display.get(column)
        if formatter is not None:
            rendered = formatter(value)
            return None if rendered is None else safe(str(rendered))
        return None if value is None else scalar(value)

    return pa.table({c: pa.array([render(c, r.get(c)) for r in rows], pa.string()) for c in columns})


#: display formatters that turn None into text rather than a NULL cell, for columns where
#: "no parent" or "not this snapshot's manifest" is an ordinary answer rather than missing data.
def blank(value: Any) -> str:
    return "" if value is None else scalar(value)


def kv(pairs: list[tuple[str, Any]]) -> pa.Table:
    """A two-column Arrow table, for the panes that are really key/value lists."""
    return pa.table(
        {
            "property": pa.array([str(k) for k, _ in pairs], pa.string()),
            "value": pa.array([None if v is None else scalar(v) for _, v in pairs], pa.string()),
        }
    )


def csv_bytes(table: pa.Table) -> bytes:
    """The exact rows, unrendered and untruncated - what an export has to be."""
    import io

    from pyarrow import csv

    buffer = io.BytesIO()
    csv.write_csv(table, buffer)
    return buffer.getvalue()

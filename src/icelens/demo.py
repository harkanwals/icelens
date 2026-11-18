"""A table worth opening, for somebody who has no catalog yet.

An empty sandbox teaches nothing until something has been written to it, and the first
thing a newcomer meets should not be a blank tree. So `icelens --demo` makes one table and
writes it a short history, chosen so that every pane has something true to say:

    snapshot 1   append      the first load: 120 orders, twelve of them test rows
    snapshot 2   append      30 late arrivals - the manifest list grows a second manifest
    tag          first-load  pinned to snapshot 1, so time travel has somewhere to go
    snapshot 3   overwrite   DELETE the test rows: copy-on-write, so the file is rewritten
    schema 1                 add `channel`, rename `customer` to `customer_id` - same field id
    snapshot 4   append      20 orders that know their channel
    spec 1                   partition by region from here on; older files keep spec 0
    snapshot 5   append      25 orders, written one file per region

Everything goes through PyIceberg and nothing else, so it works on a machine with no network
and no DuckDB extension. The data is arithmetic rather than random: the same demo every time,
on every machine, which is what lets the tests and the README describe it exactly.

A v3 sandbox gets the same story told the v3 way, by DuckDB, because nothing else writes v3:

    snapshot 1   append      the first load, as above
    snapshot 2   append      the late arrivals
    tag          first-load
    snapshot 3   delete      the test rows go into a deletion vector; the data file stays
    snapshot 4   overwrite   UPDATE one order: a delete and an insert, and the row keeps its _row_id
    schema 1                 add `channel`
    snapshot 5   append      orders that know their channel
    snapshot 6   overwrite   MERGE, the matched half: one order corrected
    snapshot 7   append      MERGE, the other half: two new orders. One statement, two snapshots
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyarrow as pa

#: Where the demo lives inside its catalog.
NAMESPACE = "demo"
TABLE = f"{NAMESPACE}.orders"

_REGIONS = ("north", "south", "east", "west")


@dataclass(frozen=True)
class Seeded:
    """What `seed` left behind, for the one line the CLI prints."""

    table: str
    created: bool
    snapshots: int
    rows: int

    def line(self) -> str:
        verb = "made" if self.created else "already there:"
        return f"{verb} {self.table} - {self.snapshots} snapshots, {self.rows} rows"


def _orders(start: int, count: int, channel: bool = False, test_every: int = 0) -> pa.Table:
    """`count` orders numbered from `start`. Every `test_every`-th one is a test row."""
    ids = list(range(start, start + count))
    columns: dict[str, pa.Array] = {
        "order_id": pa.array(ids, pa.int64()),
        "region": pa.array(
            ["test" if test_every and i % test_every == 0 else _REGIONS[i % 4] for i in ids], pa.string()
        ),
        "amount": pa.array([Decimal(i * 7 % 500) + Decimal("0.99") for i in ids], pa.decimal128(10, 2)),
        "customer" if not channel else "customer_id": pa.array([f"c{i % 37:03d}" for i in ids], pa.string()),
        "ordered_at": pa.array(
            [dt.datetime(2026, 3, 1) + dt.timedelta(hours=i * 5) for i in ids], pa.timestamp("us")
        ),
    }
    if channel:
        columns["channel"] = pa.array(["web" if i % 3 else "store" for i in ids], pa.string())
    return pa.table(columns)


def seed(lake: Any) -> Seeded:
    """Write the demo table into `lake`, once. A second call finds it and leaves it alone.

    Only for a sandbox: this writes, and a sandbox is the one place icelens does.
    """
    from pyiceberg.exceptions import NamespaceAlreadyExistsError, NoSuchTableError
    from pyiceberg.types import StringType

    if not lake.is_sandbox or lake.is_rest_sandbox:
        raise ValueError("the demo is written into a sandbox of this tool's own, and nowhere else")
    if getattr(lake, "is_served_sandbox", False):
        return seed_v3(lake)
    catalog = lake.catalog
    try:
        table = catalog.load_table(TABLE)
        return Seeded(TABLE, False, len(table.metadata.snapshots), _rows(table))
    except NoSuchTableError:
        pass
    try:
        catalog.create_namespace(NAMESPACE)
    except NamespaceAlreadyExistsError:
        pass

    table = catalog.create_table(TABLE, schema=_orders(0, 0).schema)
    table.append(_orders(1, 120, test_every=10))
    first = table.current_snapshot().snapshot_id
    table.append(_orders(121, 30))
    table.manage_snapshots().create_tag(first, "first-load").commit()
    table.delete("region = 'test'")
    with table.update_schema() as update:
        update.add_column("channel", StringType())
        update.rename_column("customer", "customer_id")
    table.append(_orders(151, 20, channel=True))
    with table.update_spec() as spec:
        spec.add_identity("region")
    table.append(_orders(171, 25, channel=True))

    table = catalog.load_table(TABLE)
    lake.forget()
    return Seeded(TABLE, True, len(table.metadata.snapshots), _rows(table))


def _rows(table: Any) -> int:
    snapshot = table.current_snapshot()
    return int(snapshot.summary.get("total-records", 0) or 0) if snapshot is not None else 0


#: One order, as SQL: the same arithmetic `_orders` uses, so both demos hold the same rows.
_ORDER = (
    "i AS order_id, "
    "CASE WHEN i % 10 = 0 AND i <= 120 THEN 'test' ELSE ['north', 'south', 'east', 'west'][i % 4 + 1] END AS region, "
    "CAST((i * 7 % 500) + 0.99 AS DECIMAL(10, 2)) AS amount, "
    "'c' || lpad(CAST(i % 37 AS VARCHAR), 3, '0') AS customer, "
    "TIMESTAMP '2026-03-01' + i * INTERVAL 5 HOUR AS ordered_at"
)

#: The v3 history, statement by statement, as a person would type it into the Writer pane.
V3_STATEMENTS = (
    f"CREATE TABLE {TABLE} AS SELECT {_ORDER} FROM range(1, 121) t(i)",
    f"INSERT INTO {TABLE} SELECT {_ORDER} FROM range(121, 151) t(i)",
    f"DELETE FROM {TABLE} WHERE region = 'test'",
    f"UPDATE {TABLE} SET amount = amount + 10 WHERE order_id = 7",
    f"ALTER TABLE {TABLE} ADD COLUMN channel VARCHAR",
    f"INSERT INTO {TABLE} SELECT {_ORDER}, CASE WHEN i % 3 = 0 THEN 'store' ELSE 'web' END FROM range(151, 171) t(i)",
    f"MERGE INTO {TABLE} t USING (SELECT * FROM (VALUES (151, 'north', 42.00, 'c001', TIMESTAMP '2026-04-08', 'store'), "
    f"(901, 'east', 10.99, 'c002', TIMESTAMP '2026-04-09', 'web'), (902, 'west', 20.99, 'c003', TIMESTAMP '2026-04-09', 'web')) "
    f"v(order_id, region, amount, customer, ordered_at, channel)) s ON t.order_id = s.order_id "
    f"WHEN MATCHED THEN UPDATE SET amount = s.amount, channel = s.channel WHEN NOT MATCHED THEN INSERT VALUES "
    f"(s.order_id, s.region, s.amount, s.customer, s.ordered_at, s.channel)",
)


def seed_v3(lake: Any) -> Seeded:
    """The demo at format-version 3, written by DuckDB through the sandbox's own REST catalog.

    Through `Lake.write`, the same path the Writer pane commits by, so the demo is also the
    first thing that path ever does in a new v3 sandbox. The tag is PyIceberg's: a tag
    changes metadata only, which the catalog applies whatever the table's version.
    """
    from pyiceberg.exceptions import NoSuchTableError

    try:
        table = lake.catalog.load_table(TABLE)
        return Seeded(TABLE, False, len(table.metadata.snapshots), _live_rows(lake))
    except NoSuchTableError:
        pass
    create, late, *rest = V3_STATEMENTS
    lake.write(create)
    lake.write(late)
    table = lake.table(TABLE, refresh=True)
    first = min(table.metadata.snapshots, key=lambda s: s.sequence_number).snapshot_id
    table.manage_snapshots().create_tag(first, "first-load").commit()
    for statement in rest:
        lake.write(statement)
    table = lake.table(TABLE, refresh=True)
    return Seeded(TABLE, True, len(table.metadata.snapshots), _live_rows(lake))


def _live_rows(lake: Any) -> int:
    """Rows counted, not read off the summary: under deletion vectors the summary overstates."""
    rows, _ = lake.rows(TABLE, limit=1_000_000, columns=["order_id"])
    return rows.num_rows

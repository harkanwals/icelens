"""`--json` is a public API, and this is its contract, written down where a rename cannot hide.

`schema_version: 1` promises that a key is never removed or renamed and a value never changes
type without the number going up. That promise is only as good as something that notices, so
every `kind` the CLI emits has its envelope and its row shape spelled out below as data, and the
tests compare what the CLI actually printed against them - key for key, type for type. Adding a
key means adding it here too, which is the moment to decide it belongs in the contract.

Three rules run through all of it, each asserted on every document rather than in one test:
the output is one object on one line, parsed strictly (a bare `NaN` is a failure); ints are
ints and never bools or strings; and a failure is a JSON object on stderr with stdout empty.

The catalog here is built by PyIceberg alone - no DuckDB - so every metadata kind is checked on
a machine without the extensions too. Only `data` on its DuckDB route, `sql` and `written` need
them.
"""

from __future__ import annotations

import datetime as dt
import time
import urllib.parse
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pytest
from headless import Ran, argv, clean_environment, run
from stub import StubCatalog

from icelens.catalog import Lake, Target
from icelens.cli import PANE_NAMES, SCHEMA_VERSION

# -- the vocabulary of the contract --------------------------------------------------------------

INT, STR, BOOL, FLOAT, LIST, DICT, NULL = int, str, bool, float, list, dict, type(None)
#: any JSON scalar: a column default, which is whatever the schema's author made it
SCALAR = (INT, STR, BOOL, FLOAT, NULL)


def _is(value: Any, allowed: Any) -> bool:
    """`type(value) is t` rather than isinstance: in Python `True` is an int, and in JSON it is not."""
    kinds = allowed if isinstance(allowed, tuple) else (allowed,)
    return any(type(value) is kind for kind in kinds)


def conforms(row: dict[str, Any], shape: dict[str, Any], where: str) -> None:
    assert isinstance(row, dict), f"{where}: {row!r} is not an object"
    extra, missing = set(row) - set(shape), set(shape) - set(row)
    assert not (extra or missing), f"{where}: keys not in the contract {sorted(extra)}, keys the contract wants {sorted(missing)}"
    for key, allowed in shape.items():
        assert _is(row[key], allowed), f"{where}.{key}: {row[key]!r} is a {type(row[key]).__name__}, the contract says {allowed}"


#: every document's first two keys
ENVELOPE = {"schema_version": INT, "kind": STR, "count": INT, "rows": LIST}


def envelope(body: dict[str, Any], kind: str, extra: dict[str, Any]) -> None:
    """The envelope for `kind`: the shared keys, the ones this kind adds, and nothing else."""
    conforms(body, {**ENVELOPE, **extra}, f"{kind} envelope")
    assert body["schema_version"] == SCHEMA_VERSION == 1
    assert body["kind"] == kind
    assert body["count"] == len(body["rows"])


#: what a pane's envelope adds. `snapshot` is there when the table has one; `columns` only on
#: the panes whose Arrow table is already typed - data and partitions - and `where` only when asked
PANE_EXTRA = {"table": STR, "snapshot": INT, "note": STR}
COLUMN = {"name": STR, "type": STR}
TYPED_PANES = {"data", "partitions"}

ROWS: dict[str, dict[str, Any]] = {
    "overview": {
        "table": STR,
        "format_version": INT,
        "uuid": STR,
        "location": STR,
        "last_updated_ms": INT,
        "current_snapshot_id": (INT, NULL),
        "viewing_snapshot_id": (INT, NULL),
        "time_travelling": BOOL,
        "snapshots": INT,
        "schemas": INT,
        "current_schema_id": INT,
        "columns": INT,
        "partition_spec_id": INT,
        "partition_spec": LIST,
        "sort_order_id": INT,
        "sort_order": LIST,
        "total_records": (INT, NULL),
        "total_data_files": (INT, NULL),
        "total_delete_files": (INT, NULL),
        "total_files_size_bytes": (INT, NULL),
        "next_row_id": (INT, NULL),
        "refs": DICT,
        "properties": DICT,
        "metadata_log": LIST,
        "engine": STR,
    },
    "schema": {
        "id": (INT, NULL),
        "name": STR,
        "depth": INT,
        "type": STR,
        "required": (BOOL, NULL),
        "identifier": BOOL,
        "reserved": BOOL,
        "partition": (STR, NULL),
        "default": SCALAR,
        "doc": STR,
    },
    "snapshots": {
        "#": INT,
        "": STR,
        "ancestor": BOOL,
        "current": BOOL,
        "committed at": INT,
        "operation": STR,
        "snapshot id": INT,
        "parent": (INT, NULL),
        "seq": INT,
        "first row id": (INT, NULL),
        "+rows": INT,
        "-rows": INT,
        "total rows": (INT, NULL),
        "+files": INT,
        "-files": INT,
        "+deletes": INT,
        "schema": (INT, NULL),
        "refs": LIST,
        "summary": DICT,
    },
    "commit": {
        "status": STR,
        "content": STR,
        "rows": INT,
        "size": INT,
        "partition": STR,
        "seq": INT,
        "spec_id": INT,
        "file": STR,
        "path": STR,
    },
    "manifests": {
        "content": STR,
        "added by": INT,
        "own": BOOL,
        "added": INT,
        "existing": INT,
        "deleted": INT,
        "+rows": INT,
        "-rows": INT,
        "seq": INT,
        "spec": INT,
        "size": INT,
        "manifest": STR,
        "path": STR,
    },
    "files": {
        "content": STR,
        "rows": INT,
        "size": INT,
        "partition": STR,
        "format": STR,
        "seq": INT,
        "spec_id": INT,
        "added by": INT,
        "file": STR,
        "path": STR,
    },
    # PyIceberg's `inspect.partitions`, passed through: its names, and its types per `columns`
    "partitions": {
        "record_count": INT,
        "file_count": INT,
        "total_data_file_size_in_bytes": INT,
        "position_delete_record_count": INT,
        "position_delete_file_count": INT,
        "equality_delete_record_count": INT,
        "equality_delete_file_count": INT,
        "last_updated_at": STR,
        "last_updated_snapshot_id": INT,
    },
    "data": {"id": INT, "name": STR, "ingest_date": STR, "measured_at": STR},
}

#: Layout rows are a tree, and each kind of node carries the facts that node has
_NODE = {"depth": INT, "node": STR, "detail": STR, "kind": STR}
LAYOUT: dict[str, dict[str, Any]] = {
    "table-metadata": {**_NODE, "path": (STR, NULL), "snapshots": INT, "schemas": INT},
    "snapshot": {**_NODE, "snapshot_id": INT, "operation": STR, "timestamp_ms": INT, "schema_id": (INT, NULL), "sequence_number": INT},
    "manifest-list": {**_NODE, "path": STR},
    "manifest": {**_NODE, "content": STR, "own": BOOL, "path": STR, "size_bytes": INT, "files": INT},
    "file": {**_NODE, "content": STR, "status": STR, "path": STR, "size_bytes": INT, "record_count": INT},
    "truncated": dict(_NODE),
}

PARTITION_FIELD = {"name": STR, "transform": STR, "source": STR, "source_id": INT, "field_id": INT}
SORT_FIELD = {"source": STR, "source_id": INT, "direction": STR, "null_order": STR}
REF = {"type": STR, "snapshot_id": INT}
METADATA_LOG = {"timestamp_ms": INT, "file": STR, "path": STR}

NAMESPACE_ROW = {"namespace": STR, "tables": (INT, NULL), "note": STR}
TABLE_ROW = {
    "table": STR,
    "v": INT,
    "rows": (INT, NULL),
    "files": (INT, NULL),
    "deletes": (INT, NULL),
    "size": (INT, NULL),
    "snapshots": INT,
    "updated": INT,
    "partitioned by": STR,
    "partitions": LIST,
    "error": NULL,
}
#: a table that would not open has nothing to say but its name and why
BROKEN_TABLE_ROW = {"table": STR, "error": STR}
TERM_INDEX = {"key": STR, "name": STR, "gloss": STR}
TERM_FULL = {**TERM_INDEX, "body": STR, "where": STR, "see": LIST}
WAREHOUSE_ROW = {"warehouse": STR}
CATALOG_ROW = {"catalog": STR, "type": (STR, NULL), "uri": (STR, NULL), "warehouse": (STR, NULL)}
WRITTEN_ROW = {
    "kind": STR,
    "table": STR,
    "operation": STR,
    "snapshot_id": INT,
    "added": INT,
    "deleted": INT,
    "records": INT,
    "files": INT,
    "engine": STR,
    "sql": STR,
}
ERROR = {"schema_version": INT, "error": DICT}
ERROR_BODY = {"kind": STR, "message": STR}


def failed(ran: Ran, kind: str, code: int = 1) -> dict[str, Any]:
    """A failure, as the contract has it: the exit code, an error object on stderr, nothing on stdout."""
    assert ran.code == code, (ran.code, ran.err)
    document = ran.error
    conforms(document, ERROR, "error document")
    assert document["schema_version"] == SCHEMA_VERSION
    error = document["error"]
    assert error["kind"] == kind, error
    #: present only when they have something to say: the choices, and what a failed write committed
    optional = {"warehouses": LIST, "written": LIST}
    shape = {**ERROR_BODY, **{key: kind_ for key, kind_ in optional.items() if key in error}}
    conforms(error, shape, f"{kind} error")
    for index, row in enumerate(error.get("written", [])):
        conforms(row, WRITTEN_ROW, f"error.written[{index}]")
    return error


# -- a catalog with more shapes in it than the session one has ------------------------------------


def _typed_rows() -> pa.Table:
    return pa.table(
        {
            "id": pa.array([1, 2, 3], pa.int64()),
            "price": pa.array([Decimal("73.34"), None, Decimal("-0.01")], pa.decimal128(12, 2)),
            "day": pa.array([dt.date(2026, 3, 2)] * 3, pa.date32()),
            "at": pa.array([dt.datetime(2026, 3, 2, 12, 0, 0, 123456)] * 3, pa.timestamp("us")),
            "at_utc": pa.array([dt.datetime(2026, 3, 2, 12, 0, tzinfo=dt.timezone.utc)] * 3, pa.timestamp("us", tz="UTC")),
            "ratio": pa.array([float("nan"), float("inf"), float("-inf")], pa.float64()),
            "blob": pa.array([b"\x00\x01", b"", None], pa.binary()),
            "ok": pa.array([True, False, None], pa.bool_()),
            "tags": pa.array([["a", "b"], [], None], pa.list_(pa.string())),
            "point": pa.array([{"x": 1, "y": 2}, None, {"x": 3, "y": None}], pa.struct([("x", pa.int32()), ("y", pa.int32())])),
            "attrs": pa.array([[("k", "v")], [], None], pa.map_(pa.string(), pa.string())),
        }
    )


@pytest.fixture(scope="module")
def shaped(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    """Every type JSON has no word for, a partitioned and sorted table with a tag and a branch,
    and a table whose metadata file has gone - all written by PyIceberg, so no DuckDB is needed.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.table.sorting import SortField, SortOrder
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import LongType, NestedField, StringType

    home = tmp_path_factory.mktemp("shaped")
    lake = Lake(
        Target(
            properties={"type": "sql", "uri": f"sqlite:///{home / 'catalog.db'}", "warehouse": home.as_uri()},
            origin="tests",
        )
    )
    catalog = lake.catalog
    catalog.create_namespace("demo")

    typed = _typed_rows()
    catalog.create_table("demo.typed", schema=typed.schema).append(typed)

    parted = catalog.create_table(
        "demo.parted",
        schema=Schema(NestedField(1, "id", LongType()), NestedField(2, "region", StringType())),
        partition_spec=PartitionSpec(PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="region")),
        sort_order=SortOrder(SortField(source_id=1, transform=IdentityTransform())),
    )
    parted.append(pa.table({"id": pa.array([1, 2], pa.int64()), "region": ["eu", "eu"]}))
    parted.append(pa.table({"id": pa.array([3], pa.int64()), "region": ["us"]}))
    first = min(parted.metadata.snapshots, key=lambda s: s.sequence_number)
    parted.manage_snapshots().create_tag(first.snapshot_id, "genesis").create_branch(first.snapshot_id, "wip").commit()

    broken = catalog.create_table("demo.broken", schema=typed.schema)
    Path(urllib.parse.urlsplit(broken.metadata_location).path).unlink()

    yield lake
    lake.close()


@pytest.fixture(autouse=True)
def _nothing_but_the_arguments(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_environment(monkeypatch, tmp_path)


# -- every pane --------------------------------------------------------------------------------------


@pytest.mark.parametrize("pane", PANE_NAMES)
def test_every_pane_keeps_its_envelope_and_its_row_shape(lake: Lake, capsys, pane: str) -> None:
    ran = run(capsys, argv(lake, "--pane", pane, "demo.events", "--json"))
    assert ran.code == 0, ran.err
    body = ran.body
    extra = {**PANE_EXTRA, **({"columns": LIST} if pane in TYPED_PANES else {})}
    envelope(body, pane, extra)
    assert body["table"] == "demo.events"
    assert body["snapshot"] == lake.table("demo.events").metadata.current_snapshot_id
    assert body["rows"], f"{pane} came back empty for a table with history"
    for index, row in enumerate(body["rows"]):
        shape = LAYOUT[row.get("kind", "?")] if pane == "layout" else ROWS[pane]
        conforms(row, shape, f"{pane}[{index}]")
    if pane in TYPED_PANES:
        for column in body["columns"]:
            conforms(column, COLUMN, f"{pane} column")
        assert [c["name"] for c in body["columns"]] == list(body["rows"][0])


@pytest.mark.parametrize("pane", PANE_NAMES)
def test_a_table_with_no_snapshot_has_no_snapshot_key(lake: Lake, capsys, pane: str) -> None:
    """Absent, not null and not 0: `snapshot` is there when there is one to name."""
    ran = run(capsys, argv(lake, "--pane", pane, "demo.empty", "--json"))
    assert ran.code == 0, ran.err
    body = ran.body
    extra = {"table": STR, "note": STR, **({"columns": LIST} if pane == "data" else {})}
    envelope(body, pane, extra)
    expected = {"overview": 1, "schema": 4, "layout": 1}.get(pane, 0)
    assert body["count"] == expected, body["rows"]


def test_where_and_time_travel_are_echoed_back(lake: Lake, capsys) -> None:
    first = min(lake.table("demo.events").metadata.snapshots, key=lambda s: s.sequence_number).snapshot_id
    ran = run(capsys, argv(lake, "--pane", "overview", "demo.events", "--snapshot", str(first), "--where", "id > 1", "--json"))
    body = ran.body
    envelope(body, "overview", {**PANE_EXTRA, "where": STR})
    assert (body["snapshot"], body["where"]) == (first, "id > 1")
    facts = body["rows"][0]
    assert facts["viewing_snapshot_id"] == first
    assert facts["time_travelling"] is True
    assert facts["current_snapshot_id"] != first


def test_overview_nests_its_facts_in_shapes_of_their_own(shaped: Lake, capsys) -> None:
    facts = run(capsys, argv(shaped, "--pane", "overview", "demo.parted", "--json")).body["rows"][0]
    conforms(facts, ROWS["overview"], "overview")
    (field,) = facts["partition_spec"]
    conforms(field, PARTITION_FIELD, "partition_spec[0]")
    assert (field["name"], field["transform"], field["source"]) == ("region", "identity", "region")
    (order,) = facts["sort_order"]
    conforms(order, SORT_FIELD, "sort_order[0]")
    assert (order["source"], order["direction"], order["null_order"]) == ("id", "asc", "nulls_first")
    assert set(facts["refs"]) == {"main", "genesis", "wip"}
    for name, ref in facts["refs"].items():
        conforms(ref, REF, f"refs.{name}")
    assert {name: ref["type"] for name, ref in facts["refs"].items()} == {"main": "branch", "genesis": "tag", "wip": "branch"}
    assert facts["metadata_log"]
    for entry in facts["metadata_log"]:
        conforms(entry, METADATA_LOG, "metadata_log")
        assert entry["path"].endswith(entry["file"])
    assert all(isinstance(value, str) for value in facts["properties"].values())


def test_a_partitioned_table_names_its_partition_in_every_pane_that_has_one(shaped: Lake, capsys) -> None:
    body = run(capsys, argv(shaped, "--pane", "partitions", "demo.parted", "--json")).body
    shape = {**ROWS["partitions"], "partition": DICT, "spec_id": INT}
    for row in body["rows"]:
        conforms(row, shape, "partitions")
    assert sorted((row["partition"]["region"], row["record_count"]) for row in body["rows"]) == [("eu", 2), ("us", 1)]
    for pane in ("files", "commit"):
        rows = run(capsys, argv(shaped, "--pane", pane, "demo.parted", "--json")).body["rows"]
        assert {row["partition"] for row in rows} <= {"region=eu", "region=us"}


def test_snapshots_carry_their_refs_as_a_list(shaped: Lake, capsys) -> None:
    rows = run(capsys, argv(shaped, "--pane", "snapshots", "demo.parted", "--json")).body["rows"]
    assert [row["refs"] for row in rows] == [["genesis", "wip"], ["main"]]
    # the summary is Iceberg's own string map, passed through as strings - not parsed into numbers
    assert all(type(value) is str for row in rows for value in row["summary"].values())
    assert rows[0]["summary"]["added-records"] == "2"


def test_every_metadata_timestamp_is_epoch_milliseconds_and_they_agree(lake: Lake, capsys) -> None:
    """One instant, four panes, one number - and an integer, so a reader never parses a date."""
    snapshots = run(capsys, argv(lake, "--pane", "snapshots", "demo.events", "--json")).body["rows"]
    layout = run(capsys, argv(lake, "--pane", "layout", "demo.events", "--json")).body["rows"]
    overview = run(capsys, argv(lake, "--pane", "overview", "demo.events", "--json")).body["rows"][0]
    tables = run(capsys, argv(lake, "--list", "demo", "--json")).body["rows"]
    now_ms = time.time() * 1000
    current = snapshots[-1]
    assert 1_500_000_000_000 < current["committed at"] <= now_ms
    (node,) = [row for row in layout if row["kind"] == "snapshot"]
    assert node["timestamp_ms"] == current["committed at"]
    assert overview["last_updated_ms"] >= current["committed at"]
    assert {row["table"]: row["updated"] for row in tables}["events"] == overview["last_updated_ms"]
    assert all(type(entry["timestamp_ms"]) is int for entry in overview["metadata_log"])


def test_partitions_is_the_one_pane_whose_timestamp_is_a_string(lake: Lake, capsys) -> None:
    """Its rows are PyIceberg's own Arrow table, passed through, so `columns` says what the string is.

    Pinned because it is the exception to the rule above, and a reader should find it here
    rather than in production.
    """
    body = run(capsys, argv(lake, "--pane", "partitions", "demo.events", "--json")).body
    types = {c["name"]: c["type"] for c in body["columns"]}
    assert types["last_updated_at"] == "timestamp[ms]"
    dt.datetime.fromisoformat(body["rows"][0]["last_updated_at"])


def test_v3_facts_are_there_on_v3_and_null_below_it(lake: Lake, v3_lake: Lake, capsys) -> None:
    v3 = run(capsys, argv(v3_lake, "--pane", "overview", "demo.events", "--json")).body["rows"][0]
    assert (v3["format_version"], v3["next_row_id"]) == (3, 60)
    v2 = run(capsys, argv(lake, "--pane", "overview", "demo.events", "--json")).body["rows"][0]
    assert (v2["format_version"], v2["next_row_id"]) == (2, None)
    schema = run(capsys, argv(v3_lake, "--pane", "schema", "demo.events", "--json")).body["rows"]
    reserved = [row for row in schema if row["reserved"]]
    assert [(r["name"], r["id"], r["required"]) for r in reserved] == [
        ("_row_id", None, None),
        ("_last_updated_sequence_number", None, None),
    ]
    snapshots = run(capsys, argv(v3_lake, "--pane", "snapshots", "demo.events", "--json")).body["rows"]
    assert [row["first row id"] for row in snapshots] == [0, 50]


# -- the data pane: the types JSON has no word for ----------------------------------------------------


def test_data_keeps_every_type_json_can_hold_and_names_the_rest(shaped: Lake, capsys) -> None:
    """Ints and bools and nulls as themselves; decimals, dates, times and bytes as strings; and
    `NaN`, `Infinity` and `-Infinity` by name, because no strict parser takes them bare.

    The values are the same whichever route read the rows. The `columns` types are Arrow's
    spelling, and two of them are not: DuckDB names a list's child `l` where PyIceberg says
    `element`, and spells UTC `Etc/UTC` - so those two are held to their family here.
    """
    body = run(capsys, argv(shaped, "--pane", "data", "demo.typed", "--json")).body
    rows = sorted(body["rows"], key=lambda row: row["id"])
    assert rows == [
        {
            "id": 1, "price": "73.34", "day": "2026-03-02", "at": "2026-03-02 12:00:00.123456",
            "at_utc": "2026-03-02 12:00:00+00:00", "ratio": "NaN", "blob": "0001", "ok": True,
            "tags": ["a", "b"], "point": {"x": 1, "y": 2}, "attrs": [["k", "v"]],
        },
        {
            "id": 2, "price": None, "day": "2026-03-02", "at": "2026-03-02 12:00:00.123456",
            "at_utc": "2026-03-02 12:00:00+00:00", "ratio": "Infinity", "blob": "", "ok": False,
            "tags": [], "point": None, "attrs": [],
        },
        {
            "id": 3, "price": "-0.01", "day": "2026-03-02", "at": "2026-03-02 12:00:00.123456",
            "at_utc": "2026-03-02 12:00:00+00:00", "ratio": "-Infinity", "blob": None, "ok": None,
            "tags": None, "point": {"x": 3, "y": None}, "attrs": None,
        },
    ]  # fmt: skip
    types = {c["name"]: c["type"] for c in body["columns"]}
    assert types == {
        **types,
        "id": "int64", "price": "decimal128(12, 2)", "day": "date32[day]", "at": "timestamp[us]",
        "ratio": "double", "blob": "binary", "ok": "bool", "point": "struct<x: int32, y: int32>",
        "attrs": "map<string, string>",
    }  # fmt: skip
    assert types["at_utc"].startswith("timestamp[us, tz=") and types["tags"].startswith("list<")


# -- the listings ----------------------------------------------------------------------------------


def test_namespaces(lake: Lake, capsys) -> None:
    ran = run(capsys, argv(lake, "--list", "--json"))
    assert ran.code == 0
    body = ran.body
    envelope(body, "namespaces", {"note": STR})
    for row in body["rows"]:
        conforms(row, NAMESPACE_ROW, "namespaces")
    assert body["rows"] == [{"namespace": "demo", "tables": 2, "note": ""}]


def test_tables_including_one_that_will_not_open(shaped: Lake, capsys) -> None:
    ran = run(capsys, argv(shaped, "--list", "demo", "--json"))
    assert ran.code == 0, "one broken table must not fail the listing"
    body = ran.body
    envelope(body, "tables", {"namespace": STR, "note": STR})
    assert body["namespace"] == "demo"
    by_name = {row["table"]: row for row in body["rows"]}
    assert set(by_name) == {"broken", "parted", "typed"}
    conforms(by_name["broken"], BROKEN_TABLE_ROW, "tables[broken]")
    for name in ("parted", "typed"):
        conforms(by_name[name], TABLE_ROW, f"tables[{name}]")
    assert by_name["parted"]["partitions"] == ["region"]
    assert (by_name["parted"]["rows"], by_name["parted"]["files"]) == (3, 2)


def test_explain_the_index_and_one_term(capsys) -> None:
    index = run(capsys, ["--explain", "--json"])
    assert index.code == 0
    envelope(index.body, "explain", {})
    for row in index.body["rows"]:
        conforms(row, TERM_INDEX, "explain index")
    one = run(capsys, ["--explain", "manifest list", "--json"])
    envelope(one.body, "explain", {"query": STR})
    for row in one.body["rows"]:
        conforms(row, TERM_FULL, "explain term")
    none = run(capsys, ["--explain", "flux capacitor", "--json"])
    assert none.code == 1  # nothing found is an answer, on stdout, and also a non-zero exit
    envelope(none.body, "explain", {"query": STR})
    assert none.body["rows"] == []


def test_catalogs_with_a_credential_in_one_of_them(tmp_path: Path, capsys) -> None:
    (tmp_path / ".pyiceberg.yaml").write_text(
        "catalog:\n"
        "  lab:\n"
        "    uri: https://alice:hunter2@lab.example/catalog\n"
        "    warehouse: analytics\n"
        "  glue:\n"
        "    type: glue\n"
    )
    ran = run(capsys, ["--catalogs", "--json"])
    assert ran.code == 0
    envelope(ran.body, "catalogs", {})
    for row in ran.body["rows"]:
        conforms(row, CATALOG_ROW, "catalogs")
    assert ran.body["rows"] == [
        {"catalog": "glue", "type": "glue", "uri": None, "warehouse": None},
        {"catalog": "lab", "type": "rest", "uri": "https://lab.example/catalog", "warehouse": "analytics"},
    ]
    assert "hunter2" not in ran.out


def test_no_catalogs_is_an_empty_answer_and_exit_1(capsys) -> None:
    ran = run(capsys, ["--catalogs", "--json"])
    assert ran.code == 1
    envelope(ran.body, "catalogs", {})
    assert ran.body["rows"] == []


# -- a REST catalog that wants a warehouse, served locally ---------------------------------------------


@pytest.fixture()
def picky_catalog() -> str:
    """A REST catalog with two warehouses, which will not answer until told which (see tests/stub.py)."""
    with StubCatalog({"wh-a": ["demo"], "wh-b": ["raw"]}) as stub:
        yield stub.uri


def test_warehouses(picky_catalog: str, capsys) -> None:
    ran = run(capsys, ["--no-env", "--uri", picky_catalog, "--warehouses", "--json"])
    assert ran.code == 0, ran.err
    envelope(ran.body, "warehouses", {})
    for row in ran.body["rows"]:
        conforms(row, WAREHOUSE_ROW, "warehouses")
    assert ran.body["rows"] == [{"warehouse": "wh-a"}, {"warehouse": "wh-b"}]


def test_a_catalog_that_lists_no_warehouses_is_an_empty_answer_and_exit_1(lake: Lake, capsys) -> None:
    ran = run(capsys, argv(lake, "--warehouses", "--json"))
    assert ran.code == 1
    envelope(ran.body, "warehouses", {})
    assert ran.body["rows"] == []


def test_needs_a_warehouse_carries_the_choices(picky_catalog: str, capsys) -> None:
    error = failed(run(capsys, ["--no-env", "--uri", picky_catalog, "--list", "--json"]), "NeedsWarehouse")
    assert error["warehouses"] == ["wh-a", "wh-b"]
    assert "--warehouse" in error["message"]


# -- failures ----------------------------------------------------------------------------------------


def test_a_table_that_is_not_there(lake: Lake, capsys) -> None:
    error = failed(run(capsys, argv(lake, "--pane", "overview", "demo.nope", "--json")), "IcelensError")
    assert "demo.nope" in error["message"]


def test_a_table_whose_metadata_has_gone(shaped: Lake, capsys) -> None:
    failed(run(capsys, argv(shaped, "--pane", "files", "demo.broken", "--json")), "IcelensError")


def test_a_catalog_that_is_not_listening_and_whose_address_carries_a_password(capsys) -> None:
    """Refused at once - nothing listens on port 1 - and the password is not in what comes back."""
    started = time.monotonic()
    ran = run(capsys, ["--no-env", "--uri", "http://alice:hunter2@127.0.0.1:1/catalog", "--list", "--json"])
    error = failed(ran, "CatalogUnreachable")
    assert "hunter2" not in ran.err
    assert "unreachable" in error["message"]
    assert time.monotonic() - started < 30


@pytest.mark.parametrize(
    ("args", "flag"),
    [
        (["--limit", "-5", "--pane", "data", "demo.events"], "--limit"),
        (["--limit", "many", "--pane", "data", "demo.events"], "--limit"),
        (["--pane", "nonsense", "demo.events"], "--pane"),
        (["--pane", "overview"], "--pane needs a table"),
        (["--snapshot", "latest", "--pane", "overview", "demo.events"], "--snapshot"),
        (["--no-such-flag"], "--no-such-flag"),
    ],
)
def test_a_usage_error_is_json_too_and_exits_2(lake: Lake, capsys, args: list[str], flag: str) -> None:
    """A program that asked for JSON should not have to read English for one class of mistake."""
    error = failed(run(capsys, argv(lake, *args, "--json")), "UsageError", code=2)
    assert flag in error["message"]


def test_asking_for_the_ui_in_a_pipe_is_a_usage_error(lake: Lake, capsys) -> None:
    error = failed(run(capsys, argv(lake, "--json")), "UsageError", code=2)
    assert "not a terminal" in error["message"]


def test_json_is_recognised_however_argparse_would_abbreviate_it(lake: Lake, capsys) -> None:
    for spelling in ("--js", "--jso"):
        failed(run(capsys, argv(lake, "--pane", "nonsense", spelling)), "UsageError", code=2)


def test_an_interrupt_exits_130_and_prints_nothing_half_done(lake: Lake, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens import model

    def interrupted(view: Any) -> Any:
        raise KeyboardInterrupt

    monkeypatch.setitem(model.PANES, "files", interrupted)
    ran = run(capsys, argv(lake, "--pane", "files", "demo.events", "--json"))
    assert (ran.code, ran.out) == (130, "")


# -- the kinds that need DuckDB ----------------------------------------------------------------------


@pytest.mark.duckdb_extensions
def test_sql(lake: Lake, capsys) -> None:
    sql = "SELECT 1::BIGINT AS n, 2.5::DECIMAL(4, 1) AS d, DATE '2026-03-02' AS day, 'nan'::DOUBLE AS f, NULL AS nothing"
    ran = run(capsys, argv(lake, "--sql", sql, "--json"))
    assert ran.code == 0, ran.err
    body = ran.body
    envelope(body, "sql", {"sql": STR, "columns": LIST})
    assert body["sql"] == sql
    assert body["rows"] == [{"n": 1, "d": "2.5", "day": "2026-03-02", "f": "NaN", "nothing": None}]
    assert [c["type"] for c in body["columns"]] == ["int64", "decimal128(4, 1)", "date32[day]", "double", "int32"]


@pytest.mark.duckdb_extensions
def test_a_refused_statement(lake: Lake, capsys) -> None:
    error = failed(run(capsys, argv(lake, "--sql", "DROP TABLE demo.events", "--json")), "IcelensError")
    assert error["message"].startswith("refusing to run DROP")


@pytest.mark.duckdb_extensions
def test_written(tmp_path: Path, capsys) -> None:
    common = ["--sandbox", "contract", "--sandbox-dir", str(tmp_path / "sandboxes")]
    ran = run(
        capsys,
        [*common, "--write", "CREATE TABLE demo.s AS SELECT i AS id FROM range(4) t(i); DELETE FROM demo.s WHERE id < 1", "--json"],
    )
    assert ran.code == 0, ran.err
    envelope(ran.body, "written", {})
    for row in ran.body["rows"]:
        conforms(row, WRITTEN_ROW, "written")
    assert [(r["kind"], r["operation"], r["added"], r["deleted"], r["records"]) for r in ran.body["rows"]] == [
        ("create", "append", 4, 0, 4),
        ("delete", "overwrite", 3, 4, 3),
    ]


@pytest.mark.duckdb_extensions
def test_a_write_that_fails_partway_carries_what_it_committed(tmp_path: Path, capsys) -> None:
    """The first statement committed and stays committed, so its record is part of the answer:
    `error.written`, in the row shape `written` has, beside the error that stopped the rest."""
    common = ["--sandbox", "contract", "--sandbox-dir", str(tmp_path / "sandboxes")]
    sql = "CREATE TABLE demo.s AS SELECT 1 AS id; INSERT INTO demo.s SELECT 'x'"
    error = failed(run(capsys, [*common, "--write", sql, "--json"]), "IcelensError")
    assert [(row["kind"], row["table"], row["added"]) for row in error["written"]] == [("create", "demo.s", 1)]
    assert "append on demo.s failed" in error["message"]
    # and a person is shown the same: the committed line on stdout, then the failure
    ran = run(capsys, [*common, "--write", "INSERT INTO demo.s SELECT 2; INSERT INTO demo.s SELECT 'y'"])
    assert ran.code == 1
    assert ran.out.startswith("append on demo.s · +1 rows · snapshot ")
    assert "append on demo.s failed" in ran.err and "1 earlier statement committed" in ran.err
    # a write that fails at its first statement committed nothing, and says nothing about it
    error = failed(run(capsys, [*common, "--write", "INSERT INTO demo.s SELECT 'z'", "--json"]), "IcelensError")
    assert "written" not in error


SERVE_ROW = {
    "sandbox": STR,
    "format_version": INT,
    "uri": STR,
    "warehouse": STR,
    "token": STR,
    "warehouse_path": STR,
    "pyiceberg": DICT,
    "duckdb": STR,
}


def test_serve(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens import cli

    monkeypatch.setattr(cli, "_wait_for_interrupt", lambda: None)
    ran = run(capsys, ["--sandbox", "contract", "--sandbox-dir", str(tmp_path / "sandboxes"), "--serve", "--json"])
    assert ran.code == 0, ran.err
    envelope(ran.body, "serve", {"note": STR})
    (row,) = ran.body["rows"]
    conforms(row, SERVE_ROW, "serve")
    conforms(row["pyiceberg"], {"type": STR, "uri": STR, "warehouse": STR, "token": STR}, "serve.pyiceberg")

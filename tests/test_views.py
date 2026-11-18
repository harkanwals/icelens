"""Iceberg views: kept by icelens's own catalog, and shown beside the tables.

A view is a name for a query, with versions of SQL where a table has snapshots. Spark 4.1 and
Trino write them through a REST catalog; before this, icelens's catalog had no view endpoints,
so `--serve` refused them, and its tree showed only tables, so a view anyone else made was
invisible. The writers here are PyIceberg and raw HTTP shaped as Spark's client sends it
(measured: Spark 4.1.1 with Iceberg 1.12.0, CREATE / CREATE OR REPLACE / ALTER / RENAME VIEW).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from harness import note, settle
from headless import run
from textual.widgets import Tree

from icelens import rest
from icelens.app import IcelensApp, NodeData
from icelens.catalog import Lake, Target


@pytest.fixture()
def server(tmp_path: Path):
    running = rest.serve(tmp_path / "sandbox")
    yield running
    running.stop()


@pytest.fixture()
def properties(server: rest.Running) -> dict[str, str]:
    return {
        "type": "rest", "uri": server.uri, "warehouse": rest.PREFIX, "token": server.token,
        "header.X-Iceberg-Access-Delegation": "",
    }


@pytest.fixture()
def made(properties: dict[str, str]) -> Lake:
    """A namespace with one table and one view of it, replaced once: two versions."""
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField
    from pyiceberg.view.metadata import SQLViewRepresentation, ViewVersion

    catalog = RestCatalog("t", **properties)
    catalog.create_namespace("core")
    catalog.create_table("core.events", schema=Schema(NestedField(1, "id", LongType(), required=False)))
    version = ViewVersion(
        version_id=1, schema_id=0, timestamp_ms=0, summary={"engine-name": "spark"}, default_namespace=["core"],
        representations=[SQLViewRepresentation(type="sql", sql="SELECT id FROM events WHERE id > 1", dialect="spark")],
    )
    catalog.create_view("core.recent", Schema(NestedField(1, "id", LongType(), required=False)), version)
    # Replaced the way Spark's CREATE OR REPLACE VIEW does it: a new schema and version, each
    # named -1, "the one added in this commit", in one POST.
    replace = {
        "requirements": [{"type": "assert-view-uuid", "uuid": str(catalog.load_view("core.recent").metadata.view_uuid)}],
        "updates": [
            {"action": "add-schema", "schema": {"type": "struct", "schema-id": 1, "fields": [
                {"id": 1, "name": "id", "required": False, "type": "long"},
                {"id": 2, "name": "name", "required": False, "type": "string"}]}, "last-column-id": 2},
            {"action": "add-view-version", "view-version": {
                "version-id": 2, "schema-id": -1, "timestamp-ms": 1, "summary": {"engine-name": "trino"},
                "default-namespace": ["core"],
                "representations": [
                    {"type": "sql", "sql": "SELECT id, 'x' AS name FROM events", "dialect": "spark"},
                    {"type": "sql", "sql": "SELECT id, 'x' AS name FROM core.events", "dialect": "trino"},
                ]}},
            {"action": "set-current-view-version", "view-version-id": -1},
        ],
    }
    assert _post(properties, "/v1/sandbox/namespaces/core/views/recent", replace)[0] == 200
    return Lake(Target(properties=dict(properties), origin="tests"))


def _post(properties: dict[str, str], path: str, body: object) -> tuple[int, dict]:
    request = urllib.request.Request(
        properties["uri"] + path, method="POST", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {properties['token']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def _args(properties: dict[str, str]) -> list[str]:
    args = ["--no-env", "--uri", properties["uri"], "--warehouse", properties["warehouse"]]
    for key in ("token", "header.X-Iceberg-Access-Delegation"):
        args += ["-p", f"{key}={properties[key]}"]
    return args


# -- the catalog -----------------------------------------------------------------------------


def test_the_catalog_keeps_views_apart_from_tables(made: Lake, properties: dict[str, str]) -> None:
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.exceptions import NamespaceNotEmptyError, NoSuchTableError

    catalog = RestCatalog("t", **properties)
    assert catalog.list_views("core") == [("core", "recent")]
    assert catalog.list_tables("core") == [("core", "events")]
    with pytest.raises(NoSuchTableError):
        catalog.load_table("core.recent")
    status, body = _post(properties, "/v1/sandbox/namespaces/core/views", {
        "name": "events", "schema": {"type": "struct", "fields": []},
        "view-version": {"version-id": 1, "schema-id": 0, "summary": {}, "representations": [], "default-namespace": []}})
    assert (status, body["error"]["type"]) == (409, "AlreadyExistsException")  # a table has the name
    catalog.drop_table("core.events")
    with pytest.raises(NamespaceNotEmptyError):
        catalog.drop_namespace("core")  # the view is still in it
    catalog.drop_view("core.recent")
    catalog.drop_namespace("core")


def test_a_replace_appends_a_version_and_a_stale_one_is_refused(made: Lake, properties: dict[str, str]) -> None:
    meta = made.view("core.recent").metadata
    assert [v.version_id for v in meta.versions] == [1, 2]
    assert meta.current_version_id == 2
    assert [s.schema_id for s in meta.schemas] == [0, 1]
    assert [v.schema_id for v in meta.versions] == [0, 1]
    status, _ = _post(properties, "/v1/sandbox/namespaces/core/views/recent", {
        "requirements": [{"type": "assert-view-uuid", "uuid": "00000000-0000-0000-0000-000000000000"}],
        "updates": [{"action": "set-properties", "updates": {"k": "v"}}]})
    assert status == 409


def test_a_view_renames_and_a_table_of_that_name_does_not_move(made: Lake, properties: dict[str, str]) -> None:
    move = {"source": {"namespace": ["core"], "name": "recent"}, "destination": {"namespace": ["core"], "name": "latest"}}
    assert _post(properties, "/v1/sandbox/views/rename", move)[0] == 204
    assert made.views("core") == ["core.latest"]
    table = {"source": {"namespace": ["core"], "name": "latest"}, "destination": {"namespace": ["core"], "name": "x"}}
    assert _post(properties, "/v1/sandbox/tables/rename", table)[0] == 404
    assert made.tables("core") == ["core.events"]


def test_a_catalog_that_keeps_no_views_lists_none(lake: Lake) -> None:
    # PyIceberg's SQL catalog has no views: an empty list, not a red pane.
    assert lake.views("demo") == []


# -- what icelens shows ----------------------------------------------------------------------


def test_a_view_shows_every_version_and_dialect(made: Lake, properties: dict[str, str], capsys) -> None:
    ran = run(capsys, [*_args(properties), "--pane", "overview", "core.recent", "--json"])
    assert ran.code == 0, ran.err
    body = json.loads(ran.out)
    assert [(r["version"], r["current"], r["dialect"], r["engine"]) for r in body["rows"]] == [
        (2, True, "spark", "trino"), (2, True, "trino", "trino"), (1, False, "spark", "spark"),
    ]
    assert body["rows"][0]["columns"] == "id long, name string"
    assert body["rows"][2]["sql"] == "SELECT id FROM events WHERE id > 1"
    assert body["note"] == "view core.recent - version 2 of 2, in spark, trino"
    refused = run(capsys, [*_args(properties), "--pane", "data", "core.recent"])
    assert refused.code != 0
    assert "core.recent is a view: it has a definition, not data - use --pane overview" in refused.err


def test_listing_a_namespace_names_its_views_without_changing_the_rows(made: Lake, properties: dict[str, str], capsys) -> None:
    body = json.loads(run(capsys, [*_args(properties), "--list", "core", "--json"]).out)
    assert [row["table"] for row in body["rows"]] == ["events"]
    assert body["note"].endswith("; 1 view: recent")


async def test_the_tree_shows_a_view_and_selecting_it_shows_its_definition(made: Lake) -> None:
    app = IcelensApp(made, limit=10, width=40)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        [namespace] = tree.root.children
        leaves = [(n.data.kind, n.data.name, str(n.label)) for n in namespace.children if isinstance(n.data, NodeData)]
        assert leaves == [("table", "core.events", "events"), ("view", "core.recent", "recent  view")]
        await pilot.press("down", "down", "down")  # namespace, table, view
        await settle(app, pilot)
        assert app.view_ident == "core.recent" and app.ident is None
        assert note(app, "overview").startswith("view core.recent - version 2 of 2")
        await pilot.press("up")  # back onto the table: its own panes again
        await settle(app, pilot)
        assert app.view_ident is None and app.ident == "core.events"


# -- a column type DuckDB cannot read ----------------------------------------------------------


@pytest.mark.duckdb_extensions
def test_a_table_duckdb_cannot_read_is_read_by_pyiceberg_and_says_so(properties: dict[str, str]) -> None:
    # Trino 484 writes `timestamptz_ns`; DuckDB 1.5.5 fails every query on such a table with a
    # bare "Unrecognized primitive type", even one that does not select the column, which used
    # to be the Data pane's answer. PyIceberg reads it. Empty here: no writer in this suite can
    # put a row in a v3 table with one, and the failure is in DuckDB's reading of the schema.
    from pyiceberg.catalog.rest import RestCatalog
    from pyiceberg.schema import Schema
    from pyiceberg.types import IntegerType, NestedField, TimestamptzNanoType

    from icelens.model import PANES
    from icelens.model import View as Seen

    catalog = RestCatalog("t", **properties)
    catalog.create_namespace("core")
    schema = Schema(NestedField(1, "id", IntegerType(), required=False), NestedField(2, "tz", TimestamptzNanoType(), required=False))
    catalog.create_table("core.ns", schema=schema, properties={"format-version": "3"})
    lake = Lake(Target(properties=dict(properties), origin="tests"))
    rows, engine = lake.rows("core.ns")
    assert rows.column_names == ["id", "tz"]
    assert engine == "pyiceberg (duckdb could not read it: Unrecognized primitive type: timestamptz_ns)"
    assert PANES["data"](Seen(lake, "core.ns")).data.num_rows == 0

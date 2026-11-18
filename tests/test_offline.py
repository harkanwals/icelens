"""The third route to the rows: no DuckDB extension, so PyIceberg reads them itself.

The README promises that a machine with no network still works: metadata panes as usual, rows
from a PyIceberg scan, and the status bar saying which. Nothing exercised that promise, because
every machine the suite had run on had either downloaded the extension or failed to. So here it
is withheld on purpose - DuckDB pointed at empty extension directories, see `tests/offline.py` -
against the same real catalog the rest of the suite reads, and every surface is driven: the
model, the headless CLI, `--json`, and the app.
"""

from __future__ import annotations

import json
import sys

import duckdb
import pytest
from harness import note, rows, settle, unbroken
from offline import withheld
from textual.widgets import Static, TextArea

from icelens import model
from icelens.app import IcelensApp, PaneBox
from icelens.catalog import IcelensError, Lake, one_statement, only_reads, route, sandbox_target
from icelens.cli import main
from icelens.model import View

#: what the session `lake` holds: ids 1..60 appended in two commits, then id < 6 deleted
LIVE_IDS = list(range(6, 61))


@pytest.fixture()
def offline(lake: Lake, tmp_path, monkeypatch: pytest.MonkeyPatch) -> Lake:
    """The session catalog, opened by a DuckDB that has no extensions and cannot fetch any."""
    monkeypatch.setattr(duckdb, "connect", withheld(tmp_path / "duckdb"))
    fresh = Lake(lake.target)
    yield fresh
    fresh.close()


def argv(lake: Lake, *rest: str) -> list[str]:
    out = ["--no-env"]
    for key, value in lake.target.properties.items():
        out += ["-p", f"{key}={value}"]
    return [*out, *rest]


# -- the lake ------------------------------------------------------------------------------


def test_the_lake_says_which_route_it_took_and_why(offline: Lake) -> None:
    assert offline.mode == "pyiceberg"
    assert offline.engine == "pyiceberg"
    # DuckDB's own words, so a person can tell "not downloaded" from "not allowed"
    assert "iceberg" in (offline.attach_error or "")


@pytest.mark.parametrize("pane", [name for name in model.PANES if name != "data"])
def test_the_metadata_panes_do_not_depend_on_the_route(lake: Lake, offline: Lake, pane: str) -> None:
    """Metadata is PyIceberg's either way, so the typed rows are identical whichever route reads rows.

    Overview is the exception that proves it: the one fact that differs is the engine it names.
    """
    there = model.PANES[pane](View(lake, "demo.events", limit=10)).records()
    here = model.PANES[pane](View(offline, "demo.events", limit=10)).records()
    if pane == "overview":
        assert here[0].pop("engine") == "pyiceberg"
        there[0].pop("engine")
    assert here == there


def test_the_rows_come_from_pyiceberg_and_are_the_right_rows(offline: Lake) -> None:
    pane = model.data(View(offline, "demo.events", limit=1000))
    assert sorted(pane.data.column("id").to_pylist()) == LIVE_IDS
    assert "via pyiceberg" in pane.note


def test_the_limit_is_obeyed_including_zero(offline: Lake) -> None:
    assert model.data(View(offline, "demo.events", limit=7)).data.num_rows == 7
    nothing = model.data(View(offline, "demo.events", limit=0))
    assert nothing.data.num_rows == 0
    assert nothing.data.column_names == ["id", "name", "ingest_date", "measured_at"]


def test_time_travel_works_without_duckdb(offline: Lake) -> None:
    first = model.ordered_snapshots(offline.table("demo.events").metadata)[0]
    old = model.data(View(offline, "demo.events", snapshot_id=first.snapshot_id, limit=1000))
    assert sorted(old.data.column("id").to_pylist()) == list(range(1, 51))
    assert f"as of snapshot {first.snapshot_id}" in old.note


def test_a_where_clause_is_handed_to_pyiceberg(offline: Lake) -> None:
    pane = model.data(View(offline, "demo.events", limit=1000, where="id > 55"))
    assert sorted(pane.data.column("id").to_pylist()) == [56, 57, 58, 59, 60]
    assert "WHERE id > 55" in pane.note


def test_sql_still_answers_over_pyiceberg_scans(offline: Lake) -> None:
    """No iceberg extension is no longer no SQL: `tests/test_arrow_sql.py` has the rest."""
    assert offline.sql_route == "arrow"
    assert offline.query('SELECT count(*) AS n FROM icelens."demo"."events"').column("n").to_pylist() == [len(LIVE_IDS)]
    assert offline.mode == "pyiceberg"  # the rows' route is unchanged by it


def test_a_v3_table_still_reads_and_says_lineage_is_out_of_reach(v3_lake: Lake, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(duckdb, "connect", withheld(tmp_path / "duckdb"))
    fresh = Lake(v3_lake.target)
    try:
        pane = model.data(View(fresh, "demo.events", limit=10, lineage=True))
    finally:
        fresh.close()
    assert pane.data.num_rows == 10
    assert "_row_id" not in pane.data.column_names
    assert "pyiceberg (no row lineage here)" in pane.note


def test_the_sandbox_cannot_write_and_says_so_rather_than_tracing_back(tmp_path, monkeypatch) -> None:
    """Writing is DuckDB's half of the split, so with no DuckDB to start there is no write.

    The refusal is the point: it names DuckDB and DuckDB's reason, rather than surfacing as an
    `IOException` from somewhere inside a commit.
    """
    monkeypatch.setattr(duckdb, "connect", withheld(tmp_path / "duckdb"))
    sandbox = Lake(sandbox_target("offline", root=tmp_path))
    try:
        with pytest.raises(IcelensError, match="writing needs DuckDB: .*iceberg"):
            sandbox.write("CREATE TABLE demo.t AS SELECT 1 AS x")
        assert sandbox.write("SELECT 1") == []  # a read commits nothing, extension or not
    finally:
        sandbox.close()


# -- the CLI and its JSON --------------------------------------------------------------------


def test_the_data_pane_prints_from_pyiceberg(offline: Lake, capsys) -> None:
    assert main(argv(offline, "--pane", "data", "demo.events", "--limit", "3")) == 0
    out = capsys.readouterr()
    assert "via pyiceberg" in out.err
    assert out.out.count("row ") == 3


def test_the_json_is_the_same_shape_whichever_route_answered(lake: Lake, offline: Lake, capsys) -> None:
    """A caller should not have to know which engine answered to read the answer.

    The note names the engine, and that is the only difference: the column types, the row
    values and the envelope are identical. (Run with the extensions available, the left-hand
    side is the DuckDB route; without them, both sides are PyIceberg and this still holds.)
    """
    shape = ("--pane", "data", "demo.events", "--where", "id > 55", "--json")
    assert main(argv(offline, *shape)) == 0
    here = json.loads(capsys.readouterr().out)
    assert main(argv(lake, *shape)) == 0
    there = json.loads(capsys.readouterr().out)
    assert "via pyiceberg" in here.pop("note")
    there.pop("note")
    key = lambda row: row["id"]  # noqa: E731 - the routes are free to return rows in any order
    here["rows"].sort(key=key)
    there["rows"].sort(key=key)
    assert here == there


def test_sql_without_the_extension_prints_the_answer(offline: Lake, capsys) -> None:
    assert main(argv(offline, "--sql", "SELECT max(id) AS top FROM demo.events", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["rows"] == [{"top": 60}]


@pytest.mark.parametrize("where", ["id >>> banana", "no_such_column = 1", "false; SELECT 1"])
def test_a_bad_where_on_the_pyiceberg_route_is_an_error_not_a_traceback(offline: Lake, capsys, where: str) -> None:
    """It used to escape main() as a ParseException or a ValueError, where the DuckDB route
    exits 1 with one line. Now both routes refuse a filter the same way, and this one says
    whose grammar it was - PyIceberg's is not SQL."""
    assert main(argv(offline, "--pane", "data", "demo.events", "--where", where, "--json")) == 1
    out = capsys.readouterr()
    assert out.out == ""
    error = json.loads(out.err)["error"]
    assert error["kind"] == "IcelensError"
    assert error["message"].startswith(f"PyIceberg cannot filter on {where!r}")


def test_a_filter_pyiceberg_can_read_still_filters(offline: Lake, capsys) -> None:
    assert main(argv(offline, "--pane", "data", "demo.events", "--where", "id >= 50", "--json")) == 0
    ids = [row["id"] for row in json.loads(capsys.readouterr().out)["rows"]]
    assert ids and min(ids) >= 50


# -- the app -------------------------------------------------------------------------------


async def test_the_app_reads_rows_and_says_it_could_not_use_duckdb(offline: Lake) -> None:
    app = IcelensApp(offline, limit=10, width=30)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        status = str(app.query_one("#status", Static).content)
        assert "pyiceberg, pyiceberg scans" in status
        assert "duckdb unavailable" in status
        assert "SQL over pyiceberg scans" in status

        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        assert rows(app, "data") == 10
        assert "via pyiceberg" in note(app, "data")

        await pilot.press("0")
        app.query_one("#sql", TextArea).text = "SELECT id FROM demo.events WHERE id > 58"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert rows(app, "sql") == 2
        assert not app.query_one("#box-sql", PaneBox).query_one(Static).has_class("error")
        unbroken(app, "the pyiceberg route")


# -- no DuckDB module at all ------------------------------------------------------------------


def test_without_the_duckdb_module_nothing_pretends_to_have_checked(lake: Lake, monkeypatch) -> None:
    """DuckDB is a dependency, but a broken install is a real machine. The guards degrade honestly.

    `one_statement` and `only_reads` pass SQL through when there is no parser to ask, which is
    safe only because nothing can run it either: `query` refuses, `route` refuses, and the rows
    still arrive from PyIceberg.
    """
    monkeypatch.setitem(sys.modules, "duckdb", None)  # `import duckdb` now raises ImportError
    assert one_statement("SELECT 1; SELECT 2") == "SELECT 1; SELECT 2"
    assert only_reads("DROP TABLE x") == "DROP TABLE x"
    with pytest.raises(IcelensError, match="writing needs DuckDB, which is not installed here"):
        route("INSERT INTO demo.t SELECT 1")
    fresh = Lake(lake.target, read_only=True)
    try:
        assert fresh.mode == "pyiceberg"
        assert "duckdb" in fresh.attach_error and "Error" in fresh.attach_error.split(":")[0]
        with pytest.raises(IcelensError, match="SQL needs DuckDB"):
            fresh.query("DROP TABLE x")
        assert model.data(View(fresh, "demo.events", limit=5)).data.num_rows == 5
    finally:
        fresh.close()

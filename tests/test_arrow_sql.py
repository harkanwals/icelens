"""SQL where DuckDB runs but cannot load its iceberg extension: the `arrow` route.

That is the browser: Pyodide has DuckDB, and extensions.duckdb.org publishes no iceberg extension
for it. So each table a query names is scanned by PyIceberg and copied into a plain DuckDB under
the name the other routes give it. Everything here runs with the extensions withheld the way
`tests/offline.py` withholds them - DuckDB pointed at empty extension directories - so it runs,
and means the same thing, on every machine.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

import duckdb
import pytest
from headless import argv, clean_environment, run
from offline import withheld
from test_json_contract import LIST, STR, envelope

from icelens import demo
from icelens.catalog import IcelensError, Lake, _name_parts, sandbox_target


@pytest.fixture()
def no_extensions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duckdb, "connect", withheld(tmp_path / "duckdb"))


@pytest.fixture()
def offline(lake: Lake, no_extensions: None) -> Lake:
    """The session catalog, opened by a DuckDB that has no extensions and cannot fetch any."""
    fresh = Lake(lake.target)
    yield fresh
    fresh.close()


@pytest.fixture()
def sandbox(tmp_path: Path, no_extensions: None) -> Lake:
    """A sandbox holding the demo table, which this test may append to and break."""
    lake = Lake(sandbox_target("arrow", tmp_path / "sandboxes"))
    demo.seed(lake)
    yield lake
    lake.close()


def scans(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("route: PyIceberg scanned")]


# -- answers ---------------------------------------------------------------------------------


def test_the_route_is_named_and_the_rows_route_is_not_moved(offline: Lake) -> None:
    assert offline.sql_route == "arrow"
    offline.query("SELECT 1")
    assert (offline.mode, offline.engine) == ("pyiceberg", "pyiceberg")


@pytest.mark.duckdb_extensions
def test_with_the_extensions_the_route_is_the_mode(lake: Lake) -> None:
    # A fresh lake: the session's may first have met DuckDB inside a test that withheld it.
    fresh = Lake(lake.target)
    try:
        assert fresh.sql_route == fresh.mode == "bridge"
    finally:
        fresh.close()


def test_a_group_by_agrees_with_pyiceberg(offline: Lake) -> None:
    sql = 'SELECT ingest_date, count(*) AS n FROM icelens."demo"."events" GROUP BY ingest_date'
    answered = {row["ingest_date"]: row["n"] for row in offline.query(sql).to_pylist()}
    own = Counter(offline.table("demo.events").scan().to_arrow().column("ingest_date").to_pylist())
    assert answered == dict(own)
    assert sum(answered.values()) == 55


def test_a_join_of_two_tables_written_the_short_way(offline: Lake) -> None:
    """`USING` is what `get_table_names` cannot bind before the tables exist; the parser can."""
    joined = offline.query(
        "SELECT count(*) AS n, count(e.id) AS matched FROM demo.events LEFT JOIN demo.empty e USING (id)"
    )
    assert joined.to_pylist() == [{"n": 55, "matched": 0}]


def test_the_alias_form_and_the_namespace_form_are_the_same_table(offline: Lake) -> None:
    both = offline.query('SELECT (SELECT max(id) FROM icelens.demo.events) AS a, (SELECT max(id) FROM "demo".events) AS b')
    assert both.to_pylist() == [{"a": 60, "b": 60}]


def test_explain_and_describe_find_their_table(offline: Lake) -> None:
    assert "id" in offline.query("DESCRIBE demo.events").column("column_name").to_pylist()
    assert offline.query("EXPLAIN SELECT * FROM demo.events").num_rows >= 1


def test_a_missing_table_is_duckdbs_catalog_error_in_words(offline: Lake) -> None:
    with pytest.raises(IcelensError, match="Catalog Error: Table with name nope does not exist") as caught:
        offline.query("SELECT * FROM demo.nope")
    assert "Traceback" not in str(caught.value)


# -- guards ----------------------------------------------------------------------------------


def test_sql_cannot_read_the_machines_files(offline: Lake, tmp_path: Path) -> None:
    # The route's DuckDB holds only copies of scanned tables; it never needs the file system, so
    # SQL typed into the pane must not reach it either.
    secret = tmp_path / "secret.txt"
    secret.write_text("do not show")
    with pytest.raises(IcelensError, match="disabled") as caught:
        offline.query(f"SELECT content FROM read_text('{secret}')")
    assert "do not show" not in str(caught.value)
    assert offline.query("SELECT count(*) AS n FROM demo.events").column("n").to_pylist() == [55]  # and SQL still works


def test_a_read_only_lake_refuses_a_write_before_anything_is_scanned(lake: Lake, no_extensions, caplog) -> None:
    guarded = Lake(lake.target, read_only=True)
    try:
        with caplog.at_level(logging.INFO, "icelens"), pytest.raises(IcelensError, match="refusing to run DROP.*--allow-writes"):
            guarded.query("DROP TABLE demo.events")
        assert scans(caplog) == []
        assert guarded.table("demo.events") is not None
    finally:
        guarded.close()


def test_a_second_statement_that_writes_is_refused(lake: Lake, no_extensions) -> None:
    guarded = Lake(lake.target, read_only=True)
    try:
        with pytest.raises(IcelensError, match="refusing to run DELETE"):
            guarded.query("SELECT * FROM demo.events; DELETE FROM demo.events")
    finally:
        guarded.close()


def test_a_write_on_an_unguarded_lake_is_refused_because_it_would_land_in_a_copy(sandbox: Lake) -> None:
    before = sandbox.table("demo.orders", refresh=True).metadata.current_snapshot_id
    with pytest.raises(IcelensError, match="refusing to run DELETE: SQL here runs over copies"):
        sandbox.query("DELETE FROM demo.orders")
    assert sandbox.table("demo.orders", refresh=True).metadata.current_snapshot_id == before


# -- the copies ------------------------------------------------------------------------------


def test_a_copy_is_reused_until_the_snapshot_moves(sandbox: Lake, caplog) -> None:
    count = 'SELECT count(*) AS n FROM icelens."demo"."orders"'
    with caplog.at_level(logging.INFO, "icelens"):
        assert sandbox.query(count).column("n").to_pylist() == [183]
        assert sandbox.query(count).column("n").to_pylist() == [183]
        assert len(scans(caplog)) == 1

        table = sandbox.table("demo.orders", refresh=True)
        table.append(table.scan(limit=2).to_arrow())
        assert sandbox.query(count).column("n").to_pylist() == [185]
        assert len(scans(caplog)) == 2


def test_a_table_whose_metadata_is_gone_says_so_rather_than_that_it_does_not_exist(sandbox: Lake) -> None:
    gone = Path(sandbox.table("demo.orders").metadata_location.removeprefix("file://"))
    gone.unlink()
    with pytest.raises(IcelensError) as caught:
        sandbox.query("SELECT count(*) FROM demo.orders")
    assert "does not exist" not in str(caught.value) and "demo.orders" in str(caught.value)


def test_a_name_in_other_letters_is_not_answered_from_an_old_copy(sandbox: Lake) -> None:
    # DuckDB matches names whatever their case; PyIceberg's catalogs do not. `DEMO.ORDERS` is no
    # table to PyIceberg, so it must not be answered from the copy `demo.orders` left behind.
    assert sandbox.query("SELECT count(*) AS n FROM demo.orders").column("n").to_pylist() == [183]
    table = sandbox.table("demo.orders", refresh=True)
    table.append(table.scan(limit=10).to_arrow())
    with pytest.raises(IcelensError, match="does not exist"):
        sandbox.query("SELECT count(*) AS n FROM DEMO.ORDERS")
    assert sandbox.query("SELECT count(*) AS n FROM demo.orders").column("n").to_pylist() == [193]


def test_a_table_too_big_to_copy_is_refused_in_words(sandbox: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens import catalog

    monkeypatch.setattr(catalog, "ARROW_ROW_LIMIT", 100)
    with pytest.raises(IcelensError, match=r"demo.orders has 183 rows.*more than 100") as caught:
        sandbox.query("SELECT count(*) FROM demo.orders LIMIT 1")
    assert "iceberg extension" in str(caught.value)


def test_a_table_pyiceberg_cannot_scan_is_named_in_words(sandbox: Lake) -> None:
    home = Path(sandbox.target.properties["warehouse"].removeprefix("file://"))
    for parquet in home.rglob("*.parquet"):
        parquet.unlink()
    with pytest.raises(IcelensError, match="cannot read demo.orders for SQL") as caught:
        sandbox.query("SELECT count(*) FROM demo.orders")
    assert "Traceback" not in str(caught.value)


def test_a_dropped_table_is_not_answered_from_its_copy(sandbox: Lake) -> None:
    assert sandbox.query("SELECT count(*) AS n FROM demo.orders").column("n").to_pylist() == [183]
    sandbox.catalog.drop_table("demo.orders")
    with pytest.raises(IcelensError, match="Table with name orders does not exist"):
        sandbox.query("SELECT count(*) FROM demo.orders")


def test_close_forgets_the_copies(sandbox: Lake, caplog) -> None:
    with caplog.at_level(logging.INFO, "icelens"):
        sandbox.query("SELECT count(*) FROM demo.orders")
        sandbox.close()
        sandbox.query("SELECT count(*) FROM demo.orders")
    assert len(scans(caplog)) == 2


# -- the CLI ---------------------------------------------------------------------------------


@pytest.fixture()
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_extensions: None) -> Path:
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def test_sql_on_the_demo_prints_the_rows(capsys: pytest.CaptureFixture[str], home: Path) -> None:
    sql = "SELECT region, count(*) AS n FROM demo.orders GROUP BY region ORDER BY region"
    ran = run(capsys, ["--demo", "--sandbox-dir", str(home / "s"), "--sql", sql, "--json"])
    assert ran.code == 0, ran.err
    body = ran.body
    envelope(body, "sql", {"sql": STR, "columns": LIST})
    assert body["sql"] == sql
    assert sum(row["n"] for row in body["rows"]) == 183
    assert [c["name"] for c in body["columns"]] == ["region", "n"]


def test_sql_refuses_a_write_through_the_cli_as_before(lake: Lake, capsys, no_extensions) -> None:
    ran = run(capsys, argv(lake, "--sql", "DROP TABLE demo.events", "--json"))
    assert ran.code == 1 and ran.error["error"]["message"].startswith("refusing to run DROP")


def test_the_trace_names_the_route(lake: Lake, capsys, tmp_path: Path, no_extensions) -> None:
    trace_file = tmp_path / "trace.log"
    ran = run(capsys, argv(lake, "--trace", str(trace_file), "--sql", "SELECT count(*) FROM demo.events"))
    assert ran.code == 0, ran.err
    logged = trace_file.read_text()
    assert "route: SQL runs in DuckDB over PyIceberg's scans" in logged
    assert "route: PyIceberg scanned demo.events" in logged


# -- reading DuckDB's spelling of a name -------------------------------------------------------


@pytest.mark.parametrize(
    ("spelled", "parts"),
    [
        ("icelens.demo.orders", ["icelens", "demo", "orders"]),
        ('icelens.demo.orders AS "my o"', ["icelens", "demo", "orders"]),
        ('icelens."a.b"."we""ird" AS w', ["icelens", "a.b", 'we"ird']),
        ("demo.orders", ["demo", "orders"]),
    ],
)
def test_get_table_names_spelling_is_undone(spelled: str, parts: list[str]) -> None:
    """The fallback for a DuckDB without json_serialize_sql; spellings measured on 1.5.1 and 1.5.5."""
    assert _name_parts(spelled) == parts

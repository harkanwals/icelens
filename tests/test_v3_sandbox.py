"""A v3 sandbox: icelens's own REST catalog, so DuckDB can write format-version 3 offline.

Until this existed, deletion vectors and row lineage - the v3 material - needed a Lakekeeper
or a Polaris running somewhere. Now `icelens --sandbox NAME --v3` serves the sandbox's own
SQLite catalog over Iceberg REST on 127.0.0.1, and the Writer pane, `--write` and `--demo`
write it through DuckDB. These tests drive it the way a person does: through the CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from headless import clean_environment, run

from icelens import demo
from icelens.catalog import Lake, list_sandboxes, sandbox_target, sandbox_version, scrub

pytestmark = pytest.mark.duckdb_extensions


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture()
def where(tmp_path: Path) -> list[str]:
    return ["--sandbox-dir", str(tmp_path / "sandboxes")]


def _ledger(capsys: pytest.CaptureFixture[str], args: list[str], sql: str) -> list[dict]:
    ran = run(capsys, [*args, "--write", sql, "--json"])
    assert ran.code == 0, ran.err
    return json.loads(ran.out)["rows"]


def test_a_v3_sandbox_writes_v3_with_duckdb(capsys: pytest.CaptureFixture[str], where: list[str], tmp_path: Path) -> None:
    args = ["--sandbox", "lesson", "--v3", *where]
    [made] = _ledger(capsys, args, "CREATE TABLE demo.sales AS SELECT i AS id, i % 4 AS bucket FROM range(1, 101) t(i)")
    assert (made["operation"], made["added"], made["engine"]) == ("append", 100, "duckdb through the catalog")
    lake = Lake(sandbox_target("lesson", tmp_path / "sandboxes"))
    try:
        assert lake.table("demo.sales").metadata.format_version == 3
    finally:
        lake.close()


def test_a_create_as_writes_nothing_where_icelens_was_started(
    capsys: pytest.CaptureFixture[str], where: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DuckDB's iceberg extension, given CREATE TABLE … AS whole, makes a `data` directory in the
    # current one and fails where it cannot. With a file of that name in the way it could not.
    started = tmp_path / "started"
    started.mkdir()
    (started / "data").write_text("mine")
    monkeypatch.chdir(started)
    args = ["--sandbox", "lesson", "--v3", *where]
    [made] = _ledger(capsys, args, "CREATE TABLE demo.sales AS SELECT i AS id, 'x' AS \"we ird\", 1.5::DECIMAL(10, 2) AS d FROM range(1, 11) t(i)")
    assert (made["kind"], made["operation"], made["added"]) == ("create", "append", 10)
    assert [p.name for p in started.iterdir()] == ["data"] and (started / "data").read_text() == "mine"
    described = json.loads(run(capsys, [*args, "--pane", "schema", "demo.sales", "--json"]).out)["rows"]
    assert [row["name"] for row in described][:3] == ["id", "we ird", "d"]  # then v3's row-lineage fields
    # A comment after the query is still the query's: it used to swallow the end of the statement
    # icelens wraps it in, and the CREATE went to DuckDB whole after all.
    [noted] = _ledger(capsys, args, "CREATE TABLE demo.noted AS SELECT 1 AS id -- one row")
    assert noted["added"] == 1 and [p.name for p in started.iterdir()] == ["data"]
    # Rows that will not go in leave no empty table behind: the statement failed, all of it.
    failed = run(capsys, [*args, "--write", "CREATE TABLE demo.bad AS SELECT CAST(v AS INTEGER) AS id FROM (VALUES ('x')) t(v)"])
    assert failed.code == 1 and "Traceback" not in failed.err
    assert "bad" not in run(capsys, [*args, "--list", "demo"]).out


def test_a_table_asked_for_at_v2_is_made_v2_and_reported_as_made(
    capsys: pytest.CaptureFixture[str], where: list[str], tmp_path: Path,
) -> None:
    # The guide says a CREATE is v3 unless it asks for v2. Asking made the table and then exited 1
    # with "asked for format-version 3 and got v2", so the retry failed on "already exists".
    args = ["--sandbox", "lesson", "--v3", *where]
    [made] = _ledger(capsys, args, 'CREATE TABLE demo.old WITH ("format-version" = 2) AS SELECT range AS id FROM range(5)')
    assert (made["kind"], made["added"]) == ("create", 5)
    lake = Lake(sandbox_target("lesson", tmp_path / "sandboxes"))
    try:
        assert lake.table("demo.old").metadata.format_version == 2
    finally:
        lake.close()


def test_sql_asks_for_a_table_by_name_and_never_lists_the_namespace(
    capsys: pytest.CaptureFixture[str], where: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A login may load a table it is not allowed to list, and a big namespace is pages of listing.
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.sales AS SELECT range AS id FROM range(5)")

    def no_listing(self: Lake, namespace: str) -> list[str]:
        raise AssertionError(f"listed {namespace}")

    monkeypatch.setattr(Lake, "tables", no_listing)
    ran = run(capsys, [*args, "--sql", "SELECT count(*) AS n FROM icelens.demo.sales", "--json"])
    assert ran.code == 0 and json.loads(ran.out)["rows"] == [{"n": 5}]
    assert run(capsys, [*args, "--sql", "SELECT * FROM icelens.demo.nope", "--json"]).code == 1


def test_a_delete_leaves_a_deletion_vector_and_an_update_keeps_the_row_id(
    capsys: pytest.CaptureFixture[str], where: list[str]
) -> None:
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.sales AS SELECT i AS id, i % 4 AS bucket FROM range(1, 101) t(i)")
    [deleted] = _ledger(capsys, args, "DELETE FROM demo.sales WHERE bucket = 0")
    assert (deleted["operation"], deleted["deleted"], deleted["records"]) == ("delete", 25, 75)
    ran = run(capsys, [*args, "--pane", "files", "demo.sales", "--json"])
    files = json.loads(ran.out)["rows"]
    assert {(row["format"], row["content"]) for row in files} == {("parquet", "data"), ("puffin", "deletion vector")}
    ran = run(capsys, [*args, "--pane", "commit", "demo.sales", "--json"])
    assert {row["content"] for row in json.loads(ran.out)["rows"]} == {"deletion vector"}
    _ledger(capsys, args, "UPDATE demo.sales SET bucket = 9 WHERE id = 7")
    ran = run(capsys, [*args, "--sql", 'SELECT _row_id FROM icelens."demo"."sales" WHERE id = 7', "--json"])
    assert json.loads(ran.out)["rows"] == [{"_row_id": 6}]


def test_a_commit_counts_the_rows_its_deletion_vectors_removed(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    # A DV commit sets no `deleted-records`, and its `added-position-deletes` counts every row
    # ever deleted from each file: the note read "-0 rows" for every v3 delete.
    args = ["--sandbox", "lesson", "--v3", *where]

    def note() -> str:
        ran = run(capsys, [*args, "--pane", "commit", "demo.sales", "--json"])
        return json.loads(ran.out)["note"]

    _ledger(capsys, args, "CREATE TABLE demo.sales AS SELECT i AS id, i % 4 AS bucket FROM range(1, 101) t(i)")
    _ledger(capsys, args, "INSERT INTO demo.sales SELECT i, i % 4 FROM range(101, 121) t(i)")
    _ledger(capsys, args, "DELETE FROM demo.sales WHERE bucket = 0")  # 25 + 5, across both files
    assert "+0 rows / -30 rows" in note()
    _ledger(capsys, args, "DELETE FROM demo.sales WHERE id = 7")  # a DV that already held 25
    assert "+0 rows / -1 rows" in note()
    _ledger(capsys, args, "UPDATE demo.sales SET bucket = 9 WHERE id = 9")
    assert "+1 rows / -1 rows" in note()


def test_a_nanosecond_timestamp_reads_in_every_form(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    # Measured before the fix: the Data pane, `--json` and the TUI's cell lookup all raised
    # pyarrow's "not safely convertible to microseconds" on a v3 timestamp_ns DuckDB wrote.
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.ticks AS SELECT 1 AS id, TIMESTAMP_NS '2026-01-01 00:00:00.123456789' AS at")
    ran = run(capsys, [*args, "--pane", "data", "demo.ticks", "--json"])
    assert ran.code == 0, ran.err
    out = json.loads(ran.out)
    assert out["rows"] == [{"id": 1, "at": "2026-01-01 00:00:00.123456789"}]
    assert {"name": "at", "type": "timestamp[ns]"} in out["columns"]
    ran = run(capsys, [*args, "--pane", "data", "demo.ticks"])
    assert ran.code == 0 and "2026-01-01 00:00:00.123456789" in ran.out


def test_a_variant_table_opens_and_shows_its_rows_as_json(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    # DuckDB and Spark 4.1 write variant; PyIceberg 0.12 cannot parse it, and refused the whole
    # table, in every pane. DuckDB reads it but cannot hand it to Arrow, so not one row showed.
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.events AS SELECT 1 AS id, 42::VARIANT AS payload")
    _ledger(capsys, args, "INSERT INTO demo.events VALUES (2, 'x'::VARIANT), (3, {'a': [1, 2]}::VARIANT)")
    schema = json.loads(run(capsys, [*args, "--pane", "schema", "demo.events", "--json"]).out)
    assert {row["name"]: row["type"] for row in schema["rows"]}["payload"] == "variant"
    ran = run(capsys, [*args, "--pane", "data", "demo.events", "--json"])
    data = json.loads(ran.out)
    assert sorted((row["id"], row["payload"]) for row in data["rows"]) == [(1, "42"), (2, '"x"'), (3, '{"a":[1,2]}')]
    assert "variant shown as JSON text" in data["note"]
    for pane in ("overview", "snapshots", "commit", "manifests", "files", "partitions", "layout"):
        assert run(capsys, [*args, "--pane", pane, "demo.events"]).code == 0, pane



def test_a_variant_column_asked_for_by_name_reads_as_json(
    tmp_path: Path, where: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    # The cast covered `SELECT *` only, so naming the column - as `Lake.rows(columns=…)` does -
    # went back to "Unsupported Arrow type VARIANT".
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.named AS SELECT 1 AS id, 'x'::VARIANT AS payload")
    lake = Lake(sandbox_target("lesson", tmp_path / "sandboxes", version=3))
    rows, engine = lake.rows("demo.named", columns=["payload"])
    assert rows.to_pylist() == [{"payload": '"x"'}]
    assert engine == "duckdb (variant shown as JSON text)"


def test_a_write_is_reported_when_pyiceberg_cannot_read_the_table_back(
    capsys: pytest.CaptureFixture[str], where: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A type PyIceberg still cannot parse: icelens.opaque made to keep nothing, which is where
    # variant stood before it. The writes commit, and used to fail on the read-back after them,
    # so a CREATE that had happened read as one that had not. DuckDB counts them instead.
    import re

    from icelens import opaque

    monkeypatch.setattr(opaque, "_KEPT", re.compile(r"(?!)"))
    args = ["--sandbox", "lesson", "--v3", *where]
    [made] = _ledger(capsys, args, "CREATE TABLE demo.events AS SELECT 1 AS id, 42::VARIANT AS payload")
    assert (made["operation"], made["added"], made["records"]) == ("append", 1, 1)
    assert "read back by DuckDB: Unsupported field type: 'variant'" in made["engine"]
    [inserted] = _ledger(capsys, args, "INSERT INTO demo.events VALUES (2, 'x'::VARIANT), (3, 4::VARIANT)")
    assert (inserted["added"], inserted["records"]) == (2, 3)
    [deleted] = _ledger(capsys, args, "DELETE FROM demo.events WHERE id = 1")
    assert (deleted["operation"], deleted["deleted"], deleted["records"]) == ("delete", 1, 2)
    assert inserted["snapshot_id"] not in (made["snapshot_id"], deleted["snapshot_id"])
    ran = run(capsys, [*args, "--pane", "schema", "demo.events"])
    assert ran.code != 0
    assert "cannot load demo.events: Unsupported field type: 'variant' - icelens reads table metadata with PyIceberg" in ran.err
    assert "validation error" not in ran.err


def test_merge_runs_in_a_v3_sandbox(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    args = ["--sandbox", "lesson", "--v3", *where]
    _ledger(capsys, args, "CREATE TABLE demo.t AS SELECT i AS id, 1 AS n FROM range(1, 4) t(i)")
    _ledger(capsys, args, "MERGE INTO demo.t t USING (SELECT 2 AS id, 5 AS n UNION ALL SELECT 9, 9) s ON t.id = s.id "
                          "WHEN MATCHED THEN UPDATE SET n = s.n WHEN NOT MATCHED THEN INSERT VALUES (s.id, s.n)")
    ran = run(capsys, [*args, "--sql", 'SELECT id, n FROM icelens."demo"."t" ORDER BY id', "--json"])
    assert json.loads(ran.out)["rows"] == [{"id": 1, "n": 1}, {"id": 2, "n": 5}, {"id": 3, "n": 1}, {"id": 9, "n": 9}]


def test_a_table_still_says_its_namespace(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    ran = run(capsys, ["--sandbox", "lesson", "--v3", *where, "--write", "CREATE TABLE sales AS SELECT 1 AS x"])
    assert ran.code == 1 and "demo.sales" in ran.err


def test_the_sandbox_remembers_it_is_v3(capsys: pytest.CaptureFixture[str], where: list[str], tmp_path: Path) -> None:
    root = tmp_path / "sandboxes"
    _ledger(capsys, ["--sandbox", "lesson", "--v3", *where], "CREATE TABLE demo.a AS SELECT 1 AS x")
    assert sandbox_version(root / "lesson") == 3
    [again] = _ledger(capsys, ["--sandbox", "lesson", *where], "CREATE TABLE demo.b AS SELECT 1 AS x")  # no --v3
    assert again["engine"] == "duckdb through the catalog"
    assert run(capsys, ["--sandbox", "lesson", "--sandbox-reset", *where, "--list"]).code == 0
    assert sandbox_version(root / "lesson") == 3  # emptied, still v3
    assert [s.tables for s in list_sandboxes(root)] == [0]


def test_a_v2_sandbox_made_v3_keeps_its_tables(capsys: pytest.CaptureFixture[str], where: list[str]) -> None:
    _ledger(capsys, ["--sandbox", "old", *where], "CREATE TABLE demo.v2 AS SELECT i AS x FROM range(5) t(i)")
    [inserted] = _ledger(capsys, ["--sandbox", "old", "--v3", *where], "INSERT INTO demo.v2 SELECT 99")
    assert inserted["records"] == 6
    [made] = _ledger(capsys, ["--sandbox", "old", *where], "CREATE TABLE demo.v3 AS SELECT 1 AS x")
    ran = run(capsys, ["--sandbox", "old", *where, "--list", "demo", "--json"])
    versions = {row["table"]: row["v"] for row in json.loads(ran.out)["rows"]}
    assert versions == {"v2": 2, "v3": 3}


def test_v3_needs_a_sandbox_to_be_about(capsys: pytest.CaptureFixture[str]) -> None:
    ran = run(capsys, ["--v3", "--list"])
    assert ran.code == 1 and "--sandbox" in ran.err
    ran = run(capsys, ["--sandbox", "x", "--v3", "--rest", "--uri", "http://x.example/c"])
    assert ran.code == 1 and "--rest" in ran.err


def test_the_v3_demo_tells_the_v3_story(capsys: pytest.CaptureFixture[str], where: list[str], tmp_path: Path) -> None:
    ran = run(capsys, ["--demo", "--v3", *where, "--pane", "snapshots", "demo.orders"])
    assert ran.code == 0, ran.err
    assert "icelens: made demo.orders - 7 snapshots, 160 rows" in ran.err
    # under --json the line is not printed at all: stderr is for an error object, and there is none
    ran = run(capsys, ["--demo", "--v3", *where, "--pane", "snapshots", "demo.orders", "--json"])
    assert (ran.code, ran.err) == (0, "")
    operations = [row["operation"] for row in json.loads(ran.out)["rows"]]
    assert operations == ["append", "append", "delete", "overwrite", "append", "overwrite", "append"]
    lake = Lake(sandbox_target("demo-v3", tmp_path / "sandboxes"))
    try:
        metadata = lake.table(demo.TABLE).metadata
        assert (metadata.format_version, sorted(metadata.refs)) == (3, ["first-load", "main"])
        assert demo.seed(lake).line() == "already there: demo.orders - 7 snapshots, 160 rows"
        rows, _ = lake.rows(demo.TABLE, limit=1000, columns=["order_id"])
        assert rows.num_rows == 160
    finally:
        lake.close()


def test_the_catalogs_token_is_a_secret(tmp_path: Path) -> None:
    lake = Lake(sandbox_target("lesson", tmp_path / "sandboxes", version=3))
    try:
        token = lake.target.properties["token"]
        assert scrub(f"Authorization: Bearer {token}") == "Authorization: Bearer ********"
        assert lake.target.label == "sandbox 'lesson'" and lake.target.uri.startswith("http://127.0.0.1:")
    finally:
        lake.close()


async def test_the_app_says_v3_and_offers_the_v3_hint(tmp_path: Path) -> None:
    from textual.widgets import TextArea

    from icelens.app import IcelensApp

    lake = Lake(sandbox_target("lesson", tmp_path / "sandboxes", version=3))
    app = IcelensApp(lake, limit=5, width=30)
    try:
        async with app.run_test(size=(200, 40)) as pilot:
            await pilot.pause()
            app.status()
            assert "v3, served locally" in str(app.query_one("#status").content)
            assert "deletion vector" in app.query_one("#write", TextArea).text
    finally:
        lake.close()

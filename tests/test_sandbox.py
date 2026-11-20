"""The one place this tool writes, and the invariant that keeps it the only one.

Two things are being proved here. The first is that the writing works - three statement shapes,
through DuckDB and PyIceberg, producing real snapshots whose numbers say what Iceberg did. The
second matters more: that nothing outside a sandbox can be written to, by any route, including
the ones that exist only in the app.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

import duckdb
import pytest
from harness import note, settle

from icelens import model
from icelens.app import IcelensApp
from icelens.catalog import (
    _ATTACH_SHAPES,
    IcelensError,
    Lake,
    Target,
    _file_uri,
    _local,
    reset_sandbox,
    rest_sandbox_target,
    route,
    route_attached,
    sandbox_table,
    sandbox_target,
    strip_alias,
    with_format_version,
)
from icelens.cli import main

SETUP = "CREATE TABLE demo.sales AS SELECT i AS id, 'r' || (i % 4) AS region, i * 1.5 AS amount FROM range(1, 101) t(i)"


@pytest.fixture
def sandbox(tmp_path) -> Lake:
    """A sandbox of its own per test, asked for read-only to prove that does not hold here."""
    lake = Lake(sandbox_target("test", root=tmp_path), read_only=True)
    yield lake
    lake.close()


# -- the invariant -----------------------------------------------------------------------
def test_only_a_sandbox_can_be_written_to(lake: Lake) -> None:
    """The session-wide fixture is an ordinary catalog, and no SQL through it may write."""
    assert not lake.is_sandbox
    with pytest.raises(IcelensError, match="only to a sandbox"):
        lake.write(SETUP)


def test_a_sandbox_is_writable_even_when_asked_for_read_only(sandbox: Lake) -> None:
    """`read_only` is about somebody else's data, and a sandbox is nobody's."""
    assert sandbox.is_sandbox
    assert sandbox.read_only is False
    assert sandbox.target.sandbox == "test"


def test_a_read_only_lake_still_refuses_write_sql(lake: Lake) -> None:
    """The guard on the SQL pane is unchanged by any of this."""
    with pytest.raises(IcelensError, match="refusing to run"):
        Lake(lake.target, read_only=True).query("DELETE FROM x")


# -- routing -----------------------------------------------------------------------------
def test_the_three_shapes_are_recognised() -> None:
    writes = route(f"{SETUP}; INSERT INTO demo.sales SELECT 1, 'r0', 2.0; DELETE FROM demo.sales WHERE id < 3")
    assert [w.kind for w in writes] == ["create", "append", "delete"]
    assert [w.table for w in writes] == ["demo.sales"] * 3
    assert writes[2].payload == "id < 3"


def test_reads_pass_through_and_commit_nothing(sandbox: Lake) -> None:
    assert [w.kind for w in route("SELECT 1; EXPLAIN SELECT 1")] == ["read", "read"]
    assert sandbox.write("SELECT 1") == []


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE demo.sales SET id = 1",
        "MERGE INTO demo.sales USING x ON true WHEN MATCHED THEN DELETE",
        "DROP TABLE demo.sales",
        "DELETE FROM demo.sales",  # no WHERE: deleting everything by accident is the classic
        "ALTER TABLE demo.sales RENAME TO other",
    ],
)
def test_anything_else_is_refused_with_the_statement_quoted_back(sql: str) -> None:
    with pytest.raises(IcelensError, match="It cannot do this one"):
        route(sql)


def test_comments_around_a_statement_are_not_part_of_it() -> None:
    """The Writer pane opens on a commented example, so this is the first thing anyone runs."""
    writes = route("-- a lesson\n/* still a lesson */\nDELETE FROM demo.sales WHERE id < 3;\n-- try this next")
    assert [(w.kind, w.payload) for w in writes] == [("delete", "id < 3")]


def test_a_double_dash_inside_a_string_is_not_a_comment() -> None:
    assert route("DELETE FROM demo.sales WHERE region = 'a--b'")[0].payload == "region = 'a--b'"


def test_the_attached_alias_is_stripped_from_a_table_name() -> None:
    """The SQL pane spells a table `icelens."demo"."sales"`; the Writer must reach the same one."""
    assert strip_alias('icelens."demo"."sales"') == "demo.sales"
    assert strip_alias("demo.sales") == "demo.sales"
    assert strip_alias('icelens."a.b"."t"') == "a.b.t"


# -- writing, end to end -----------------------------------------------------------------
@pytest.mark.duckdb_extensions
def test_create_insert_delete_produce_the_snapshots_they_should(sandbox: Lake) -> None:
    created = sandbox.write(SETUP)[0]
    assert (created.operation, created.added, created.records, created.files) == ("append", 100, 100, 1)

    inserted = sandbox.write("INSERT INTO demo.sales SELECT i, 'r0', 9.99 FROM range(101, 111) t(i)")[0]
    assert (inserted.operation, inserted.added, inserted.records, inserted.files) == ("append", 10, 110, 2)

    # The lesson: a DELETE is an overwrite, and it *adds* the rows that survived. This one is two
    # commits: the inserted file is all r0 and is dropped whole, then the first is rewritten.
    dropped, rewritten = sandbox.write("DELETE FROM demo.sales WHERE region = 'r0'")
    assert (dropped.operation, dropped.added, dropped.deleted, dropped.records, dropped.files) == ("delete", 0, 10, 100, 1)
    assert rewritten.operation == "overwrite"
    assert (rewritten.added, rewritten.deleted, rewritten.records) == (75, 100, 75)
    assert sandbox.table("demo.sales", refresh=True).scan().to_arrow().num_rows == 75
    # and the records add up to what the table did, which one record could not
    assert sum(w.added - w.deleted for w in (dropped, rewritten)) == 75 - 110


@pytest.mark.duckdb_extensions
def test_an_insert_maps_columns_by_position_and_casts_them(sandbox: Lake) -> None:
    """`INSERT INTO t SELECT …` has always been positional. Unaliased expressions must work.

    Both halves are load-bearing: `add_files` adopts the Parquet as written, so a column named
    `(i * 1.5)` or typed `DECIMAL(3,2)` against a `DECIMAL(21,1)` table is refused outright.
    """
    sandbox.write(SETUP)
    written = sandbox.write("INSERT INTO demo.sales SELECT i, 'r' || (i % 2), 9.99 FROM range(101, 106) t(i)")[0]
    assert written.added == 5
    rows = sandbox.table("demo.sales", refresh=True).scan(row_filter="id >= 101").to_arrow()
    assert rows.num_rows == 5
    assert str(rows.schema.field("amount").type) == "decimal128(21, 1)"


@pytest.mark.duckdb_extensions
def test_the_wrong_number_of_columns_says_so(sandbox: Lake) -> None:
    sandbox.write(SETUP)
    with pytest.raises(IcelensError, match="has 3 columns .*and the query returns 2"):
        sandbox.write("INSERT INTO demo.sales SELECT 1, 'r0'")


@pytest.mark.duckdb_extensions
def test_a_query_that_will_not_run_is_reported_as_the_query_it_is(sandbox: Lake) -> None:
    sandbox.write(SETUP)
    with pytest.raises(IcelensError, match="that query will not run"):
        sandbox.write("INSERT INTO demo.sales SELECT no_such_column FROM range(1) t(i)")


@pytest.mark.duckdb_extensions
def test_the_data_files_live_in_the_table_they_belong_to(sandbox: Lake) -> None:
    """A file written outside the table's own directory survives `DROP TABLE` as litter."""
    sandbox.write(SETUP)
    table = sandbox.table("demo.sales", refresh=True)
    paths = [task.file.file_path for task in table.scan().plan_files()]
    assert paths and all(path.startswith(table.location()) for path in paths)


@pytest.mark.duckdb_extensions
def test_writing_to_a_table_that_is_not_there_says_which(sandbox: Lake) -> None:
    with pytest.raises(IcelensError, match="demo.nope"):
        sandbox.write("INSERT INTO demo.nope SELECT 1")


@pytest.mark.duckdb_extensions
@pytest.mark.parametrize(
    ("sql", "said"),
    [
        (SETUP, "create on demo.sales failed: .*already exists"),  # PyIceberg's TableAlreadyExistsError
        ("DELETE FROM demo.sales WHERE id >>> 3", "delete on demo.sales failed"),  # pyparsing's ParseException
        ("DELETE FROM demo.sales WHERE no_such_column = 1", "delete on demo.sales failed: .*no_such_column"),  # ValueError
        ("DELETE FROM demo.sales WHERE region = 'r0' -- on the same line", "delete on demo.sales failed"),
        ("INSERT INTO demo.sales SELECT 'x' || i, 'r', 1.0 FROM range(2) t(i)", "append on demo.sales failed: .*convert"),
    ],
)
def test_a_statement_the_engines_refuse_is_an_error_not_a_traceback(tmp_path, capsys, sql: str, said: str) -> None:
    """Each of these used to escape `--write` as somebody else's exception, and a traceback.

    They are the statement's fault, and fixable - which is what IcelensError means - so they
    arrive the way every other refusal does: exit 1, one line naming the table, and JSON when
    JSON was asked for. The table is untouched by the one that failed.
    """
    common = ["--sandbox", "cli", "--sandbox-dir", str(tmp_path)]
    assert main([*common, "--write", SETUP]) == 0
    capsys.readouterr()
    assert main([*common, "--write", sql]) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert re.search(said, err), err
    assert main([*common, "--write", sql, "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["kind"] == "IcelensError"
    lake = Lake(sandbox_target("cli", root=tmp_path))
    try:
        assert lake.table("demo.sales").scan().to_arrow().num_rows == 100
    finally:
        lake.close()


# -- the sandbox on disk -----------------------------------------------------------------
@pytest.mark.duckdb_extensions
def test_a_sandbox_is_a_named_directory_that_persists(tmp_path) -> None:
    first = Lake(sandbox_target("teaching", root=tmp_path), read_only=True)
    first.write(SETUP)
    first.close()
    again = Lake(sandbox_target("teaching", root=tmp_path), read_only=True)
    assert again.tables("demo") == ["demo.sales"]
    assert again.table("demo.sales").scan().to_arrow().num_rows == 100
    # a different name is a different catalog, not the same one under another label
    assert Lake(sandbox_target("other", root=tmp_path), read_only=True).namespaces() == []
    again.close()


@pytest.mark.parametrize("name", ["", "..", "../elsewhere", "a/b", "has space"])
def test_a_sandbox_name_is_a_directory_name(name: str, tmp_path) -> None:
    with pytest.raises(IcelensError, match="sandbox name"):
        sandbox_target(name, root=tmp_path)


@pytest.mark.duckdb_extensions
def test_reset_empties_the_sandbox(tmp_path) -> None:
    lake = Lake(sandbox_target("teaching", root=tmp_path), read_only=True)
    lake.write(SETUP)
    lake.close()
    reset_sandbox("teaching", root=tmp_path)
    assert Lake(sandbox_target("teaching", root=tmp_path), read_only=True).namespaces() == []


def test_reset_refuses_a_directory_that_is_not_a_sandbox(tmp_path) -> None:
    """It deletes a tree, so it looks at what it is about to delete first."""
    (tmp_path / "mine").mkdir()
    (tmp_path / "mine" / "thesis.tex").write_text("years of work")
    with pytest.raises(IcelensError, match="does not look like a sandbox"):
        reset_sandbox("mine", root=tmp_path)
    assert (tmp_path / "mine" / "thesis.tex").exists()
    reset_sandbox("never-made", root=tmp_path)  # nothing there is not an error


@pytest.mark.duckdb_extensions
def test_a_sandbox_under_a_path_with_spaces_and_accents_writes_where_it_reads(tmp_path) -> None:
    """`~/My Lessons`, `C:\\Users\\Zoë Smith`: ordinary homes, and a sandbox could not write in one.

    The warehouse was handed to PyIceberg percent-encoded, PyIceberg does not decode a `file://`
    path, so every table landed in a sibling directory literally called `My%20Lessons` - and the
    Parquet icelens wrote was then looked for at `My%2520Lessons`, so the first write failed and
    a reset left the misplaced tree behind. Now it all lands inside the sandbox, and a reset
    leaves nothing.
    """
    root = tmp_path / "My Lessons ü"
    lake = Lake(sandbox_target("café", root=root))
    try:
        lake.write(SETUP)
        lake.write("INSERT INTO demo.sales SELECT i, 'r0', 9.99 FROM range(101, 111) t(i)")
        lake.write("DELETE FROM demo.sales WHERE region = 'r0'")
        table = lake.table("demo.sales", refresh=True)
        assert table.scan().to_arrow().num_rows == 75
        assert "My Lessons ü" in table.location()
        paths = [task.file.file_path for task in table.scan().plan_files()]
        assert paths and all(path.startswith(table.location()) for path in paths)
    finally:
        lake.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["My Lessons ü"], "something was written beside the sandbox"
    reset_sandbox("café", root=root)
    assert list(root.iterdir()) == []


@pytest.mark.duckdb_extensions
def test_a_v3_sandbox_under_a_path_with_spaces_and_accents_writes_where_it_reads(tmp_path) -> None:
    """The same homes at v3, where DuckDB writes through icelens's own REST catalog.

    That catalog handed out locations percent-encoded too, and DuckDB decodes a `file://` path
    no more than PyIceberg does: its first write failed with "Cannot open file
    …/My%20Lessons%20%C3%BC/…", and left that directory beside the sandbox.

    No DELETE here: DuckDB 1.5 cannot write a deletion vector for a data file whose path is not
    ASCII ("Invalid byte encountered in STRING -> BLOB conversion"), whatever the catalog does.
    """
    root = tmp_path / "My Lessons ü"
    lake = Lake(sandbox_target("café", root=root, version=3))
    try:
        lake.write(SETUP)
        lake.write("INSERT INTO demo.sales SELECT i, 'r0', 9.99 FROM range(101, 111) t(i)")
        table = lake.table("demo.sales", refresh=True)
        assert table.metadata.format_version == 3
        assert "My Lessons ü/café/warehouse" in table.location()
        rows, _ = lake.rows("demo.sales", limit=1000, columns=["id"])
        assert rows.num_rows == 110
        paths = [task.file.file_path for task in table.scan().plan_files()]
        assert len(paths) == 2 and all(path.startswith(table.location()) for path in paths)
    finally:
        lake.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["My Lessons ü"], "something was written beside the sandbox"
    reset_sandbox("café", root=root)
    assert [p.name for p in (root / "café").iterdir()] == ["sandbox.json"]  # emptied, and still v3


@pytest.mark.parametrize(
    "path",
    [PurePosixPath("/tmp/My Lessons ü/w"), PurePosixPath("/tmp/100% done/w"), PureWindowsPath(r"C:\Users\Zoë Smith\w")],
)
def test_a_local_path_survives_the_round_trip_through_a_file_uri(path) -> None:
    """The URI goes to PyIceberg unencoded, and comes back from it as the same path, drive letter and all."""
    uri = _file_uri(path)
    assert uri.startswith("file:///") and "%" not in uri.replace("100%", "")
    assert _local(uri).as_posix() == path.as_posix()


def test_a_rest_sandbox_reset_empties_its_own_namespace_and_nothing_else(tmp_path) -> None:
    """The confinement `empty_sandbox_namespace` promises, checked against a real catalog.

    A SQLite catalog standing in for the REST one: the method only lists one namespace and drops
    what is in it, which any catalog can do - so the namespace beside it, holding a table with
    the same name, is the part being tested.
    """
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField

    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    properties = {"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}", "warehouse": warehouse.as_uri()}
    lake = Lake(rest_sandbox_target("demo", Target(properties=properties)), read_only=True)
    try:
        assert lake.is_rest_sandbox and lake.sandbox_namespace == "sandbox_demo"
        assert lake.empty_sandbox_namespace() == []  # never made: nothing to empty, and no error
        schema = Schema(NestedField(1, "id", LongType()))
        for namespace in ("sandbox_demo", "core"):
            lake.catalog.create_namespace(namespace)
            lake.catalog.create_table(f"{namespace}.sales", schema=schema)
        lake.catalog.create_table("sandbox_demo.other", schema=schema)
        assert sorted(lake.empty_sandbox_namespace()) == ["sandbox_demo.other", "sandbox_demo.sales"]
        assert lake.tables("sandbox_demo") == []
        assert lake.tables("core") == ["core.sales"]
    finally:
        lake.close()


def test_a_reset_the_catalog_forbids_is_refused_not_reported_as_done(tmp_path, monkeypatch, capsys) -> None:
    """Only "no such namespace" means there is nothing to empty. A 403 on the listing is the
    catalog saying no, and it must not come out as "emptied sandbox_x, 0 tables"."""
    from headless import clean_environment, run

    from icelens import rest
    from icelens.catalog import CatalogPermissionDenied

    clean_environment(monkeypatch, tmp_path)
    sandbox_target("served", root=tmp_path, version=3)
    running = rest.serve(tmp_path / "served")
    route = rest._Handler._route

    def forbidding(self, method, parts, query):  # type: ignore[no-untyped-def]
        if method == "GET" and parts[-1] == "tables" and "sandbox_x" in parts:
            raise rest.RestError(403, "ForbiddenException", "principal may not list sandbox_x")
        return route(self, method, parts, query)

    monkeypatch.setattr(rest._Handler, "_route", forbidding)
    base = Target({"type": "rest", "uri": running.uri, "warehouse": rest.PREFIX, "token": running.token})
    lake = Lake(rest_sandbox_target("x", base), read_only=True)
    try:
        lake.catalog.create_namespace("sandbox_x")
        with pytest.raises(CatalogPermissionDenied, match="access denied"):
            lake.empty_sandbox_namespace()
    finally:
        lake.close()
    connect = ["--no-env", "--uri", running.uri, "--warehouse", rest.PREFIX, "-p", f"token={running.token}"]
    ran = run(capsys, [*connect, "--sandbox", "x", "--rest", "--sandbox-reset", "--list"])
    assert ran.code == 1 and "access denied" in ran.err and "emptied" not in ran.err


# -- the ledger --------------------------------------------------------------------------
@pytest.mark.duckdb_extensions
def test_the_ledger_puts_the_statement_beside_what_it_did(sandbox: Lake) -> None:
    written = sandbox.write(f"{SETUP}; DELETE FROM demo.sales WHERE region = 'r0'")
    pane = model.written(written)
    assert pane.data.column_names[:5] == ["#", "statement", "table", "operation", "snapshot id"]
    assert pane.data.column("operation").to_pylist() == ["append", "overwrite"]
    assert pane.data.column("-rows").to_pylist() == [0, 100]
    assert "DELETE FROM demo.sales WHERE region = 'r0'" in pane.data.column("statement")[1].as_py()
    assert "2 commits this session" in pane.note


def test_an_empty_ledger_says_what_to_do() -> None:
    pane = model.written([])
    assert "ctrl+r" in pane.note
    assert pane.data.num_rows == 0


# -- the CLI -----------------------------------------------------------------------------
def test_write_needs_a_sandbox(capsys) -> None:
    assert main(["--write", "DELETE FROM x.y WHERE id < 1"]) == 1
    assert "need --sandbox" in capsys.readouterr().err


def test_a_sandbox_takes_no_connection_settings(capsys) -> None:
    """Ignoring `--uri` here would read as a connection that quietly failed over to local."""
    assert main(["--sandbox", "--uri", "http://localhost:8181/catalog", "--list"]) == 1
    assert "takes no connection settings" in capsys.readouterr().err


@pytest.mark.duckdb_extensions
def test_write_prints_one_line_per_commit(tmp_path, capsys) -> None:
    code = main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--write", SETUP])
    assert code == 0
    assert "append on demo.sales" in capsys.readouterr().out


@pytest.mark.duckdb_extensions
def test_write_as_json_is_machine_readable(tmp_path, capsys) -> None:
    main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--write", SETUP, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["kind"] == "written"
    row = payload["rows"][0]
    assert row["operation"] == "append" and row["added"] == 100 and row["table"] == "demo.sales"


@pytest.mark.duckdb_extensions
def test_a_sandbox_reads_back_through_the_ordinary_panes(tmp_path, capsys) -> None:
    """The point of writing here is to then look at it with everything else the tool has."""
    main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--write", SETUP])
    capsys.readouterr()
    assert main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--pane", "snapshots", "demo.sales"]) == 0
    assert "append" in capsys.readouterr().out


def test_reset_from_the_command_line(tmp_path, capsys) -> None:
    main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--write", SETUP])
    capsys.readouterr()
    main(["--sandbox", "cli", "--sandbox-dir", str(tmp_path), "--sandbox-reset", "--list"])
    assert "(no rows)" in capsys.readouterr().out


# -- the Writer pane ---------------------------------------------------------------------
async def test_there_is_no_writer_pane_without_a_sandbox(app: IcelensApp) -> None:
    assert "writer" not in {pane for pane, _label, _key in app.panes}
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert not app.query("#tab-writer")
        await pilot.press("e")
        await settle(app, pilot)
        assert app.active_pane != "writer"


@pytest.mark.duckdb_extensions
async def test_the_writer_pane_commits_what_you_type(sandbox: Lake) -> None:
    from textual.widgets import TextArea

    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        await settle(app, pilot)
        assert app.active_pane == "writer"
        assert isinstance(app.focused, TextArea)

        app.query_one("#write", TextArea).text = SETUP
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert [w.operation for w in app._written] == ["append"]
        assert "1 commits this session" in note(app, "writer")
        # the table did not exist a moment ago, and the tree now knows it
        assert app.known_tables == ["demo.sales"]
        assert app.ident == "demo.sales"

        app.query_one("#write", TextArea).text = "DELETE FROM demo.sales WHERE region = 'r0'"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert [w.operation for w in app._written] == ["append", "overwrite"]
        assert app.query_one("#grid-writer").row_count == 2

        # and the panes that read agree with the ledger
        await pilot.press("escape")
        await pilot.press("4")
        await settle(app, pilot)
        assert "2 snapshots" in note(app, "snapshots")


async def test_the_writer_reports_a_bad_statement_without_falling_over(sandbox: Lake) -> None:
    from textual.widgets import TextArea

    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        await settle(app, pilot)
        app.query_one("#write", TextArea).text = "UPDATE demo.sales SET id = 1"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert "It cannot do this one" in note(app, "writer")
        assert app._written == []


async def test_the_status_bar_says_it_is_a_sandbox(sandbox: Lake) -> None:
    from textual.widgets import Static

    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "sandbox test" in str(app.query_one("#status", Static).content)


def test_a_sandbox_target_is_not_mistaken_for_a_configured_catalog(tmp_path) -> None:
    """`icelens.sandbox` is what marks one, not the catalog type - plenty of people run SQLite."""
    ordinary = Target(properties={"type": "sql", "uri": "sqlite:///x.db", "warehouse": "file:///w"})
    assert ordinary.sandbox == ""
    assert Lake(ordinary, read_only=True).is_sandbox is False
    assert sandbox_target("test", root=tmp_path).sandbox == "test"


# -- the attached sandbox: a namespace of a catalog somebody runs, and v3 -------------

def test_a_rest_sandbox_keeps_the_connection_it_was_given() -> None:
    """The offline sandbox invents a catalog. This one borrows one, and says whose."""
    base = Target(properties={"type": "rest", "uri": "http://localhost:8181/catalog"}, profile="p", origin=".env")
    target = rest_sandbox_target("lesson", base)
    assert target.properties["uri"] == "http://localhost:8181/catalog"
    assert target.sandbox == "lesson"
    assert target.sandbox_namespace == "sandbox_lesson"
    assert "lesson" in target.origin and ".env" in target.origin


def test_a_rest_sandbox_name_is_held_to_the_same_rule() -> None:
    """It is half a namespace name here rather than a directory name, and still has to be one."""
    base = Target(properties={"type": "rest", "uri": "http://x/catalog"}, profile=None, origin="x")
    with pytest.raises(IcelensError, match="letters, digits"):
        rest_sandbox_target("../escape", base)


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("sales", "sandbox_demo.sales"),
        ("sandbox_demo.sales", "sandbox_demo.sales"),
        ('"sandbox_demo"."sales"', "sandbox_demo.sales"),
        ('icelens."sandbox_demo"."sales"', "sandbox_demo.sales"),
    ],
)
def test_a_bare_name_means_the_sandbox_namespace(written: str, expected: str) -> None:
    assert sandbox_table(written, "sandbox_demo") == expected


@pytest.mark.parametrize("outside", ["crm.accounts", "main.users", '"crm"."accounts"'])
def test_an_attached_sandbox_cannot_name_a_table_outside_itself(outside: str) -> None:
    """The safety property the whole feature rests on: the catalog on the other end is real."""
    with pytest.raises(IcelensError, match="writes only inside sandbox_demo"):
        sandbox_table(outside, "sandbox_demo")


def test_the_attached_router_qualifies_into_the_sandbox_and_keeps_the_rest() -> None:
    """Only the table name is rewritten. The statement DuckDB runs is the one that was typed."""
    (job,) = route_attached("INSERT INTO sales SELECT 1, 'r0', 2.5", "sandbox_demo")
    assert job.kind == "insert"
    assert job.table == "sandbox_demo.sales"
    assert job.payload == 'INSERT INTO icelens."sandbox_demo"."sales" SELECT 1, \'r0\', 2.5'
    assert job.sql == "INSERT INTO sales SELECT 1, 'r0', 2.5"


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("UPDATE sales SET amount = 1 WHERE id = 2", "update"),
        ("MERGE INTO sales AS t USING sales AS s ON t.id = s.id WHEN MATCHED THEN UPDATE SET amount = 1", "merge"),
        ("ALTER TABLE sales ADD COLUMN note VARCHAR", "alter"),
        ("DROP TABLE sales", "drop"),
    ],
)
def test_the_attached_router_takes_what_the_offline_one_refuses(sql: str, kind: str) -> None:
    """DuckDB performs these, so nothing here has to know how - which is the point of attaching."""
    (job,) = route_attached(sql, "sandbox_demo")
    assert job.kind == kind
    assert job.table == "sandbox_demo.sales"
    with pytest.raises(IcelensError):
        route(sql)


def test_the_attached_router_still_reads_through() -> None:
    jobs = route_attached("SELECT 1; INSERT INTO sales VALUES (1)", "sandbox_demo")
    assert [job.kind for job in jobs] == ["read", "insert"]


def test_a_create_without_a_version_is_asked_for_v3() -> None:
    sql = 'CREATE TABLE icelens."sandbox_demo"."sales" AS SELECT 1 AS id'
    match = _ATTACH_SHAPES["CREATE"].match(sql)
    assert match is not None
    assert with_format_version(sql, match.end("table")) == (
        'CREATE TABLE icelens."sandbox_demo"."sales" WITH ("format-version" = 3) AS SELECT 1 AS id'
    )


def test_a_create_that_names_a_version_is_left_alone() -> None:
    """Asking for v2 on purpose is a lesson: the two tables side by side are the lesson."""
    sql = 'CREATE TABLE icelens."sandbox_demo"."sales" WITH ("format-version" = 2) AS SELECT 1 AS id'
    match = _ATTACH_SHAPES["CREATE"].match(sql)
    assert match is not None
    assert with_format_version(sql, match.end("table")) == sql


def test_a_cte_after_as_is_not_mistaken_for_the_create_clause() -> None:
    """`CREATE TABLE t AS WITH cte AS (…)` puts a WITH in the statement that is not ours."""
    sql = 'CREATE TABLE icelens."sandbox_demo"."sales" AS WITH c AS (SELECT 1 AS id) SELECT * FROM c'
    match = _ATTACH_SHAPES["CREATE"].match(sql)
    assert match is not None
    assert '"format-version" = 3' in with_format_version(sql, match.end("table"))


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # a column list: the clause goes after it - before it, DuckDB cannot parse the statement
        (
            'CREATE TABLE icelens."core"."c" (id INTEGER)',
            'CREATE TABLE icelens."core"."c" (id INTEGER) WITH ("format-version" = 3)',
        ),
        (
            'CREATE TABLE icelens."core"."c"(id INTEGER, amount DECIMAL(10, 2))',
            'CREATE TABLE icelens."core"."c"(id INTEGER, amount DECIMAL(10, 2)) WITH ("format-version" = 3)',
        ),
        # quoted names and defaults that hold parentheses do not end the list early
        (
            'CREATE TABLE icelens."core"."c" ("a)b" INTEGER, "x""(y" VARCHAR DEFAULT \')\')',
            'CREATE TABLE icelens."core"."c" ("a)b" INTEGER, "x""(y" VARCHAR DEFAULT \')\') WITH ("format-version" = 3)',
        ),
        # a WITH already after the column list is the user's, as one before AS is
        (
            'CREATE TABLE icelens."core"."c" (id INTEGER) WITH ("format-version" = 2)',
            'CREATE TABLE icelens."core"."c" (id INTEGER) WITH ("format-version" = 2)',
        ),
        (
            'CREATE TABLE icelens."core"."c" (id INTEGER)  with ("format-version" = 2)',
            'CREATE TABLE icelens."core"."c" (id INTEGER)  with ("format-version" = 2)',
        ),
        # CTAS is as it was
        (
            'CREATE TABLE IF NOT EXISTS icelens."core"."c" AS SELECT 1 AS id',
            'CREATE TABLE IF NOT EXISTS icelens."core"."c" WITH ("format-version" = 3) AS SELECT 1 AS id',
        ),
    ],
)
def test_a_column_list_create_is_asked_for_v3_after_its_columns(sql: str, expected: str) -> None:
    match = _ATTACH_SHAPES["CREATE"].match(sql)
    assert match is not None
    assert with_format_version(sql, match.end("table")) == expected
    assert len(duckdb.extract_statements(expected)) == 1, "and DuckDB parses what comes out"


def test_the_sql_pane_stays_a_reader_on_a_rest_sandbox() -> None:
    """The Writer pane is confined to one namespace. The SQL pane is not, so it stays read-only."""
    base = Target(properties={"type": "rest", "uri": "http://localhost:8181/catalog"}, profile=None, origin="x")
    lake = Lake(rest_sandbox_target("demo", base), read_only=True)
    assert lake.read_only is False, "it must attach writable, or the Writer pane cannot commit"
    assert lake.sql_is_guarded is True
    with pytest.raises(IcelensError, match="icelens reads"):
        lake.query("DROP TABLE crm.accounts")


def test_allow_writes_opens_the_sql_pane_on_a_rest_sandbox() -> None:
    """`--allow-writes` is the one way past every guard, and stays the one way past this one."""
    base = Target(properties={"type": "rest", "uri": "http://localhost:8181/catalog"}, profile=None, origin="x")
    lake = Lake(rest_sandbox_target("demo", base), read_only=False)
    assert lake.sql_is_guarded is False


def test_an_offline_sandbox_has_no_namespace_and_no_sql_guard(sandbox: Lake) -> None:
    assert sandbox.is_rest_sandbox is False
    assert sandbox.sandbox_namespace == ""
    with pytest.raises(IcelensError, match="this sandbox is a directory"):
        sandbox.empty_sandbox_namespace()


def test_rest_needs_sandbox_on_the_command_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--rest", "--list"]) == 1
    assert "need --sandbox" in capsys.readouterr().err


def test_a_rest_sandbox_refuses_a_sandbox_dir(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    assert main(["--sandbox", "--rest", "--sandbox-dir", str(tmp_path), "--list"]) == 1
    assert "--sandbox-dir is for an offline sandbox" in capsys.readouterr().err


def test_a_rest_sandbox_without_a_catalog_names_the_flag_that_needs_one(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a first --rest run hits when it is started anywhere but the project directory.

    Discovery is the same here as everywhere else, so this used to arrive as "cannot open
    catalog \'icelens\' (unknown): URI missing" - four layers down, naming neither --sandbox nor
    --rest, about a sandbox the reader was told needs no server at all.
    """
    for key in [name for name in os.environ if name.startswith("ICELENS_")]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))  # no ~/.pyiceberg.yaml underneath
    monkeypatch.chdir(tmp_path)  # and no .env beside us

    assert main(["--sandbox", "lesson", "--rest", "--list"]) == 1

    complaint = capsys.readouterr().err
    assert "--rest puts the sandbox inside a catalog you are running" in complaint
    assert "--uri" in complaint and "--catalog" in complaint


@pytest.mark.duckdb_extensions
def test_a_create_whose_rows_will_not_go_in_leaves_no_table(sandbox: Lake) -> None:
    # The table is made first, so the rows have somewhere to go. A failed write used to leave it
    # there, empty, and the corrected statement then failed on "already exists".
    with pytest.raises(IcelensError, match="Could not convert"):
        sandbox.write("CREATE TABLE demo.bad AS SELECT CAST(v AS INTEGER) AS id FROM (VALUES ('1'), ('x')) t(v)")
    assert "demo.bad" not in sandbox.tables("demo")
    [made] = sandbox.write("CREATE TABLE demo.bad AS SELECT CAST(v AS INTEGER) AS id FROM (VALUES ('1'), ('2')) t(v)")
    assert made.added == 2


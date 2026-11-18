"""Attach mode, against a REST catalog that is actually running.

They run against the live Lakekeeper when one is configured (see `tests/live.py`), and against
icelens's own REST catalog (`icelens.rest`) when none is - so they run everywhere DuckDB's
extensions load, CI included. What they cover is the half of icelens a SQLite catalog cannot
reach: DuckDB speaking the catalog itself, `READ_ONLY` as the engine enforces it,
`AT (VERSION => ...)`, and a v3 table written rather than hand-forged.

Several tests reach for `lake._duck()` on purpose. The point of a read-only *attach* is that it
holds when icelens's own string guard is out of the way, so these tests deliberately step round
that guard and put the statement to DuckDB directly. A test that only asked `Lake.query` would
be re-testing `only_reads`, which `test_cli.py` already does.
"""

from __future__ import annotations

import json

import pytest
from harness import settle

from icelens.catalog import IcelensError, Lake
from icelens.model import ordered_snapshots

#: attach mode is DuckDB speaking the catalog, so every test here needs its extensions too
pytestmark = pytest.mark.duckdb_extensions

#: what the `rest_lake` fixture wrote: 50 appended, 10 appended, 5 deleted
HEAD_ROWS = 55
FIRST_SNAPSHOT_ROWS = 50


def counted(lake: Lake, ident: str = "icelens.live.events") -> int:
    return int(lake.query(f"SELECT count(*) FROM {ident}").column(0)[0].as_py())


# -- the mode itself ------------------------------------------------------------------


def test_a_rest_catalog_is_attached_not_bridged(rest_lake: Lake) -> None:
    """The distinction the whole suite was blind to: `attach`, not `bridge`, not `pyiceberg`."""
    assert rest_lake.mode == "attach"
    assert rest_lake.attach_error is None
    assert rest_lake.engine == "duckdb"


def test_namespaces_and_tables_come_back_from_the_server(rest_lake: Lake) -> None:
    assert "live" in rest_lake.namespaces()
    assert rest_lake.tables("live") == ["live.events", "live.lineage"]


# -- read-only, as the engine enforces it ---------------------------------------------


def test_a_read_only_attach_refuses_a_write_that_a_writable_one_accepts(
    read_only_lake: Lake, rest_lake: Lake
) -> None:
    """One statement, two lakes that differ only in `read_only`. The control is the point.

    Without it this test could pass because the statement is broken, the warehouse is full, or
    the catalog is unhappy - any of which would look exactly like enforcement.
    """
    import duckdb

    statement = "CREATE TABLE icelens.live.scratch AS SELECT 1 AS id"

    with pytest.raises(duckdb.Error) as refused:
        read_only_lake._duck().execute(statement)
    assert "read-only" in str(refused.value).lower()

    rest_lake._duck().execute(statement)
    assert counted(rest_lake, "icelens.live.scratch") == 1


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM icelens.live.events",
        "INSERT INTO icelens.live.events VALUES (999, 'x', '2026-03-04', TIMESTAMP '2026-03-04 00:00:00')",
        "EXPLAIN ANALYZE DELETE FROM icelens.live.events",
        "EXPLAIN ANALYZE INSERT INTO icelens.live.events VALUES (999, 'x', '2026-03-04', TIMESTAMP '2026-03-04 00:00:00')",
        "DROP TABLE icelens.live.events",
    ],
)
def test_the_engine_refuses_every_write_through_a_read_only_attach(
    read_only_lake: Lake, rest_lake: Lake, statement: str
) -> None:
    """`EXPLAIN ANALYZE` is here because it *runs* what it explains - measured, not assumed.

    The row count afterwards is the assertion that matters: an error alone would not prove the
    statement had no effect, and this exact word is how the string guard was got round once.
    """
    import duckdb

    with pytest.raises(duckdb.Error):
        read_only_lake._duck().execute(statement)
    assert counted(rest_lake) == HEAD_ROWS


def test_reads_still_work_through_a_read_only_attach(read_only_lake: Lake) -> None:
    """The guard is worth nothing if it also refuses the reads the tool exists to do."""
    assert counted(read_only_lake) == HEAD_ROWS
    read_only_lake._duck().execute("DESCRIBE icelens.live.events")
    read_only_lake._duck().execute("EXPLAIN SELECT * FROM icelens.live.events")


def test_the_string_guard_answers_first_and_in_english(read_only_lake: Lake) -> None:
    """Two guards, and this is the one whose job is the message, not the enforcement."""
    with pytest.raises(IcelensError) as refused:
        read_only_lake.query("DELETE FROM icelens.live.events")
    assert "refusing to run DELETE" in str(refused.value)
    assert "--allow-writes" in str(refused.value)


def test_copy_to_a_local_file_is_refused_even_though_the_catalog_is_untouched(
    read_only_lake: Lake,
) -> None:
    """The one thing `READ_ONLY` does not cover, which is why the string guard is still here."""
    with pytest.raises(IcelensError) as refused:
        read_only_lake.query("COPY (SELECT 1) TO '/tmp/icelens-should-not-exist.csv'")
    assert "refusing to run COPY" in str(refused.value)


# -- time travel, which only attach mode spells this way ------------------------------


def test_time_travel_reads_the_snapshot_it_was_given(rest_lake: Lake) -> None:
    """`AT (VERSION => ...)` is attach mode's spelling, and nothing else in the suite runs it."""
    # `metadata.snapshots` is not in any order the writer promises - measured against
    # Lakekeeper, DuckDB left three snapshots listed 2, 3, 1 - which is why the panes go
    # through `ordered_snapshots` and why this test does too.
    snapshots = ordered_snapshots(rest_lake.table("live.events", refresh=True).metadata)
    assert len(snapshots) == 3

    first, _, head = (snapshot.snapshot_id for snapshot in snapshots)
    at_first, engine = rest_lake.rows("live.events", limit=1000, snapshot_id=first)
    assert engine == "duckdb"
    assert at_first.num_rows == FIRST_SNAPSHOT_ROWS

    at_head, _ = rest_lake.rows("live.events", limit=1000, snapshot_id=head)
    assert at_head.num_rows == HEAD_ROWS

    now, _ = rest_lake.rows("live.events", limit=1000)
    assert now.num_rows == HEAD_ROWS


def test_time_travel_works_on_a_read_only_lake_too(read_only_lake: Lake) -> None:
    """Reading the past is a read. It would be easy for the extra ATTACH option to say otherwise."""
    first = ordered_snapshots(read_only_lake.table("live.events", refresh=True).metadata)[0].snapshot_id
    rows, _ = read_only_lake.rows("live.events", limit=1000, snapshot_id=first)
    assert rows.num_rows == FIRST_SNAPSHOT_ROWS


# -- v3, written rather than hand-forged ----------------------------------------------


def test_the_v3_table_is_really_v3(rest_lake: Lake) -> None:
    """The hermetic `v3_lake` rewrites a metadata document by hand. This one was written by DuckDB."""
    assert rest_lake.table("live.lineage", refresh=True).metadata.format_version == 3


def test_row_lineage_comes_back_from_an_attached_v3_table(rest_lake: Lake) -> None:
    """Row ids are computed by the reader, so this is the only route that can produce them."""
    rows, engine = rest_lake.rows("live.lineage", limit=100, lineage=True)
    assert engine == "duckdb", f"fell back: {engine}"
    assert rows.column_names[:2] == ["_row_id", "_last_updated_sequence_number"]
    assert rows.column("_row_id").null_count == 0
    assert sorted(rows.column("_row_id").to_pylist()) == list(range(10))


def test_a_v2_table_offers_no_row_lineage_and_says_so(rest_lake: Lake) -> None:
    """Found by this fixture, and the reason it exists.

    DuckDB does not refuse `_row_id` on a v2 table through an attached catalog - it answers
    with a column of NULLs - so the "could not, and here is why" fallback never fired here,
    while the same call in bridge mode fell back correctly. Two modes, two answers, one of
    them silently claiming every row has no id.
    """
    rows, engine = rest_lake.rows("live.events", limit=5, lineage=True)
    assert "no row lineage here" in engine
    assert "_row_id" not in rows.column_names


# -- the whole way through, as a script would drive it --------------------------------


def test_the_cli_reads_the_live_catalog_as_json(attachable, rest_lake, capsys) -> None:
    """`--no-env` on purpose: this must not quietly pick up a `.env` from whatever directory
    pytest was started in, which is how a passing test can be testing the wrong catalog."""
    from icelens.cli import main

    argv = attachable.cli_args()
    assert main([*argv, "--sql", "SELECT count(*) AS n FROM icelens.live.events", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"] == [{"n": HEAD_ROWS}]
    assert payload["columns"][0]["name"] == "n"


def test_the_cli_refuses_to_write_to_the_live_catalog(attachable, rest_lake, capsys) -> None:
    from icelens.cli import main

    argv = attachable.cli_args()
    assert main([*argv, "--sql", "DROP TABLE icelens.live.events", "--json"]) == 1

    # A failure is JSON on stderr, leaving stdout empty: a caller redirecting stdout to a file
    # gets an empty file, not half a result.
    printed = capsys.readouterr()
    assert printed.out == ""
    payload = json.loads(printed.err)
    assert payload["error"]["kind"] == "IcelensError"
    assert "refusing to run DROP" in payload["error"]["message"]
    assert counted(rest_lake) == HEAD_ROWS


# -- the sandbox on a real catalog, which is the only place v3 can be written ---------


def test_a_sandbox_write_lands_at_format_version_3(rest_sandbox: Lake) -> None:
    """The whole reason this mode exists. PyIceberg cannot serialise a v3 metadata document
    at all, and DuckDB writing to a bare directory produces v2 - so this is the one route."""
    (written,) = rest_sandbox.write("CREATE TABLE lineage AS SELECT i AS id, 'row ' || i AS name FROM range(1, 11) t(i)")
    assert written.table == "sandbox_t.lineage"
    assert written.added == 10
    assert rest_sandbox.table("sandbox_t.lineage", refresh=True).metadata.format_version == 3


def test_row_lineage_is_readable_off_a_table_the_sandbox_wrote(rest_sandbox: Lake) -> None:
    """v3's flagship column, on a table a person made by typing into the Writer pane."""
    rest_sandbox.write("CREATE TABLE lineage_read AS SELECT i AS id FROM range(1, 6) t(i)")
    rows, engine = rest_sandbox.rows("sandbox_t.lineage_read", limit=10, lineage=True)
    assert engine == "duckdb", f"fell back: {engine}"
    assert "_row_id" in rows.column_names


def test_a_delete_on_a_v3_sandbox_table_writes_a_deletion_vector(rest_sandbox: Lake) -> None:
    """The lesson the offline sandbox cannot teach, asserted rather than implied.

    Offline, PyIceberg's `delete` rewrites the data file: the ledger reads `overwrite`,
    `+75 / -100`, and the file count does not move. Here the data file is untouched and a
    Puffin blob lands beside it, which is what `merge-on-read` means and why the Files pane
    is worth looking at afterwards.
    """
    rest_sandbox.write("CREATE TABLE dv AS SELECT i AS id, i % 4 AS bucket FROM range(1, 101) t(i)")
    (written,) = rest_sandbox.write("DELETE FROM dv WHERE bucket = 0")
    assert written.kind == "delete"
    table = rest_sandbox.table("sandbox_t.dv", refresh=True)
    assert table.metadata.format_version == 3
    assert rest_sandbox.rows("sandbox_t.dv", limit=200)[0].num_rows == 75

    assert written.operation == "delete", "a rewrite would say `overwrite`"
    snapshot = table.current_snapshot()
    assert snapshot.summary.get("total-data-files") == "1", "the data file was rewritten"
    formats = [
        (entry.data_file.file_format, entry.data_file.content)
        for manifest in snapshot.manifests(table.io)
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False)
    ]
    assert any(str(fmt) == "FileFormat.PUFFIN" for fmt, _ in formats), formats


def test_update_and_merge_run_on_an_attached_sandbox(rest_sandbox: Lake) -> None:
    """Neither is in the offline router, because offline nothing here knows how to perform
    them. Attached, DuckDB performs them and the router only has to find the table."""
    rest_sandbox.write("CREATE TABLE edits AS SELECT i AS id, 1.0 AS amount FROM range(1, 11) t(i)")
    (updated,) = rest_sandbox.write("UPDATE edits SET amount = 2.0 WHERE id <= 5")
    assert updated.kind == "update"
    rows, _ = rest_sandbox.rows("sandbox_t.edits", limit=20)
    assert sorted(rows.column("amount").to_pylist()) == [1.0] * 5 + [2.0] * 5


def test_a_sandbox_on_a_real_catalog_cannot_write_outside_its_namespace(rest_sandbox: Lake) -> None:
    """The safety property, against the real catalog rather than against a regex."""
    with pytest.raises(IcelensError, match="writes only inside sandbox_t"):
        rest_sandbox.write("CREATE TABLE live.hijack AS SELECT 1 AS id")


def test_the_sql_pane_still_refuses_writes_on_a_rest_sandbox(rest_sandbox: Lake) -> None:
    """It attached writable so the Writer pane could commit. The SQL pane is not that pane."""
    assert rest_sandbox.read_only is False
    with pytest.raises(IcelensError, match="icelens reads"):
        rest_sandbox.query("DROP TABLE icelens.live.events")


def test_resetting_a_rest_sandbox_empties_one_namespace(rest_sandbox: Lake) -> None:
    """Runs last in this file by name; it drops what the tests above made and nothing else."""
    rest_sandbox.write("CREATE TABLE zz_doomed AS SELECT 1 AS id")
    dropped = rest_sandbox.empty_sandbox_namespace()
    assert "sandbox_t.zz_doomed" in dropped
    assert rest_sandbox.catalog.list_tables("sandbox_t") == []


def test_successive_deletes_each_report_their_own_rows(rest_sandbox: Lake) -> None:
    """The counter trap, held shut. Measured: three deletes of ten rows reported
    `added-position-deletes` of 10, 20 and 30, because a deletion vector is rewritten whole
    and carries every position ever deleted from its data file. `total-records` never moved
    off 100. A ledger built on either number would teach the wrong thing, twice over."""
    rest_sandbox.write("CREATE TABLE counted AS SELECT i AS id, i % 10 AS bucket FROM range(1, 101) t(i)")
    lines = [rest_sandbox.write(f"DELETE FROM counted WHERE bucket = {n}")[0] for n in (0, 1, 2)]
    assert [item.deleted for item in lines] == [10, 10, 10]
    assert [item.records for item in lines] == [90, 80, 70]

    summaries = rest_sandbox.table("sandbox_t.counted", refresh=True).current_snapshot().summary
    assert summaries.get("total-records") == "100", "the summary still says what it said"


def test_an_update_is_a_delete_and_an_insert_and_says_so(rest_sandbox: Lake) -> None:
    """The honesty constraint (an UPDATE is shown as a delete and an insert), on the one path that can produce an UPDATE."""
    rest_sandbox.write("CREATE TABLE paired AS SELECT i AS id, 1.0 AS amount FROM range(1, 21) t(i)")
    (written,) = rest_sandbox.write("UPDATE paired SET amount = 2.0 WHERE id <= 5")
    assert (written.operation, written.added, written.deleted, written.records) == ("overwrite", 5, 5, 20)


async def test_the_writer_pane_writes_v3_through_the_app(rest_sandbox: Lake) -> None:
    """The route a person actually takes: press `e`, type, ctrl+r - and get a v3 table."""
    from textual.widgets import Static, TextArea

    from icelens.app import IcelensApp

    app = IcelensApp(rest_sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "writer" in [pane[0] for pane in app.panes]
        await pilot.press("e")
        await settle(app, pilot)
        # the hint is the v3 one, and it names the namespace this sandbox is confined to
        assert "sandbox_t" in app.query_one("#write", TextArea).text

        app.query_one("#write", TextArea).text = "CREATE TABLE ui AS SELECT i AS id FROM range(1, 21) t(i)"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert [item.table for item in app._written] == ["sandbox_t.ui"]
        assert rest_sandbox.table("sandbox_t.ui", refresh=True).metadata.format_version == 3

        app.query_one("#write", TextArea).text = "UPDATE ui SET id = id + 100 WHERE id <= 5"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert [item.operation for item in app._written] == ["append", "overwrite"]
        assert "in sandbox_t, v3" in str(app.query_one("#status", Static).content)


# -- one connection, kept until there is a reason to replace it ------------------------


@pytest.fixture(scope="module")
def nested(attachable, rest_lake: Lake) -> str:
    """`core.nested.deep`: three rows in a namespace inside a namespace, written by PyIceberg."""
    import pyarrow as pa

    writer = Lake(attachable.target())
    try:
        catalog = writer.catalog
        for levels in (("core",), ("core", "nested")):
            catalog.create_namespace(levels)
        rows = pa.table({"id": pa.array([1, 2, 3], pa.int64())})
        catalog.create_table(("core", "nested", "deep"), schema=rows.schema).append(rows)
    finally:
        writer.close()
    return "core.nested.deep"


def test_a_nested_table_is_read_by_duckdb_after_a_flat_attach(attachable, nested: str) -> None:
    """Attached flat - nothing walked yet - and then asked for a nested table's rows.

    icelens's own catalog and Lakekeeper answer a nested name either way, so the rows alone
    cannot tell; Polaris does not (`NameListToString NOT IMPLEMENTED`). What has to hold is
    the route: the connection is reattached to look for nested namespaces before the read.
    """
    lake = Lake(attachable.target(), read_only=True)
    try:
        assert lake.engine == "duckdb" and not lake._nested
        rows, engine = lake.rows(nested, limit=10)
        assert (sorted(rows.column("id").to_pylist()), engine) == ([1, 2, 3], "duckdb")
        assert lake._nested and "SUPPORT_NESTED_NAMESPACES true" in (lake._attach_sql() or "")
    finally:
        lake.close()


def test_sql_finds_a_nested_table_with_no_walk_first(attachable, nested: str) -> None:
    lake = Lake(attachable.target(), read_only=True)
    try:
        assert counted(lake, 'icelens."core.nested"."deep"') == 3
    finally:
        lake.close()


def test_a_misspelt_table_walks_the_namespaces_once_not_every_time(attachable, rest_lake: Lake, monkeypatch) -> None:
    """Each typo in `--sql` re-walked the whole tree: a list call per namespace, every time."""
    lake = Lake(attachable.target(), read_only=True)
    try:
        catalog = lake.catalog
        calls: list[object] = []
        listing = catalog.list_namespaces
        monkeypatch.setattr(catalog, "list_namespaces", lambda *a, **k: calls.append(a) or listing(*a, **k))
        with pytest.raises(IcelensError):
            lake.query("SELECT * FROM icelens.live.typo")
        walked = len(calls)
        assert walked >= 1, "the first one walks, to find out whether the catalog nests"
        for _ in range(3):
            with pytest.raises(IcelensError):
                lake.query("SELECT * FROM icelens.live.typo")
        assert len(calls) == walked
    finally:
        lake.close()


def test_a_reattach_asked_for_before_duckdb_opened_is_not_done_again(attachable, rest_lake: Lake) -> None:
    """PyIceberg can stop asking for vended credentials before DuckDB is ever opened. The
    connection then opened already reflects that - and the flag left behind threw it away on
    the next call, the status bar's included."""
    lake = Lake(attachable.target(), read_only=True)
    try:
        lake._reattach = True  # what `_stop_asking_for_credentials` leaves when nothing is open yet
        assert lake.engine == "duckdb"
        opened = lake._con
        lake.rows("live.events", limit=1)
        assert lake.engine == "duckdb" and lake._con is opened
    finally:
        lake.close()


def test_a_reattach_for_any_reason_keeps_a_nested_request_nested(attachable, rest_lake: Lake) -> None:
    lake = Lake(attachable.target(), read_only=True)
    try:
        assert lake.engine == "duckdb" and not lake._nested
        opened = lake._con
        lake._reattach = True  # mid-session, as a refusal to vend sets it
        lake._duck(nested=True)
        assert lake._con is not opened
        assert lake._nested and "SUPPORT_NESTED_NAMESPACES true" in (lake._attach_sql() or "")
    finally:
        lake.close()

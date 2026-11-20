"""The Writer's ledger: whether the numbers it prints are the numbers that happened.

The risk: every other ledger test wrote one unpartitioned table with one
data file, and a partitioned overwrite that rewrites several files at once is where a derived
number could be wrong and still look plausible. So the identity the ledger rests on is checked
here against a table that spans partitions - `+rows - -rows` is exactly how far the table's row
count moved, and `total rows` is what a scan finds - along with the arithmetic the attached side
uses to derive deletions from counted rows, run against a real copy-on-write commit whose answer
is known.
"""

from __future__ import annotations

import duckdb
import pyarrow as pa
import pytest
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import LongType, NestedField, StringType

from icelens import model
from icelens.catalog import IcelensError, Lake, Write, Written, sandbox_target


@pytest.fixture()
def parted(tmp_path) -> Lake:
    """A sandbox holding `demo.parted`: 30 rows across three identity partitions, one file each."""
    lake = Lake(sandbox_target("ledger", root=tmp_path))
    lake.catalog.create_namespace("demo")
    table = lake.catalog.create_table(
        "demo.parted",
        schema=Schema(NestedField(1, "id", LongType()), NestedField(2, "region", StringType())),
        partition_spec=PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "region")),
    )
    ids = list(range(30))
    table.append(pa.table({"id": pa.array(ids, pa.int64()), "region": pa.array([f"r{i % 3}" for i in ids], pa.string())}))
    yield lake
    lake.close()


def _count(lake: Lake, ident: str) -> int:
    return lake.table(ident, refresh=True).scan().to_arrow().num_rows


def _files(lake: Lake, ident: str) -> int:
    return len(list(lake.table(ident, refresh=True).scan().plan_files()))


@pytest.mark.parametrize(
    ("where", "gone", "rewritten"),
    [
        ("id >= 5 AND id < 25", 20, 3),  # every partition loses rows: all three files rewritten
        ("region = 'r1'", 10, 1),  # one partition, whole: its file retired, nothing rewritten in
        ("id < 3", 3, 3),  # one row from each partition
    ],
)
def test_a_partitioned_delete_balances(parted: Lake, where: str, gone: int, rewritten: int) -> None:
    before, files_before = _count(parted, "demo.parted"), _files(parted, "demo.parted")
    (written,) = parted.write(f"DELETE FROM demo.parted WHERE {where}")
    after = _count(parted, "demo.parted")
    assert before - after == gone
    # the identity every ledger row has to satisfy, however many files the commit touched
    assert written.added - written.deleted == after - before
    assert written.records == after
    assert written.files == _files(parted, "demo.parted")
    # copy-on-write: what was deleted is every row of every file it had to touch
    assert written.deleted == 10 * rewritten
    assert written.operation in ("overwrite", "delete")
    assert files_before == 3


def test_a_delete_that_drops_files_and_rewrites_others_reports_both_commits(parted: Lake) -> None:
    """PyIceberg commits a `delete` for the files it drops whole, then an `overwrite` for the
    ones it rewrites. Only the second was reported, and its rows did not add up to the change."""
    before = _count(parted, "demo.parted")
    dropped, rewritten = parted.write("DELETE FROM demo.parted WHERE region = 'r1' OR id < 3")
    after = _count(parted, "demo.parted")
    assert (dropped.operation, dropped.added, dropped.deleted, dropped.records) == ("delete", 0, 10, 20)
    assert (rewritten.operation, rewritten.added, rewritten.deleted, rewritten.records) == ("overwrite", 18, 20, 18)
    assert sum(w.added - w.deleted for w in (dropped, rewritten)) == after - before == -12
    snapshots = parted.table("demo.parted", refresh=True).metadata.snapshots
    assert [dropped.snapshot_id, rewritten.snapshot_id] == [s.snapshot_id for s in snapshots[1:]]


def test_a_delete_that_matches_nothing_commits_nothing_and_says_nothing(parted: Lake) -> None:
    """It reported the previous statement's snapshot as its own, under a PyIceberg warning."""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert parted.write("DELETE FROM demo.parted WHERE id > 1000") == []
    assert len(parted.table("demo.parted", refresh=True).metadata.snapshots) == 1


@pytest.fixture()
def v3(tmp_path) -> Lake:
    """A v3 sandbox: DuckDB commits, through icelens's own REST catalog."""
    lake = Lake(sandbox_target("ledger", root=tmp_path, version=3))
    lake.write("CREATE TABLE demo.d AS SELECT range AS id FROM range(10)")
    yield lake
    lake.close()


@pytest.mark.duckdb_extensions
@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO demo.d SELECT 1 WHERE false",
        "DELETE FROM demo.d WHERE false",
        "CREATE TABLE IF NOT EXISTS demo.d AS SELECT 2 AS id",
        "DROP TABLE IF EXISTS demo.never_was",
    ],
)
def test_a_statement_that_commits_nothing_has_no_record(v3: Lake, statement: str) -> None:
    """Each was reported as the snapshot before it, which the ledger then showed twice."""
    snapshots = len(v3.table("demo.d", refresh=True).metadata.snapshots)
    assert v3.write(statement) == []
    assert len(v3.table("demo.d", refresh=True).metadata.snapshots) == snapshots


@pytest.mark.duckdb_extensions
def test_a_merge_is_two_commits_and_the_ledger_has_both(v3: Lake) -> None:
    """DuckDB's MERGE commits the matched half and the inserted half separately."""
    rows = v3.write(
        "MERGE INTO demo.d t USING (SELECT 2 AS id UNION ALL SELECT 99) s ON t.id = s.id "
        "WHEN MATCHED THEN UPDATE SET id = 20 WHEN NOT MATCHED THEN INSERT VALUES (s.id)"
    )
    assert [w.operation for w in rows] == ["overwrite", "append"]
    assert [(w.added, w.deleted) for w in rows] == [(1, 1), (1, 0)]
    assert [w.records for w in rows] == [10, 11]
    assert len({w.snapshot_id for w in rows}) == 2


@pytest.mark.duckdb_extensions
def test_a_change_to_metadata_alone_is_one_metadata_record(v3: Lake) -> None:
    current = v3.table("demo.d", refresh=True).metadata.current_snapshot_id
    (altered,) = v3.write("ALTER TABLE demo.d ADD COLUMN note VARCHAR")
    assert (altered.operation, altered.snapshot_id, altered.added, altered.deleted, altered.records) == ("metadata", current, 0, 0, 10)
    (made,) = v3.write("CREATE TABLE demo.c (id INTEGER, label VARCHAR)")
    assert (made.operation, made.snapshot_id) == ("metadata", 0)
    assert v3.table("demo.c", refresh=True).metadata.format_version == 3, "asked for v3 after its columns, and got it"


@pytest.mark.duckdb_extensions
def test_an_insert_into_one_partition_lands_in_it(parted: Lake) -> None:
    (written,) = parted.write("INSERT INTO demo.parted SELECT 100, 'r1'")
    assert (written.operation, written.added, written.records) == ("append", 1, 31)
    files = model.files(model.View(parted, "demo.parted")).records()
    assert sorted(r["partition"] for r in files) == ["region=r0", "region=r1", "region=r1", "region=r2"]


@pytest.mark.duckdb_extensions
def test_an_insert_that_spans_partitions_is_refused_and_writes_nothing(parted: Lake) -> None:
    """`add_files` adopts one Parquet file as written, and one file cannot sit in two partitions.

    SANDBOX.md calls this the known limit of the offline route. It has to arrive as a refusal
    that leaves the table as it was, rather than as somebody else's exception.
    """
    with pytest.raises(IcelensError, match="append on demo.parted failed"):
        parted.write("INSERT INTO demo.parted SELECT i, 'r' || (i % 2) FROM range(100, 104) t(i)")
    assert _count(parted, "demo.parted") == 30
    assert len(parted.table("demo.parted", refresh=True).metadata.snapshots) == 1


def test_the_attached_arithmetic_agrees_with_a_commit_whose_answer_is_known(lake: Lake) -> None:
    """An attached sandbox counts rows before and after, and derives the deletions from the move.

    That derivation is `deleted = added - (after - before)`, and the session table's last commit
    is a copy-on-write delete whose summary already says what it did: 45 rows rewritten in, 50
    retired, the table going from 60 rows to 55. Fed those counts, the derivation must land on
    the summary's own 50 - the case where both ways of counting are right and so must agree.
    """
    table = lake.table("demo.events")
    summary = table.current_snapshot().summary
    job = Write("delete", "demo.events", "id < 6", "DELETE FROM demo.events WHERE id < 6")
    (derived,) = lake._report(job, table, "duckdb through the catalog", live=(60, 55))
    assert derived.deleted == int(summary["deleted-records"]) == 50
    assert derived.added == int(summary["added-records"]) == 45
    assert derived.records == 55
    # a count that grew by more than the commit added - another writer in between - is never
    # reported as a negative number of deletions
    assert lake._report(job, table, "x", live=(0, 100))[0].deleted == 0


def test_a_commit_with_no_snapshot_reads_as_metadata_only(lake: Lake) -> None:
    """`ALTER TABLE … ADD COLUMN` makes no snapshot: a real thing to do, reported, not failed."""
    empty = lake.table("demo.empty")
    (written,) = lake._report(Write("alter", "demo.empty", "", "ALTER TABLE demo.empty ADD COLUMN c INT"), empty, "x")
    assert (written.operation, written.added, written.deleted, written.records, written.files) == ("metadata", 0, 0, 0, 0)
    assert Lake._counts(empty) == (0, 0)


def test_live_rows_counts_what_is_there_and_zero_for_what_is_not() -> None:
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS icelens")
    con.execute("CREATE SCHEMA icelens.demo")
    con.execute("CREATE TABLE icelens.demo.t AS SELECT * FROM range(7)")
    assert Lake._live_rows(con, "demo.t") == 7
    assert Lake._live_rows(con, "demo.not_yet") == 0  # before a CREATE there is nothing to count


@pytest.mark.parametrize(
    ("added", "deleted", "line"),
    [
        (100, 0, "append on demo.s · +100 rows · snapshot 7 · e"),
        (0, 10, "delete on demo.s · -10 rows · snapshot 7 · e"),  # a deletion vector adds nothing
        (25, 25, "overwrite on demo.s · +25 rows · -25 rows · snapshot 7 · e"),  # an UPDATE, said as both
        (0, 0, "metadata on demo.s · snapshot 7 · e"),
    ],
)
def test_the_ledger_line_says_only_what_happened(added: int, deleted: int, line: str) -> None:
    operation = line.split(" ", 1)[0]
    assert Written("x", "demo.s", operation, 7, added, deleted, 0, 0, "e", "sql").line() == line

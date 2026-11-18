"""Every pane, against a real table with a real history."""

from __future__ import annotations

import pytest

from icelens import model
from icelens.catalog import IcelensError, Lake
from icelens.model import View


@pytest.fixture()
def view(lake: Lake) -> View:
    return View(lake, "demo.events", limit=10)


def test_catalog_and_namespace_overview(lake: Lake) -> None:
    catalog = model.catalog_overview(lake)
    assert catalog.data.column("namespace").to_pylist() == ["demo"]
    namespace = model.namespace_overview(lake, "demo")
    tables = namespace.data.column("table").to_pylist()
    assert tables == ["empty", "events"]
    rows = dict(zip(tables, namespace.data.column("rows").to_pylist(), strict=True))
    assert rows["events"] == "55"  # 50 + 10 appended, 5 deleted


def test_overview_reports_the_metadata_pointer(view: View) -> None:
    pairs = dict(zip(*[model.overview(view).data.column(c).to_pylist() for c in ("property", "value")], strict=True))
    assert pairs["table"] == "demo.events"
    assert pairs["format version"] == "2"
    assert pairs["snapshots"] == "3"
    assert pairs["partition spec"] == "unpartitioned"


def test_schema_lists_fields_with_ids(view: View) -> None:
    pane = model.schema(view)
    assert pane.data.column("column").to_pylist() == ["id", "name", "ingest_date", "measured_at"]
    assert pane.data.column("id").to_pylist() == [1, 2, 3, 4]
    assert "timestamp" in pane.data.column("type").to_pylist()[3]


def test_snapshots_are_ordered_and_marked(view: View) -> None:
    pane = model.snapshots(view)
    assert pane.data.num_rows == 3
    markers = pane.data.column("").to_pylist()
    assert markers == ["│", "│", "◆"]  # a linear history, newest current
    # a delete that rewrites the one data file is an overwrite in Iceberg terms, not a delete
    assert pane.data.column("operation").to_pylist() == ["append", "append", "overwrite"]
    assert pane.data.column("+rows").to_pylist()[:2] == ["50", "10"]


@pytest.mark.duckdb_extensions
def test_data_pane_reads_a_catalog_duckdb_cannot_attach(view: View) -> None:
    """A SQLite catalog is one DuckDB has never heard of, and the rows still come from DuckDB.

    This is the bridge: PyIceberg resolves the table to its metadata.json, DuckDB scans that.
    The fallback to a PyIceberg scan is still there, but it is now the third choice, not the
    answer for every catalog that is not REST.
    """
    pane = model.data(view)
    assert pane.data.num_rows == 10
    assert "duckdb" in pane.note
    assert view.lake.mode == "bridge"


def test_time_travel_sees_the_older_table(lake: Lake, view: View) -> None:
    first = sorted(view.metadata.snapshots, key=lambda s: s.timestamp_ms)[0]
    old = View(lake, "demo.events", snapshot_id=first.snapshot_id, limit=1000)
    assert old.is_time_travelling
    assert model.data(old).data.num_rows == 50
    assert model.data(view.__class__(lake, "demo.events", limit=1000)).data.num_rows == 55


def test_commit_shows_what_one_snapshot_changed(lake: Lake, view: View) -> None:
    ordered = sorted(view.metadata.snapshots, key=lambda s: s.timestamp_ms)
    second = View(lake, "demo.events", snapshot_id=ordered[1].snapshot_id)
    pane = model.commit(second)
    assert pane.data.column("status").to_pylist() == ["ADDED"]
    assert pane.data.column("rows").to_pylist() == ["10"]
    last = View(lake, "demo.events", snapshot_id=ordered[2].snapshot_id)
    assert set(model.commit(last).data.column("status").to_pylist()) <= {"ADDED", "DELETED"}


def test_manifests_and_files_follow_the_snapshot(lake: Lake, view: View) -> None:
    manifests = model.manifests(view)
    assert manifests.data.num_rows >= 1
    assert "own" in manifests.data.column_names
    files = model.files(view)
    assert files.data.num_rows >= 1
    assert set(files.data.column("content").to_pylist()) <= {"data", "position deletes", "equality deletes"}


def test_layout_draws_the_pointer_chain(view: View) -> None:
    nodes = model.layout(view).data.column("node").to_pylist()
    assert nodes[0] == "table metadata"
    assert nodes[1].strip().startswith("snapshot ")
    assert any(node.strip() == "manifest list" for node in nodes)
    assert any("manifest" in node for node in nodes[3:])


def test_partitions_pane_survives_an_unpartitioned_table(view: View) -> None:
    assert "unpartitioned" in model.partitions(view).note


def test_a_table_with_no_snapshots_says_so(lake: Lake) -> None:
    empty = View(lake, "demo.empty")
    for name in ("snapshots", "commit", "manifests", "files", "layout"):
        pane = model.PANES[name](empty)
        assert "no snapshots" in pane.note


def test_a_missing_snapshot_is_an_error_the_user_can_read(lake: Lake) -> None:
    with pytest.raises(IcelensError, match="not in demo.events"):
        model.overview(View(lake, "demo.events", snapshot_id=1))


@pytest.mark.duckdb_extensions
def test_sql_runs_against_a_non_rest_catalog(lake: Lake) -> None:
    """The SQL pane's promise - `icelens."<ns>"."<table>"` - holds for every catalog type."""
    rows = lake.query('SELECT count(*) AS n FROM icelens."demo"."events"').to_pylist()
    assert rows == [{"n": 55}]


@pytest.mark.duckdb_extensions
def test_sql_time_travel_on_a_bridged_catalog(lake: Lake, view: View) -> None:
    first = sorted(view.metadata.snapshots, key=lambda s: s.timestamp_ms)[0]
    sql = (
        f"SELECT count(*) AS n FROM iceberg_scan("
        f"'{lake.table('demo.events').metadata_location}', snapshot_from_id => {first.snapshot_id})"
    )
    assert lake.query(sql).to_pylist() == [{"n": 50}]


def test_the_file_walking_panes_are_bounded(lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pane that would read ten thousand manifest entries reads the cap and says so.

    Squeezed to two rows here rather than faked: the point is that the cap is applied while
    walking, so the pane costs the cap and not the table.
    """
    monkeypatch.setitem(model.LIMITS, "files", 1)
    view = View(lake, "demo.events", limit=1000)
    pane = model.files(view)
    assert pane.data.num_rows == 1
    assert "showing the first 1 of 2 files" in pane.note


def test_a_bounded_pane_says_nothing_when_it_read_everything(lake: Lake) -> None:
    pane = model.files(View(lake, "demo.events", limit=1000))
    assert "showing the first" not in pane.note


# -- v3 row lineage --------------------------------------------------------------------------
def test_v2_table_shows_no_row_lineage(view: View) -> None:
    """Row lineage is a v3 concept, so on a v2 table every trace of it stays absent.

    An empty `_row_id` column would be worse than none: it would say the table has lineage and
    that it is blank, which is two wrong things.
    """
    assert view.metadata.format_version == 2
    pairs = dict(zip(*[model.overview(view).data.column(c).to_pylist() for c in ("property", "value")], strict=True))
    assert "next row id" not in pairs
    assert "row lineage" not in model.schema(view).data.column("key").to_pylist()
    assert set(model.snapshots(view).data.column("first row id").to_pylist()) == {""}


def test_asking_a_v2_table_for_row_lineage_says_why_it_cannot(lake: Lake) -> None:
    asked = View(lake, "demo.events", limit=10, lineage=True)
    pane = model.data(asked)
    assert "_row_id" not in pane.data.column_names
    assert "v3 only" in pane.note
    assert pane.data.num_rows == 10  # the rows still arrive; only the two columns cannot


def test_v3_table_surfaces_row_lineage_in_the_metadata_panes(v3_lake: Lake) -> None:
    view = View(v3_lake, "demo.events", limit=10)
    assert view.metadata.format_version == 3

    pairs = dict(zip(*[model.overview(view).data.column(c).to_pylist() for c in ("property", "value")], strict=True))
    assert pairs["next row id"] == "60"

    schema_pane = model.schema(view)
    reserved = dict(zip(
        schema_pane.data.column("column").to_pylist(),
        schema_pane.data.column("key").to_pylist(),
        strict=True,
    ))
    assert reserved["_row_id"] == "row lineage"
    assert reserved["_last_updated_sequence_number"] == "row lineage"
    # the reserved columns are shown but not counted as schema fields, because they are not
    assert "4 fields including nested + 2 reserved row-lineage columns (v3)" in schema_pane.note
    ids = schema_pane.data.column("id").to_pylist()
    assert ids == [1, 2, 3, 4, None, None]

    assert model.snapshots(view).data.column("first row id").to_pylist() == ["0", "50"]


@pytest.mark.duckdb_extensions
def test_a_v3_table_with_no_assigned_row_ids_says_null_rather_than_nothing(v3_lake: Lake) -> None:
    """DuckDB computes `_row_id` from the data file's `first_row_id`, which this one has not got.

    The metadata says format version 3; the manifests below it were written by a v2 writer and
    carry no `first_row_id`, so there is no base to count from and the honest answer is NULL.
    `_last_updated_sequence_number` comes from the manifest's own sequence number, so it does
    resolve - which is the useful half on a table part-way to v3.
    """
    pane = model.data(View(v3_lake, "demo.events", limit=5, lineage=True))
    assert pane.data.column_names[:2] == ["_row_id", "_last_updated_sequence_number"]
    assert pane.data.column("_row_id").to_pylist() == [None] * 5
    # which file a 5-row scan lands in is the scanner's business, so assert the sequence
    # numbers are real and from this table's two commits, not which one came back first
    assert set(pane.data.column("_last_updated_sequence_number").to_pylist()) <= {1, 2}
    assert None not in pane.data.column("_last_updated_sequence_number").to_pylist()


def test_row_lineage_degrades_out_loud_when_the_route_cannot_compute_it(
    v3_lake: Lake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PyIceberg 0.12 has no reserved-field constant for `_row_id` and no way to scan for it.

    The rows are worth more than the two columns, so the pane falls back to them - but the
    engine string says so, because a silently missing column teaches that v3 has no lineage.
    """
    monkeypatch.setattr(type(v3_lake), "_duck", lambda self: None)
    pane = model.data(View(v3_lake, "demo.events", limit=10, lineage=True))
    assert pane.data.num_rows == 10
    assert "_row_id" not in pane.data.column_names
    assert "pyiceberg (no row lineage here)" in pane.note

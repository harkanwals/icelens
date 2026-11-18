"""The panes, against table shapes `demo.events` does not have.

`test_model.py` reads one table with a tidy history. Real tables are less tidy: nested types,
manifests that were merged so a commit's own manifest is mostly inherited entries, a table deleted
down to nothing, a namespace that will not list, more files than a pane will read. Each of those is
built here for real - PyIceberg writing to a SQLite catalog - and each pane is asked about it.
"""

from __future__ import annotations

import pyarrow as pa
import pytest
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    IntegerType,
    ListType,
    LongType,
    MapType,
    NestedField,
    StringType,
    StructType,
)

from icelens import model, terms
from icelens.catalog import IcelensError, Lake, Target
from icelens.model import View


def _rows(ids: list[int]) -> pa.Table:
    return pa.table({"id": pa.array(ids, pa.int64()), "region": pa.array([f"r{i % 3}" for i in ids], pa.string())})


@pytest.fixture(scope="module")
def shapes(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    home = tmp_path_factory.mktemp("panes")
    lake = Lake(Target(properties={"type": "sql", "uri": f"sqlite:///{home / 'catalog.db'}", "warehouse": home.as_uri()}))
    catalog = lake.catalog
    catalog.create_namespace("demo")

    nested = Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "point", StructType(NestedField(3, "x", IntegerType()), NestedField(4, "y", IntegerType()))),
        NestedField(5, "tags", ListType(6, StringType())),
        NestedField(7, "attrs", MapType(8, StringType(), 9, LongType())),
        identifier_field_ids=[1],
    )
    catalog.create_table("demo.nested", schema=nested, properties={"owner": "the data team", "comment": "nested on purpose"})

    # Manifest merging on, and at a low threshold: every append after the first rewrites the
    # manifest list into one manifest, which the new snapshot owns and which is mostly the
    # files earlier snapshots wrote - EXISTING entries in a manifest the commit itself added.
    merged = catalog.create_table(
        "demo.merged",
        schema=_rows([]).schema,
        properties={"commit.manifest-merge.enabled": "true", "commit.manifest.min-count-to-merge": "2"},
    )
    for start in (0, 10, 20):
        merged.append(_rows(list(range(start, start + 10))))

    gone = catalog.create_table("demo.gone", schema=_rows([]).schema)
    gone.append(_rows([1, 2]))
    gone.delete("id >= 0")

    parted = catalog.create_table(
        "demo.parted",
        schema=Schema(NestedField(1, "id", LongType()), NestedField(2, "region", StringType())),
        partition_spec=PartitionSpec(PartitionField(2, 1000, IdentityTransform(), "region")),
    )
    parted.append(_rows(list(range(30))))
    yield lake
    lake.close()


# -- Schema: nesting --------------------------------------------------------------------------------


def test_nested_fields_are_listed_under_their_parents_with_their_own_ids(shapes: Lake) -> None:
    pane = model.schema(View(shapes, "demo.nested"))
    rows = pane.records()
    assert [(r["name"], r["depth"]) for r in rows] == [
        ("id", 0),
        ("point", 0), ("x", 1), ("y", 1),
        ("tags", 0), ("element", 1),
        ("attrs", 0), ("key", 1), ("value", 1),
    ]  # fmt: skip
    # the ids are the table's - PyIceberg assigns fresh ones on create - and every one is distinct
    schema = shapes.table("demo.nested").schema()
    paths = ("id", "point", "point.x", "point.y", "tags", "tags.element", "attrs", "attrs.key", "attrs.value")
    assert [r["id"] for r in rows] == [schema.find_field(path).field_id for path in paths]
    assert len({r["id"] for r in rows}) == len(rows)
    types = {r["name"]: r["type"] for r in rows if r["depth"] == 0}
    assert types == {"id": "long", "point": "struct<2 fields>", "tags": "list<string>", "attrs": "map<string, long>"}
    assert pane.data.column("column").to_pylist()[2] == "   └ x"
    # the key column says identifier for the one field the schema names as one
    assert pane.data.column("key").to_pylist()[0] == "identifier"
    assert [r["identifier"] for r in rows].count(True) == 1
    assert pane.data.column("required").to_pylist()[0] == "required"


def test_table_properties_are_rows_of_the_overview(shapes: Lake) -> None:
    pane = model.overview(View(shapes, "demo.nested"))
    pairs = dict(zip(pane.data.column("property").to_pylist(), pane.data.column("value").to_pylist(), strict=True))
    assert pairs["property owner"] == "the data team"
    assert pane.records()[0]["properties"] == {"comment": "nested on purpose", "owner": "the data team"}
    # and the rail explains a property row as what it is: a line of the metadata file
    assert terms.resolve("overview", "property", "property owner").name == "Table metadata (metadata.json)"


# -- Commit: what a commit inherited is not what it changed ----------------------------------------------


def test_a_merged_manifest_reports_only_what_the_commit_added(shapes: Lake) -> None:
    """The third append's own manifest lists 30 files' worth of rows and added ten of them.

    Its EXISTING entries were written by the two commits before it; reporting them as this
    commit's changes would claim the append wrote three files. The Manifests pane shows the
    merge for what it is - one manifest, one added and two existing - and Commit shows one file.
    """
    view = View(shapes, "demo.merged")
    (manifest,) = model.manifests(view).records()
    assert (manifest["own"], manifest["added"], manifest["existing"]) == (True, 1, 2)
    commit = model.commit(view)
    assert [(r["status"], r["rows"]) for r in commit.records()] == [("ADDED", 10)]
    assert "+10 rows" in commit.note and "across 1 new manifests of 1" in commit.note


def test_the_commit_pane_stops_at_its_cap_and_says_how_many_it_did_not_read(shapes: Lake, monkeypatch) -> None:
    """The delete that emptied `demo.gone` retired its one file; a cap of zero reads none of that."""
    monkeypatch.setitem(model.LIMITS, "commit", 0)
    pane = model.commit(View(shapes, "demo.gone"))
    assert pane.records() == []
    assert "showing the first 0 of 1 changed files" in pane.note


# -- a table deleted down to nothing ---------------------------------------------------------------------


def test_a_table_deleted_to_nothing_says_so_in_every_pane(shapes: Lake) -> None:
    view = View(shapes, "demo.gone")
    assert model.data(view).data.num_rows == 0
    assert "has no live files" in model.files(view).note
    # the manifest list still lists the manifest that recorded the deletion
    (manifest,) = model.manifests(view).records()
    assert (manifest["deleted"], manifest["-rows"]) == (1, 2)
    retired = model.commit(view).records()
    assert [(r["status"], r["rows"]) for r in retired] == [("DELETED", 2)]
    layout = model.layout(view).records()
    assert [r["kind"] for r in layout] == ["table-metadata", "snapshot", "manifest-list", "manifest", "file"]
    assert layout[-1]["status"] == "DELETED"


# -- Layout: the cap ------------------------------------------------------------------------------------


def test_the_layout_stops_at_its_cap_and_marks_where(shapes: Lake, monkeypatch) -> None:
    monkeypatch.setitem(model.LIMITS, "layout", 5)
    pane = model.layout(View(shapes, "demo.parted"))
    kinds = [r["kind"] for r in pane.records()]
    assert kinds[-1] == "truncated"
    assert kinds[:4] == ["table-metadata", "snapshot", "manifest-list", "manifest"]
    assert "stopped at the first 5 nodes" in pane.note


def test_the_files_pane_stops_at_its_cap(shapes: Lake, monkeypatch) -> None:
    monkeypatch.setitem(model.LIMITS, "files", 2)
    pane = model.files(View(shapes, "demo.parted"))
    assert len(pane.records()) == 2
    assert "showing the first 2 of 3 files" in pane.note


# -- partitions, as each pane spells them ------------------------------------------------------------------


def test_a_partition_reads_as_name_equals_value(shapes: Lake) -> None:
    files = model.files(View(shapes, "demo.parted")).records()
    assert sorted(r["partition"] for r in files) == ["region=r0", "region=r1", "region=r2"]
    assert all(r["spec_id"] == 0 for r in files)


@pytest.mark.parametrize(
    ("record", "names", "shown"),
    [
        (None, None, ""),  # no partition record at all
        (42, None, "42"),  # not a tuple: shown as itself
        ([], ["region"], "(unpartitioned)"),  # a spec that names a field, and a file written before it
        (["eu", 7], None, "eu, 7"),  # values the spec does not name, shown in order
        (["eu", 7], ["region", "bucket"], "region=eu, bucket=7"),
        ([], [], ""),  # the unpartitioned table's empty record, against its empty spec
    ],
)
def test_a_partition_record_is_shown_whatever_shape_it_arrives_in(record, names, shown: str) -> None:
    class Spec:
        fields = [type("Field", (), {"name": name})() for name in names or []]

    assert model._partition(record, Spec() if names is not None else None) == shown


# -- the catalog and namespace overviews ---------------------------------------------------------------


def test_a_namespace_that_will_not_list_is_still_a_row(shapes: Lake, monkeypatch) -> None:
    """One namespace refusing must not empty the catalog pane, and the reason goes in its row."""
    real = shapes.tables

    def stubborn(namespace: str) -> list[str]:
        if namespace == "demo":
            raise IcelensError("cannot list the tables in demo: permission denied")
        return real(namespace)

    monkeypatch.setattr(shapes, "tables", stubborn)
    pane = model.catalog_overview(shapes)
    (row,) = [r for r in pane.records() if r["namespace"] == "demo"]
    assert row["tables"] is None
    assert "permission denied" in row["note"]


def test_a_snapshot_the_table_does_not_have_is_refused_by_every_pane(shapes: Lake) -> None:
    for name in ("overview", "schema", "data", "commit", "manifests", "files", "layout"):
        with pytest.raises(IcelensError, match="snapshot 1 is not in demo.parted"):
            model.PANES[name](View(shapes, "demo.parted", snapshot_id=1))

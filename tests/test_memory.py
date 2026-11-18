"""The in-memory catalog the in-browser demo runs on (icelens.memory), on CPython.

It replaces SQLite there to save SQLAlchemy's import, so it has to do everything the demo asks of
a catalog: the five commits, the tag, schema and partition evolution, and the reads every pane
makes. CI's browser job runs the same demo on it under Pyodide.
"""

from __future__ import annotations

from typing import Any

import pyarrow as pa
import pytest
from pyiceberg.exceptions import (
    CommitFailedException,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    TableAlreadyExistsError,
)
from pyiceberg.schema import Schema
from pyiceberg.types import LongType, NestedField

from icelens import demo, memory
from icelens.catalog import Lake

SCHEMA = Schema(NestedField(1, "id", LongType(), required=False))


@pytest.fixture
def lake(tmp_path: Any) -> Any:
    lake = Lake(memory.memory_target("demo", tmp_path), read_only=True)
    yield lake
    lake.close()
    memory.forget()


def test_it_is_an_in_memory_sandbox(lake: Lake) -> None:
    assert lake.target.catalog_type == "in-memory"
    assert lake.is_sandbox
    assert type(lake.catalog).__name__ == "MemoryCatalog"


def test_the_demo_writes_its_whole_history(lake: Lake) -> None:
    seeded = demo.seed(lake)
    table = lake.table(seeded.table)
    assert [s.summary.operation.value for s in table.snapshots()] == ["append", "append", "overwrite", "append", "append"]
    assert "first-load" in table.metadata.refs
    assert len(table.metadata.schemas) == 2 and len(table.metadata.partition_specs) == 2
    rows, engine = lake.rows(seeded.table, limit=500)
    assert rows.num_rows == 183
    # A second seed finds the table, as it does in SQLite: the page's restart depends on it.
    assert demo.seed(lake).table == seeded.table


def test_another_lake_on_the_same_warehouse_sees_the_same_tables(lake: Lake, tmp_path: Any) -> None:
    demo.seed(lake)
    other = Lake(memory.memory_target("demo", tmp_path), read_only=True)
    try:
        assert other.tables("demo") == ["demo.orders"]
    finally:
        other.close()


def test_namespaces(lake: Lake) -> None:
    catalog = lake.catalog
    catalog.create_namespace("a", {"owner": "me"})
    catalog.create_namespace(("a", "b"))
    with pytest.raises(NamespaceAlreadyExistsError):
        catalog.create_namespace("a")
    assert catalog.list_namespaces() == [("a",)]
    assert catalog.list_namespaces("a") == [("a", "b")]
    assert catalog.load_namespace_properties("a") == {"owner": "me"}
    summary = catalog.update_namespace_properties("a", removals={"owner", "nope"}, updates={"k": "v"})
    assert summary.removed == ["owner"] and summary.missing == ["nope"] and summary.updated == ["k"]
    catalog.create_table("a.t", SCHEMA)
    with pytest.raises(NamespaceNotEmptyError):
        catalog.drop_namespace("a")
    with pytest.raises(NoSuchNamespaceError):
        catalog.list_tables("missing")


def test_tables(lake: Lake) -> None:
    catalog = lake.catalog
    catalog.create_namespace("n")
    with pytest.raises(NoSuchNamespaceError):
        catalog.create_table("missing.t", SCHEMA)
    table = catalog.create_table("n.t", SCHEMA)
    with pytest.raises(TableAlreadyExistsError):
        catalog.create_table("n.t", SCHEMA)
    table.append(pa.table({"id": pa.array([1, 2], pa.int64())}))
    assert catalog.load_table("n.t").scan().to_arrow().num_rows == 2
    renamed = catalog.rename_table("n.t", "n.u")
    assert catalog.list_tables("n") == [("n", "u")] and renamed.scan().to_arrow().num_rows == 2
    again = catalog.register_table("n.t", renamed.metadata_location)
    assert again.scan().to_arrow().num_rows == 2
    catalog.drop_table("n.u")
    with pytest.raises(NoSuchTableError):
        catalog.load_table("n.u")
    assert catalog.list_views("n") == [] and not catalog.view_exists("n.v")


def test_a_commit_based_on_old_metadata_is_refused_and_retried(lake: Lake, caplog: Any) -> None:
    catalog = lake.catalog
    catalog.create_namespace("n")
    first = catalog.create_table("n.t", SCHEMA)
    stale = catalog.load_table("n.t")
    first.append(pa.table({"id": pa.array([1], pa.int64())}))
    # The stale table's commit names metadata that is no longer current: the catalog refuses it,
    # and PyIceberg refreshes and retries, as it does against a SQL or REST catalog. Both stay.
    stale.append(pa.table({"id": pa.array([2], pa.int64())}))
    assert "concurrent update" in caplog.text
    assert sorted(catalog.load_table("n.t").scan().to_arrow()["id"].to_pylist()) == [1, 2]


def test_the_pointer_moves_only_if_no_commit_landed_in_between(lake: Lake, monkeypatch: Any) -> None:
    catalog = lake.catalog
    catalog.create_namespace("n")
    table = catalog.create_table("n.t", SCHEMA)
    write = catalog._write_metadata

    def another_commit_lands(*args: Any, **kwargs: Any) -> None:
        write(*args, **kwargs)
        catalog._store.tables[(("n",), "t")] = "another-commit.metadata.json"

    monkeypatch.setattr(catalog, "_write_metadata", another_commit_lands)
    from pyiceberg.table.update import SetPropertiesUpdate

    with pytest.raises(CommitFailedException):
        catalog.commit_table(table, (), (SetPropertiesUpdate(updates={"k": "v"}),))
    assert catalog._store.tables[(("n",), "t")] == "another-commit.metadata.json"


def test_a_warehouse_is_required() -> None:
    with pytest.raises(ValueError, match="warehouse"):
        memory.MemoryCatalog("x")

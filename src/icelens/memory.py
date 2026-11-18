"""A PyIceberg catalog kept in a dict, for the in-browser demo.

The sandbox's catalog is SQLite through SQLAlchemy, and under Pyodide importing SQLAlchemy is
most of a second of the wait before the first frame (`sqlalchemy` and `sqlalchemy.orm`, measured
with `-X importtime`), on top of its 1.9 MB download. A page that forgets everything on reload
has nothing for a database to do: the table files live in Pyodide's in-memory filesystem and so
can the pointers to them. Opened by PyIceberg through `py-catalog-impl` (see `memory_target`).

The pointers live in a module-level store keyed by warehouse, not in the catalog object, because
icelens opens a fresh catalog per `Lake` and the demo is written by one lake and read by another.
A commit swaps a table's pointer only if it still names the metadata the commit was based on,
which is the same optimistic check SQL and REST catalogs make.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

from pyiceberg.catalog import Catalog, MetastoreCatalog, PropertiesUpdateSummary
from pyiceberg.exceptions import (
    CommitFailedException,
    NamespaceAlreadyExistsError,
    NamespaceNotEmptyError,
    NoSuchNamespaceError,
    NoSuchTableError,
    NoSuchViewError,
    TableAlreadyExistsError,
)
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC, PartitionSpec
from pyiceberg.serializers import FromInputFile
from pyiceberg.table import CommitTableResponse, Table
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER, SortOrder
from pyiceberg.typedef import EMPTY_DICT, Identifier, Properties

#: What `py-catalog-impl` names to open this catalog; `Target.catalog_type` reports it as in-memory.
IMPL = "icelens.memory.MemoryCatalog"


@dataclass
class _Store:
    namespaces: dict[tuple[str, ...], dict[str, str]] = field(default_factory=dict)
    tables: dict[tuple[tuple[str, ...], str], str] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)


_STORES: dict[str, _Store] = {}
_STORES_LOCK = threading.Lock()


def forget(warehouse: str | None = None) -> None:
    """Drop the store for one warehouse, or every store. The files are the caller's to delete."""
    with _STORES_LOCK:
        if warehouse is None:
            _STORES.clear()
        else:
            _STORES.pop(warehouse, None)


class MemoryCatalog(MetastoreCatalog):
    def __init__(self, name: str, **properties: str):
        super().__init__(name, **properties)
        warehouse = properties.get("warehouse", "")
        if not warehouse:
            raise ValueError("an in-memory catalog needs a warehouse: the directory its tables are written under")
        with _STORES_LOCK:
            self._store = _STORES.setdefault(warehouse, _Store())

    # -- namespaces ------------------------------------------------------------------
    def _namespace(self, namespace: str | Identifier) -> tuple[str, ...]:
        return tuple(Catalog.identifier_to_tuple(namespace))

    def create_namespace(self, namespace: str | Identifier, properties: Properties = EMPTY_DICT) -> None:
        key = self._namespace(namespace)
        with self._store.lock:
            if key in self._store.namespaces:
                raise NamespaceAlreadyExistsError(f"Namespace already exists: {'.'.join(key)}")
            self._store.namespaces[key] = dict(properties)

    def drop_namespace(self, namespace: str | Identifier) -> None:
        key = self._namespace(namespace)
        with self._store.lock:
            if key not in self._store.namespaces:
                raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(key)}")
            if any(ns == key for ns, _ in self._store.tables):
                raise NamespaceNotEmptyError(f"Namespace is not empty: {'.'.join(key)}")
            del self._store.namespaces[key]

    def list_namespaces(self, namespace: str | Identifier = ()) -> list[Identifier]:
        parent = self._namespace(namespace) if namespace else ()
        if parent and parent not in self._store.namespaces:
            raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(parent)}")
        depth = len(parent) + 1
        with self._store.lock:  # a create on another thread would change the dict mid-loop
            return sorted({ns[:depth] for ns in self._store.namespaces if len(ns) >= depth and ns[: len(parent)] == parent})

    def load_namespace_properties(self, namespace: str | Identifier) -> Properties:
        key = self._namespace(namespace)
        if key not in self._store.namespaces:
            raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(key)}")
        return dict(self._store.namespaces[key])

    def update_namespace_properties(
        self, namespace: str | Identifier, removals: set[str] | None = None, updates: Properties = EMPTY_DICT
    ) -> PropertiesUpdateSummary:
        key = self._namespace(namespace)
        with self._store.lock:
            if key not in self._store.namespaces:
                raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(key)}")
            current = self._store.namespaces[key]
            removed = [k for k in removals or () if k in current]
            missing = [k for k in removals or () if k not in current]
            for k in removed:
                del current[k]
            current.update(updates)
        return PropertiesUpdateSummary(removed=removed, updated=list(updates), missing=missing)

    # -- tables ----------------------------------------------------------------------
    def _key(self, identifier: str | Identifier) -> tuple[tuple[str, ...], str]:
        return tuple(Catalog.namespace_from(identifier)), Catalog.table_name_from(identifier)

    def _register(self, identifier: str | Identifier, metadata_location: str) -> None:
        namespace, name = self._key(identifier)
        with self._store.lock:
            if namespace not in self._store.namespaces:
                raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(namespace)}")
            if (namespace, name) in self._store.tables:
                raise TableAlreadyExistsError(f"Table {'.'.join(namespace)}.{name} already exists")
            self._store.tables[(namespace, name)] = metadata_location

    def create_table(
        self,
        identifier: str | Identifier,
        schema: Any,
        location: str | None = None,
        partition_spec: PartitionSpec = UNPARTITIONED_PARTITION_SPEC,
        sort_order: SortOrder = UNSORTED_SORT_ORDER,
        properties: Properties = EMPTY_DICT,
    ) -> Table:
        namespace, name = self._key(identifier)
        if namespace not in self._store.namespaces:
            raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(namespace)}")
        if (namespace, name) in self._store.tables:
            raise TableAlreadyExistsError(f"Table {'.'.join(namespace)}.{name} already exists")
        staged = self._create_staged_table(identifier, schema, location, partition_spec, sort_order, properties)
        self._write_metadata(staged.metadata, staged.io, staged.metadata_location)
        self._register(identifier, staged.metadata_location)
        return self.load_table(identifier)

    def register_table(self, identifier: str | Identifier, metadata_location: str) -> Table:
        self._register(identifier, metadata_location)
        return self.load_table(identifier)

    def load_table(self, identifier: str | Identifier) -> Table:
        namespace, name = self._key(identifier)
        location = self._store.tables.get((namespace, name))
        if location is None:
            raise NoSuchTableError(f"Table does not exist: {'.'.join(namespace)}.{name}")
        io = self._load_file_io(properties=self.properties, location=location)
        metadata = FromInputFile.table_metadata(io.new_input(location))
        return Table(
            identifier=(*namespace, name),
            metadata=metadata,
            metadata_location=location,
            io=self._load_file_io(metadata.properties, location),
            catalog=self,
        )

    def commit_table(self, table: Table, requirements: tuple[Any, ...], updates: tuple[Any, ...]) -> CommitTableResponse:
        namespace, name = self._key(table.name())
        try:
            current: Table | None = self.load_table(table.name())
        except NoSuchTableError:
            current = None
        staged = self._update_and_stage_table(current, table.name(), requirements, updates)
        if current is not None and staged.metadata == current.metadata:
            # By field name, as PyIceberg's own catalogs pass it; pyright sees only the alias.
            return CommitTableResponse(metadata=current.metadata, metadata_location=current.metadata_location)  # pyright: ignore[reportCallIssue]
        self._write_metadata(metadata=staged.metadata, io=staged.io, metadata_path=staged.metadata_location)
        with self._store.lock:
            now = self._store.tables.get((namespace, name))
            expected = current.metadata_location if current is not None else None
            if now != expected:
                raise CommitFailedException(f"Table {'.'.join(namespace)}.{name} was changed by another commit")
            self._store.tables[(namespace, name)] = staged.metadata_location
        return CommitTableResponse(metadata=staged.metadata, metadata_location=staged.metadata_location)  # pyright: ignore[reportCallIssue]

    def drop_table(self, identifier: str | Identifier) -> None:
        namespace, name = self._key(identifier)
        with self._store.lock:
            if self._store.tables.pop((namespace, name), None) is None:
                raise NoSuchTableError(f"Table does not exist: {'.'.join(namespace)}.{name}")

    def rename_table(self, from_identifier: str | Identifier, to_identifier: str | Identifier) -> Table:
        source, target = self._key(from_identifier), self._key(to_identifier)
        with self._store.lock:
            if source not in self._store.tables:
                raise NoSuchTableError(f"Table does not exist: {'.'.join(source[0])}.{source[1]}")
            if target[0] not in self._store.namespaces:
                raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(target[0])}")
            if target in self._store.tables:
                raise TableAlreadyExistsError(f"Table {'.'.join(target[0])}.{target[1]} already exists")
            self._store.tables[target] = self._store.tables.pop(source)
        return self.load_table(to_identifier)

    def list_tables(self, namespace: str | Identifier) -> list[Identifier]:
        key = self._namespace(namespace)
        if key not in self._store.namespaces:
            raise NoSuchNamespaceError(f"Namespace does not exist: {'.'.join(key)}")
        with self._store.lock:
            return sorted((*ns, name) for ns, name in self._store.tables if ns == key)

    # -- views: none, and nothing in icelens makes one ---------------------------------
    def list_views(self, namespace: str | Identifier) -> list[Identifier]:
        return []

    def view_exists(self, identifier: str | Identifier) -> bool:
        return False

    def load_view(self, identifier: str | Identifier) -> Any:
        raise NoSuchViewError(f"View does not exist: {identifier}")

    def drop_view(self, identifier: str | Identifier) -> None:
        raise NoSuchViewError(f"View does not exist: {identifier}")

    def register_view(self, identifier: str | Identifier, metadata_location: str) -> Any:
        raise NotImplementedError("an in-memory catalog keeps no views")


def memory_target(name: str, root: Any) -> Any:
    """A sandbox like `sandbox_target`'s v2 one, with its catalog in this module instead of SQLite."""
    from icelens.catalog import Target, _file_uri, make_sandbox_home

    home = make_sandbox_home(name, root)
    return Target(
        properties={
            "py-catalog-impl": IMPL,
            "warehouse": _file_uri(home / "warehouse"),
            "icelens.sandbox": name,
        },
        profile=None,
        origin=f"sandbox {name!r}, in memory",
    )

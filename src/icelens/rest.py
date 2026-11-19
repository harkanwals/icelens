"""A small Iceberg REST catalog that icelens runs itself, so a sandbox can be written at v3.

Nothing offline can write a format-version 3 table. PyIceberg 0.12 refuses outright (its v3
serialiser is a stub), and DuckDB writes Iceberg to a bare directory only at v2, ignoring any
version it is asked for. DuckDB writes v3 through an attached REST catalog, and only there -
which used to mean a Lakekeeper or a Polaris, a database and a container, for a lesson.

In the REST protocol the *server* writes `metadata.json`: a client writes its data files,
manifests and manifest list, then sends the catalog a list of updates - add this snapshot,
point `main` at it - and the catalog applies them to the table's metadata and writes the next
version of it. So this module writes table metadata as JSON itself, following what Java
Iceberg writes, and leaves everything below the metadata file to the client. That is what
makes v3 reachable: no Iceberg library on this side has to support it, only the spec does.

The server is small on purpose:

- bound to `127.0.0.1`, on a port the OS picks, and refusing any request without the bearer
  token this process generated - a sandbox is somebody's files, and another user on the same
  machine is not somebody who should be able to write them;
- stored in the sandbox's own directory: `catalog.db` in the schema PyIceberg's SQL catalog
  uses, so the same directory opens either way, and metadata files beside the data;
- local files only, no credentials to vend, and no views.

It serves what DuckDB 1.5 and PyIceberg 0.12 ask of a catalog - configuration, namespaces
(nested too), tables, staged creates, single- and multi-table commits, drops with purge,
renames and registration - and was checked against both of them, not against the spec alone.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_log = logging.getLogger("icelens.rest")

#: The catalog name rows are stored under, the one PyIceberg's SQL catalog uses when icelens opens it.
CATALOG = "icelens"

#: The URL prefix tables live under. A REST catalog may serve several warehouses; this one serves one.
PREFIX = "sandbox"

#: Iceberg's separator between namespace levels inside one path segment.
UNIT = "\x1f"

#: The largest request body this will read. Commits are small; anything bigger is not a commit.
MAX_BODY = 16 * 1024 * 1024

#: How many earlier metadata files a table's `metadata-log` remembers, as Java's default.
PREVIOUS_VERSIONS_MAX = 100

#: Requests this server answers, advertised in `/v1/config` so a client uses no others.
ENDPOINTS = [
    "GET /v1/{prefix}/namespaces",
    "POST /v1/{prefix}/namespaces",
    "GET /v1/{prefix}/namespaces/{namespace}",
    "HEAD /v1/{prefix}/namespaces/{namespace}",
    "DELETE /v1/{prefix}/namespaces/{namespace}",
    "POST /v1/{prefix}/namespaces/{namespace}/properties",
    "GET /v1/{prefix}/namespaces/{namespace}/tables",
    "POST /v1/{prefix}/namespaces/{namespace}/tables",
    "GET /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "HEAD /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "DELETE /v1/{prefix}/namespaces/{namespace}/tables/{table}",
    "POST /v1/{prefix}/namespaces/{namespace}/register",
    "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}/metrics",
    "POST /v1/{prefix}/tables/rename",
    "POST /v1/{prefix}/transactions/commit",
    "GET /v1/{prefix}/namespaces/{namespace}/views",
    "POST /v1/{prefix}/namespaces/{namespace}/views",
    "GET /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "HEAD /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "POST /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "DELETE /v1/{prefix}/namespaces/{namespace}/views/{view}",
    "POST /v1/{prefix}/views/rename",
]

#: How a row in `iceberg_tables` is told apart: PyIceberg's and Java's JDBC catalogs both
#: write `TABLE` or `VIEW`; an older catalog.db has NULL, which was always a table.
_IS_TABLE = "COALESCE(iceberg_type, 'TABLE') = 'TABLE'"


class RestError(Exception):
    """An answer in the REST spec's error shape: a status, an exception type, a message."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


def _not_found(kind: str, message: str) -> RestError:
    return RestError(404, kind, message)


def _conflict(kind: str, message: str) -> RestError:
    return RestError(409, kind, message)


def _bad(message: str) -> RestError:
    return RestError(400, "BadRequestException", message)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _file_uri(path: Path) -> str:
    """`file:///…` with the path as it is: no percent-encoding, as `catalog._file_uri` writes it.

    Neither DuckDB nor PyIceberg 0.12 decodes a `file://` path, so `Path.as_uri()` put a v3
    sandbox under `dir with space` in a sibling directory called `dir%20with%20space`, and
    DuckDB's first write there failed. Forward slashes, so a drive letter reads `file:///C:/…`.
    """
    return "file:///" + path.as_posix().lstrip("/")


def _local(uri: str) -> Path:
    """The local path a `file:` URI names, read the way `_file_uri` writes it: not decoded.

    Anything but this machine's files is not this server's to touch, so another host is
    refused, and so is a `?` or `#`, which PyIceberg would cut the path at and DuckDB would not.
    """
    parsed = urllib.parse.urlsplit(uri if isinstance(uri, str) else "")
    if parsed.scheme != "file" or parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
        raise _bad(f"this catalog keeps tables in local files, and {uri!r} is not one")
    path = parsed.path
    if re.match(r"/[A-Za-z]:", path):
        path = path[1:]  # `file:///C:/x` is `C:/x`, not a directory called `C:` at the root
    return Path(path)


def _read_metadata(location: str) -> dict[str, Any]:
    """The table metadata at `location`, or an error naming the file - never what is in it."""
    try:
        metadata = json.loads(_local(location).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _not_found("NotFoundException", f"Metadata file does not exist: {location}") from exc
    except OSError as exc:
        raise RestError(500, "ServiceFailureException", f"Cannot read metadata file {location}: {exc.strerror}") from exc
    except ValueError:  # not UTF-8, or not JSON: and its bytes are not this answer's to repeat
        metadata = None
    if not isinstance(metadata, dict) or not all(key in metadata for key in ("format-version", "table-uuid", "location")):
        raise RestError(500, "ServiceFailureException", f"Not table metadata: {location}")
    return metadata


#: What a name may not hold: a path separator, or a control character (C0, DEL and C1).
_UNSAFE = re.compile(r"[/\\\x00-\x1f\x7f-\x9f]")


def _table_name(name: Any) -> str:
    """A table name, refused when it could not also be the directory its default location is."""
    if not isinstance(name, str) or name in ("", ".", "..") or _UNSAFE.search(name):
        raise _bad(f"not a table name this catalog can keep: {name!r}")
    return name


def _namespace_name(levels: Any) -> str:
    """Namespace levels as the dotted name the store keeps, and the `<name>.db` directory tables
    get. A dot would read back as two levels, and a separator as more than one directory."""
    if not isinstance(levels, list) or not levels or not all(
        isinstance(level, str) and level and "." not in level and not _UNSAFE.search(level) for level in levels
    ):
        raise _bad(f"not a namespace this catalog can keep: {levels!r}")
    return ".".join(levels)


# -- storage -------------------------------------------------------------------------------


def _unmarked(properties: dict[str, str]) -> None:
    """Refuse to set `exists`, the row PyIceberg's SQL schema marks a namespace with: it is not a
    property this catalog can store, and reading it back would never show it."""
    if "exists" in properties:
        raise _bad("`exists` is reserved: it marks the namespace itself in this catalog's tables")


class Store:
    """Which tables exist and where their current metadata is - in PyIceberg's SQL schema.

    The same `catalog.db` can therefore be opened by PyIceberg's own SQL catalog, which is how
    an offline sandbox has always been read. Every change to a table pointer is a compare-and-
    swap on its old value, so two writers - two icelens processes on one sandbox - cannot
    silently overwrite each other: the second one gets the 409 the REST spec says it should.
    """

    def __init__(self, database: Path, warehouse: Path) -> None:
        self.database = database
        self.warehouse = warehouse
        warehouse.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS iceberg_tables ("
                "catalog_name VARCHAR(255) NOT NULL, table_namespace VARCHAR(255) NOT NULL, "
                "table_name VARCHAR(255) NOT NULL, metadata_location VARCHAR(1000), "
                "previous_metadata_location VARCHAR(1000), iceberg_type VARCHAR(5), "
                "PRIMARY KEY (catalog_name, table_namespace, table_name))"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS iceberg_namespace_properties ("
                "catalog_name VARCHAR(255) NOT NULL, namespace VARCHAR(255) NOT NULL, "
                "property_key VARCHAR(255) NOT NULL, property_value VARCHAR(1000) NOT NULL, "
                "PRIMARY KEY (catalog_name, namespace, property_key))"
            )
            columns = {row[1] for row in con.execute("PRAGMA table_info(iceberg_tables)")}
            if "iceberg_type" not in columns:  # a catalog.db an older PyIceberg made
                con.execute("ALTER TABLE iceberg_tables ADD COLUMN iceberg_type VARCHAR(5)")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database, timeout=30)

    # -- namespaces --------------------------------------------------------------------
    def namespaces(self) -> set[str]:
        """Every namespace: those with properties, and those only implied by a table in them."""
        with self._connect() as con:
            named = {row[0] for row in con.execute(
                "SELECT DISTINCT namespace FROM iceberg_namespace_properties WHERE catalog_name = ?", (CATALOG,)
            )}
            implied = {row[0] for row in con.execute(
                "SELECT DISTINCT table_namespace FROM iceberg_tables WHERE catalog_name = ?", (CATALOG,)
            )}
        return named | implied

    def namespace_properties(self, namespace: str) -> dict[str, str]:
        if namespace not in self.namespaces():
            raise _not_found("NoSuchNamespaceException", f"Namespace does not exist: {namespace}")
        with self._connect() as con:
            rows = con.execute(
                "SELECT property_key, property_value FROM iceberg_namespace_properties "
                "WHERE catalog_name = ? AND namespace = ?",
                (CATALOG, namespace),
            ).fetchall()
        return {key: value for key, value in rows if key != "exists"}

    def create_namespace(self, namespace: str, properties: dict[str, str]) -> None:
        if not namespace or any(not level for level in namespace.split(".")):
            raise _bad(f"not a namespace: {namespace!r}")
        _unmarked(properties)
        known = self.namespaces()
        if namespace in known:
            raise _conflict("AlreadyExistsException", f"Namespace already exists: {namespace}")
        parent = namespace.rpartition(".")[0]
        if parent and parent not in known:  # else it would be listed, and answer 404 when asked for
            raise _not_found("NoSuchNamespaceException", f"Namespace does not exist: {parent}")
        try:
            with self._connect() as con:
                # PyIceberg's SQL catalog marks a namespace with no properties by `exists=true`.
                for key, value in {"exists": "true", **properties}.items():
                    con.execute(
                        "INSERT INTO iceberg_namespace_properties VALUES (?, ?, ?, ?)", (CATALOG, namespace, key, str(value))
                    )
        except sqlite3.IntegrityError as exc:  # made by another request since `known` was read
            raise _conflict("AlreadyExistsException", f"Namespace already exists: {namespace}") from exc

    def update_namespace(self, namespace: str, removals: list[str], updates: dict[str, str]) -> dict[str, list[str]]:
        both = sorted(set(removals) & set(updates))
        if both:
            raise RestError(422, "UnprocessableEntityException", f"Properties both removed and updated: {both}")
        _unmarked(updates)
        current = self.namespace_properties(namespace)
        with self._connect() as con:
            for key in removals:
                if key not in current:  # the `exists` marker never is: removing it deleted the namespace
                    continue
                con.execute(
                    "DELETE FROM iceberg_namespace_properties WHERE catalog_name = ? AND namespace = ? AND property_key = ?",
                    (CATALOG, namespace, key),
                )
            for key, value in updates.items():
                con.execute(
                    "INSERT OR REPLACE INTO iceberg_namespace_properties VALUES (?, ?, ?, ?)",
                    (CATALOG, namespace, key, str(value)),
                )
        return {
            "updated": sorted(updates),
            "removed": sorted(key for key in removals if key in current),
            "missing": sorted(key for key in removals if key not in current),
        }

    def drop_namespace(self, namespace: str) -> None:
        self.namespace_properties(namespace)  # raises when there is none
        if any(ns == namespace or ns.startswith(namespace + ".") for ns in self.namespaces() - {namespace}):
            raise _conflict("NamespaceNotEmptyException", f"Namespace {namespace} has namespaces in it")
        if self.tables(namespace) or self.views(namespace):
            raise _conflict("NamespaceNotEmptyException", f"Namespace {namespace} is not empty")
        with self._connect() as con:
            con.execute(
                "DELETE FROM iceberg_namespace_properties WHERE catalog_name = ? AND namespace = ?", (CATALOG, namespace)
            )

    # -- tables ------------------------------------------------------------------------
    def tables(self, namespace: str) -> list[str]:
        with self._connect() as con:
            return sorted(row[0] for row in con.execute(
                f"SELECT table_name FROM iceberg_tables WHERE catalog_name = ? AND table_namespace = ? AND {_IS_TABLE}",
                (CATALOG, namespace),
            ))

    def location_of(self, namespace: str, table: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT metadata_location FROM iceberg_tables "
                f"WHERE catalog_name = ? AND table_namespace = ? AND table_name = ? AND {_IS_TABLE}",
                (CATALOG, namespace, table),
            ).fetchone()
        return row[0] if row else None

    # -- views: the same rows, typed VIEW; a view and a table cannot share a name ----------
    def views(self, namespace: str) -> list[str]:
        with self._connect() as con:
            return sorted(row[0] for row in con.execute(
                "SELECT table_name FROM iceberg_tables WHERE catalog_name = ? AND table_namespace = ? AND iceberg_type = 'VIEW'",
                (CATALOG, namespace),
            ))

    def view_location_of(self, namespace: str, view: str) -> str | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT metadata_location FROM iceberg_tables "
                "WHERE catalog_name = ? AND table_namespace = ? AND table_name = ? AND iceberg_type = 'VIEW'",
                (CATALOG, namespace, view),
            ).fetchone()
        return row[0] if row else None

    def swap_view(self, namespace: str, view: str, expected: str | None, new: str) -> None:
        """Point a view at new metadata, or make it when `expected` is None; as `swap` does."""
        with self._connect() as con:
            if expected is None:
                try:
                    con.execute("INSERT INTO iceberg_tables VALUES (?, ?, ?, ?, NULL, 'VIEW')", (CATALOG, namespace, view, new))
                except sqlite3.IntegrityError as exc:
                    raise _conflict("AlreadyExistsException", f"Table or view already exists: {namespace}.{view}") from exc
                return
            moved = con.execute(
                "UPDATE iceberg_tables SET metadata_location = ?, previous_metadata_location = ? WHERE catalog_name = ? "
                "AND table_namespace = ? AND table_name = ? AND metadata_location = ? AND iceberg_type = 'VIEW'",
                (new, expected, CATALOG, namespace, view, expected),
            ).rowcount
        if moved != 1:
            raise _conflict("CommitFailedException", f"Commit failed: {namespace}.{view} changed since it was read")

    def forget_view(self, namespace: str, view: str) -> None:
        with self._connect() as con:
            gone = con.execute(
                "DELETE FROM iceberg_tables WHERE catalog_name = ? AND table_namespace = ? AND table_name = ? "
                "AND iceberg_type = 'VIEW'",
                (CATALOG, namespace, view),
            ).rowcount
        if gone != 1:
            raise _not_found("NoSuchViewException", f"View does not exist: {namespace}.{view}")

    def pointers(self) -> list[str]:
        """Every table's metadata location, in every namespace."""
        with self._connect() as con:
            return [row[0] for row in con.execute(
                "SELECT metadata_location FROM iceberg_tables WHERE catalog_name = ? AND metadata_location IS NOT NULL",
                (CATALOG,),
            )]

    def swap(self, changes: list[tuple[str, str, str | None, str]]) -> None:
        """Move each `(namespace, table, expected, new)` pointer, all of them or none.

        `expected` None means the table must not exist yet. A pointer that has moved since the
        caller read it is a conflict, and nothing is changed.
        """
        con = self._connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            for namespace, table, expected, new in changes:
                if expected is None:
                    try:
                        con.execute(
                            "INSERT INTO iceberg_tables VALUES (?, ?, ?, ?, NULL, 'TABLE')",
                            (CATALOG, namespace, table, new),
                        )
                    except sqlite3.IntegrityError as exc:
                        raise _conflict("AlreadyExistsException", f"Table already exists: {namespace}.{table}") from exc
                    continue
                moved = con.execute(
                    "UPDATE iceberg_tables SET metadata_location = ?, previous_metadata_location = ? "
                    "WHERE catalog_name = ? AND table_namespace = ? AND table_name = ? AND metadata_location = ?",
                    (new, expected, CATALOG, namespace, table, expected),
                ).rowcount
                if moved != 1:
                    raise _conflict(
                        "CommitFailedException", f"Commit failed: {namespace}.{table} changed since it was read"
                    )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def forget(self, namespace: str, table: str) -> None:
        with self._connect() as con:
            con.execute(
                "DELETE FROM iceberg_tables WHERE catalog_name = ? AND table_namespace = ? AND table_name = ?",
                (CATALOG, namespace, table),
            )

    def rename(self, source: tuple[str, str], destination: tuple[str, str], kind: str = "TABLE") -> None:
        if destination[0] not in self.namespaces():
            raise _not_found("NoSuchNamespaceException", f"Namespace does not exist: {destination[0]}")
        typed = _IS_TABLE if kind == "TABLE" else "iceberg_type = 'VIEW'"
        what = "Table" if kind == "TABLE" else "View"
        with self._connect() as con:
            try:
                moved = con.execute(
                    "UPDATE iceberg_tables SET table_namespace = ?, table_name = ? "
                    f"WHERE catalog_name = ? AND table_namespace = ? AND table_name = ? AND {typed}",
                    (*destination, CATALOG, *source),
                ).rowcount
            except sqlite3.IntegrityError as exc:
                raise _conflict("AlreadyExistsException", f"Table or view already exists: {'.'.join(destination)}") from exc
        if moved != 1:
            raise _not_found(f"NoSuch{what}Exception", f"{what} does not exist: {'.'.join(source)}")


# -- table metadata ------------------------------------------------------------------------


def _max_field_id(node: Any) -> int:
    """The highest field id anywhere in a schema, nested fields, list elements and map keys included."""
    found = 0
    if isinstance(node, dict):
        for key in ("id", "element-id", "key-id", "value-id"):
            if isinstance(node.get(key), int):
                found = max(found, node[key])
        for value in node.values():
            found = max(found, _max_field_id(value))
    elif isinstance(node, list):
        for value in node:
            found = max(found, _max_field_id(value))
    return found


def _with_spec_ids(spec: dict[str, Any], spec_id: int, last_partition_id: int) -> tuple[dict[str, Any], int]:
    """A partition spec with its id set and every field given an id, as Java assigns them."""
    spec = copy.deepcopy(spec)
    spec["spec-id"] = spec_id
    for field in spec.setdefault("fields", []):
        if "field-id" not in field:
            last_partition_id += 1
            field["field-id"] = last_partition_id
        last_partition_id = max(last_partition_id, field["field-id"])
    return spec, last_partition_id


def fresh_metadata(request: dict[str, Any], location: str) -> dict[str, Any]:
    """The first metadata of a table, from a CreateTableRequest - what Java would write.

    `format-version` arrives as a property and leaves as a field: it is reserved, and a table
    that carried it in both places would be saying the same thing twice in two ways.
    """
    properties = {str(k): str(v) for k, v in (request.get("properties") or {}).items()}
    version = int(properties.pop("format-version", 2))
    if version not in (2, 3):
        raise _bad(f"this catalog writes format-version 2 or 3, not {version}")
    schema = copy.deepcopy(request.get("schema") or {})
    if schema.get("type") != "struct":
        raise _bad("a table needs a schema")
    schema["schema-id"] = 0
    spec, last_partition_id = _with_spec_ids(request.get("partition-spec") or {"fields": []}, 0, 999)
    order = copy.deepcopy(request.get("write-order") or {"order-id": 0, "fields": []})
    order.setdefault("order-id", 0 if not order.get("fields") else 1)
    metadata: dict[str, Any] = {
        "format-version": version,
        "table-uuid": str(uuid.uuid4()),
        "location": location,
        "last-sequence-number": 0,
        "last-updated-ms": _now_ms(),
        "last-column-id": _max_field_id(schema.get("fields", [])),
        "current-schema-id": 0,
        "schemas": [schema],
        "default-spec-id": 0,
        "partition-specs": [spec],
        "last-partition-id": last_partition_id,
        "default-sort-order-id": order["order-id"],
        "sort-orders": [order] if order["order-id"] == 0 else [{"order-id": 0, "fields": []}, order],
        "properties": properties,
        "current-snapshot-id": None,
        "refs": {},
        "snapshots": [],
        "statistics": [],
        "partition-statistics": [],
        "snapshot-log": [],
        "metadata-log": [],
    }
    if version >= 3:
        metadata["next-row-id"] = 0
    return metadata


def _empty_metadata(location: str) -> dict[str, Any]:
    """Where a staged create starts: nothing, to which the commit's own updates add everything."""
    return {
        "format-version": 2,
        "table-uuid": "",
        "location": location,
        "last-sequence-number": 0,
        "last-updated-ms": _now_ms(),
        "last-column-id": 0,
        "current-schema-id": -1,
        "schemas": [],
        "default-spec-id": -1,
        "partition-specs": [],
        "last-partition-id": 999,
        "default-sort-order-id": -1,
        "sort-orders": [],
        "properties": {},
        "current-snapshot-id": None,
        "refs": {},
        "snapshots": [],
        "statistics": [],
        "partition-statistics": [],
        "snapshot-log": [],
        "metadata-log": [],
    }


def _with_defaults(metadata: dict[str, Any]) -> dict[str, Any]:
    """Metadata another writer made, with what the spec lets it leave out filled in as Java's
    parser fills it - else such a table loaded, and then answered every commit with a KeyError."""
    for key in ("snapshots", "snapshot-log", "metadata-log", "statistics", "partition-statistics"):
        if metadata.get(key) is None:
            metadata[key] = []
    if metadata.get("properties") is None:
        metadata["properties"] = {}
    if metadata.get("current-snapshot-id", -1) == -1:  # older Java writes -1 for none
        metadata["current-snapshot-id"] = None
    if metadata.get("refs") is None:  # and with no refs, `main` is wherever the current snapshot is
        current = metadata["current-snapshot-id"]
        metadata["refs"] = {} if current is None else {"main": {"snapshot-id": current, "type": "branch"}}
    return metadata


def check(metadata: dict[str, Any] | None, requirements: list[dict[str, Any]]) -> None:
    """Every requirement of a commit, against the table as it is now. A failure is a 409."""
    for requirement in requirements:
        kind = requirement.get("type")
        if kind == "assert-create":
            if metadata is not None:
                raise _conflict("CommitFailedException", "Requirement failed: table already exists")
            continue
        if metadata is None:
            raise _not_found("NoSuchTableException", "Requirement failed: table does not exist")
        if kind == "assert-table-uuid":
            ok = requirement.get("uuid") == metadata["table-uuid"]
        elif kind == "assert-ref-snapshot-id":
            ref = metadata["refs"].get(requirement.get("ref"))
            expected = requirement.get("snapshot-id")
            ok = ref is None if expected is None else ref is not None and ref["snapshot-id"] == expected
        elif kind == "assert-last-assigned-field-id":
            ok = requirement.get("last-assigned-field-id") == metadata["last-column-id"]
        elif kind == "assert-current-schema-id":
            ok = requirement.get("current-schema-id") == metadata["current-schema-id"]
        elif kind == "assert-last-assigned-partition-id":
            ok = requirement.get("last-assigned-partition-id") == metadata["last-partition-id"]
        elif kind == "assert-default-spec-id":
            ok = requirement.get("default-spec-id") == metadata["default-spec-id"]
        elif kind == "assert-default-sort-order-id":
            ok = requirement.get("default-sort-order-id") == metadata["default-sort-order-id"]
        else:
            raise _bad(f"unknown requirement {kind!r}")
        if not ok:
            raise _conflict("CommitFailedException", f"Requirement failed: {kind} {json.dumps(requirement)}")


def apply(metadata: dict[str, Any], updates: list[dict[str, Any]], now: int | None = None) -> dict[str, Any]:
    """The table's metadata with a commit's updates applied, in order, as Java applies them.

    The v3 part is one line, and it is the reason this file exists: a snapshot added to a v3
    table carries the first row id it was given and how many rows it added, and the table's
    `next-row-id` moves on by that many, so ids are never reused.

    `now` is the commit's time, which the snapshot log records when `main` moves to a snapshot
    this commit did not add: a rollback happens now, not when its snapshot was first made.
    """
    metadata = copy.deepcopy(metadata)
    now = _now_ms() if now is None else now
    added: set[int] = set()
    removed = False
    last_schema: int | None = None
    last_spec: int | None = None
    last_order: int | None = None
    for update in updates:
        action = update.get("action")
        if action == "assign-uuid":
            if metadata["table-uuid"] and metadata["table-uuid"] != update["uuid"]:
                raise _bad("a table's uuid cannot change")
            metadata["table-uuid"] = update["uuid"]
        elif action == "upgrade-format-version":
            version = int(update["format-version"])
            if version < metadata["format-version"]:
                raise _bad(f"cannot downgrade format-version {metadata['format-version']} to {version}")
            if version not in (2, 3):
                raise _bad(f"this catalog writes format-version 2 or 3, not {version}")
            metadata["format-version"] = version
            if version >= 3:
                metadata.setdefault("next-row-id", 0)
        elif action == "add-schema":
            schema = copy.deepcopy(update["schema"])
            existing = [s["schema-id"] for s in metadata["schemas"]]
            if schema.get("schema-id") is None or schema["schema-id"] in existing:
                schema["schema-id"] = max(existing, default=-1) + 1
            metadata["schemas"].append(schema)
            metadata["last-column-id"] = max(
                metadata["last-column-id"], int(update.get("last-column-id") or 0), _max_field_id(schema.get("fields", []))
            )
            last_schema = schema["schema-id"]
        elif action == "set-current-schema":
            wanted = update["schema-id"]
            wanted = last_schema if wanted == -1 else wanted
            if wanted is None or wanted not in [s["schema-id"] for s in metadata["schemas"]]:
                raise _bad(f"no schema {update['schema-id']} to make current")
            metadata["current-schema-id"] = wanted
        elif action == "add-spec":
            existing = [s["spec-id"] for s in metadata["partition-specs"]]
            wanted = update["spec"].get("spec-id")
            spec_id = wanted if wanted is not None and wanted not in existing else max(existing, default=-1) + 1
            spec, metadata["last-partition-id"] = _with_spec_ids(update["spec"], spec_id, metadata["last-partition-id"])
            metadata["partition-specs"].append(spec)
            last_spec = spec_id
        elif action == "set-default-spec":
            wanted = last_spec if update["spec-id"] == -1 else update["spec-id"]
            if wanted is None or wanted not in [s["spec-id"] for s in metadata["partition-specs"]]:
                raise _bad(f"no partition spec {update['spec-id']} to make the default")
            metadata["default-spec-id"] = wanted
        elif action == "add-sort-order":
            order = copy.deepcopy(update["sort-order"])
            existing = [o["order-id"] for o in metadata["sort-orders"]]
            if not order.get("fields"):
                order["order-id"] = 0
            elif order.get("order-id") is None or order["order-id"] in existing:
                order["order-id"] = max([0, *existing]) + 1
            if order["order-id"] not in existing:
                metadata["sort-orders"].append(order)
            last_order = order["order-id"]
        elif action == "set-default-sort-order":
            wanted = last_order if update["sort-order-id"] == -1 else update["sort-order-id"]
            if wanted is None or wanted not in [o["order-id"] for o in metadata["sort-orders"]]:
                raise _bad(f"no sort order {update['sort-order-id']} to make the default")
            metadata["default-sort-order-id"] = wanted
        elif action == "add-snapshot":
            snapshot = copy.deepcopy(update["snapshot"])
            if any(s["snapshot-id"] == snapshot["snapshot-id"] for s in metadata["snapshots"]):
                raise _bad(f"snapshot {snapshot['snapshot-id']} already exists")
            sequence = int(snapshot.get("sequence-number", 0))
            if sequence <= metadata["last-sequence-number"] and snapshot.get("parent-snapshot-id") is not None:
                raise _bad(
                    f"a snapshot's sequence number must move forward: {sequence} after "
                    f"{metadata['last-sequence-number']}"
                )
            if metadata["format-version"] >= 3:
                first, rows = snapshot.get("first-row-id"), snapshot.get("added-rows")
                if type(first) is not int:  # not a bool either, which JSON's `true` would be
                    raise _bad("a v3 snapshot needs a first-row-id")
                if type(rows) is not int or rows < 0:  # missing or negative, and ids would be handed out twice
                    raise _bad(f"a v3 snapshot needs added-rows, a count of zero or more, not {rows!r}")
                if first < metadata["next-row-id"]:
                    raise _bad(f"first-row-id {first} is behind the table's next-row-id {metadata['next-row-id']}")
                metadata["next-row-id"] = first + rows
            metadata["snapshots"].append(snapshot)
            metadata["last-sequence-number"] = max(metadata["last-sequence-number"], sequence)
            metadata["last-updated-ms"] = max(metadata["last-updated-ms"], int(snapshot.get("timestamp-ms") or 0))
            added.add(snapshot["snapshot-id"])
        elif action == "set-snapshot-ref":
            name, snapshot_id = update["ref-name"], update["snapshot-id"]
            snapshot = next((s for s in metadata["snapshots"] if s["snapshot-id"] == snapshot_id), None)
            if snapshot is None:
                raise _bad(f"cannot point {name} at snapshot {snapshot_id}, which does not exist")
            ref = {"snapshot-id": snapshot_id, "type": update.get("type", "branch")}
            if name == "main" and ref["type"] != "branch":
                raise _bad("main is a branch, and cannot be made a tag")
            for key in ("max-ref-age-ms", "max-snapshot-age-ms", "min-snapshots-to-keep"):
                if update.get(key) is not None:
                    ref[key] = update[key]
            moved = (metadata["refs"].get(name) or {}).get("snapshot-id") != snapshot_id
            metadata["refs"][name] = ref
            if name == "main":
                metadata["current-snapshot-id"] = snapshot_id
                if moved:
                    # Java's rule: a snapshot this commit added at its own time, any other at the
                    # commit's - logging a rollback at its old snapshot's time ran the log backwards.
                    when = int(snapshot.get("timestamp-ms") or now) if snapshot_id in added else now
                    metadata["snapshot-log"].append({"timestamp-ms": when, "snapshot-id": snapshot_id})
        elif action == "remove-snapshot-ref":
            metadata["refs"].pop(update["ref-name"], None)
            if update["ref-name"] == "main":
                metadata["current-snapshot-id"] = None
        elif action == "remove-snapshots":
            gone = set(update["snapshot-ids"])
            metadata["snapshots"] = [s for s in metadata["snapshots"] if s["snapshot-id"] not in gone]
            metadata["refs"] = {k: v for k, v in metadata["refs"].items() if v["snapshot-id"] not in gone}
            metadata["statistics"] = [s for s in metadata["statistics"] if s["snapshot-id"] not in gone]
            metadata["partition-statistics"] = [s for s in metadata["partition-statistics"] if s["snapshot-id"] not in gone]
            if metadata["current-snapshot-id"] in gone:
                metadata["current-snapshot-id"] = None
            removed = True
        elif action == "set-location":
            metadata["location"] = str(update["location"]).rstrip("/")
        elif action == "set-properties":
            updates_ = {str(k): str(v) for k, v in update["updates"].items()}
            if "format-version" in updates_:
                raise _bad("format-version is set with upgrade-format-version, not as a property")
            metadata["properties"].update(updates_)
        elif action == "remove-properties":
            for key in update["removals"]:
                metadata["properties"].pop(key, None)
        elif action == "set-statistics":
            statistics = update["statistics"]
            metadata["statistics"] = [
                s for s in metadata["statistics"] if s["snapshot-id"] != statistics["snapshot-id"]
            ] + [statistics]
        elif action == "remove-statistics":
            metadata["statistics"] = [s for s in metadata["statistics"] if s["snapshot-id"] != update["snapshot-id"]]
        elif action == "set-partition-statistics":
            statistics = update["partition-statistics"]
            metadata["partition-statistics"] = [
                s for s in metadata["partition-statistics"] if s["snapshot-id"] != statistics["snapshot-id"]
            ] + [statistics]
        elif action == "remove-partition-statistics":
            metadata["partition-statistics"] = [
                s for s in metadata["partition-statistics"] if s["snapshot-id"] != update["snapshot-id"]
            ]
        elif action == "remove-partition-specs":
            # Refused, as Java refuses it, rather than quietly skipped: the client asked for
            # something it believes happened.
            gone = set(update["spec-ids"])
            if metadata["default-spec-id"] in gone:
                raise _bad("cannot remove the default partition spec")
            metadata["partition-specs"] = [s for s in metadata["partition-specs"] if s["spec-id"] not in gone]
        elif action == "remove-schemas":
            gone = set(update["schema-ids"])
            if metadata["current-schema-id"] in gone:
                raise _bad("cannot remove the current schema")
            metadata["schemas"] = [s for s in metadata["schemas"] if s["schema-id"] not in gone]
        elif action == "enable-row-lineage":
            # An early v3 draft's switch; row lineage is simply part of v3 now, and not of v2.
            if metadata["format-version"] < 3:
                raise _bad("row lineage needs format-version 3: upgrade the table first")
        else:
            raise _bad(f"this catalog cannot apply {action!r}")
    if removed:
        # As Java's `updateSnapshotLog`: an entry whose snapshot is gone takes every entry before
        # it too, or the log would say the snapshot after the gap was current through the gap.
        existing = {s["snapshot-id"] for s in metadata["snapshots"]}
        log: list[dict[str, Any]] = []
        for entry in metadata["snapshot-log"]:
            if entry["snapshot-id"] in existing:
                log.append(entry)
            else:
                log.clear()
        metadata["snapshot-log"] = log
    if metadata["table-uuid"] == "":
        metadata["table-uuid"] = str(uuid.uuid4())
    if metadata["format-version"] >= 3:
        metadata.setdefault("next-row-id", 0)
    return metadata


def _validated(metadata: dict[str, Any]) -> dict[str, Any]:
    """Refuse to write a metadata document no reader could use. Cheaper than a corrupt table."""
    for key, ids, name in (
        ("current-schema-id", [s["schema-id"] for s in metadata["schemas"]], "schema"),
        ("default-spec-id", [s["spec-id"] for s in metadata["partition-specs"]], "partition spec"),
        ("default-sort-order-id", [o["order-id"] for o in metadata["sort-orders"]], "sort order"),
    ):
        if metadata[key] not in ids:
            raise _bad(f"the table would have no current {name}")
    return metadata


def write_metadata(metadata: dict[str, Any], previous: str | None) -> str:
    """Write the next `NNNNN-<uuid>.metadata.json` beside the table and return where it went.

    Written to a temporary name and renamed, so a reader never sees half a file, and the
    previous file is remembered in `metadata-log` the way Java remembers it.
    """
    folder = _local(metadata["location"].rstrip("/") + "/metadata")
    folder.mkdir(parents=True, exist_ok=True)
    version = 0
    if previous:
        stem = previous.rsplit("/", 1)[-1].split("-", 1)[0]
        version = int(stem) + 1 if stem.isdigit() else 1
        metadata["metadata-log"].append({"timestamp-ms": metadata.pop("_previous_ms"), "metadata-file": previous})
        maximum = int(metadata["properties"].get("write.metadata.previous-versions-max", PREVIOUS_VERSIONS_MAX))
        metadata["metadata-log"] = metadata["metadata-log"][-maximum:]
    metadata.pop("_previous_ms", None)
    path = folder / f"{version:05d}-{uuid.uuid4()}.metadata.json"
    staging = folder / f".{path.name}.tmp"
    with open(staging, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(metadata))
        handle.flush()
        # The bytes reach the disk before the name does: else a crash between the two can
        # leave the new name on an empty file, and the pointer already moved to it.
        os.fsync(handle.fileno())
    os.replace(staging, path)
    return _file_uri(path)


# -- the catalog ---------------------------------------------------------------------------


class Catalog:
    """The REST catalog's behaviour, without HTTP: what each request does to the store."""

    def __init__(self, home: Path) -> None:
        self.home = home = home.resolve()  # every URI made from it is absolute, and compares
        self.store = Store(home / "catalog.db", home / "warehouse")
        self._lock = threading.Lock()

    # -- reading -----------------------------------------------------------------------
    def load(self, namespace: str, table: str) -> tuple[str, dict[str, Any]]:
        location = self.store.location_of(namespace, table)
        if location is None:
            raise _not_found("NoSuchTableException", f"Table does not exist: {namespace}.{table}")
        return location, _read_metadata(location)

    def default_location(self, namespace: str, table: str) -> str:
        """Where PyIceberg's SQL catalog puts a table, so both kinds of sandbox lay out alike."""
        return _file_uri(self.store.warehouse / f"{namespace}.db" / table)

    @staticmethod
    def prepare(location: str) -> None:
        """Make the table's folders. DuckDB writes `data/` and the manifests into `metadata/`
        when the location is a local path, but makes only the first: measured, its first
        commit to a new local table fails with "Cannot open file …/metadata/…-m0.avro"."""
        root = _local(location)
        for folder in ("metadata", "data"):
            (root / folder).mkdir(parents=True, exist_ok=True)

    # -- writing -----------------------------------------------------------------------
    def create(self, namespace: str, request: dict[str, Any]) -> dict[str, Any]:
        name = request.get("name") or ""
        if not name:
            raise _bad("a table needs a name")
        _table_name(name)
        self.store.namespace_properties(namespace)  # raises NoSuchNamespace
        if self.store.location_of(namespace, name) is not None:
            raise _conflict("AlreadyExistsException", f"Table already exists: {namespace}.{name}")
        location = (request.get("location") or self.default_location(namespace, name)).rstrip("/")
        self._confine(location)
        metadata = fresh_metadata(request, location)
        with self._lock:  # a purge may be deleting what was this location a moment ago
            self.prepare(location)
            if request.get("stage-create"):
                # Nothing is stored: the commit that follows, with `assert-create`, makes the table.
                return {"metadata-location": None, "metadata": metadata, "config": {}}
            path = write_metadata(metadata, None)
            self.store.swap([(namespace, name, None, path)])
        return {"metadata-location": path, "metadata": metadata, "config": {}}

    def commit(self, changes: list[tuple[str, str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        """Apply one commit per table, atomically across all of them.

        Every table's requirements are checked against its current metadata before anything
        is written, and the pointers move in one SQLite transaction that re-checks each old
        pointer - so either every table in a transaction moves, or none does.
        """
        with self._lock:
            planned = []
            for namespace, table, change in changes:
                current = self.store.location_of(namespace, table)
                metadata = _with_defaults(_read_metadata(current)) if current else None
                check(metadata, change.get("requirements") or [])
                if metadata is None:
                    if not any(r.get("type") == "assert-create" for r in change.get("requirements") or []):
                        raise _not_found("NoSuchTableException", f"Table does not exist: {namespace}.{table}")
                    _table_name(table)
                    self.store.namespace_properties(namespace)
                    base = _empty_metadata(self.default_location(namespace, table))
                else:
                    base = metadata
                now = _now_ms()
                updated = _validated(apply(base, change.get("updates") or [], now))
                # Every commit's, not only a create's: `set-location` moves where the next
                # metadata file is written and what a purge deletes, on a table that exists too.
                self._confine(updated["location"])
                if metadata is None:
                    self.prepare(updated["location"])
                else:
                    updated["_previous_ms"] = metadata["last-updated-ms"]
                    updated["last-updated-ms"] = max(updated["last-updated-ms"], now)
                planned.append((namespace, table, current, updated))
            written = [(namespace, table, current, write_metadata(updated, current), updated)
                       for namespace, table, current, updated in planned]
            self.store.swap([(namespace, table, current, path) for namespace, table, current, path, _ in written])
            return [(path, updated) for _, _, _, path, updated in written]

    def drop(self, namespace: str, table: str, purge: bool) -> None:
        """Forget a table by its pointer alone, which works when its metadata file is gone too -
        else neither the table nor its namespace could ever be dropped. `purge` deletes its files."""
        with self._lock:
            pointer = self.store.location_of(namespace, table)
            if pointer is None:
                raise _not_found("NoSuchTableException", f"Table does not exist: {namespace}.{table}")
            self.store.forget(namespace, table)
            if purge:
                self._purge(f"{namespace}.{table}", pointer)

    def _purge(self, name: str, pointer: str) -> None:
        """Delete a dropped table's files, when they are inside the warehouse and its alone.

        Two tables share a directory without anyone meaning harm - rename `t` and create a new
        `t`, and it gets the old one's default location; register one file under two names - so
        a root that is, holds or sits inside another table's keeps its files. The others' roots
        come from their pointers, not their metadata: one purge should not parse every table,
        and a pointer this catalog wrote is always `<root>/metadata/<file>`.
        """
        try:
            root = _local(_read_metadata(pointer)["location"]).resolve()
        except RestError:  # no metadata file left to say where: the pointer still does
            root = self._root_of(pointer)
        if root is None or self._inside(root) is None:
            _log.info("dropped %s and kept its files: %s is not inside the warehouse", name, root or pointer)
            return
        for other in filter(None, map(self._root_of, self.store.pointers())):
            if other == root or other.is_relative_to(root) or root.is_relative_to(other):
                _log.info("dropped %s and kept its files: another table's are at %s", name, other)
                return
        shutil.rmtree(root, ignore_errors=True)

    @staticmethod
    def _root_of(pointer: str) -> Path | None:
        """Where a table's files are, from its pointer alone: `<root>/metadata/<file>`."""
        try:
            return _local(pointer).parent.parent.resolve()
        except RestError:
            return None

    def register(self, namespace: str, request: dict[str, Any]) -> dict[str, Any]:
        name, location = request.get("name") or "", request.get("metadata-location") or ""
        if not name or not location:
            raise _bad("registering a table needs a name and a metadata-location")
        _table_name(name)
        self.store.namespace_properties(namespace)
        # The file is read and its contents answered, so where it is matters as much as where
        # it says the table lives: anything readable anywhere would otherwise be one request away.
        self._confine(location)
        try:
            metadata = _read_metadata(location)
        except RestError as exc:
            raise _bad(exc.message) from None  # the request named the file, so the request is wrong
        self._confine(metadata["location"])
        self.store.swap([(namespace, name, None, location)])
        return {"metadata-location": location, "metadata": metadata, "config": {}}

    def create_view(self, namespace: str, request: dict[str, Any]) -> dict[str, Any]:
        name = _table_name(request.get("name") or "")
        self.store.namespace_properties(namespace)
        location = (request.get("location") or self.default_location(namespace, name)).rstrip("/")
        self._confine(location)
        metadata = fresh_view(request, location)
        with self._lock:
            path = write_view_metadata(metadata, None)
            self.store.swap_view(namespace, name, None, path)
        return {"metadata-location": path, "metadata": metadata, "config": {}}

    def load_view(self, namespace: str, view: str) -> tuple[str, dict[str, Any]]:
        location = self.store.view_location_of(namespace, view)
        if location is None:
            raise _not_found("NoSuchViewException", f"View does not exist: {namespace}.{view}")
        return location, _read_view(location)

    def commit_view(self, namespace: str, view: str, change: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            current, metadata = self.load_view(namespace, view)
            updated = apply_view_change(metadata, change)
            self._confine(updated["location"])
            path = write_view_metadata(updated, current)
            self.store.swap_view(namespace, view, current, path)
        return {"metadata-location": path, "metadata": updated, "config": {}}

    def _inside(self, path: Path) -> Path | None:
        """`path` resolved, if it is inside the warehouse - strictly: a table *at* the warehouse,
        a location set to it or a table named `..`, would hold every table, and a purge of it
        would delete them all."""
        path, warehouse = path.resolve(), self.store.warehouse.resolve()
        return path if path != warehouse and path.is_relative_to(warehouse) else None

    def _confine(self, location: str) -> Path:
        """The table location's directory, or a 400 unless it is inside the warehouse."""
        root = self._inside(_local(location))
        if root is None:
            raise _bad(f"a sandbox keeps its tables inside {self.store.warehouse}, not at {location}")
        return root


# -- views ---------------------------------------------------------------------------------
#
# A view is a pointer to a metadata file like a table's, holding SQL rather than snapshots
# (view spec, format-version 1). Spark and Trino create and replace them through these
# endpoints; icelens only shows them. Kept small: the updates Java's REST client sends.


def fresh_view(request: dict[str, Any], location: str) -> dict[str, Any]:
    version = request.get("view-version")
    schema = request.get("schema")
    if not isinstance(version, dict) or not isinstance(schema, dict):
        raise _bad("a view needs a schema and a view-version")
    schema = {**schema, "schema-id": 0}
    version = {**version, "version-id": 1, "schema-id": 0}
    version.setdefault("timestamp-ms", _now_ms())
    return {
        "view-uuid": str(uuid.uuid4()),
        "format-version": 1,
        "location": location,
        "current-version-id": 1,
        "versions": [version],
        "version-log": [{"timestamp-ms": version["timestamp-ms"], "version-id": 1}],
        "schemas": [schema],
        "properties": {str(k): str(v) for k, v in (request.get("properties") or {}).items()},
    }


def apply_view_change(metadata: dict[str, Any], change: dict[str, Any]) -> dict[str, Any]:
    """The view after one commit's requirements are checked and its updates applied."""
    metadata = json.loads(json.dumps(metadata))
    for requirement in change.get("requirements") or []:
        if requirement.get("type") == "assert-view-uuid" and requirement.get("uuid") != metadata["view-uuid"]:
            raise _conflict("CommitFailedException", "Commit failed: the view's uuid has changed")
    added_schema: int | None = None
    added_version: int | None = None
    for update in change.get("updates") or []:
        action = update.get("action")
        if action == "assign-uuid":
            metadata["view-uuid"] = update["uuid"]
        elif action == "upgrade-format-version":
            if update["format-version"] != 1:
                raise _bad(f"this catalog keeps view format-version 1, not {update['format-version']}")
        elif action == "add-schema":
            schema = dict(update["schema"])
            added_schema = schema["schema-id"] = max((s["schema-id"] for s in metadata["schemas"]), default=-1) + 1
            metadata["schemas"].append(schema)
        elif action == "set-location":
            metadata["location"] = update["location"]
        elif action == "set-properties":
            metadata["properties"].update({str(k): str(v) for k, v in update["updates"].items()})
        elif action == "remove-properties":
            for key in update["removals"]:
                metadata["properties"].pop(key, None)
        elif action == "add-view-version":
            version = dict(update["view-version"])
            if version.get("schema-id") == -1:  # "the schema added in this commit"
                version["schema-id"] = added_schema
            added_version = version["version-id"] = max(v["version-id"] for v in metadata["versions"]) + 1
            version.setdefault("timestamp-ms", _now_ms())
            metadata["versions"].append(version)
        elif action == "set-current-view-version":
            wanted = added_version if update["view-version-id"] == -1 else update["view-version-id"]
            if wanted not in {v["version-id"] for v in metadata["versions"]}:
                raise _bad(f"no view version {update['view-version-id']} to make current")
            metadata["current-version-id"] = wanted
            metadata["version-log"].append({"timestamp-ms": _now_ms(), "version-id": wanted})
        else:
            raise _bad(f"this catalog cannot apply {action!r} to a view")
    return metadata


def write_view_metadata(metadata: dict[str, Any], previous: str | None) -> str:
    """The next `NNNNN-<uuid>.metadata.json` for a view, written whole or not at all."""
    folder = _local(metadata["location"].rstrip("/") + "/metadata")
    folder.mkdir(parents=True, exist_ok=True)
    version = 0
    if previous:
        stem = previous.rsplit("/", 1)[-1].split("-", 1)[0]
        version = int(stem) + 1 if stem.isdigit() else 1
    path = folder / f"{version:05d}-{uuid.uuid4()}.metadata.json"
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps(metadata), encoding="utf-8")
    partial.replace(path)
    return _file_uri(path)


def _read_view(location: str) -> dict[str, Any]:
    try:
        metadata = json.loads(_local(location).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise _not_found("NotFoundException", f"Metadata file does not exist: {location}") from exc
    except (OSError, ValueError) as exc:
        raise RestError(500, "ServiceFailureException", f"Cannot read view metadata {location}") from exc
    if not isinstance(metadata, dict) or "view-uuid" not in metadata:
        raise RestError(500, "ServiceFailureException", f"Not view metadata: {location}")
    return metadata


# -- HTTP ----------------------------------------------------------------------------------


def _namespace(segment: str) -> str:
    """A namespace path segment - levels separated by 0x1F - as the dotted name the store keeps."""
    return _namespace_name(urllib.parse.unquote(segment).split(UNIT))


def _levels(namespace: str) -> list[str]:
    return namespace.split(".")


class _Handler(BaseHTTPRequestHandler):
    """One request at a time per connection; the catalog serialises what has to be serial."""

    server: _Server
    protocol_version = "HTTP/1.1"
    #: Seconds a connection may sit idle or half-sent before it is closed. Each one holds a
    #: thread, and the stdlib's default is to wait forever.
    timeout = 120

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if self.server.log is not None and self.command:  # no command: the request line never parsed
            self.server.log.append(f"{self.command} {self.path}")

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the stdlib's name
        pass  # the client was answered; the stdlib would also print it to stderr

    # -- plumbing ----------------------------------------------------------------------
    def _send(self, status: int, body: Any = None) -> None:
        error = body.get("error", {}).get("type", "") if isinstance(body, dict) and isinstance(body.get("error"), dict) else ""
        _log.info("%s %s -> %d%s", self.command, self.path, status, f" {error}" if error else "")
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if status >= 400:
            # A refusal can come before the body was read - no token, a bad namespace, an
            # unknown route - and the body left behind was parsed as the next request on this
            # connection, failing the client's next, unrelated call. The header also sets
            # `close_connection`: one reconnect, after an error, is the price.
            self.send_header("Connection", "close")
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length < 0:
            raise _bad("Content-Length cannot be negative")  # `rfile.read(-1)` reads until the client hangs up
        if length > MAX_BODY:
            raise _bad("request body too large")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise _bad(f"request body is not JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise _bad("request body is not a JSON object")
        return body

    def _dispatch(self) -> None:
        try:
            if self.server.stopped:  # `shutdown` stops new connections, not ones kept alive
                raise RestError(503, "ServiceUnavailableException", "this catalog has stopped")
            if not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {self.server.token}"):
                raise RestError(401, "NotAuthorizedException", "Not authorized: this catalog wants its bearer token")
            if "Transfer-Encoding" in self.headers:  # a body this reads only by its Content-Length
                raise _bad("send a request body with a Content-Length, not a Transfer-Encoding")
            parsed = urllib.parse.urlsplit(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            parts = [p for p in parsed.path.split("/") if p]
            status, body = self._route(self.command, parts, query)
            self._send(status, body)
        except RestError as exc:
            self._send(exc.status, {"error": {"message": exc.message, "type": exc.kind, "code": exc.status}})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(400, {"error": {"message": f"malformed request: {exc!r}", "type": "BadRequestException", "code": 400}})
        except Exception as exc:  # noqa: BLE001 - a server answers; it does not die on one request
            self._send(500, {"error": {"message": f"{type(exc).__name__}: {exc}", "type": "ServiceFailureException", "code": 500}})

    do_GET = do_POST = do_DELETE = do_HEAD = _dispatch  # noqa: N815 - the stdlib's names

    # -- routes ------------------------------------------------------------------------
    def _route(self, method: str, parts: list[str], query: dict[str, str]) -> tuple[int, Any]:
        catalog = self.server.catalog
        if parts == ["v1", "config"]:
            asked = query.get("warehouse")
            if asked not in (None, PREFIX):  # rather than serve the sandbox under a name that is not its own
                raise _bad(f"this catalog serves one warehouse, {PREFIX!r}, not {asked!r}")
            return 200, {"defaults": {}, "overrides": {"prefix": PREFIX}, "endpoints": ENDPOINTS}
        if len(parts) < 3 or parts[:2] != ["v1", PREFIX]:
            raise _not_found("NotFoundException", f"no such endpoint: {method} /{'/'.join(parts)}")
        rest = parts[2:]
        if rest == ["namespaces"]:
            if method == "GET":
                parent = _namespace(query["parent"]) if query.get("parent") else ""
                depth = len(_levels(parent)) + 1 if parent else 1
                found = sorted({
                    ".".join(_levels(ns)[:depth])
                    for ns in catalog.store.namespaces()
                    if len(_levels(ns)) >= depth and (not parent or ns.startswith(parent + "."))
                })
                if parent and parent not in catalog.store.namespaces():
                    raise _not_found("NoSuchNamespaceException", f"Namespace does not exist: {parent}")
                return 200, {"namespaces": [_levels(ns) for ns in found], "next-page-token": None}
            if method == "POST":
                body = self._body()
                levels = body.get("namespace") or []
                namespace = _namespace_name(levels)
                properties = {str(k): str(v) for k, v in (body.get("properties") or {}).items()}
                catalog.store.create_namespace(namespace, properties)
                return 200, {"namespace": levels, "properties": properties}
        if rest == ["tables", "rename"] and method == "POST":
            body = self._body()
            source, destination = body["source"], body["destination"]
            catalog.store.rename(
                (_namespace_name(source["namespace"]), source["name"]),
                (_namespace_name(destination["namespace"]), _table_name(destination["name"])),
            )
            return 204, None
        if rest == ["views", "rename"] and method == "POST":
            body = self._body()
            source, destination = body["source"], body["destination"]
            catalog.store.rename(
                (_namespace_name(source["namespace"]), source["name"]),
                (_namespace_name(destination["namespace"]), _table_name(destination["name"])),
                kind="VIEW",
            )
            return 204, None
        if rest == ["transactions", "commit"] and method == "POST":
            changes = []
            for change in self._body().get("table-changes") or []:
                identifier = change.get("identifier") or {}
                changes.append((_namespace_name(identifier.get("namespace")), identifier.get("name") or "", change))
            catalog.commit(changes)
            return 204, None
        if rest[0] != "namespaces" or len(rest) < 2:
            raise _not_found("NotFoundException", f"no such endpoint: {method} /{'/'.join(parts)}")
        namespace = _namespace(rest[1])
        tail = rest[2:]
        if not tail:
            if method in ("GET", "HEAD"):
                properties = catalog.store.namespace_properties(namespace)
                return (200, {"namespace": _levels(namespace), "properties": properties}) if method == "GET" else (204, None)
            if method == "DELETE":
                catalog.store.drop_namespace(namespace)
                return 204, None
        if tail == ["properties"] and method == "POST":
            body = self._body()
            return 200, catalog.store.update_namespace(namespace, body.get("removals") or [], body.get("updates") or {})
        if tail == ["register"] and method == "POST":
            return 200, catalog.register(namespace, self._body())
        if tail == ["tables"]:
            if method == "GET":
                catalog.store.namespace_properties(namespace)
                names = catalog.store.tables(namespace)
                return 200, {
                    "identifiers": [{"namespace": _levels(namespace), "name": n} for n in names],
                    "next-page-token": None,
                }
            if method == "POST":
                return 200, catalog.create(namespace, self._body())
        if tail == ["views"]:
            if method == "GET":
                catalog.store.namespace_properties(namespace)
                return 200, {
                    "identifiers": [{"namespace": _levels(namespace), "name": n} for n in catalog.store.views(namespace)],
                    "next-page-token": None,
                }
            if method == "POST":
                return 200, catalog.create_view(namespace, self._body())
        if len(tail) == 2 and tail[0] == "views":
            view = urllib.parse.unquote(tail[1])
            if method == "HEAD":
                if catalog.store.view_location_of(namespace, view) is None:
                    raise _not_found("NoSuchViewException", f"View does not exist: {namespace}.{view}")
                return 204, None
            if method == "GET":
                location, metadata = catalog.load_view(namespace, view)
                return 200, {"metadata-location": location, "metadata": metadata, "config": {}}
            if method == "POST":
                return 200, catalog.commit_view(namespace, view, self._body())
            if method == "DELETE":
                catalog.store.forget_view(namespace, view)
                return 204, None
        if len(tail) >= 2 and tail[0] == "tables":
            table = urllib.parse.unquote(tail[1])
            if tail[2:] == ["metrics"] and method == "POST":
                self._body()
                return 204, None
            if tail[2:]:
                raise _not_found("NotFoundException", f"no such endpoint: {method} /{'/'.join(parts)}")
            if method == "HEAD":  # the pointer alone, as a drop: a missing metadata file is GET's to report
                if catalog.store.location_of(namespace, table) is None:
                    raise _not_found("NoSuchTableException", f"Table does not exist: {namespace}.{table}")
                return 204, None
            if method == "GET":
                location, metadata = catalog.load(namespace, table)
                return 200, {"metadata-location": location, "metadata": metadata, "config": {}}
            if method == "POST":
                change = self._body()
                [(location, metadata)] = catalog.commit([(namespace, table, change)])
                return 200, {"metadata-location": location, "metadata": metadata}
            if method == "DELETE":
                catalog.drop(namespace, table, query.get("purgeRequested", "false").lower() == "true")
                return 204, None
        raise _not_found("NotFoundException", f"no such endpoint: {method} /{'/'.join(parts)}")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # socketserver's default listen backlog is 5. Eight clients connecting at once while the
    # accept loop waited for the CPU overflowed it, and macOS answers an overflow with a reset:
    # measured, 3 runs in 40 under load had one client get ECONNRESET. A DuckDB or Spark
    # session opening its connections at once is that same burst.
    request_queue_size = 128
    # On Windows SO_REUSEADDR lets a second socket bind a port already in use, so a fixed
    # `--serve --port` could end up sharing it instead of being refused. Elsewhere it only
    # skips TIME_WAIT.
    allow_reuse_address = os.name != "nt"

    #: Connections served at once. Each holds a thread and a descriptor until it closes or idles
    #: out, and the token is checked only once a request arrives - so without a cap any local
    #: process could open connections until the process ran out of either. Far above what a
    #: DuckDB or Spark session opens; a connection past it is closed at once.
    max_connections = 64

    def __init__(self, catalog: Catalog, token: str, log: list[str] | None, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.catalog, self.token, self.log = catalog, token, log
        self.stopped = False
        self._slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


@dataclass
class Running:
    """A server that is up: where it is, the token it wants, and how to stop it."""

    uri: str
    token: str
    home: Path
    _server: _Server
    _thread: threading.Thread

    def stop(self) -> None:
        # Before anything else: a reset deletes the sandbox next, and a client's open
        # connection would otherwise go on committing to it with the token of a stopped server.
        self._server.stopped = True
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        _running.pop(self.home, None)

    @property
    def requests(self) -> list[str]:
        """What clients asked for, when started with `log=True`. For tests."""
        return self._server.log if self._server.log is not None else []


#: one server per sandbox directory per process, started on first use
_running: dict[Path, Running] = {}
_starting = threading.Lock()


def serve(home: Path, log: bool = False, port: int = 0, token: str | None = None) -> Running:
    """The REST catalog for one sandbox directory, started if it is not running yet.

    One per directory, shared by everything in this process that opens that sandbox, and
    stopped when the process ends: its thread is a daemon and holds nothing that needs
    closing, because every change is already committed to SQLite and to a metadata file.

    `port` 0 takes any free port, and a `token` of None a fresh random one; `icelens --serve`
    passes both when it was asked for a fixed address. They apply only to a server this call
    starts: one already running for `home` is returned as it is. Raises OSError when the port
    is taken.
    """
    home = home.resolve()
    with _starting:
        if home in _running:
            return _running[home]
        home.mkdir(parents=True, exist_ok=True)
        server = _Server(Catalog(home), token or secrets.token_urlsafe(32), [] if log else None, port)
        thread = threading.Thread(target=server.serve_forever, name=f"icelens-rest-{home.name}", daemon=True)
        thread.start()
        running = Running(f"http://127.0.0.1:{server.server_port}", server.token, home, server, thread)
        _running[home] = running
        return running


def stop(home: Path) -> None:
    """Stop the server for one sandbox directory, if this process is running one."""
    running = _running.get(home.resolve())
    if running is not None:
        running.stop()

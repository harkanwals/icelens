"""What an Iceberg table actually is, arranged so a person can read it.

Every pane in the app is a function from a `View` - a table plus the snapshot you are looking
at - to an Arrow table. That uniformity is not tidiness for its own sake: it means the grid
widget, CSV export, the headless `--pane` printer and the tests all share one code path, and a
new pane is one function.

Two choices are worth knowing about:

*Snapshot-scoped, not current-scoped.* PyIceberg's `inspect` helpers mostly describe the
current snapshot. Reading history means asking "what did the table look like at snapshot N",
so where `inspect` cannot be pointed at a snapshot, this module walks the manifest list itself.

*Manifests read directly.* `inspect.entries()` raises on v3 tables that carry delete files
without column sizes (PyIceberg 0.12), and it cannot answer "what did this one commit change"
anyway. `commit()` fetches the manifests that this snapshot added and reports their non-EXISTING
entries, which is exactly the set of files the commit wrote or retired.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pyarrow as pa

from icelens.catalog import LINEAGE_COLUMNS, IcelensError, Lake, row_limit, scrub
from icelens.format import blank, count, kv, pack, pylist, safe, scalar, size, when

#: How many rows each manifest-walking pane will read before it stops and says so.
#:
#: Every entry row below costs an object-store read of a manifest, and a busy table has tens
#: of thousands. Unbounded, the Files pane on such a table is a UI that hangs and a bill that
#: does not have to be paid to answer the question on screen. The caps are per pane because
#: what "enough" means differs: a manifest list is worth showing whole, a file list is not.
LIMITS: dict[str, int] = {
    "commit": 2_000,
    "manifests": 1_000,
    "files": 2_000,
    "layout": 1_000,
    # One catalog round trip per table, so a namespace with thousands of them would hold the
    # worker for minutes. The note says when the list was cut.
    "namespace": 500,
}

# Catalog errors reach a pane note, and a note reaches a terminal. Truncate before it gets there:
# a DuckDB or REST failure can carry a whole query back, and a pane note is one line.
NOTE_ERROR_CHARS = 300


def _live_file_count(manifests: list[Any]) -> int:
    """How many live files the manifest list claims, without opening a single manifest.

    The manifest list carries per-manifest counts, so the honest "first 2,000 of 50,000" costs
    one file read rather than fifty thousand.
    """
    total = 0
    for manifest in manifests:
        total += int(manifest.added_files_count or 0) + int(manifest.existing_files_count or 0)
    return total


def _note_error(exc: BaseException) -> str:
    """An exception, made safe to put in a one-line note on a terminal.

    Three things happen to it: credentials are scrubbed, control characters and bidi overrides
    are replaced, and it is cut to one line of sane length. The text comes from a remote catalog
    or a Parquet footer, so none of the three is optional.
    """
    text = " ".join(scrub(str(exc)).split()) or exc.__class__.__name__
    return safe(text if len(text) <= NOTE_ERROR_CHARS else text[: NOTE_ERROR_CHARS - 1] + "\u2026")


def _truncated(shown: int, total: int, what: str) -> str:
    """The half-sentence a bounded pane owes the reader, or nothing when it read it all."""
    if total > shown:
        return f"  -  showing the first {shown:,} of {total:,} {what}"
    return ""


#: manifest entry status codes, in the spec's own words
_STATUS = {0: "EXISTING", 1: "ADDED", 2: "DELETED"}
#: file content codes
_CONTENT = {0: "data", 1: "position deletes", 2: "equality deletes"}


def _content(file: Any) -> str:
    """What a data file holds. A v3 deletion vector is position deletes by content code, but a
    Puffin bitmap that is cumulative per data file, not a Parquet list of new positions; calling
    it "position deletes" hides the one difference a reader of a v3 commit has to know."""
    code = int(file.content)
    if code == 1 and getattr(file.file_format, "name", str(file.file_format)).upper() == "PUFFIN":
        return "deletion vector"
    return _CONTENT.get(code, str(code))


@dataclass
class View:
    """One table, seen at one snapshot. `snapshot_id=None` means "whatever is current"."""

    lake: Lake
    ident: str
    snapshot_id: int | None = None
    limit: int = 200
    where: str | None = None
    #: show the v3 row-lineage columns beside the data. Off by default because on a v2 table
    #: they do not exist, and asking for them there is an error rather than an empty column.
    lineage: bool = False

    @property
    def table(self) -> Any:
        return self.lake.table(self.ident)

    @property
    def metadata(self) -> Any:
        return self.table.metadata

    @property
    def snapshot(self) -> Any:
        """The snapshot being viewed, or None for a table nothing has ever been written to."""
        meta = self.metadata
        if self.snapshot_id is not None:
            found = meta.snapshot_by_id(self.snapshot_id)
            if found is None:
                raise IcelensError(f"snapshot {self.snapshot_id} is not in {self.ident} (expired?)")
            return found
        return self.table.current_snapshot()

    @property
    def effective_snapshot_id(self) -> int | None:
        snapshot = self.snapshot
        return None if snapshot is None else snapshot.snapshot_id

    @property
    def is_time_travelling(self) -> bool:
        current = self.metadata.current_snapshot_id
        return self.snapshot_id is not None and self.snapshot_id != current

    def schema(self) -> Any:
        """The schema the viewed snapshot was written with - not necessarily the current one."""
        snapshot = self.snapshot
        if snapshot is not None and snapshot.schema_id is not None:
            found = self.metadata.schema_by_id(snapshot.schema_id)
            if found is not None:
                return found
        return self.table.schema()


@dataclass(frozen=True)
class Pane:
    """A pane's answer: rows to show, one line of context above them, and the same rows typed.

    `data` is for a terminal: every cell a string, already formatted, already truncated.
    `facts` is the same answer with its types intact - ints as ints, byte counts as byte
    counts, timestamps as epoch milliseconds - which is what `--json` emits and what anything
    that is not a person reading a screen actually wants. They are built together, from one
    pass over the metadata, so the two cannot drift.
    """

    data: pa.Table
    note: str = ""
    facts: list[dict[str, Any]] | None = None

    def records(self) -> list[dict[str, Any]]:
        """The typed rows, falling back to the rendered ones for panes that are already typed."""
        return self.facts if self.facts is not None else pylist(self.data)


def empty(note: str, columns: tuple[str, ...] = ("nothing to show",)) -> Pane:
    return Pane(pa.table({c: pa.array([], pa.string()) for c in columns}), note, facts=[])


# -- catalog and namespace ---------------------------------------------------------------
def written(items: list[Any]) -> Pane:
    """The ledger of writes made in this session: the statement, and what Iceberg made of it.

    Statement beside snapshot on one row, because the gap between the two is the whole lesson.
    A `DELETE` that lands as an `overwrite` which *added* sixteen rows is not a quirk to be
    smoothed over - it is copy-on-write, seen for the first time, and the only way to see it
    is to have what you typed and what the table did side by side.
    """
    if not items:
        return empty("nothing written yet - type a statement above and press ctrl+r", _WRITTEN_COLUMNS)
    data = pa.table(
        {
            "#": pa.array(list(range(1, len(items) + 1)), pa.int64()),
            "statement": pa.array([_one_line_sql(w.sql) for w in items], pa.string()),
            "table": pa.array([w.table for w in items], pa.string()),
            "operation": pa.array([w.operation for w in items], pa.string()),
            "snapshot id": pa.array([w.snapshot_id for w in items], pa.int64()),
            "+rows": pa.array([w.added for w in items], pa.int64()),
            "-rows": pa.array([w.deleted for w in items], pa.int64()),
            "total rows": pa.array([w.records for w in items], pa.int64()),
            "files": pa.array([w.files for w in items], pa.int64()),
            "written by": pa.array([w.engine for w in items], pa.string()),
        }
    )
    last = items[-1]
    return Pane(data, f"{len(items)} commits this session - last: {last.line()}   ·   escape, then 4, to read it")


#: the shape the writer ledger keeps even with nothing in it, so the pane has columns to show
_WRITTEN_COLUMNS: tuple[str, ...] = (
    "#", "statement", "table", "operation", "snapshot id", "+rows", "-rows", "total rows", "files", "written by",
)


def _one_line_sql(sql: str) -> str:
    """A statement as one line, for a grid cell. The full text is in the editor above."""
    return " ".join(sql.split())


def catalog_overview(lake: Lake) -> Pane:
    rows = []
    for namespace in lake.namespaces():
        try:
            tables = lake.tables(namespace)
        except Exception as exc:  # noqa: BLE001 - a namespace we cannot list is still worth listing
            rows.append({"namespace": namespace, "tables": None, "note": str(exc)[:80]})
            continue
        rows.append({"namespace": namespace, "tables": len(tables), "note": ""})
    data = pa.table(
        {
            "namespace": pa.array([r["namespace"] for r in rows], pa.string()),
            "tables": pa.array([r["tables"] for r in rows], pa.int64()),
            "note": pa.array([r["note"] for r in rows], pa.string()),
        }
    )
    total = sum(r["tables"] or 0 for r in rows)
    return Pane(data, f"{lake.target.label} - {len(rows)} namespaces, {total} tables", facts=rows)


def namespace_overview(lake: Lake, namespace: str) -> Pane:
    """Every table in a namespace with the numbers its current snapshot already knows.

    Cheap on purpose: one catalog call per table and no manifest reads, so a namespace of a
    hundred tables still lands in a second or two.
    """
    idents = lake.tables(namespace)
    rows: list[dict[str, Any]] = []
    for ident in idents[: LIMITS["namespace"]]:
        entry: dict[str, Any] = {"table": ident.split(".")[-1]}
        try:
            table = lake.table(ident)
            meta = table.metadata
            snapshot = table.current_snapshot()
            summary = dict(snapshot.summary.additional_properties) if snapshot else {}
            entry.update(
                {
                    "v": meta.format_version,
                    "rows": _int(summary.get("total-records")),
                    "files": _int(summary.get("total-data-files")),
                    "deletes": _int(summary.get("total-delete-files")),
                    "size": _int(summary.get("total-files-size")),
                    "snapshots": len(meta.snapshots),
                    "updated": meta.last_updated_ms,
                    "partitioned by": ", ".join(f.name for f in table.spec().fields) or "-",
                    "partitions": [f.name for f in table.spec().fields],
                    "error": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - one broken table must not empty the pane
            entry["error"] = _note_error(exc)[:60]
        rows.append(entry)
    columns = ["table", "v", "rows", "files", "deletes", "size", "snapshots", "updated", "partitioned by"]
    data = pa.table(
        {
            column: pa.array(
                # A table that would not open has nothing to say in any other column, so its
                # error is shown where the timestamp would be rather than in a column of its own.
                [
                    f"error: {row['error']}" if row.get("error") and column == "updated" else _render(column, row.get(column))
                    for row in rows
                ],
                pa.string(),
            )
            for column in columns
        }
    )
    note = f"namespace {namespace} - {len(rows)} tables" + _truncated(len(rows), len(idents), "tables")
    views = lake.views(namespace)
    if views:
        # In the note, not the rows: a view has none of a table's columns, and `--json` rows
        # are a contract that says every row is a table.
        note += f"; {len(views)} view{'' if len(views) == 1 else 's'}: " + ", ".join(v.split(".")[-1] for v in views[:10]) + ("…" if len(views) > 10 else "")
    return Pane(data, note, facts=rows)


def view_definition(lake: Lake, ident: str) -> Pane:
    """A view: one row per version and SQL dialect, newest first, the current one marked.

    A view is a name for a query, kept as metadata like a table's but with versions of SQL in
    place of snapshots. Each version may carry the query in several dialects - Spark's and
    Trino's spelling of one query - so each is a row: which dialect an engine finds is what
    decides whether that engine can read the view at all.
    """
    meta = lake.view(ident).metadata
    schemas = {s.schema_id: s for s in meta.schemas}
    rows: list[dict[str, Any]] = []
    for version in sorted(meta.versions, key=lambda v: v.version_id, reverse=True):
        schema = schemas.get(version.schema_id)
        columns = ", ".join(f"{f.name} {f.field_type}" for f in schema.fields) if schema else "-"
        summary = dict(version.summary or {})
        for representation in version.representations:
            sql = getattr(representation, "root", representation)
            rows.append({
                "version": version.version_id,
                "current": version.version_id == meta.current_version_id,
                "created": version.timestamp_ms,
                "engine": summary.get("engine-name") or summary.get("engine-version") or "-",
                "dialect": getattr(sql, "dialect", "-"),
                "sql": getattr(sql, "sql", str(sql)),
                "columns": columns,
                "default namespace": ".".join(version.default_namespace or []) or "-",
            })
    display = {"current": lambda v: "*" if v else "", "created": when}
    data = pack(rows, ["version", "current", "created", "engine", "dialect", "sql", "columns", "default namespace"], display)
    dialects = sorted({r["dialect"] for r in rows if r["current"]})
    note = f"view {ident} - version {meta.current_version_id} of {len(meta.versions)}, in {', '.join(dialects) or 'no dialect'}"
    return Pane(data, note, facts=rows)


def _render(column: str, value: Any) -> str | None:
    if value is None:
        return None
    if column == "size":
        return size(value)
    if column == "updated":
        return when(value)
    if column in {"rows", "files", "deletes", "snapshots"}:
        return count(value)
    return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# -- table panes -------------------------------------------------------------------------
def overview(view: View) -> Pane:
    """The facts you would otherwise open metadata.json to find."""
    meta = view.metadata
    snapshot = view.snapshot
    summary = dict(snapshot.summary.additional_properties) if snapshot else {}
    spec = view.table.spec()
    sort_order = view.table.sort_order()
    pairs: list[tuple[str, Any]] = [
        ("table", view.ident),
        ("format version", meta.format_version),
        ("uuid", meta.table_uuid),
        ("location", meta.location),
        ("last updated", when(meta.last_updated_ms)),
        ("current snapshot", meta.current_snapshot_id),
        ("viewing snapshot", view.effective_snapshot_id or "-"),
        ("snapshots", len(meta.snapshots)),
        ("schemas", f"{len(meta.schemas)} (current id {meta.current_schema_id})"),
        ("columns", len(view.schema().fields)),
        ("partition spec", ", ".join(f"{f.name} = {f.transform}({_source(view, f.source_id)})" for f in spec.fields) or "unpartitioned"),
        ("sort order", ", ".join(f"{_source(view, f.source_id)} {f.direction.name.lower()}" for f in sort_order.fields) or "unsorted"),
        ("rows (metadata)", count(_int(summary.get("total-records")))),
        ("data files", count(_int(summary.get("total-data-files")))),
        ("delete files", count(_int(summary.get("total-delete-files")))),
        ("total size", size(_int(summary.get("total-files-size")))),
    ]
    # v3 only: the next `_row_id` the table will hand out, which is also the number of rows it
    # has ever assigned an id to - not the number it holds now, since ids are never reused.
    if meta.format_version >= 3 and meta.next_row_id is not None:
        after = next(i for i, (key, _) in enumerate(pairs) if key == "rows (metadata)")
        pairs.insert(after + 1, ("next row id", count(meta.next_row_id)))
    for name, ref in sorted(meta.refs.items()):
        kind = getattr(ref.snapshot_ref_type, "value", str(ref.snapshot_ref_type))
        pairs.append((f"ref {name}", f"{kind} -> {ref.snapshot_id}"))
    for key, value in sorted(meta.properties.items()):
        pairs.append((f"property {key}", value))
    for entry in meta.metadata_log[-3:]:
        pairs.append((f"metadata log {when(entry.timestamp_ms)}", entry.metadata_file.rsplit("/", 1)[-1]))
    engine = view.lake.engine
    note = f"{view.ident} - read through {engine}"
    if view.is_time_travelling:
        note += f"  [time travel: snapshot {view.snapshot_id}]"
    # The displayed rows are labels and formatted text; these are the same facts with their
    # types. Written out rather than derived from `pairs`, because two of the rows above are
    # a sentence made of several facts ("10 (current id 9)") and a caller should get both.
    facts: dict[str, Any] = {
        "table": view.ident,
        "format_version": meta.format_version,
        "uuid": str(meta.table_uuid),
        "location": meta.location,
        "last_updated_ms": meta.last_updated_ms,
        "current_snapshot_id": meta.current_snapshot_id,
        "viewing_snapshot_id": view.effective_snapshot_id,
        "time_travelling": view.is_time_travelling,
        "snapshots": len(meta.snapshots),
        "schemas": len(meta.schemas),
        "current_schema_id": meta.current_schema_id,
        "columns": len(view.schema().fields),
        "partition_spec_id": spec.spec_id,
        "partition_spec": [
            {
                "name": f.name,
                "transform": str(f.transform),
                "source": _source(view, f.source_id),
                "source_id": f.source_id,
                "field_id": f.field_id,
            }
            for f in spec.fields
        ],
        "sort_order_id": sort_order.order_id,
        "sort_order": [
            {
                "source": _source(view, f.source_id),
                "source_id": f.source_id,
                "direction": f.direction.name.lower(),
                "null_order": getattr(f.null_order, "name", str(f.null_order)).lower(),
            }
            for f in sort_order.fields
        ],
        "total_records": _int(summary.get("total-records")),
        "total_data_files": _int(summary.get("total-data-files")),
        "total_delete_files": _int(summary.get("total-delete-files")),
        "total_files_size_bytes": _int(summary.get("total-files-size")),
        # v3 only, and null below it: shown on screen since row lineage arrived, and missing
        # from these facts until a contract test compared the two.
        "next_row_id": getattr(meta, "next_row_id", None) if meta.format_version >= 3 else None,
        "refs": {
            name: {
                "type": getattr(ref.snapshot_ref_type, "value", str(ref.snapshot_ref_type)),
                "snapshot_id": ref.snapshot_id,
            }
            for name, ref in sorted(meta.refs.items())
        },
        "properties": dict(sorted(meta.properties.items())),
        "metadata_log": [
            {"timestamp_ms": entry.timestamp_ms, "file": entry.metadata_file.rsplit("/", 1)[-1], "path": entry.metadata_file}
            for entry in meta.metadata_log
        ],
        "engine": engine,
    }
    return Pane(kv(pairs), note, facts=[facts])


def _spec_at(view: View) -> Any | None:
    """The partition spec the viewed snapshot was written with; None when that cannot be told.

    Not `table.spec()`, which is today's: on the demo, snapshot 1 showed `region` as an identity
    partition of a table that was not partitioned until snapshot 5. A snapshot does not record
    its spec, but the manifests it wrote do - each names the spec its files were written under,
    and a writer writes under the default one - so one read of the manifest list answers it.
    No manifest of its own, or a list that will not read, is None: no answer beats a wrong one.
    """
    snapshot = view.snapshot
    if snapshot is None or not view.is_time_travelling:
        return view.table.spec()
    try:
        manifests = snapshot.manifests(view.table.io)
    except Exception:  # noqa: BLE001 - a missing or unreadable manifest list is not this pane's failure
        return None
    own = [m.partition_spec_id for m in manifests if m.added_snapshot_id == snapshot.snapshot_id]
    return view.table.specs().get(max(own)) if own else None


def _source(view: View, source_id: int) -> str:
    field = view.schema().find_field(source_id) if source_id else None
    return field.name if field is not None else f"field {source_id}"


def schema(view: View) -> Pane:
    """The schema of the viewed snapshot, nested fields and all.

    Field ids matter more than names in Iceberg - a rename keeps the id, a drop-and-re-add
    does not - so the id is the first column, not a footnote.
    """
    schema_obj = view.schema()
    spec = _spec_at(view)
    partition_sources = {f.source_id: f for f in spec.fields} if spec is not None else {}
    identifiers = set(schema_obj.identifier_field_ids or [])
    rows: list[dict[str, Any]] = []

    def walk(fields: list[Any], depth: int) -> None:
        for field in fields:
            prefix = "   " * depth + ("└ " if depth else "")
            field_type = field.field_type
            partition = partition_sources.get(field.field_id)
            rows.append(
                {
                    "id": field.field_id,
                    "name": field.name,
                    "depth": depth,
                    "column": f"{prefix}{field.name}",
                    "type": _type_name(field_type),
                    "required": field.required,
                    "identifier": field.field_id in identifiers,
                    "reserved": False,
                    "partition": str(partition.transform) if partition else None,
                    "default": field.write_default,
                    "doc": field.doc or "",
                }
            )
            for nested in _children(field_type):
                walk([nested], depth + 1)

    walk(list(schema_obj.fields), 0)
    # Row lineage is real but it is not in the schema: no field id, no entry in the data files.
    # Showing it here, marked reserved, is the difference between "this table has row lineage"
    # and "go and read the spec to find out whether it does". `required` is None on those rows
    # rather than False - the spec does not say they are optional, it says they are not fields.
    reserved = 0
    if view.metadata.format_version >= 3:
        for name in LINEAGE_COLUMNS:
            rows.append(
                {
                    "id": None,
                    "name": name,
                    "depth": 0,
                    "column": name,
                    "type": "long",
                    "required": None,
                    "identifier": False,
                    "reserved": True,
                    "partition": None,
                    "default": None,
                    "doc": "v3 row lineage - computed on read, not stored in the data files",
                }
            )
            reserved += 1
    # `required`, `reserved` and `identifier` read as words on screen and as booleans in JSON;
    # `column` carries the tree prefix that makes nesting visible, which is why `name` and
    # `depth` exist beside it.
    data = pa.table(
        {
            "id": pa.array([r["id"] for r in rows], pa.int32()),
            "column": pa.array([r["column"] for r in rows], pa.string()),
            "type": pa.array([r["type"] for r in rows], pa.string()),
            "required": pa.array([_requiredness(r) for r in rows], pa.string()),
            "key": pa.array([_field_key(r) for r in rows], pa.string()),
            "partition": pa.array([r["partition"] or "" for r in rows], pa.string()),
            "default": pa.array([scalar(r["default"]) if r["default"] is not None else "" for r in rows], pa.string()),
            "doc": pa.array([r["doc"] for r in rows], pa.string()),
        }
    )
    versions = ", ".join(str(s.schema_id) for s in view.metadata.schemas)
    note = f"schema id {schema_obj.schema_id} of [{versions}] - {len(rows) - reserved} fields including nested"
    if reserved:
        note += f" + {reserved} reserved row-lineage columns (v3)"
    keys = ("id", "name", "depth", "type", "required", "identifier", "reserved", "partition", "default", "doc")
    return Pane(data, note, facts=[{k: r[k] for k in keys} for r in rows])


def _requiredness(row: dict[str, Any]) -> str:
    if row["reserved"]:
        return "reserved"
    return "required" if row["required"] else "optional"


def _field_key(row: dict[str, Any]) -> str:
    if row["reserved"]:
        return "row lineage"
    return "identifier" if row["identifier"] else ""


def _type_name(field_type: Any) -> str:
    from pyiceberg.types import ListType, MapType, StructType

    if isinstance(field_type, StructType):
        return f"struct<{len(field_type.fields)} fields>"
    if isinstance(field_type, ListType):
        return f"list<{_type_name(field_type.element_field.field_type)}>"
    if isinstance(field_type, MapType):
        return f"map<{_type_name(field_type.key_field.field_type)}, {_type_name(field_type.value_field.field_type)}>"
    return str(field_type)


def _children(field_type: Any) -> list[Any]:
    from pyiceberg.types import ListType, MapType, StructType

    if isinstance(field_type, StructType):
        return list(field_type.fields)
    if isinstance(field_type, ListType):
        return [field_type.element_field]
    if isinstance(field_type, MapType):
        return [field_type.key_field, field_type.value_field]
    return []


def data(view: View) -> Pane:
    # Asking a v2 table for `_row_id` gets a binder error out of `iceberg_scan` and a column of
    # NULLs out of an attached catalog: two different wrong answers, neither of them "this table
    # has no row lineage". So the format version decides whether the toggle does anything, and
    # the note says which happened. `Lake.rows` holds the same line for callers that skip this.
    version = view.metadata.format_version
    # A pinned snapshot this table does not have should say so and name the table, rather than
    # surfacing whichever engine noticed first ("Could not find snapshot with id 999"). Reading
    # it here costs nothing - the metadata is already in hand - and it fails before any I/O.
    pinned = view.effective_snapshot_id
    lineage = view.lineage and version >= 3
    rows, engine = view.lake.rows(
        view.ident,
        limit=view.limit,
        snapshot_id=view.snapshot_id,
        where=view.where or None,
        lineage=lineage,
    )
    # The effective limit, not the requested one: `--limit -5` reads 0 rows, and a note that
    # says "limit -5" describes a read that never happened.
    note = f"{rows.num_rows} rows (limit {row_limit(view.limit)}) via {engine}"
    if view.lineage and not lineage:
        note += f"  [row lineage is v3 only; this table is format version {version}]"
    if view.where:
        note += f"  WHERE {view.where}"
    if view.is_time_travelling:
        note += f"  as of snapshot {pinned}"
    return Pane(rows, note)


def snapshots(view: View) -> Pane:
    """Every snapshot, oldest first, with the shape of the history drawn in the margin.

    `◆` is the current snapshot, `│` a snapshot that is one of its ancestors, and `~` one that
    is not - the tell-tale of a rollback or a branch, and the reason this column exists.
    """
    meta = view.metadata
    if not meta.snapshots:
        return empty(f"{view.ident} has no snapshots yet - nothing has been written to it")
    ancestors = _ancestors(meta)
    refs_by_snapshot: dict[int, list[str]] = {}
    for name, ref in meta.refs.items():
        refs_by_snapshot.setdefault(ref.snapshot_id, []).append(name)

    ordered = ordered_snapshots(meta)
    rows = []
    for index, snapshot in enumerate(ordered, start=1):
        summary = dict(snapshot.summary.additional_properties)
        marker = "◆" if snapshot.snapshot_id == meta.current_snapshot_id else ("│" if snapshot.snapshot_id in ancestors else "~")
        rows.append(
            {
                "#": index,
                "": marker,
                "ancestor": snapshot.snapshot_id in ancestors,
                "current": snapshot.snapshot_id == meta.current_snapshot_id,
                "committed at": snapshot.timestamp_ms,
                "operation": snapshot.summary.operation.value,
                "snapshot id": snapshot.snapshot_id,
                "parent": snapshot.parent_snapshot_id,
                "seq": snapshot.sequence_number,
                "first row id": snapshot.first_row_id,
                "+rows": _int(summary.get("added-records")) or 0,
                "-rows": _int(summary.get("deleted-records")) or 0,
                "total rows": _int(summary.get("total-records")),
                "+files": _int(summary.get("added-data-files")) or 0,
                "-files": _int(summary.get("removed-data-files")) or _int(summary.get("deleted-data-files")) or 0,
                "+deletes": _int(summary.get("added-delete-files")) or 0,
                "schema": snapshot.schema_id,
                "refs": sorted(refs_by_snapshot.get(snapshot.snapshot_id, [])),
                "summary": summary,
            }
        )
    columns = ["#", "", "committed at", "operation", "snapshot id", "parent", "seq", "first row id",
               "+rows", "-rows", "total rows", "+files", "-files", "+deletes", "schema", "refs"]
    table = pack(
        rows,
        columns,
        display={
            "committed at": when,
            "parent": blank,
            "first row id": blank,
            "+rows": count,
            "-rows": count,
            "total rows": count,
            "+files": count,
            "-files": count,
            "+deletes": count,
            "refs": lambda names: ", ".join(names),
        },
    )
    off_chain = sum(1 for s in meta.snapshots if s.snapshot_id not in ancestors and s.snapshot_id != meta.current_snapshot_id)
    note = f"{len(ordered)} snapshots - ◆ current, │ ancestor, ~ off the current chain ({off_chain})"
    return Pane(table, note, facts=rows)


def ordered_snapshots(meta: Any) -> list[Any]:
    """Every snapshot oldest first - the order the Snapshots pane lists them in.

    The stepping keys walk this same list, so what `,` and `.` move through is exactly what
    the pane shows, off-chain snapshots included. Two orderings would eventually disagree.
    """
    return sorted(meta.snapshots, key=lambda s: (s.timestamp_ms, s.snapshot_id))


def _ancestors(meta: Any) -> set[int]:
    """Snapshot ids reachable by walking parents from the current snapshot."""
    seen: set[int] = set()
    current = meta.current_snapshot_id
    while current is not None and current not in seen:
        seen.add(current)
        snapshot = meta.snapshot_by_id(current)
        current = None if snapshot is None else snapshot.parent_snapshot_id
    return seen


def commit(view: View) -> Pane:
    """What one snapshot changed: the files it added and the files it retired.

    Read from the manifests the snapshot itself added, with deleted entries kept. A commit's
    own manifests are the only place its ADDED and DELETED entries live; everything else in
    the manifest list is inherited and marked EXISTING.
    """
    snapshot = view.snapshot
    if snapshot is None:
        return empty(f"{view.ident} has no snapshots yet")
    table = view.table
    spec = table.specs().get(0)
    rows: list[dict[str, Any]] = []
    manifests = snapshot.manifests(table.io)
    own = [m for m in manifests if m.added_snapshot_id == snapshot.snapshot_id]
    cap = LIMITS["commit"]
    changed = sum(
        int(m.added_files_count or 0) + int(m.deleted_files_count or 0) for m in own
    )
    for manifest in own:
        if len(rows) >= cap:
            break
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False):
            if len(rows) >= cap:
                break
            status = _STATUS.get(int(entry.status), str(entry.status))
            if status == "EXISTING":
                continue
            file = entry.data_file
            rows.append(
                {
                    "status": status,
                    "content": _content(file),
                    "rows": file.record_count,
                    "size": file.file_size_in_bytes,
                    "partition": _partition(file.partition, table.specs().get(file.spec_id, spec)),
                    "seq": entry.sequence_number,
                    "spec_id": file.spec_id,
                    "file": file.file_path.rsplit("/", 1)[-1],
                    "path": file.file_path,
                }
            )
    summary = dict(snapshot.summary.additional_properties)
    plus, minus = summary.get("added-records", 0), summary.get("deleted-records", 0)
    counted = _rows_under_deletion_vectors(own, table.io)
    if counted is not None:
        plus, minus = counted
    note = (
        f"snapshot {snapshot.snapshot_id}  {snapshot.summary.operation.value}  {when(snapshot.timestamp_ms)}"
        f"  +{plus} rows / -{minus} rows"
        f"  across {len(own)} new manifests of {len(manifests)}"
        + _truncated(len(rows), changed, "changed files")
    )
    if not rows:
        return empty(note + " - no file-level changes (a metadata-only commit)",
                     ("status", "content", "rows", "size", "partition", "seq", "file", "path"))
    columns = ["status", "content", "rows", "size", "partition", "seq", "file", "path"]
    return Pane(pack(rows, columns, display={"rows": count, "size": size}), note, facts=rows)


def _rows_under_deletion_vectors(own: list[Any], io: Any) -> tuple[int, int] | None:
    """Rows a commit added and removed, counted from its own manifest entries - or None when it
    touched no deletion vector, and the summary's counters can be believed.

    Under deletion vectors they cannot. A DV commit sets no `deleted-records`, so every v3
    delete read "-0 rows"; and `added-position-deletes` will not do instead, because a DV is
    cumulative per data file: measured, DuckDB reported 6 for a DELETE of one row. A DV's record
    count is its cardinality, and the spec has a writer remove a file's old DV in the commit that
    replaces it, or that removes the file. So every removed DV is either superseded by an added
    one or went with its data file, and the rows removed are the removed data files' rows, plus
    the added DVs, less the removed DVs. That needs no pairing of a DV with its data file, which
    PyIceberg 0.12 cannot do (its DataFile has no `referenced_data_file`). It makes a compaction
    come out even, and agrees with counting file by file on Spark 4.1's and DuckDB's
    commits (measured: -4, -1, +1/-1, +2/-1, +17/-17).
    """
    rows = {"added data": 0, "removed data": 0, "added dv": 0, "removed dv": 0}
    for manifest in own:
        for entry in manifest.fetch_manifest_entry(io, discard_deleted=False):
            status = _STATUS.get(int(entry.status))
            if status == "EXISTING":
                continue
            file = entry.data_file
            kind = "data" if int(file.content) == 0 else "dv" if _content(file) == "deletion vector" else None
            if kind is not None:
                rows[f"{'added' if status == 'ADDED' else 'removed'} {kind}"] += int(file.record_count or 0)
    if not rows["added dv"] and not rows["removed dv"]:
        return None
    return rows["added data"], rows["removed data"] + rows["added dv"] - rows["removed dv"]


def manifests(view: View) -> Pane:
    """The manifest list of the viewed snapshot: one row per manifest file."""
    snapshot = view.snapshot
    if snapshot is None:
        return empty(f"{view.ident} has no snapshots yet")
    table = view.table
    rows = []
    listed = snapshot.manifests(table.io)
    for manifest in listed[: LIMITS["manifests"]]:
        content = "data" if int(manifest.content) == 0 else "deletes"
        rows.append(
            {
                "content": content,
                "added by": manifest.added_snapshot_id,
                "own": manifest.added_snapshot_id == snapshot.snapshot_id,
                "added": manifest.added_files_count,
                "existing": manifest.existing_files_count,
                "deleted": manifest.deleted_files_count,
                "+rows": manifest.added_rows_count,
                "-rows": manifest.deleted_rows_count,
                "seq": manifest.sequence_number,
                "spec": manifest.partition_spec_id,
                "size": manifest.manifest_length,
                "manifest": manifest.manifest_path.rsplit("/", 1)[-1],
                "path": manifest.manifest_path,
            }
        )
    if not rows:
        return empty(f"snapshot {snapshot.snapshot_id} lists no manifests")
    columns = ["content", "added by", "own", "added", "existing", "deleted",
               "+rows", "-rows", "seq", "spec", "size", "manifest"]
    note = (
        f"snapshot {snapshot.snapshot_id} lists {len(listed)} manifests"
        f"  (manifest-list {snapshot.manifest_list.rsplit('/', 1)[-1]})"
        + _truncated(len(rows), len(listed), "manifests")
    )
    display = {
        "own": lambda own: "yes" if own else "",
        "size": size,
        **{c: count for c in ("added", "existing", "deleted", "+rows", "-rows")},
    }
    return Pane(pack(rows, columns, display), note, facts=rows)


def files(view: View) -> Pane:
    """Every file live at the viewed snapshot - data files and delete files together."""
    snapshot = view.snapshot
    if snapshot is None:
        return empty(f"{view.ident} has no snapshots yet")
    table = view.table
    rows = []
    # Summed from the file metadata itself, never re-parsed out of the formatted cells:
    # `record_count` is nullable, and `count(None)` is "-", which int() will not take.
    total_rows = 0
    total_size = 0
    listed = snapshot.manifests(table.io)
    live = _live_file_count(listed)
    cap = LIMITS["files"]
    for manifest in listed:
        if len(rows) >= cap:
            break
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
            if len(rows) >= cap:
                break
            file = entry.data_file
            kind = _content(file)
            if kind == "data":
                total_rows += int(file.record_count or 0)
            total_size += int(file.file_size_in_bytes or 0)
            rows.append(
                {
                    "content": kind,
                    "rows": file.record_count,
                    "size": file.file_size_in_bytes,
                    "partition": _partition(file.partition, table.specs().get(file.spec_id)),
                    "format": getattr(file.file_format, "name", str(file.file_format)).lower(),
                    "seq": entry.sequence_number,
                    "spec_id": file.spec_id,
                    "added by": entry.snapshot_id,
                    "file": file.file_path.rsplit("/", 1)[-1],
                    "path": file.file_path,
                }
            )
    if not rows:
        return empty(f"snapshot {snapshot.snapshot_id} has no live files")
    columns = ["content", "rows", "size", "partition", "format", "seq", "added by", "file", "path"]
    note = (
        f"{len(rows)} files, {total_rows:,} rows in data files, {size(total_size)} on disk"
        + _truncated(len(rows), live, "files")
    )
    return Pane(pack(rows, columns, display={"rows": count, "size": size}), note, facts=rows)


def partitions(view: View) -> Pane:
    """Partition-level statistics, plus the spec that produced them."""
    table = view.table
    spec = table.spec()
    try:
        stats = table.inspect.partitions(snapshot_id=view.snapshot_id)
    except Exception as exc:  # noqa: BLE001 - inspect is fragile on odd metadata; say so, do not crash
        return empty(f"partition statistics unavailable: {_note_error(exc)}")
    spec_text = ", ".join(f"{f.name} = {f.transform}({_source(view, f.source_id)})" for f in spec.fields)
    note = f"spec {spec.spec_id}: {spec_text or 'unpartitioned - one row below covers the whole table'}"
    return Pane(stats, note)


def layout(view: View) -> Pane:
    """The metadata chain as a tree: metadata file, snapshot, manifest list, manifests, files.

    This is the pane that answers "what is actually in object storage". Every level below the
    snapshot is a real file with a real size, and the indentation is the pointer chain Iceberg
    follows on every read.
    """
    meta = view.metadata
    snapshot = view.snapshot
    rows: list[dict[str, str]] = []
    # The tree is drawn with indentation and a summary sentence per node; the same walk records
    # each node's depth and its numbers separately, because "3.9 KB, 12 files" is two facts.
    facts: list[dict[str, Any]] = []

    def add(depth: int, node: str, detail: str = "", extent: str = "", **fact: Any) -> None:
        rows.append({"node": "  " * depth + node, "detail": detail, "size / rows": extent})
        facts.append({"depth": depth, "node": node, "detail": detail, **fact})

    latest_path = meta.metadata_log[-1].metadata_file if meta.metadata_log else None
    latest_metadata = latest_path.rsplit("/", 1)[-1] if latest_path else "(current)"
    add(
        0,
        "table metadata",
        latest_metadata,
        f"{len(meta.snapshots)} snapshots, {len(meta.schemas)} schemas",
        kind="table-metadata",
        path=latest_path,
        snapshots=len(meta.snapshots),
        schemas=len(meta.schemas),
    )
    if snapshot is None:
        return Pane(
            pa.table({c: pa.array([r[c] for r in rows], pa.string()) for c in ("node", "detail", "size / rows")}),
            f"{view.ident} has no snapshots yet",
            facts=facts,
        )
    table = view.table
    add(
        1,
        f"snapshot {snapshot.snapshot_id}",
        f"{snapshot.summary.operation.value} at {when(snapshot.timestamp_ms)}  schema {snapshot.schema_id}",
        f"seq {snapshot.sequence_number}",
        kind="snapshot",
        snapshot_id=snapshot.snapshot_id,
        operation=snapshot.summary.operation.value,
        timestamp_ms=snapshot.timestamp_ms,
        schema_id=snapshot.schema_id,
        sequence_number=snapshot.sequence_number,
    )
    add(2, "manifest list", snapshot.manifest_list.rsplit("/", 1)[-1], "", kind="manifest-list", path=snapshot.manifest_list)
    manifest_files = snapshot.manifests(table.io)
    cap = LIMITS["layout"]
    for manifest in manifest_files:
        if len(rows) >= cap:
            break
        kind = "data manifest" if int(manifest.content) == 0 else "delete manifest"
        own = " (added by this snapshot)" if manifest.added_snapshot_id == snapshot.snapshot_id else ""
        listed_files = int(manifest.added_files_count or 0) + int(manifest.existing_files_count or 0)
        add(
            3,
            kind + own,
            manifest.manifest_path.rsplit("/", 1)[-1],
            f"{size(manifest.manifest_length)}, {listed_files} files",
            kind="manifest",
            content="data" if int(manifest.content) == 0 else "deletes",
            own=manifest.added_snapshot_id == snapshot.snapshot_id,
            path=manifest.manifest_path,
            size_bytes=manifest.manifest_length,
            files=listed_files,
        )
        for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=False):
            if len(rows) >= cap:
                add(4, "…", "more files than this pane will read", "", kind="truncated")
                break
            file = entry.data_file
            status = _STATUS.get(int(entry.status), "")
            add(
                4,
                f"{_content(file)} [{status}]",
                file.file_path.rsplit("/", 1)[-1],
                f"{size(file.file_size_in_bytes)}, {count(file.record_count)} rows",
                kind="file",
                content=_content(file),
                status=status,
                path=file.file_path,
                size_bytes=file.file_size_in_bytes,
                record_count=file.record_count,
            )
    note = (
        f"pointer chain for snapshot {snapshot.snapshot_id} - {len(manifest_files)} manifests, "
        f"{_live_file_count(manifest_files):,} files"
        + (f"  -  stopped at the first {cap:,} nodes" if len(rows) >= cap else "")
    )
    return Pane(
        pa.table({c: pa.array([r[c] for r in rows], pa.string()) for c in ("node", "detail", "size / rows")}),
        note,
        facts=facts,
    )


def _partition(record: Any, spec: Any) -> str:
    """A partition tuple as `col=value`, falling back to whatever the record prints as."""
    if record is None:
        return ""
    try:
        values = list(record)
    except TypeError:
        return scalar(record)
    names = [f.name for f in spec.fields] if spec is not None else []
    if len(names) == len(values):
        return ", ".join(f"{name}={scalar(value)}" for name, value in zip(names, values, strict=True))
    return ", ".join(scalar(value) for value in values) or "(unpartitioned)"


#: the table panes, in the order the app shows them
PANES: dict[str, Callable[[View], Pane]] = {
    "overview": overview,
    "schema": schema,
    "data": data,
    "snapshots": snapshots,
    "commit": commit,
    "manifests": manifests,
    "files": files,
    "partitions": partitions,
    "layout": layout,
}

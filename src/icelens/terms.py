"""What every word on screen means, so the vocabulary is learned by using the tool.

Iceberg's nouns are the barrier to reading an Iceberg table. `manifest list`, `sequence
number`, `equality delete`, `spec id` - none of them mean anything on first sight, all of
them are column headers in this app, and the usual remedy is a help page in a different
window that nobody opens while they are in the middle of looking at something.

So the explanation is not a page. It is a rail at the bottom of the screen that always
describes whatever the cursor is on: the pane, the column, the row, or the value in the
cell. Move right one column in Snapshots and the rail moves from `operation` to
`snapshot id`. That is the whole design - the reader never asks for the definition,
because it is already there, and it is about the thing in front of them rather than about
Iceberg in the abstract.

Three rules this corpus is written to:

* **A gloss is one line and must stand alone.** It is what the reader sees by default and
  usually all they read, so "the manifest list for this snapshot" is a failure - it uses
  the word it is defining.
* **The body says where the thing lives and what it costs.** A term the reader can find on
  disk, or count, or watch change between two snapshots, has been explained; a paraphrase
  of the spec has not.
* **`see` is for the terms the reader will need next**, not everything related. The rail
  prints them as the trail to follow, and five suggestions is no suggestion.

Terms are resolved in `resolve()` from (pane, column, row label, cell value), most specific
first, so a cell reading `equality deletes` explains equality deletes rather than the
column it sits in.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Term:
    """One piece of vocabulary: a line for the rail, a paragraph for `g`."""

    name: str
    gloss: str
    body: str = ""
    see: tuple[str, ...] = field(default_factory=tuple)
    #: where it lives - a file on disk, a field in metadata.json, a spec section
    where: str = ""


def _t(key: str, name: str, gloss: str, body: str = "", see: tuple[str, ...] = (), where: str = "") -> tuple[str, Term]:
    return key, Term(name=name, gloss=gloss, body=" ".join(body.split()), see=see, where=where)


TERMS: dict[str, Term] = dict(
    (
        # -- the shape of a catalog ------------------------------------------------------
        _t(
            "catalog",
            "Catalog",
            "The service that knows where each table's current metadata file is - and nothing else.",
            """A catalog maps `namespace.table` to one pointer: the path of the newest metadata
            file. Everything else - schema, history, statistics, the data - is in object storage,
            written by whoever did the writing. That is why the same table can be read by DuckDB,
            Spark and Trino at once, and why swapping catalogs (REST, Glue, Hive, SQL) changes how
            you find a table but not what the table is.""",
            see=("namespace", "table-metadata", "commit-conflict"),
            where="here: a REST catalog, Lakekeeper or another; `icelens --catalogs` lists the rest",
        ),
        _t(
            "namespace",
            "Namespace",
            "A folder of table names in the catalog, one or more levels deep.",
            """Purely a naming device: a namespace has no schema, no storage and no data of its
            own. Most engines call it a database or a schema, and multi-level names
            (`raw.events.eu`) are allowed but unevenly supported.""",
            see=("catalog", "table-metadata"),
        ),
        _t(
            "view",
            "View",
            "A name for a SQL query, kept in the catalog beside the tables.",
            """Its metadata file is like a table's, but holds versions of a query instead of
            snapshots of data. Each version can carry the same query in several SQL dialects;
            an engine reads the view only if one of them is its own. icelens shows the
            definition; it does not run it.""",
            see=("namespace", "table-metadata"),
        ),
        _t(
            "table-metadata",
            "Table metadata (metadata.json)",
            "One immutable JSON file holding everything true about the table at one moment.",
            """Schema list, partition specs, sort orders, properties, every snapshot, and which
            snapshot is current. Writes never edit it: a writer reads the current file, writes a
            *new* numbered one, and asks the catalog to swap the pointer. The old files stay,
            which is why the history is readable at all.""",
            see=("snapshot", "metadata-log", "commit-conflict"),
            where="s3://…/<table>/metadata/00042-<uuid>.metadata.json",
        ),
        _t(
            "format-version",
            "Format version",
            "Which revision of the Iceberg spec this table is written to: 1, 2 or 3.",
            """v1 had no row-level deletes. v2 added delete files, so a single row can be removed
            without rewriting its whole data file. v3 adds deletion vectors (one compact file per
            data file instead of many delete files), row lineage, and new types. A reader must
            understand the version to read the table; a writer must understand it to write one.""",
            see=("delete-file", "deletion-vector", "row-lineage"),
            where="`format-version` in metadata.json",
        ),
        _t(
            "table-uuid",
            "Table UUID",
            "The table's identity, which survives being renamed or moved between catalogs.",
            """Two tables with the same name in two catalogs are the same table only if the uuid
            matches. It is also how a reader notices it has been handed metadata for a different
            table than it asked for.""",
            see=("table-metadata",),
        ),
        _t(
            "location",
            "Table location",
            "The object-storage prefix under which every file of this table is written.",
            """`data/` for data files, `metadata/` for metadata files, manifest lists and
            manifests. Nothing in Iceberg requires files to live there - paths in manifests are
            absolute - but every writer puts them there, and it is what you delete when you
            really mean to drop the table.""",
            see=("data-file", "manifest"),
        ),
        _t(
            "metadata-log",
            "Metadata log",
            "The trail of previous metadata files, newest last.",
            """Each entry is (timestamp, path). It is how you go back to how the table was
            described - not just what rows it held - and it is trimmed by
            `write.metadata.previous-versions-max`, which is why a very old entry may name a file
            that has been deleted.""",
            see=("table-metadata", "snapshot"),
        ),
        # -- history ---------------------------------------------------------------------
        _t(
            "snapshot",
            "Snapshot",
            "The complete set of files that made up the table at one instant, named by an id.",
            """Every successful write produces one. A snapshot names its parent, so the snapshots
            form a chain (usually) you can walk backwards - that is what time travel reads. It
            does not store rows: it points at one manifest list, which points at manifests, which
            point at the data files. Reading "as of" a snapshot is exactly as cheap as reading the
            current one.""",
            see=("manifest-list", "parent-snapshot", "sequence-number", "snapshot-summary"),
            where="the `snapshots` array in metadata.json",
        ),
        _t(
            "snapshot-id",
            "Snapshot id",
            "A random 64-bit number identifying one commit. Not a counter - do not read order into it.",
            """Order comes from `committed at` and from the sequence number. The id is what you
            hand to a time-travel query (`FOR VERSION AS OF <id>`), what a manifest entry records
            as the commit that added a file, and what a branch or tag points at.""",
            see=("snapshot", "sequence-number", "time-travel"),
        ),
        _t(
            "parent-snapshot",
            "Parent snapshot",
            "The snapshot this one was built on top of. Empty means the first commit.",
            """Walking parents from the current snapshot gives the current chain. A snapshot that
            is not on that chain (`~` in the margin) is still a real, readable snapshot - it is
            the evidence of a rollback, or of work on a branch.""",
            see=("snapshot", "branch", "rollback"),
        ),
        _t(
            "sequence-number",
            "Sequence number",
            "A counter that increases by one per commit, and decides which deletes apply to which data.",
            """This is the mechanism behind row-level deletes: a delete file only removes rows
            from data files with a *lower* sequence number. Re-inserting the same row afterwards
            gives it a higher number, so the old delete cannot touch it. Inherited entries keep
            the sequence number of the commit that first wrote them, which is why an old file's
            `seq` stays low forever.""",
            see=("delete-file", "snapshot", "manifest-entry"),
        ),
        _t(
            "snapshot-summary",
            "Snapshot summary",
            "The counters a writer records about its own commit: rows and files added, removed, total.",
            """Cheap and useful - `total-records` on the current snapshot is the row count without
            reading a single data file - but they are *claims written by the writer*, not derived.
            They can be missing on tables written by unusual tools, and a scan is the only thing
            that is authoritative.""",
            see=("operation", "snapshot", "total-records"),
        ),
        _t(
            "operation",
            "Operation",
            "What kind of write made this snapshot: append, overwrite, delete or replace.",
            """`append` added files and removed none - the cheapest commit and the only one that
            never conflicts with another append. `overwrite` replaced data (a MERGE, an upsert, a
            partition rewrite). `delete` removed rows or files and added none. `replace` rewrote
            files without changing the rows - compaction. The operation is what a downstream
            incremental reader keys on to decide whether it can read only the new files.""",
            see=("snapshot-summary", "append", "compaction"),
        ),
        _t(
            "append",
            "append",
            "A commit that only added files. Nothing that existed before was touched.",
            """The one operation that lets a consumer read strictly the new data files and be
            correct. It is also the commit least likely to lose a race: two appends to the same
            table can usually both be applied, where two overwrites of the same partition cannot.""",
            see=("operation", "commit-conflict"),
        ),
        _t(
            "compaction",
            "Compaction (replace)",
            "Rewriting many small files into fewer big ones without changing a single row.",
            """It shows up as a `replace` snapshot: files removed, files added, row count
            unchanged. The reason it exists is that every data file costs a read, and a stream of
            small commits produces small files. It also materialises pending deletes, so the
            delete files it retires stop being read on every scan.""",
            see=("operation", "data-file", "delete-file"),
        ),
        _t(
            "time-travel",
            "Time travel",
            "Reading the table as it was at an older snapshot, at the same cost as reading it now.",
            """Nothing is reconstructed or replayed: an old snapshot names its own manifest list,
            so reading it is the same work as reading the current one. It stops working only when
            the snapshot has been expired and its files deleted.""",
            see=("snapshot-id", "expire-snapshots", "rollback"),
            where="press enter on a snapshot row here; `u` to come back",
        ),
        _t(
            "rollback",
            "Rollback",
            "Moving the table's current pointer back to an earlier snapshot.",
            """A rollback writes new metadata that names an old snapshot as current; the snapshots
            written in between still exist, now off the chain (`~`). Nothing is deleted, so the
            operation is reversible until someone expires snapshots.""",
            see=("parent-snapshot", "expire-snapshots", "branch"),
        ),
        _t(
            "expire-snapshots",
            "Expire snapshots",
            "The maintenance job that drops old snapshots and deletes files nothing still references.",
            """This is the only routine operation in Iceberg that destroys history, and the reason
            time travel has a horizon. It must compute which files are still reachable from the
            snapshots being kept - deleting by age alone corrupts the table.""",
            see=("time-travel", "snapshot", "compaction"),
        ),
        _t(
            "branch",
            "Branch / tag (ref)",
            "A name pointing at a snapshot: `main` is one, and so is any tag you create.",
            """A tag is a fixed point ("the numbers we reported in March"); a branch moves as it is
            written to, which is how audit-then-publish workflows stage data where readers of
            `main` cannot see it. Every ref keeps its snapshot alive against expiry, each with its
            own retention.""",
            see=("snapshot", "rollback", "expire-snapshots"),
            where="the `refs` map in metadata.json",
        ),
        # -- the pointer chain -----------------------------------------------------------
        _t(
            "manifest-list",
            "Manifest list",
            "One file per snapshot, listing that snapshot's manifests and what each one covers.",
            """It is an Avro file, one row per manifest, carrying per-manifest partition ranges and
            file counts. That summary is what makes planning fast: a reader can skip a whole
            manifest - and every file in it - on partition bounds, without opening it. It is also
            why "how many files does this table have" costs one read rather than thousands.""",
            see=("manifest", "snapshot", "partition-summary"),
            where="s3://…/metadata/snap-<snapshot id>-<n>-<uuid>.avro",
        ),
        _t(
            "manifest",
            "Manifest",
            "An Avro file listing data (or delete) files, with per-column statistics for each one.",
            """One manifest covers many files and belongs to the snapshot that wrote it; later
            snapshots that still contain those files *reuse the same manifest* rather than
            rewriting it. Its per-file statistics - null counts, lower and upper bounds per column
            - are what lets a query skip files without reading them. A manifest holds either data
            files or delete files, never both.""",
            see=("manifest-entry", "manifest-list", "data-file", "column-bounds"),
            where="s3://…/metadata/<uuid>-m0.avro",
        ),
        _t(
            "manifest-entry",
            "Manifest entry",
            "One row in a manifest: a file, plus its status in this snapshot - ADDED, EXISTING or DELETED.",
            """The status is relative to the snapshot that wrote the manifest, which is the subtlety
            worth holding on to: a file added ten commits ago appears as EXISTING in every manifest
            since. To see what one commit actually changed, read only the manifests that commit
            added, and keep the non-EXISTING rows.""",
            see=("status", "manifest", "sequence-number"),
        ),
        _t(
            "status",
            "Entry status",
            "ADDED, EXISTING or DELETED - what this snapshot did to this file.",
            """ADDED: written by this commit. DELETED: removed by this commit (the file may still
            exist on disk, and older snapshots still read it). EXISTING: inherited untouched, which
            is most rows in most manifests.""",
            see=("manifest-entry", "operation"),
        ),
        _t(
            "data-file",
            "Data file",
            "A Parquet (or ORC/Avro) file holding rows. Immutable once written.",
            """Nothing ever edits one. A changed row means a new file plus either a delete file or
            a rewrite of the old file. Its manifest entry carries the row count, the file size, the
            partition tuple, and per-column bounds - so most planning questions are answered
            without opening it.""",
            see=("delete-file", "manifest-entry", "column-bounds", "record-count"),
        ),
        _t(
            "delete-file",
            "Delete file",
            "A file that removes rows from data files older than it, without rewriting them.",
            """Added in format v2 to make row-level deletes and updates affordable: an UPDATE of
            one row writes a small delete plus a small insert instead of rewriting a 200 MB
            Parquet file. The cost lands on readers, who must apply every applicable delete on
            every scan, which is why compaction matters on tables that update often.""",
            see=("position-delete", "equality-delete", "deletion-vector", "sequence-number"),
        ),
        _t(
            "position-delete",
            "Position delete",
            "Removes rows by (file path, row position): 'rows 3 and 91 of that file are gone'.",
            """Exact and cheap to apply, because a reader can skip straight to the positions. It
            requires the writer to know where the rows were, so it is what an engine produces when
            it has just scanned the data it is deleting. In v3 this role is taken over by deletion
            vectors.""",
            see=("delete-file", "deletion-vector", "equality-delete"),
        ),
        _t(
            "equality-delete",
            "Equality delete",
            "Removes every row matching a value: 'anything where id = 42 is gone'.",
            """Cheap to write - a streaming writer can emit one without having read the data - and
            expensive to read, because every candidate row must be compared against it. Seeing
            many of these on a table that is queried often is a sign the write path is streaming
            and nobody is compacting.""",
            see=("delete-file", "position-delete", "sequence-number"),
        ),
        _t(
            "deletion-vector",
            "Deletion vector",
            "v3's replacement for position deletes: one compact bitmap of deleted rows per data file.",
            """A Puffin file holding a roaring bitmap, at most one live vector per data file, so a
            reader loads exactly one small blob instead of merging a pile of delete files. It is
            the main practical reason to be on format v3.""",
            see=("delete-file", "position-delete", "format-version"),
        ),
        _t(
            "content",
            "File content",
            "Whether this file holds rows (data) or removes them (position / equality deletes).",
            """The same distinction runs through manifests: a manifest is a data manifest or a
            delete manifest, and a scan reads both, applying the second to the first.""",
            see=("data-file", "delete-file", "position-delete", "equality-delete"),
        ),
        _t(
            "record-count",
            "Record count",
            "How many rows are in this file, as its manifest entry records.",
            """Summed over live data files it gives the table's row count without opening a single
            file - but before deletes are applied. A table with delete files has fewer live rows
            than its data files claim.""",
            see=("data-file", "total-records", "delete-file"),
        ),
        _t(
            "file-size",
            "File size",
            "Bytes on object storage.",
            """Worth reading next to the row count: consistently tiny files mean a write pattern
            that commits too often, and they cost one request each on every scan.""",
            see=("compaction", "data-file"),
        ),
        _t(
            "column-bounds",
            "Column bounds / statistics",
            "Per-column min, max and null counts stored per file, so whole files can be skipped.",
            """This is the difference between a lakehouse and a directory of Parquet. A predicate
            on a column with useful bounds eliminates files during planning - no read, no bill.
            Bounds are only as useful as the data's clustering, which is what sort order is for.""",
            see=("manifest", "sort-order", "partition-spec"),
        ),
        _t(
            "total-records",
            "Total records",
            "The whole table's row count at this snapshot, as claimed by the writer's summary.",
            """Free to read and usually right. It is a recorded number rather than a computed one,
            so a disagreement with `SELECT count(*)` means the writer's bookkeeping is off - worth
            knowing about.""",
            see=("snapshot-summary", "record-count"),
        ),
        # -- schema ----------------------------------------------------------------------
        _t(
            "schema-evolution",
            "Schema evolution",
            "Adding, dropping, renaming or reordering columns without rewriting any data.",
            """Because columns are addressed by id rather than by name or position, a rename is a
            metadata edit and old files keep working. Each snapshot records the schema id it was
            written with, so reading history shows the schema of the time - not today's schema
            projected backwards.""",
            see=("field-id", "schema-id", "snapshot"),
        ),
        _t(
            "column-name",
            "Column name",
            "A label, not an identity: what a column is called can change without touching a file.",
            """Iceberg keys every column by its field id, so a rename is one line in a new
            metadata file and every existing data file stays readable - even though the name
            inside the Parquet footer never changed. The consequence worth remembering: two
            columns that have held the same name at different times are still different columns,
            and a column dropped and re-added is a new one whatever it is called.""",
            see=("field-id", "schema-evolution", "column-doc"),
            where="metadata.json -> schemas[].fields[].name",
        ),
        _t(
            "data-type",
            "Data type",
            "The Iceberg type, which is not the engine's type - each engine maps onto it.",
            """Iceberg has its own small type system (boolean, int, long, float, double, decimal,
            date, time, timestamp/timestamptz, string, uuid, fixed, binary, plus struct, list and
            map). Only widening changes are legal in place - int to long, float to double, decimal
            precision up - because every existing file has to stay readable without being
            rewritten. Anything narrower means a new column, which is why a type change that looks
            trivial in a database is a migration here.""",
            see=("schema-evolution", "field-id", "required-optional"),
            where="metadata.json -> schemas[].fields[].type",
        ),
        _t(
            "file-format",
            "File format",
            "What each file on disk actually is: Parquet almost always, sometimes ORC or Avro.",
            """The format is recorded per file, not per table, so a table can hold a mix - which
            is what makes a migration possible without a rewrite. Manifests themselves are always
            Avro, and in v3 a deletion vector is a Puffin file rather than Parquet, so a delete
            file whose format looks unfamiliar is usually that.""",
            see=("data-file", "deletion-vector", "manifest"),
            where="the manifest entry's `file_format`, and the file's own extension",
        ),
        _t(
            "commit-time",
            "Commit time",
            "The writer's wall clock at the moment the snapshot was committed.",
            """It is what `AS OF TIMESTAMP` resolves against, and it is the one number in the
            history that no machine guarantees: it comes from whichever process did the writing,
            so clock skew between two writers can put a later snapshot slightly earlier in time.
            Sequence numbers are the ordering that is guaranteed; use this to find a snapshot, and
            the sequence number to reason about order.""",
            see=("snapshot", "sequence-number", "time-travel"),
            where="metadata.json -> snapshots[].timestamp-ms, in milliseconds",
        ),
        _t(
            "table-scan",
            "Reading a table (scan)",
            "The rows you see are the live data files minus whatever the delete files remove.",
            """Nothing stores "the current rows". A read plans a scan: take the manifest list for
            this snapshot, keep the data files that survive partition and column-bounds pruning,
            then subtract the rows their delete files mark as gone. That is why a table can answer
            a query about last Tuesday as cheaply as about today - a different snapshot is a
            different manifest list, and nothing else changes.""",
            see=("data-file", "delete-file", "time-travel"),
            where="this pane, via DuckDB's iceberg_scan or PyIceberg - the status bar says which",
        ),
        _t(
            "field-id",
            "Field id",
            "The column's real identity. Names are labels; the id is what the files are keyed by.",
            """Rename a column and the id survives, so every old data file is still readable. Drop
            a column and add one with the same name and the id is different - which is exactly
            right, because they are different columns, and Iceberg is one of the few formats that
            can tell you so.""",
            see=("schema-evolution", "schema-id", "identifier-field"),
        ),
        _t(
            "schema-id",
            "Schema id",
            "Which version of the schema this snapshot was written with.",
            """A table keeps every schema it has ever had. The current one is in metadata.json;
            each snapshot names its own, which is what makes reading an old snapshot show the old
            shape.""",
            see=("schema-evolution", "field-id", "snapshot"),
        ),
        _t(
            "identifier-field",
            "Identifier field",
            "The columns Iceberg treats as the row's key - the table's declared primary key.",
            """Not enforced: nothing stops a duplicate. It tells engines which columns an equality
            delete or a MERGE can match on, and it is the documentation of what a row *is*.""",
            see=("equality-delete", "field-id"),
        ),
        _t(
            "required-optional",
            "required / optional",
            "Whether the column may be null. `required` means never null, in every file.",
            """The only safe evolution is required → optional. Going the other way would make old
            files - which may contain nulls - invalid, so Iceberg refuses it.""",
            see=("schema-evolution", "write-default"),
        ),
        _t(
            "write-default",
            "Write default",
            "The value written for this column when a writer does not supply one.",
            """Paired with an initial-default, which is what *reads* return for rows written before
            the column existed. Together they are how a v3 table can add a required column without
            rewriting a byte.""",
            see=("schema-evolution", "required-optional", "format-version"),
        ),
        _t(
            "column-doc",
            "Column doc",
            "The column's description, carried in the table metadata itself.",
            """It travels with the table rather than living in a separate catalogue that drifts.
            Almost nothing sets it, which is a missed opportunity rather than a limitation.""",
            see=("schema-evolution",),
        ),
        # -- partitioning ----------------------------------------------------------------
        _t(
            "partition-spec",
            "Partition spec",
            "The rule that decides which partition a row belongs to, as transforms over columns.",
            """`days(event_ts)`, `bucket(16, id)`, `truncate(10, name)`. The spec is versioned: you
            can change how a table is partitioned, and old files keep their old spec rather than
            being rewritten, which is why a file's `spec` column matters.""",
            see=("partition-transform", "hidden-partitioning", "spec-id"),
        ),
        _t(
            "partition-transform",
            "Partition transform",
            "The function from a column value to a partition value: identity, days, hours, bucket, truncate.",
            """The transform is recorded, so a query filtering on the *source* column is
            automatically narrowed to the right partitions. This is why Iceberg has no partition
            columns to remember to filter on, and no way to silently full-scan because you wrote
            `WHERE event_ts` instead of `WHERE event_date`.""",
            see=("hidden-partitioning", "partition-spec"),
        ),
        _t(
            "hidden-partitioning",
            "Hidden partitioning",
            "Partitions are derived from real columns - there are no extra columns to write or filter on.",
            """In Hive you stored `dt=2026-03-02` as its own column and every query had to mention
            it. Iceberg records the transform instead, so the layout can change without breaking
            queries, and a query cannot miss a partition filter it was supposed to write.""",
            see=("partition-transform", "partition-spec"),
        ),
        _t(
            "spec-id",
            "Spec id",
            "Which version of the partition spec this file was written under.",
            """A table that was repartitioned holds files under several specs at once. Readers
            handle the mix; it is worth noticing because a partition filter prunes old files less
            well than new ones.""",
            see=("partition-spec", "data-file"),
        ),
        _t(
            "partition-summary",
            "Partition summary",
            "Per-partition counts: files, rows, deletes, and when it was last touched.",
            """Derived by walking manifests. It answers the two questions that actually get asked -
            which partitions are skewed, and which are full of small files - and it is where a
            compaction job gets its work list.""",
            see=("partition-spec", "compaction", "manifest-list"),
        ),
        _t(
            "sort-order",
            "Sort order",
            "How rows are ordered inside each file - a hint to writers, not a guarantee to readers.",
            """Sorting is what makes column bounds narrow, and narrow bounds are what let a scan
            skip files. An unsorted table with a `WHERE id = …` filter has every file's bounds
            covering nearly the whole range, so nothing can be skipped.""",
            see=("column-bounds", "partition-spec"),
        ),
        # -- v3 and concurrency ----------------------------------------------------------
        _t(
            "row-lineage",
            "Row lineage (v3)",
            "Per-row `_row_id` and `_last_updated_sequence_number`, assigned by the table itself.",
            """It tracks a row through file rewrites and compaction, so a reader can tell "this row
            moved" from "this row changed". It is *physical* lineage: it says how the warehouse
            stored the row, not what the business change was. Type 2 dimension history answers the
            second question and the two are not substitutes.""",
            see=("format-version", "sequence-number", "compaction"),
        ),
        _t(
            "commit-conflict",
            "Commit conflict (optimistic concurrency)",
            "Two writers committed against the same snapshot; the catalog takes one and rejects the other.",
            """Nothing is locked. A writer reads the current metadata, does its work, and asks the
            catalog to swap the pointer *only if* it still points where it did - so the loser gets
            a 409 and must re-read and retry. This is what makes many concurrent writers safe, and
            why a write path with no retry looks flaky under parallelism.""",
            see=("catalog", "table-metadata", "append"),
        ),
        _t(
            "engine-route",
            "Read route",
            "How this tool got the rows: an attached catalog, a metadata-file scan, or PyIceberg.",
            """`attached` means DuckDB spoke to the catalog and did the scan - fastest, and the
            same path a query engine uses. `scanning metadata files` means it read the metadata
            paths directly because the catalog type cannot be attached. `pyiceberg scans` is the
            pure-Python fallback. The status bar always names which one answered.""",
            see=("catalog", "data-file"),
        ),
        # -- writing, in a sandbox --------------------------------------------------------
        _t(
            "sandbox",
            "Sandbox",
            "A scratch catalog of this tool's own making, on local files, that you are meant to write to.",
            """Everywhere else this tool reads and nothing more. A sandbox is the exception: a
            SQLite catalog and a directory, made on demand, holding nothing anybody can lose. It
            exists so the way Iceberg behaves can be produced on purpose rather than waited for -
            write a row, delete a row, and watch which snapshot appears and what it claims.
            An offline sandbox writes v2, because nothing that runs without a server can write
            v3. `--rest` puts the sandbox in a namespace of a catalog you are already running,
            where DuckDB writes v3 - deletion vectors, row lineage, `UPDATE` and `MERGE` - and
            where it is confined to that one namespace and refuses to write outside it.""",
            see=("catalog", "snapshot", "copy-on-write", "deletion-vector"),
            where="here: `icelens --sandbox` (add `--rest` for v3), and the Writer pane",
        ),
        _t(
            "copy-on-write",
            "Copy-on-write",
            "Removing rows by rewriting each affected file without them, so a delete adds files.",
            """The reason a `DELETE` in the Writer ledger reads `overwrite`, `+16 rows`, `-20
            rows`: twenty rows lived in one file, four of them had to go, so the file was rewritten
            with the surviving sixteen and the original was retired. Nothing was edited in place,
            because nothing in Iceberg ever is. The alternative - merge-on-read - leaves the data
            file alone and records the dead rows in a delete file, which is cheaper to write and
            more expensive to read.""",
            see=("delete-file", "data-file", "snapshot-summary"),
            where="here: the Writer ledger's operation and row counts after a DELETE",
        ),
    )
)


# -- resolution ------------------------------------------------------------------------
#: the term a pane is *about*, shown whenever nothing more specific applies
PANE_TERMS: dict[str, str] = {
    "overview": "table-metadata",
    "schema": "schema-evolution",
    "data": "table-scan",
    "snapshots": "snapshot",
    "commit": "manifest-entry",
    "manifests": "manifest",
    "files": "data-file",
    "partitions": "partition-summary",
    "layout": "manifest-list",
    "sql": "engine-route",
    "writer": "sandbox",
}

#: tree nodes, by NodeData.kind
TREE_TERMS: dict[str, str] = {"catalog": "catalog", "namespace": "namespace", "table": "table-metadata", "view": "view"}

#: columns that mean the same thing in every pane they appear in
COMMON_COLUMNS: dict[str, str] = {
    "content": "content",
    "seq": "sequence-number",
    "size": "file-size",
    "rows": "record-count",
    "partition": "partition-spec",
    "spec": "spec-id",
    "schema": "schema-id",
    "snapshot id": "snapshot-id",
    "parent": "parent-snapshot",
    "operation": "operation",
    "status": "status",
    "added by": "snapshot-id",
    "file": "data-file",
    "path": "location",
    "manifest": "manifest",
    "format": "file-format",
    "refs": "branch",
    "total rows": "total-records",
    "deletes": "delete-file",
    "snapshots": "snapshot",
    "v": "format-version",
    "namespace": "namespace",
    "table": "table-metadata",
}

#: (pane, column) -> term, where the column means something particular to that pane
COLUMN_TERMS: dict[tuple[str, str], str] = {
    ("snapshots", "#"): "snapshot",
    ("snapshots", ""): "parent-snapshot",
    ("snapshots", "committed at"): "commit-time",
    ("writer", "statement"): "sandbox",
    ("writer", "+rows"): "copy-on-write",
    ("writer", "-rows"): "copy-on-write",
    ("writer", "files"): "data-file",
    ("writer", "written by"): "engine-route",
    ("snapshots", "+rows"): "snapshot-summary",
    ("snapshots", "-rows"): "snapshot-summary",
    ("snapshots", "+files"): "snapshot-summary",
    ("snapshots", "-files"): "snapshot-summary",
    ("snapshots", "+deletes"): "delete-file",
    ("snapshots", "first row id"): "row-lineage",
    ("data", "_row_id"): "row-lineage",
    ("data", "_last_updated_sequence_number"): "row-lineage",
    ("schema", "id"): "field-id",
    ("schema", "column"): "column-name",
    ("schema", "type"): "data-type",
    ("schema", "required"): "required-optional",
    ("schema", "key"): "identifier-field",
    ("schema", "partition"): "partition-transform",
    ("schema", "default"): "write-default",
    ("schema", "doc"): "column-doc",
    ("manifests", "own"): "manifest-entry",
    ("manifests", "added"): "status",
    ("manifests", "existing"): "status",
    ("manifests", "deleted"): "status",
    ("manifests", "+rows"): "record-count",
    ("manifests", "-rows"): "record-count",
    # `path` is a data file's own location, not the table's - the `location` term
    # COMMON_COLUMNS would otherwise hand back describes the wrong thing entirely.
    ("commit", "path"): "data-file",
    ("files", "path"): "data-file",
    ("layout", "node"): "manifest-list",
    ("layout", "detail"): "manifest-list",
    ("layout", "size / rows"): "file-size",
    ("partitions", "spec_id"): "spec-id",
    ("partitions", "record_count"): "record-count",
    ("partitions", "file_count"): "data-file",
    ("partitions", "total_data_file_size_in_bytes"): "file-size",
    ("partitions", "position_delete_record_count"): "position-delete",
    ("partitions", "position_delete_file_count"): "position-delete",
    ("partitions", "equality_delete_record_count"): "equality-delete",
    ("partitions", "equality_delete_file_count"): "equality-delete",
    ("partitions", "last_updated_at"): "commit-time",
    ("partitions", "last_updated_snapshot_id"): "snapshot-id",
    ("namespace", "partitioned by"): "partition-spec",
    ("namespace", "updated"): "commit-time",
}

#: rows, not columns: panes whose first column is a label, so the *row* names the term
ROW_KEYED: frozenset[str] = frozenset({"overview", "layout"})

#: first-cell text (lowercased, prefix-matched) -> term, for the row-keyed panes
ROW_TERMS: tuple[tuple[str, str], ...] = (
    ("format version", "format-version"),
    ("uuid", "table-uuid"),
    ("location", "location"),
    ("last updated", "commit-time"),
    ("current snapshot", "snapshot-id"),
    ("viewing snapshot", "time-travel"),
    ("snapshots", "snapshot"),
    ("schemas", "schema-id"),
    ("columns", "field-id"),
    ("partition spec", "partition-spec"),
    ("sort order", "sort-order"),
    ("rows (metadata)", "total-records"),
    ("next row id", "row-lineage"),
    ("data files", "data-file"),
    ("delete files", "delete-file"),
    ("total size", "file-size"),
    ("ref ", "branch"),
    ("property ", "table-metadata"),
    ("metadata log", "metadata-log"),
    ("table metadata", "table-metadata"),
    ("snapshot ", "snapshot"),
    ("manifest list", "manifest-list"),
    ("data manifest", "manifest"),
    ("delete manifest", "manifest"),
    ("position deletes", "position-delete"),
    ("equality deletes", "equality-delete"),
    ("deletion vector", "deletion-vector"),
    ("data [", "data-file"),
    ("table", "table-metadata"),
)

#: the value in the cell, when it names a thing of its own. Prefix-matched, lowercased.
VALUE_TERMS: tuple[tuple[str, str], ...] = (
    ("position deletes", "position-delete"),
    ("equality deletes", "equality-delete"),
    ("deletion vector", "deletion-vector"),
    ("append", "append"),
    ("overwrite", "operation"),
    ("replace", "compaction"),
    ("delete", "delete-file"),
    ("added", "status"),
    ("existing", "status"),
    ("deleted", "status"),
    ("identifier", "identifier-field"),
    ("required", "required-optional"),
    ("optional", "required-optional"),
    ("bucket[", "partition-transform"),
    ("truncate[", "partition-transform"),
    ("days", "partition-transform"),
    ("hours", "partition-transform"),
    ("months", "partition-transform"),
    ("years", "partition-transform"),
)

#: columns whose *value* is worth explaining; anywhere else a value is just a value
VALUE_COLUMNS: frozenset[str] = frozenset(
    {"content", "operation", "status", "type", "key", "required", "partition", "node"}
)


def _prefix_match(text: str, table: tuple[tuple[str, str], ...]) -> str | None:
    needle = " ".join(text.split()).lower().lstrip("└ ")
    for prefix, key in table:
        if needle.startswith(prefix):
            return key
    return None


def resolve(
    pane: str,
    column: str | None = None,
    row_label: str | None = None,
    value: str | None = None,
) -> Term | None:
    """The term that explains what the cursor is on, most specific first.

    Order matters and is the whole behaviour of the rail: a cell reading `equality deletes`
    explains equality deletes, not the `content` column it sits in; a row labelled `sort
    order` explains sort order, not the pane. Falling all the way through gives the term the
    pane is about, so the rail is never empty.
    """
    if value and column in VALUE_COLUMNS:
        key = _prefix_match(value, VALUE_TERMS)
        if key:
            return TERMS.get(key)
    if row_label and pane in ROW_KEYED:
        key = _prefix_match(row_label, ROW_TERMS)
        if key:
            return TERMS.get(key)
    if column is not None:
        key = COLUMN_TERMS.get((pane, column)) or COMMON_COLUMNS.get(column.strip().lower())
        if key:
            return TERMS.get(key)
    return TERMS.get(PANE_TERMS.get(pane, ""))


def for_tree(kind: str) -> Term | None:
    return TERMS.get(TREE_TERMS.get(kind, ""))


def search(text: str) -> list[Term]:
    """Terms whose name or key contains `text` - for `icelens --explain <word>`."""
    needle = text.strip().lower()
    if not needle:
        return []
    exact = [t for key, t in TERMS.items() if key == needle or t.name.lower() == needle]
    if exact:
        return exact
    return [t for key, t in TERMS.items() if needle in key or needle in t.name.lower() or needle in t.gloss.lower()]

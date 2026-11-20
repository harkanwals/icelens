# icelens against the newest Iceberg

Checked 2026-11-13. Every "run" below was run locally that day; everything else carries a
source link, and anything neither run nor sourced is marked **unverified**.

## The answer

icelens opens most v3 tables that Spark 4.1 and DuckDB write today: deletion vectors, row lineage,
default values, nanosecond timestamps, uuid, time and v3 partitioning all work in every pane.
**Two v3 types made a table impossible to open at all: `variant` and `geometry`/`geography`.**
PyIceberg 0.12, which icelens reads all metadata with, cannot parse them as Spark 4.1 and DuckDB
write them.

**Update, same day:** all of those now open.
`icelens.opaque` keeps the two types as written. Variant rows show as JSON text. The one remaining
gap is rows of a table with a `geography` column, which DuckDB cannot read. DV commits now count
their rows, and a write PyIceberg can't read back is reported as committed. `--serve` now says how
each client asks for v3, and the suite no longer depends on the host's time zone or config.

## What is newest, and who writes it

| Release | Version | Date | Source |
|---|---|---|---|
| Apache Iceberg (Java) | 1.12.0 | 2026-09-30 | [GitHub release](https://github.com/apache/iceberg/releases/tag/apache-iceberg-1.12.0) |
| Spark runtimes for it | Spark 4.0 and 4.1 only (no 4.2/4.3 runtime) | | [Maven Central](https://repo1.maven.org/maven2/org/apache/iceberg/) |
| Spark | 4.3.0 newest image; 4.1.1 is the newest Iceberg supports | | [Docker Hub](https://hub.docker.com/r/apache/spark/tags) |
| PyIceberg | 0.12.0 | 2026-09-01 | [PyPI](https://pypi.org/project/pyiceberg/) |
| DuckDB | 2.0.0 | 2026-10-21 | [GitHub release](https://github.com/duckdb/duckdb/releases/tag/v2.0.0) |
| iceberg-rust | 0.11.0 | 2026-11-03 | [GitHub release](https://github.com/apache/iceberg-rust/releases/tag/v0.11.0) |
| Trino | 484 | 2026-10-16 | [GitHub release](https://github.com/trinodb/trino/releases/tag/484) |
| Flink runtime | iceberg-flink-runtime-2.1 1.12.0 | | [Maven Central](https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-flink-runtime-2.1/) |

**Format v3** is complete ("Spec: Mark version 3 as completed", Iceberg 1.10.0). Its features, from
[the spec](https://github.com/apache/iceberg/blob/main/format/spec.md#version-3): nanosecond
timestamps, `unknown`, `variant`, `geometry`, `geography`, default values, multi-argument
transforms, row lineage, deletion vectors, table encryption keys.

**Format v4** is "under active development and has not been formally adopted"
([spec](https://github.com/apache/iceberg/blob/main/format/spec.md#version-4-metadata-structure-and-representation)):
an adaptive metadata tree, data files and their DV in one entry, relative paths, no new equality
deletes, a `file` type, table constraints, default-value expressions. Java 1.12.0 already has code
for v4 manifests and relative paths
([#15634](https://github.com/apache/iceberg/pull/15634), [#15630](https://github.com/apache/iceberg/pull/15630)).
Nothing writes a v4 table yet that could be tested; no action for icelens until it is adopted.

**REST catalog spec** additions ([OpenAPI on main](https://github.com/apache/iceberg/blob/main/open-api/rest-catalog-open-api.yaml)):
views (`/views`, `/register-view`), server-side scan planning (`/plan`, `/plan/{id}`, `/tasks`),
multi-table commit (`/transactions/commit`), vended-credential refresh (`/credentials`, now with
`planId`), remote signing (`/sign`), and in 1.12.0 `unregister` and function list/load
([#16400](https://github.com/apache/iceberg/pull/16400), [#15180](https://github.com/apache/iceberg/pull/15180)).

## The matrix

Writers: **run** = a table was written with it locally; **docs** = from the linked source, not run.
icelens panes: overview, schema, data, snapshots, commit, manifests, files, partitions, layout,
each run headless (`--pane X --json`) against the table.

| v3 feature | Spark 4.1.1 + Iceberg 1.12.0 | DuckDB 1.5.5 / 2.0.0 | PyIceberg 0.12.0 | Trino 484 | iceberg-rust 0.11 | Flink | icelens result |
|---|---|---|---|---|---|---|---|
| Deletion vectors | ✅ run (DELETE, UPDATE, MERGE, compaction) | ✅ run | ❌ run: no v3 data writes at all¹ | ✅ run, 484 (DELETE, UPDATE; icelens commit pane counts -1) | reads DVs ([#3035](https://github.com/apache/iceberg-rust/pull/3035)) | unverified | ✅ all 9 panes. **Fixed:** labelled "position deletes", now "deletion vector"; commit note said "-0 rows", now counts |
| Row lineage (`_row_id`) | ✅ run (read back in Spark) | ✅ run | ❌¹ | unverified | unverified | rewrite preserves it ([#14149](https://github.com/apache/iceberg/pull/14149)) | ✅ schema shows reserved columns; data pane lineage toggle works |
| `variant` | ✅ run, shredded too | ✅ run, both versions | ❌ cannot even parse it³ | ✅ run, 484 (`CAST(JSON … AS VARIANT)`; icelens shows it) | ✅ ([#2188](https://github.com/apache/iceberg-rust/pull/2188)) | Flink 2.x, docs⁴ | ✅ now: all panes; rows as JSON text (DuckDB 1.5.5 cannot export variant to Arrow) |
| `geometry` / `geography` | ✅ run, needs `spark.sql.geospatial.enabled=true` | ✅ run (`geometry` only) | metadata only, in a non-spec form⁵ | not in type map² | not supported per [status page](https://github.com/apache/iceberg/blob/main/site/docs/status.md) | unverified | ✅ now: all metadata panes; `geometry` rows read. ❌ rows of a table with `geography`: DuckDB "Geography support" not implemented |
| `timestamp_ns` / `timestamptz_ns` | not tested; Spark SQL has no nanosecond type (**unverified**) | ✅ run (`TIMESTAMP_NS`) | metadata only¹ | ✅ run, 484 (`TIMESTAMP(9)`, `TIMESTAMP(9) WITH TIME ZONE`) | ✅ ([#2832](https://github.com/apache/iceberg-rust/pull/2832)) | unverified | **Fixed:** data pane, `--json` and TUI cell lookup raised a traceback; now nine digits shown. DuckDB 1.5.5 cannot read a `timestamptz_ns` column ("Unrecognized primitive type"), said in words. `timestamptz_ns`: DuckDB 1.5.5 cannot read the table at all ("Unrecognized primitive type"); rows read through PyIceberg |
| `unknown` | not tested | not tested | metadata only¹ | ✅ docs² | ❌ status page | unverified | ✅ metadata panes; data pane says DuckDB cannot read it, in words |
| Default values | ❌ run: "setting default values in Spark is currently unsupported" | ✅ run (v3 only; refused on v2) | dropped on `create_table`⁶ | unverified | ✅ reads them | unverified | ✅ schema pane shows the default; old rows read the initial default |
| v3 partitioning (`day`, `bucket`) | ✅ run (`days(ts_ntz)`) | ✅ run (`day(d)`, `bucket(4,k)`) | needs pyiceberg-core | ✅ run, 484 (`month(d)`) | unverified | unverified | ✅ partitions pane |
| Multi-argument transforms | no writer found that exposes them (**unverified**) | | no transform class for them | | | | not tested |
| Encryption keys | not tested | | | writing encrypted tables unsupported² | reads encrypted Puffin | | not tested |
| uuid, time (v2 types, v3 table) | | ✅ run | | | | | ✅ |
| Compaction (`rewrite_data_files`) | ✅ run | | | | | | ✅; commit shows physical rows (+17 / -23), not "no logical change" |

| REST feature | Who has it | icelens |
|---|---|---|
| Views | Spark 4.1 + Iceberg 1.12 `CREATE VIEW`, `CREATE OR REPLACE`, `ALTER … SET TBLPROPERTIES`, `SHOW VIEWS`, `RENAME`, `SELECT` ✅ run against `--serve`; Trino 484 `CREATE VIEW` ✅ run, shown with dialect `trino` (before: "Server does not support endpoint: POST …/views") | ✅ now: in the tree, every version and dialect shown; `--pane overview` prints it. Untested against Lakekeeper/Polaris |
| Server-side scan planning | PyIceberg 0.12 has `plan_scan`, `supports_server_side_planning` | not used; untested |
| Credential refresh endpoint | in spec | not used; a long session outlives its token |
| Multi-table commit, unregister, functions | in spec | not relevant to a reader, except listing functions (**unverified** whether any catalog serves them) |

Footnotes:

1. Run: PyIceberg 0.12.0 `append` on any v3 table raises `ValueError: Cannot write manifest for
   table version: 3`. Tracked as [iceberg-python#1551](https://github.com/apache/iceberg-python/issues/1551) (open).
2. [Trino Iceberg connector docs](https://trino.io/docs/current/connector/iceberg.html): "Support for
   format version 3 is experimental… Row-level updates and deletes on version 3 tables use deletion
   vectors"; type map includes `VARIANT`, `TIMESTAMP(9)`, `UNKNOWN`; no geometry.
3. Run: `Unsupported field type: 'variant'`. [iceberg-python#1819](https://github.com/apache/iceberg-python/issues/1819)
   (open). PyIceberg main's `types.py` has no variant either. Iceberg's
   [status page](https://github.com/apache/iceberg/blob/main/site/docs/status.md) says PyIceberg
   supports `variant` (Y); that is wrong for 0.12.0 and main.
4. [Flink docs](https://github.com/apache/iceberg/blob/main/docs/docs/flink.md): variant "Flink 2.x
   only"; Iceberg 1.12.0 adds shredded variant writes ([#15596](https://github.com/apache/iceberg/pull/15596)).
5. The spec's JSON form is unquoted, `"geometry(srid:4326)"`
   ([Appendix C](https://github.com/apache/iceberg/blob/main/format/spec.md#appendix-c-json-serialization)).
   Spark writes `geometry(OGC:CRS84)`, DuckDB `geometry(ogc:crs84)`. PyIceberg 0.12 and main accept
   only the quoted `geometry('…')`, and write that form. Upstream: [iceberg-python#4079](https://github.com/apache/iceberg-python/issues/4079) (open).
   PyIceberg cannot write geometry data either: its schema check rejects Arrow `binary` for it.
6. Run: `assign_fresh_schema_ids` rebuilds each field without `initial_default`/`write_default`, so
   a default given to `create_table` is silently lost. Same code on PyIceberg main today. No
   upstream issue found.

## Bugs and gaps found in icelens

Fixed in icelens (each test fails without its fix; checked):

1. **Nanosecond timestamps crashed the data pane.** pyarrow's `to_pylist()` raises on a timestamp
   with a nanosecond digit unless pandas is installed. `format.pylist()` now renders those columns as
   text; the grid, `--json` rows and the TUI's cell lookup use it. `--json` `columns` still says
   `timestamp[ns]`. Tests: `test_format.py`, `test_v3_sandbox.py`.
2. **Deletion vectors were labelled "position deletes"** in the commit, files and layout panes,
   though `terms.py` already had a "deletion vector" explanation no cell ever reached. A Puffin
   position-delete file is now "deletion vector". `--json` `content` gains that value (keys and types
   unchanged).
3. **"cannot load t: 1 validation error for TableResponse"** now reads "cannot load t: Unsupported
   field type: 'variant' - icelens reads table metadata with PyIceberg 0.12.0, which cannot read this
   column type yet". Same for geometry. Tests: `test_catalog.py`, `test_v3_sandbox.py`.

Fixed afterwards, each with a test that fails without it:

| Gap | What changed |
|---|---|
| A: variant / geometry tables would not open | `icelens.opaque` keeps the types as written and serialises them back exactly; variant rows read as JSON text. Still open: rows of a `geography` table (DuckDB) and a variant nested in a struct |
| B: "-0 rows" on every DV commit | rows removed = removed data rows + added DV cardinality - removed DV cardinality; matches Spark and DuckDB commits, compaction +17/-17. The snapshots pane's `-rows` column still reads the summary |
| C: `--write` reported a committed CREATE as failed | reported from DuckDB's `iceberg_snapshots` and row counts when PyIceberg cannot read the table back |
| F: Iceberg views were invisible, and `--serve` refused them | the served catalog keeps views (create, load, replace, rename, drop); the tree lists them; a view shows its versions and SQL per dialect |
| D: `--serve` and format version | decided against a v3 default: DuckDB sends `format-version: 2` explicitly, so a default would only reach PyIceberg, which cannot append to v3. `--serve` prints how DuckDB and Spark ask for v3 |
| tests depended on the host's time zone and `~/.pyiceberg.yaml` | suite pins `TZ=UTC`; `clean_environment` seeds a catalog-less config |

Still open:

| # | Gap | Effort |
|---|---|---|
| E | DuckDB 2.0 warning on every `--sql` (`catalog.py`, `con.execute(sql).to_arrow_table()`) | half a day |

## What icelens should support next, ranked

1. ~~Open tables with `variant` and `geometry` columns~~ - done.
2. ~~Correct row counts for DV commits~~ - done for the commit pane; the
   row-level diff (which rows) is still open.
3. ~~Iceberg views in the tree~~ - done; running a view's SQL is not.
4. ~~Writer read-back~~ - done.
5. **DuckDB 2.0** (half a day).
6. **Rows of a `geography` table**: wait for DuckDB, or read through PyIceberg without the column
   (1 day).
7. **Verify Trino, Flink, iceberg-rust writers** (1 day); multi-argument transforms and
   encryption (1 day).
8. **Server-side scan planning** (3 days, later); **v4** when adopted.

## Worth filing upstream

1. PyIceberg `assign_fresh_schema_ids` drops default values (footnote 6).
2. Iceberg's status page claims PyIceberg variant support (footnote 3).

## How this was run

- icelens's own REST catalog: `ICELENS_SERVE_TOKEN=… icelens --sandbox feat --v3 --serve --port 18999`
  with the sandbox in a scratch directory.
- Spark: `apache/spark:4.1.1-python3` plus `iceberg-spark-runtime-4.1_2.13-1.12.0.jar`, reaching the
  catalog at `host.docker.internal:18999`, with the warehouse directory mounted at the same absolute
  path so `file://` locations resolve on both sides.
- DuckDB 1.5.5 (icelens's lock) and 2.0.0 (`uv run --with duckdb==2.0.0`), through `ATTACH … (TYPE ICEBERG)`,
  `CREATE TABLE … WITH ("format-version" = 3)`.
- PyIceberg 0.12.0 through the same REST catalog.
- Each table, each pane: `icelens --uri … --pane P TABLE --json`.
- Not run: Flink, iceberg-rust, Lakekeeper/Polaris (views), encryption, multi-argument
  transforms. Trino 484 was run afterwards.
- Full suite afterwards: 1044 passed, 25 skipped (live catalogs, packaging), 0
  failed; `ruff check` and `pyright` clean. The 3 host-dependent failures seen first (time zone,
  `~/.pyiceberg.yaml`) were fixed.

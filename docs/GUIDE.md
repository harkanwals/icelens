# The icelens guide

Reference for everything icelens does. Installing and a first look are in the
[README](../README.md); what works on which platform is in [Platforms](PLATFORMS.md).

## Finding your catalog

With nothing configured, `icelens` offers a demo sandbox, the same at format-version 3, an empty
sandbox, any sandbox you made before, or how to connect to your own catalog. Without a terminal
it prints one line and exits 1 (`"kind": "NoCatalog"` under `--json`).

Settings come from these places, the first that applies winning:

1. The command line: `--uri`, `--warehouse`, and `-p key=value` for any PyIceberg property
   (repeatable: `-p s3.endpoint=http://localhost:9000`).
2. `ICELENS_*` environment variables: `ICELENS_CATALOG_URI`, `ICELENS_WAREHOUSE`,
   `ICELENS_S3_ENDPOINT`, `ICELENS_CATALOG_TOKEN`, `ICELENS_CATALOG_CREDENTIAL`.
3. The profile `--catalog NAME` names in `~/.pyiceberg.yaml`. No `.env` is read when you name one.
4. The nearest `.env`, searching up from the working directory as far as your home directory.
   Keys match on their suffix, so `MYAPP_CATALOG_URI` and `ACME_S3_ACCESS_KEY` work unedited.
   Only a name ending `CATALOG_TOKEN` or `CATALOG_CREDENTIAL` is read as the catalog's secret.
   A `.env` that another user owns or can change is refused. `--no-env` skips the search.
5. The `default` profile in `~/.pyiceberg.yaml`.

```yaml
# ~/.pyiceberg.yaml
catalog:
  lab:                                    # icelens --catalog lab
    type: rest
    uri: http://localhost:8181/catalog
    warehouse: analytics
    s3.endpoint: http://localhost:9000
    header.X-Iceberg-Access-Delegation: vended-credentials   # short-lived token per table
  glue:
    type: glue
    warehouse: s3://my-bucket/warehouse   # credentials from the usual AWS chain
```

`icelens --catalogs` lists the profiles, and `ctrl+o` switches between them in the app.

**Secrets.** Keep them out of the YAML file. Use PyIceberg's environment variables
(`PYICEBERG_CATALOG__LAB__CREDENTIAL=id:secret`) or a `.env` only you can read. `-p token=…`
works, but a command line is visible to every user on the machine (`ps`). icelens shows any
secret, token, password or credential as `********`. The one exception is `--serve`, which prints
the token of the catalog it serves.

**Warehouses.** A REST catalog that hosts several (Lakekeeper, Polaris) has to be told which one.
icelens asks the server for the list: one warehouse is selected for you, several open a picker.
`icelens --warehouses` prints them, and `ctrl+w` switches in the app.

## Keys

```
↑ ↓ ← →   the tree and the grids         enter    open a table / pin a snapshot
1 … 9 0   jump to a pane                 tab      next pane
/         filter the tree                ctrl+p   fuzzy-find any table
escape    back to the tree               r        reload the selected table (drops the cache)
u         back to the current snapshot   q        quit
, .       older / newer snapshot         [ ]      halve / double the row limit
w         filter rows (SQL WHERE, Data pane)
x         export the focused grid to CSV c        copy the focused cell
l         row lineage on/off (v3, Data)  g        cycle the guide rail
ctrl+r    run the SQL pane, or commit    ?        help
          the Writer pane (sandbox only)
e         the Writer pane (sandbox only)
ctrl+o    switch catalog (~/.pyiceberg.yaml)
ctrl+w    switch warehouse (REST catalogs that host several)
```

## The panes

| key | pane | what it shows |
|---|---|---|
| 1 | Overview | `metadata.json`: format version, uuid, location, refs, properties, recent metadata log entries |
| 2 | Schema | the viewed snapshot's schema with field ids, nested fields, identifier fields and partition transforms |
| 3 | Data | rows; `[` and `]` change the limit, `w` filters, `l` shows row lineage |
| 4 | Snapshots | every commit, oldest first: `◆` current, `│` an ancestor of current, `~` off the current chain |
| 5 | Commit | the files one snapshot added and the files it retired |
| 6 | Manifests | the snapshot's manifest list, one row per manifest, with partition ranges |
| 7 | Files | every live file at that snapshot: data, position deletes, equality deletes |
| 8 | Partitions | partitions with their file and row counts |
| 9 | Layout | the pointer chain: table metadata → snapshot → manifest list → manifests → files |
| 0 | SQL | DuckDB SQL over the catalog; tables are `icelens."<namespace>"."<table>"` |

- **Views** sit in the tree beside the tables, marked `view`. Every pane shows the definition:
  one row per version and SQL dialect, newest first, with the engine that wrote it and its
  columns. icelens does not run a view's SQL.
- **Large tables.** Commit, Manifests, Files and Layout stop at a few thousand rows, because every
  row below the manifest list is an object-store read. The pane says when it has stopped
  (`showing the first 2,000 of 50,000 files`).
- **The guide rail** under each pane explains whatever the cursor is on: the pane, the column or
  the value. `g` cycles it between full, one line and off. `icelens --explain` prints every term,
  and `icelens --explain "manifest list"` one in full.

### Time travel

Enter on a snapshot pins it, and every pane re-reads as of that snapshot: its schema, its files,
its rows. `u` returns to the current snapshot.

- Enter works on any cell that names a snapshot: `snapshot id` and `parent` in Snapshots, and
  `added by` in Manifests and Files.
- `,` and `.` step to the older and newer snapshot, in the order the Snapshots pane lists them.
- Without the app: `icelens --pane data demo.orders --snapshot <snapshot-id>`.

SQL's own time travel works where DuckDB has attached the catalog (REST, Glue, S3 Tables, and a
`--v3` sandbox):

```sql
SELECT count(*) FROM icelens."demo"."orders" AT (VERSION => <snapshot-id>);
```

On the other routes DuckDB answers "Catalog type does not support time travel". Use a pane there.

### Row lineage, on v3

Format version 3 gives every row a `_row_id` and a `_last_updated_sequence_number` that survive
file rewrites and compaction. `SELECT *` does not return them.

- `l` in the Data pane shows both, as the first columns. On a v2 table it says so and changes
  nothing.
- Overview shows `next row id`, Snapshots the `first row id` of each commit, and Schema lists the
  two columns marked `row lineage` and `reserved`.
- When PyIceberg reads the rows it cannot compute the columns. The rows still arrive, and the
  engine is named `pyiceberg (no row lineage here)`.

## The sandbox

A sandbox is a local catalog icelens can write to. It is the only place icelens writes by default.

```bash
icelens --sandbox                      # a local catalog; `e` opens the Writer pane
icelens --sandbox teaching             # a named one, kept between runs
icelens --sandbox teaching --sandbox-reset      # empty it
icelens --demo                         # the sandbox called demo, with a table already in it
```

A sandbox lives in `~/.local/share/icelens/sandboxes/<name>/`, readable only by you, or under
`--sandbox-dir`. A path containing `#` or `?` is refused.

### The demo table

`--demo` writes `demo.orders` with the same five commits on every machine. It needs no network.

| commit | what happened | where to look |
|---|---|---|
| 1 | 120 orders, twelve of them test rows; tagged `first-load` | Overview's refs |
| 2 | 30 late arrivals | Manifests: a second manifest |
| 3 | `DELETE` the test rows: an **overwrite**, `+108 / -120` | Commit: one file retired, one added (copy-on-write) |
| - | add `channel`, rename `customer` to `customer_id` | Schema: the rename kept field id 4 |
| 4 | 20 orders with a channel | Data: `channel` is null for older rows |
| - | partition by `region` from here on | Partitions, Files: two specs |
| 5 | 25 orders, one file per region | Layout |

`--sandbox NAME` puts the demo in another sandbox, and `--sandbox-reset` starts its history again.

### The Writer pane

The Writer pane (`e`) takes `CREATE TABLE t AS SELECT …`, `INSERT INTO t SELECT …` and
`DELETE FROM t WHERE …`, and `ctrl+r` commits them. Below the editor a ledger lists what each
commit did:

```
 #  statement                                    operation  snapshot id           +rows  -rows  total rows  files
 1  CREATE TABLE demo.sales AS SELECT i AS id…   append     365503770289136103      100      0         100      1
 2  INSERT INTO demo.sales SELECT i, 'r0', 9.99  append     943875501117234734       10      0         110      2
 3  DELETE FROM demo.sales WHERE region = 'r0'   delete     5294193392015656713       0     10         100      1
 4  DELETE FROM demo.sales WHERE region = 'r0'   overwrite  3471267070479190902      75    100          75      1
```

Rows 3 and 4 are one `DELETE` that made two commits. A file holding only deleted rows was dropped
(a `delete`). A file holding deleted rows and rows to keep was rewritten without them (an
`overwrite`, which adds the kept rows back). A statement that changes nothing adds no row.

- The query half of a statement is DuckDB's: `read_csv_auto`, window functions and joins all work.
  Name another table in full, `icelens."demo"."sales"`.
- `INSERT` maps columns by position and casts them to the table's types.
- Tables are format-version 2 unless the sandbox is v3 (`--v3` or `--rest`, below).
- Without the app, `--sandbox NAME --write "<sql>"` prints the same ledger, one line per commit.
  Add `--json` for a script.

### `--v3`: a sandbox at format-version 3

```bash
icelens --sandbox lesson --v3          # deletion vectors, row lineage, UPDATE, MERGE
icelens --demo --v3                    # the demo table at v3, in the sandbox demo-v3
```

DuckDB's iceberg extension does the writing, so it has to be installed. icelens serves the
sandbox's catalog over Iceberg REST inside its own process, on `127.0.0.1` behind a token made
for the run, and DuckDB writes through that. Nothing is left listening when icelens exits.

A sandbox made v3 stays v3, also after `--sandbox-reset`, and keeps the v2 tables it had. A new
table is v3 unless the statement says `WITH ("format-version" = 2)`.

The v3 demo:

| commit | what happened | where to look |
|---|---|---|
| 1, 2 | 120 orders, then 30 more; the first tagged `first-load` | Snapshots: `first row id` 0, then 120 |
| 3 | `DELETE` the test rows: a **delete**, not an overwrite | Files: a deletion vector, and the data file untouched |
| 4 | `UPDATE` one order | Data, `l`: order 7 keeps `_row_id` 6 |
| - | `ALTER TABLE … ADD COLUMN channel` | Schema: schema 1 |
| 5 | 20 orders with a channel | Data: `channel` null for older rows |
| 6, 7 | one `MERGE`: a correction, then two new orders | Snapshots: one statement, two snapshots |

### `--serve`: the sandbox, for other engines

```bash
icelens --demo --v3 --serve      # or: --sandbox NAME --serve
```

Serves the sandbox's catalog over Iceberg REST until ctrl+c, and prints the URI, the warehouse
and a bearer token as a `~/.pyiceberg.yaml` entry and a DuckDB `ATTACH`. `--json` prints one
object instead. `--port N` and `ICELENS_SERVE_TOKEN` (16 characters or more) fix the port and the
token.

- It listens on 127.0.0.1 only, and the data files are local paths, so clients run on this machine.
- Anyone on this machine with the token can write to the sandbox, and to nothing outside it.
- Tested with PyIceberg, DuckDB and icelens. Other Iceberg REST clients have not been tried.

### `--rest`: a sandbox in a catalog you run

```bash
icelens --sandbox lesson --rest --warehouse lab      # takes the usual connection settings
```

Puts the sandbox in one namespace, `sandbox_lesson`, of a catalog you already run (Lakekeeper,
Polaris, anything DuckDB attaches). Writes outside that namespace are refused: a bare `sales`
means `sandbox_lesson.sales`, and `crm.accounts` is an error. DuckDB does the writing, so `UPDATE`
and `MERGE` work, and tables are v3 unless you ask for v2.

```
append     on sandbox_lesson.sales · +100 rows · snapshot 1698712542466490902 · duckdb through the catalog
delete     on sandbox_lesson.sales ·  -25 rows · snapshot 1977770655278908907 · duckdb through the catalog
overwrite  on sandbox_lesson.sales · +25 rows · -25 rows · snapshot 319156682419751923 · duckdb through the catalog
```

A delete here leaves a deletion vector beside the data file instead of rewriting it. An `UPDATE`
is reported as `+25 / -25`: a delete and an insert. The ledger's row counts are measured, because
the snapshot summary's `added-position-deletes` overcounts when a deletion vector is rewritten.

## How rows are read

The metadata panes work the same on every catalog, through PyIceberg. Rows come by one of three
routes, and the status bar names the one in use:

| route | when | how |
|---|---|---|
| `attached` | REST, Glue, S3 Tables | DuckDB attaches the catalog and sees new tables and snapshots itself |
| `scanning metadata files` | Hive, SQL, DynamoDB, in-memory, or a REST catalog DuckDB refused | PyIceberg finds each table's `metadata.json` and DuckDB scans it |
| `pyiceberg scans` | DuckDB's iceberg extension is missing, or DuckDB could not reach a table's files | PyIceberg reads the rows |

- `icelens."<namespace>"."<table>"` works in SQL on every route, and so do the panes' time travel.
- A nested namespace is one name to DuckDB: `icelens."core.nested"."deep"`.
- When DuckDB is refused, icelens says so: `attach refused, not authenticated: …` in the status
  bar, or `90 rows via pyiceberg (duckdb could not read it: HTTP 403 …)` on a table.
- Object-store credentials configured for the catalog are passed to DuckDB.

### OAuth catalogs: Polaris, Snowflake, Databricks

These log in with OAuth and hand out storage credentials per table, so the connection needs a
login and nothing about the store:

```bash
# Apache Polaris, and Snowflake Open Catalog (which is Polaris)
icelens --uri https://HOST/api/catalog --warehouse CATALOG \
        -p credential=CLIENT_ID:CLIENT_SECRET -p scope=PRINCIPAL_ROLE:ALL

# Snowflake Horizon: the warehouse is a database, the secret is a PAT, and there is no client id
icelens --uri https://ORG-ACCOUNT.snowflakecomputing.com/polaris/api/catalog --warehouse DATABASE \
        -p credential=$SNOWFLAKE_PAT -p scope=session:role:ROLE

# Databricks Unity Catalog: the warehouse is a Unity catalog; a PAT, or a service principal
icelens --uri https://WORKSPACE/api/2.1/unity-catalog/iceberg-rest --warehouse UC_CATALOG -p token=$DATABRICKS_PAT
icelens --uri https://WORKSPACE/api/2.1/unity-catalog/iceberg-rest --warehouse UC_CATALOG \
        -p credential=SP_ID:SP_SECRET -p oauth2-server-uri=https://WORKSPACE/oidc/v1/token -p scope=all-apis
```

- DuckDB gets the same login PyIceberg uses, and the storage credentials the catalog vends for
  each table.
- A Polaris over a store with no STS refuses to vend credentials. icelens asks once, then uses
  the store keys you configured. The status bar says so.
- Without `--warehouse`, the picker lists Polaris's catalogs if your login may see them.
- Polaris is tested live. Snowflake and Databricks are written from their documentation and have
  not been run against a real account; see [Platforms](PLATFORMS.md).

### Google Cloud Storage and BigLake

```bash
icelens --uri https://biglake.googleapis.com/iceberg/v1/restcatalog \
        --warehouse bl://projects/PROJECT_ID/catalogs/CATALOG_ID \
        -p token=$(gcloud auth print-access-token) \
        -p header.x-goog-user-project=PROJECT_ID
```

- BigLake (Google's Lakehouse runtime catalog) needs no second credential: the catalog's token
  also reads Cloud Storage. icelens reuses it only when the catalog's host is `googleapis.com`.
- The token lasts about an hour and is read once. When panes start failing with 401, press
  `ctrl+o` and connect again.
- DuckDB does not accept a `bl://` warehouse for an attach, so rows come by the scanning route.
- For any other `gs://` warehouse, set `gcs.oauth2.token` or an HMAC key pair, or let DuckDB find
  application default credentials.

## Without a terminal

Every pane prints, as a table, CSV or JSON:

```bash
icelens --list                                   # namespaces and table counts
icelens --list demo                              # the tables in one namespace
icelens --pane snapshots demo.orders             # a pane, as a table
icelens --pane layout demo.orders --snapshot <snapshot-id>
icelens --pane files demo.orders --csv > files.csv
icelens --pane data demo.orders --where "region = 'west'" --json
icelens --sql 'SELECT count(*) FROM icelens."demo"."orders"'
icelens --catalogs                               # every catalog in ~/.pyiceberg.yaml
icelens --catalog lab --list                     # one of them
```

`--csv` writes exact values to a file or a pipe. Printed to a terminal, control characters are
replaced first, as in all other output.

| exit code | meaning |
|---|---|
| `0` | fine |
| `1` | the catalog or the query refused |
| `2` | bad usage, including asking for the app when stdout is not a terminal |
| `130` | interrupted |
| `141` | the reader closed the pipe, as `head` does |

### `--sql`

`--sql` runs one DuckDB statement and only reads.

- Against a real catalog, the catalog is attached `READ_ONLY`.
- Only `SELECT` and `EXPLAIN` run. That includes `DESCRIBE`, `SHOW`, `SUMMARIZE` and CTEs, and
  excludes `COPY … TO` and an `EXPLAIN ANALYZE` of a write. This holds in a sandbox too: write
  there with `--write` or the Writer pane.
- Every table named has to be in the catalog, written `icelens."namespace"."table"`, or be a CTE
  of the query. A mistyped table is refused with "not a table in this catalog".
- The only table functions allowed are `range`, `generate_series` and `unnest`. Functions that
  read DuckDB's settings are refused.

```bash
$ icelens --sql "SELECT 1; DROP TABLE demo.orders" --json
{"schema_version": 1, "error": {"kind": "IcelensError",
 "message": "refusing to run DROP: icelens reads. Pass --allow-writes to mean it."}}
```

**Use it with caution.** `--sql` is DuckDB, and DuckDB can read local files and URLs. The check
reads the text of a query; it does not isolate DuckDB from the machine, and a spelling it does
not know would get through.

- Run SQL you wrote or have read. Do not run SQL taken from a catalog's contents: a table
  comment, a property, a value.
- Give an agent that drives `--sql` no more file and network access than you would give DuckDB.
- To query a local file on purpose, use DuckDB itself.

Not checked: the app's SQL pane, a sandbox's `--write` (which may read a local file:
`CREATE TABLE demo.t AS SELECT * FROM 'orders.csv'`), and anything run with `--allow-writes`,
which also attaches the catalog read-write.

`--where` filters `--pane data` only. It is one condition: `--where "false; SELECT 1"` is refused.

### `--json`

`--json` prints one object. Its rows keep their types: the grid shows `2.5 KB`, the JSON `2549`.

```bash
$ icelens --demo --pane files demo.orders --json | jq '.rows[0] | {size, rows, seq}'
{ "size": 2549, "rows": 7, "seq": 5 }
```

| key | what it is |
|---|---|
| `schema_version` | `1`. Changes only when a key is removed or renamed, or a value's type changes |
| `kind` | the pane name, or `namespaces` / `tables` / `sql` / `explain` / `warehouses` / `catalogs` / `written` / `serve` |
| `note` | the line a person would see on stderr, including how a pane was truncated |
| `count`, `rows` | how many rows, and the rows |
| `columns` | on `data`, `sql` and `partitions`: each column's Arrow type |
| `table`, `snapshot`, `where` | on a pane: what was asked for. `snapshot` is absent for a table with none |

Values:

- Metadata timestamps are epoch milliseconds, byte counts are bytes, and a missing value is `null`.
- Dates, decimals, `NaN` and the infinities arrive as strings. `columns` gives the real type.
- `partitions` passes PyIceberg's table through, so its `last_updated_at` is a string.
- Overview and Layout give each fact its own key (`schemas`, `current_schema_id`).

Failures are JSON on stderr, with stdout empty:

```bash
$ icelens --pane overview core.nope --json
{"schema_version": 1, "error": {"kind": "IcelensError", "message": "cannot load core.nope: …"}}
```

- A usage mistake has `error.kind` `UsageError` and exit code `2`.
- `NeedsWarehouse` lists the catalog's warehouses in `error.warehouses`.
- A `--write` whose later statement fails lists what the earlier ones committed in
  `error.written`. Those commits stay.
- `icelens --explain <term> --json` returns the guide rail's whole entry for a term.

The keys of every kind are fixed by `tests/test_json_contract.py`.

## When something fails

- No DuckDB extension, or a catalog DuckDB cannot attach: the metadata panes work, rows come from
  PyIceberg, and the status bar says why.
- A table that will not load: the error is shown in the pane.
- A namespace that will not list: it still appears in the tree.
- An expired snapshot id: a message naming the snapshot.

For more, `--trace FILE` (or `ICELENS_TRACE=FILE`) records every HTTP request PyIceberg made,
every statement icelens ran in DuckDB, and each decision about the route, with timings:

```text
07:10:32.203 icelens.http        11.1 ms  POST http://127.0.0.1:18181/api/catalog/v1/oauth/tokens -> 200
07:10:32.212 icelens.http         8.2 ms  GET http://127.0.0.1:18181/api/catalog/v1/config?warehouse=lake -> 200
07:08:24.160 icelens.duckdb       2.9 ms  ATTACH IF NOT EXISTS 'lake' AS icelens (TYPE ICEBERG, ENDPOINT '…', TOKEN '***')
07:08:24.160 icelens.catalog  route: DuckDB attached the catalog; it reads rows and runs SQL
```

Secrets are masked, so the file can go in a bug report. It appends, and only you can read it.
Requests DuckDB's own extensions make are not in it; `CALL enable_logging('HTTP')` in the SQL
pane shows those.

[Platforms](PLATFORMS.md#when-something-goes-wrong) lists common errors and what to do.

## Limitations

- **Read-only on a real catalog.** `--allow-writes` is the only way past that. Sandboxes are
  writable.
- **Refs are shown, not edited.** No branch or tag management.
- **The Data pane's `WHERE`** is SQL when DuckDB reads the rows. When PyIceberg reads them the
  filter language is narrower: comparisons, `and`/`or`/`not`, `in`, `is null`, `starts with`.
- **On the scanning route** the SQL pane sees the tables that existed when it first ran; `r`
  reloads.
- **Without DuckDB's iceberg extension** (offline, or in the browser) SQL runs over copies of the
  tables, up to 5 million rows each, with no `AT (VERSION => …)`.
- **The in-browser demo** runs the demo catalog only; see
  [Platforms](PLATFORMS.md#in-the-browser).
- **Azure (ADLS):** not supported. PyIceberg 0.12 cannot read an `abfss://` metadata file, so the
  metadata panes fail too.
- **Windows:** local catalogs and sandboxes do not work yet, and nothing else has been run there.
- **A v3 sandbox under a path with a non-ASCII character** (`~/Leçons`) can be read and inserted
  into, but DuckDB 1.5.5 cannot `DELETE` or `UPDATE` there.
- **Untested platforms** are marked in [Platforms](PLATFORMS.md).

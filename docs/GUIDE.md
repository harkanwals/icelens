# The icelens guide

Everything icelens does, for people using it. Installing and a first look are in the
[README](../README.md); what works on which platform is in [Platforms](PLATFORMS.md).

## The guide rail

Iceberg's structure is only readable if you know its nouns, so the tool explains them while you
use it. A rail under every pane describes whatever the cursor is on - the pane, the column, the
row, or the value in the cell - and changes as you move:

```
Snapshots   #  │  committed at  │  operation  │  snapshot id  │  seq  │ …
                                   ^cursor
┌───────────────────────────────────────────────────────────────────────────────────┐
│ Operation  What kind of write made this snapshot: append, overwrite, delete or…    │
└───────────────────────────────────────────────────────────────────────────────────┘
```

Arriving at a pane names the pane first - "Manifest", not "File content" - because being told
what the column means presumes you already know what you are looking at. One keypress in any
direction and the rail is back to the cell, where it spends the rest of its life.

`g` cycles it: one line (the default), the full entry with where the thing lives on disk and
which terms to read next, then off. It is not a help window - it is part of the layout, and it
is about the cell in front of you rather than about Iceberg in general.

The same terms are available without the UI:

```bash
icelens --explain                      # every term, one line each
icelens --explain "manifest list"      # one in full
icelens --explain deletes              # anything matching
```

## Finding your catalog

No catalog at all is a fine place to start. `icelens` in a terminal with nothing configured asks
what to open rather than failing: a sandbox with a demo table in it (the default - enter and
you are looking at a table), the same at format-version 3, an empty sandbox, any sandbox you made
before, or how to connect to yours. Without a terminal - a script, a pipe - it says the same in one line and exits
1, or `"kind": "NoCatalog"` under `--json`.

Otherwise the connection is assembled from these, each overriding the ones below it:

1. Command line: `--uri`, `--warehouse`, `-p key=value` (any PyIceberg property, repeatable:
   `-p s3.endpoint=http://localhost:9000`).
2. `ICELENS_*` environment variables: `ICELENS_CATALOG_URI`, `ICELENS_WAREHOUSE`, `ICELENS_S3_ENDPOINT`, …
3. A profile named with `--catalog <name>` from `~/.pyiceberg.yaml`. Naming one is a decision,
   so a `.env` in the working directory is not read when you do.
4. Otherwise the nearest `.env`, searching upwards from the working directory and stopping at your
   home directory or at a directory you do not own. Keys are matched on their **suffix**, so a
   project's own names work unedited - `MYAPP_CATALOG_URI`, `ACME_S3_ACCESS_KEY` and
   `ICEBERG_WAREHOUSE` are all understood. A `.env` that another user owns or could change is
   refused, and icelens says so. `--no-env` turns the search off.
5. The `default` profile in `~/.pyiceberg.yaml`, whenever nothing above named a catalog.

`icelens --catalogs` lists every catalog in `~/.pyiceberg.yaml`, and `ctrl+o` switches between
them inside the app without a restart. A profile's properties are merged into the connection
rather than left to PyIceberg, because the catalog's *type* decides how its rows can be read.

One profile per catalog is what makes the tool worth installing globally - `icelens --catalog
lab` from any directory, and `ctrl+o` to step between a REST catalog, a Glue one and a local
SQL one without leaving the app:

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

Keep secrets out of it. A catalog that vends credentials needs none. One that wants a token or a
client secret can take it from PyIceberg's environment variables -
`PYICEBERG_CATALOG__LAB__CREDENTIAL=id:secret icelens --catalog lab` - or from a `.env` only you
can read. `-p token=…` works too, but a command line is visible to every user on the machine
(`ps`) for as long as the app runs.

A REST catalog that hosts several warehouses (Lakekeeper, Polaris) will refuse to answer without
being told which one. icelens asks the server for the list rather than handing back
"specify the warehouse parameter": one warehouse is selected automatically, several open a
picker. `icelens --warehouses` prints them, and `ctrl+w` switches between them in the app -
one address often holds a warehouse per project, per environment and per test run, and which
one you are in is the difference between every table and none.

Credentials are never printed: anything whose key looks like a secret, token, password or
credential - or an Azure account key, connection string or SAS token - is shown as `********`.
The one exception is `--serve`, whose whole job is to print
the token of the catalog it serves.

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

| key | pane | what it answers |
|---|---|---|
| 1 | Overview | what `metadata.json` says: format version, uuid, location, refs, properties, the last few metadata log entries |
| 2 | Schema | the viewed snapshot's schema, nested fields included, with **field ids** - the thing that actually identifies a column - plus identifier fields and partition transforms |
| 3 | Data | rows, through DuckDB when it can attach, PyIceberg otherwise; `[`/`]` for the limit, `w` for a WHERE, `l` for row lineage |
| 4 | Snapshots | every commit, oldest first: `◆` current, `│` an ancestor of current, `~` off the current chain (an orphan from a rollback or a branch), and on v3 the `first row id` each one was handed |
| 5 | Commit | what *one* snapshot changed - the files it added and the files it retired, read from the manifests that snapshot itself added |
| 6 | Manifests | the manifest list the snapshot points at, one row per manifest, with its partition ranges |
| 7 | Files | every live file at that snapshot: data, position deletes, equality deletes |
| 8 | Partitions | partitions with their file and row counts |
| 9 | Layout | the pointer chain, indented: table metadata → snapshot → manifest list → manifests → files |
| 0 | SQL | any DuckDB SQL over the attached catalog; tables are `icelens."<namespace>"."<table>"` |

An Iceberg **view** sits in the tree beside the tables, marked `view`. It has a definition
rather than data, so selecting one shows that in every pane: one row per version and SQL dialect,
newest first, with the engine that wrote it and the columns it returns. `icelens --pane overview
core.recent` prints the same; icelens does not run the view's SQL.

The panes that walk manifests - Commit, Manifests, Files, Layout - stop at a few thousand rows.
Every row below the manifest list is an object-store read, and a table with 50,000 files is one
where an unbounded pane means a frozen UI and a bill nobody agreed to. The cap is stated where it
bites (`showing the first 2,000 of 50,000 files`), and the *total* comes from the manifest list's
own counts, which costs one file read rather than all of them.

Time travel in SQL works the usual DuckDB way:

```sql
SELECT count(*) FROM icelens."demo"."orders" AT (VERSION => <snapshot-id>);
```

## Row lineage, on v3

Format version 3 gives every row a `_row_id` and a `_last_updated_sequence_number` that survive
file rewrites and compaction, so a reader can tell *this row moved* from *this row changed*. No
engine shows them by default - they are absent from `SELECT *`, because nothing stores them: the
reader computes each row's id from its data file's `first_row_id` plus its position in the file.

`l` names them, and the Data pane puts them first. Around that, the metadata half is always on
where it exists: Overview carries `next row id` (the next id the table will hand out, which is
also how many it has ever assigned - ids are never reused), Snapshots carries the `first row id`
each commit was allocated, and Schema lists the two columns marked `row lineage` and `reserved`,
because they are real but are not fields of the schema and have no field id.

On a v2 table `l` says so and changes nothing. Where the read route cannot compute the columns -
a PyIceberg scan, which has no way to ask for them - the rows still arrive and the engine is
named `pyiceberg (no row lineage here)`, because a column that silently vanishes teaches that
the table has no lineage.

This is *physical* lineage: how the warehouse stored a row, not what the business change was.
Type 2 dimension history answers the second question, and the two are not substitutes. Iceberg
has no foreign keys, so table-to-table relationships are not here - they are not in the format.

**Enter on a snapshot is the point of the tool.** It pins that snapshot, and every pane
re-reads at it: Schema shows the schema that snapshot was written with, Files and Layout show
what existed then, Data reads the rows as of then. `u` comes back to now.

Enter works on any cell that names a snapshot, not only the Snapshots pane: `snapshot id` and
`parent` there, and `added by` on Manifests and Files - so enter on a data file shows the table
exactly as the commit that wrote that file left it. The cell under the cursor wins, which is
what makes enter on a `parent` cell step back one commit. `,` and `.` walk the history one
snapshot at a time in the order the Snapshots pane lists them, off-chain snapshots included;
stepping onto the current snapshot unpins, so `.` off the end of history lands back on now.

## The sandbox: make Iceberg do it in front of you

Every other pane waits for a catalog to have done something interesting. A sandbox is the one
place this tool writes, so the interesting thing can be produced on demand:

```bash
icelens --sandbox                      # a local catalog, made on the spot; `e` opens the Writer
icelens --sandbox teaching             # another one, kept separately, still there tomorrow
icelens --sandbox teaching --sandbox-reset      # empty it and start again
icelens --demo                         # the sandbox called demo, with a table already in it
```

`--demo` writes one table, `demo.orders`, and gives it a history chosen so that every pane has
something true to say. It is the same five commits on every machine, written by PyIceberg alone,
so it works offline:

| commit | what happened | where to look |
|---|---|---|
| 1 | 120 orders, twelve of them test rows; tagged `first-load` | Overview's refs; enter on its id in Snapshots to see the table then |
| 2 | 30 late arrivals | Manifests: the list grew a second manifest |
| 3 | `DELETE` the test rows: an **overwrite**, `+108 / -120` | Commit: one file retired, one born - copy-on-write |
| - | add `channel`, rename `customer` to `customer_id` | Schema: the rename kept field id 4 |
| 4 | 20 orders that know their channel | Data: `channel` is null for the older rows |
| - | partition by `region` from here on | Partitions, Files: two specs, side by side |
| 5 | 25 orders, one file per region | Layout: the pointer chain, partitioned |

Add `--sandbox NAME` to put it in another sandbox, `--sandbox-reset` to start its history again.

The Writer pane takes three statements - `CREATE TABLE t AS SELECT …`, `INSERT INTO t SELECT …`,
`DELETE FROM t WHERE …` - and ctrl+r commits them. Under the editor is a ledger of what they
actually did, one row per commit:

```
 #  statement                                    operation  snapshot id           +rows  -rows  total rows  files
 1  CREATE TABLE demo.sales AS SELECT i AS id…   append     365503770289136103      100      0         100      1
 2  INSERT INTO demo.sales SELECT i, 'r0', 9.99  append     943875501117234734       10      0         110      2
 3  DELETE FROM demo.sales WHERE region = 'r0'   delete     5294193392015656713       0     10         100      1
 4  DELETE FROM demo.sales WHERE region = 'r0'   overwrite  3471267070479190902      75    100          75      1
```

Rows 3 and 4 are the whole reason the pane exists: one `DELETE` made **two** commits. Thirty-five
rows had to go: the ten-row file the `INSERT` wrote held nothing else, so it was dropped whole -
a `delete`; the other file held twenty-five of them among seventy-five keepers, so it was
rewritten without them and retired - an **overwrite** that *added* seventy-five rows. That is
copy-on-write, and reading about it is nothing like watching your own statement do it. Then
escape, `4`, and the Snapshots pane has four commits in it that you made. A statement that
commits nothing - a `DELETE` that matches no row - gets no row at all.

The split underneath is what makes this work against a catalog DuckDB cannot attach at all:
**DuckDB parses the statement and runs the query** - its whole vocabulary, `read_csv_auto`,
window functions, joins against another table - **and PyIceberg turns the Parquet it wrote into
a snapshot** with `add_files`. `INSERT` maps columns by position and casts them to the table's
types, as SQL has always meant it. A sandbox writes v2 tables unless it is made v3 - with
`--v3` or `--rest`, below.

A sandbox lives in `~/.local/share/icelens/sandboxes/<name>/` - a SQLite catalog and a directory
of Parquet, in a directory only you can read - or under `--sandbox-dir`, relative to where you
run it (a path with `#` or `?` in it is refused: it would cut a file URI short). Headless, `--sandbox NAME --write "<sql>"` prints the same ledger, one line per
commit, and `--json` makes it machine-readable.

### `--v3`: the same sandbox at format-version 3

Copy-on-write above is a v2 lesson. Deletion vectors and row lineage are v3, and no library here
writes a v3 table offline - PyIceberg's v3 serialiser is a stub, and DuckDB writing Iceberg to a
bare directory produces v2 while silently ignoring any version you ask it for. DuckDB writes v3
through a REST catalog it can attach, and nowhere else. So icelens runs one:

```bash
icelens --sandbox lesson --v3          # a v3 sandbox: deletion vectors, row lineage, UPDATE, MERGE
icelens --demo --v3                    # the demo table, told the v3 way
```

`--v3` serves the sandbox's own SQLite catalog over Iceberg REST from inside the process - bound to
`127.0.0.1`, behind a bearer token generated for the run, gone when icelens exits - and DuckDB
attaches it and writes. In the REST protocol the *catalog* writes `metadata.json`, so the v3 parts
(`next-row-id`, each snapshot's `first-row-id`) are written by `icelens/rest.py` following the spec,
not by a library that does not support them yet. Nothing to install beyond DuckDB's iceberg
extension, which does the writing, and nothing left listening.

The sandbox remembers it is v3, a `--sandbox-reset` keeps it so, and a v2 sandbox made v3 keeps
its tables: DuckDB writes v2 tables too, and a new `CREATE TABLE` is v3 unless it asks for
`WITH ("format-version" = 2)`. Tables still name their namespace (`demo.sales`), as offline. The
v3 demo, `demo.orders` in the sandbox `demo-v3`:

| commit | what happened | where to look |
|---|---|---|
| 1, 2 | 120 orders, then 30 more; the first tagged `first-load` | Snapshots: `first row id` 0, then 120 |
| 3 | `DELETE` the test rows: a **delete**, not an overwrite | Files: a Puffin deletion vector, and the data file untouched |
| 4 | `UPDATE` one order | Data, `l`: order 7 keeps `_row_id` 6 - an update is not a new row |
| - | `ALTER TABLE … ADD COLUMN channel` | Schema: schema 1 |
| 5 | 20 orders that know their channel | Data: `channel` null for the older rows |
| 6, 7 | one `MERGE`: a correction, then two new orders | Snapshots: one statement, **two** snapshots |

### `--serve`: the sandbox, for other engines

```bash
icelens --demo --v3 --serve      # or --sandbox NAME --serve; --port N and ICELENS_SERVE_TOKEN (16+ characters) fix them
```

serves the sandbox's catalog over Iceberg REST on 127.0.0.1 until ctrl+c, and prints what other
tools need to reach it: the URI, the warehouse and a bearer token, as a `~/.pyiceberg.yaml` entry
and as a DuckDB `ATTACH` to paste. With `--json` it prints one object instead, for a script to
read before it starts its own clients. PyIceberg, DuckDB (reading and writing) and another
icelens are tested against it; anything else that speaks Iceberg REST and reads local files
should work, and has not been tried.

Two limits, both deliberate. It listens on 127.0.0.1 and nowhere else, and the data files are
paths on this machine, so its clients run here too. And anyone here with the token can write to
the sandbox - which is what a sandbox is for - but nothing outside it: every location a request
names, created, moved or registered, has to be inside the sandbox's warehouse, and a purge never
deletes files another table uses.

### `--rest`: a sandbox in a catalog you run

The same writes can land in a real catalog instead - Lakekeeper, Polaris, anything DuckDB attaches:

```bash
icelens --sandbox lesson --rest --warehouse lab      # takes the usual connection settings
```

That puts the sandbox in a namespace of its own - `sandbox_lesson` - inside a catalog you are
already running, and **it cannot write anywhere else**: a bare `sales` means that namespace, and
`crm.accounts` is refused by name. The SQL pane stays read-only there too, because the Writer
pane is the confined one. DuckDB does the writing, so `UPDATE` and `MERGE` work here and not
offline, and every table is created at v3 unless you ask for v2 on purpose:

```
append     on sandbox_lesson.sales · +100 rows · snapshot 1698712542466490902 · duckdb through the catalog
delete     on sandbox_lesson.sales ·  -25 rows · snapshot 1977770655278908907 · duckdb through the catalog
delete     on sandbox_lesson.sales ·  -25 rows · snapshot    8524246523268566 · duckdb through the catalog
overwrite  on sandbox_lesson.sales · +25 rows · -25 rows · snapshot 319156682419751923 · duckdb through the catalog
```

The two deletes did not rewrite the data file: each left a Puffin **deletion vector** beside it,
which is what merge-on-read means and what the Files pane shows you afterwards. The `UPDATE` on
row four reports `+25 / -25`, because an update is a delete and an insert and this tool will not
pretend otherwise.

Those row counts are measured, not read off the snapshot: a deletion vector is rewritten whole
every commit, so Iceberg's own `added-position-deletes` reported 10, then 20, then 30 for three
deletes of ten rows, and `total-records` never moved. Watching the ledger disagree with the
summary is, on its own, worth the trip.

## Any catalog, the same tool

Metadata is never the hard part: PyIceberg speaks every dialect, so the metadata panes work the
same everywhere. Rows are the hard part, and icelens takes three routes to them, always saying in
the status bar which one it took:

| route | when | what you get |
|---|---|---|
| `attached` | REST, Glue, S3 Tables | DuckDB owns the catalog: it sees new tables and new snapshots by itself |
| `scanning metadata files` | Hive, SQL, DynamoDB, in-memory, or a REST catalog DuckDB refused | PyIceberg resolves each table to its `metadata.json` and DuckDB scans that file |
| `pyiceberg scans` | no DuckDB extension and no network to fetch one - or DuckDB could not reach that table's files | PyIceberg reads the rows itself |

The middle route is the one that matters. `icelens."<namespace>"."<table>"` resolves in the SQL
pane whichever route is in play, and time travel works in all three, so a Hive or Glue catalog is
not a second-class citizen with half the tool switched off. A nested namespace is one name to
DuckDB, dots and all: `icelens."core.nested"."deep"`.

When DuckDB is turned away, the tool says so instead of quietly taking another road. A catalog
that refuses the attach - usually over a login - is named in the status bar (`attach refused,
not authenticated: …`), and a table DuckDB cannot read comes from PyIceberg with the reason in the
note: `90 rows via pyiceberg (duckdb could not read it: HTTP 403 …)`.

Checked against DuckDB 1.5.5 rather than assumed: `ENDPOINT_TYPE` accepts `glue` and `s3_tables`
and names them when it refuses anything else; a Glue attach wants an account id, `catalog/catalog`
or `:`, not the `s3://` warehouse path a Glue catalog is usually configured with; and
`iceberg_scan('…/metadata.json', snapshot_from_id => N)` returns that snapshot with its deletes
applied. Object-store credentials configured for the catalog are handed to DuckDB as a secret, so
the scanning route works against S3-compatible storage and not only local files.

### Polaris, Snowflake and Databricks: OAuth and vended credentials

These catalogs log in with OAuth client credentials and hand out storage credentials per
table, so the connection names a login and nothing about the store:

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

The login is handed to DuckDB exactly as PyIceberg uses it, scope and token URL included,
because the two clients' defaults differ and both halves have to be the same principal. The
storage credentials a catalog vends reach DuckDB too, as a secret scoped to that one table's
location. A Polaris over a store with no STS refuses every table load that asks for
credentials; icelens asks once, then stops asking and uses the store keys you configured,
and the status bar says so. Without a warehouse, the picker lists Polaris's catalogs when the
login is allowed to (an administrator usually is), and asks for a name when it is not.

How sure to be: **Polaris** is tested, live, in `tests/test_polaris.py`. **Snowflake and
Databricks** are not - nothing here has met a real account. The recipes above come from their
documentation, the Azure-hosted versions of both need more than this does today, and
[PLATFORMS.md](PLATFORMS.md) lists what is known, what is guessed and what to watch.

### Cloud Storage, and Google's catalog

A `gs://` warehouse gets a `TYPE GCS` secret alongside the S3 one - `BEARER_TOKEN` for an OAuth
token (`gcs.oauth2.token`), or an HMAC `KEY_ID`/`SECRET` pair if you made one. Checked against
DuckDB 1.5.5: `TYPE GCS` takes those two and refuses `TOKEN` by name.

Google's Lakehouse runtime catalog needs no second credential, because the token that
authenticates the catalog is an ordinary Google access token and that is what Cloud Storage
wants too:

```bash
icelens --uri https://biglake.googleapis.com/iceberg/v1/restcatalog \
        --warehouse bl://projects/PROJECT_ID/catalogs/CATALOG_ID \
        -p token=$(gcloud auth print-access-token) \
        -p header.x-goog-user-project=PROJECT_ID
```

That reuse happens **only** when the catalog's host is `googleapis.com`, matched on the host and
never as a substring of the URI: a token for Lakekeeper is not a storage credential, and handing
it to an unrelated bucket would leak it. Everywhere else, set `gcs.oauth2.token` yourself or let
DuckDB's own chain find application default credentials.

Two things are worth knowing before you point it at BigLake. The token expires in about an hour
and is read once at connect, so a long session ends in 401s - `ctrl+o` back to the profile to
rebind. And `bl://` is not a warehouse string DuckDB's iceberg extension accepts, so the attach
is refused and rows come from the scanning route; the status bar says so.

## Without a terminal

Every pane prints, which makes icelens usable in a script, a pipe, or a teaching handout:

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

Exit codes: `0` fine, `1` something the catalog or the query refused, `2` bad usage (including
asking for the UI when stdout is not a terminal), `130` interrupted, `141` the reader closed the
pipe (`| head`) - quietly, with no traceback.

### `--sql` reads, and says so

`--sql` runs against the same attached DuckDB the panes read with, which means it could just as
easily run `DROP TABLE`. Two things stop it.

**The catalog is attached `READ_ONLY`** for every headless run against a real catalog, and
DuckDB enforces that in the engine: `CREATE`, `DELETE` and `EXPLAIN ANALYZE DELETE` are all refused on the attached database,
while `SELECT`, `DESCRIBE` and `EXPLAIN SELECT` work exactly as before.

**The SQL is checked first**, so the refusal is in English rather than DuckDB's words, and so
it also covers what a read-only attach does not - `COPY … TO '/tmp/x.csv'` writes a local file
without touching the catalog at all. Anything that is not a `SELECT` or an `EXPLAIN` is refused
(DuckDB types `DESCRIBE`, `SHOW`, `SUMMARIZE` and a CTE as `SELECT`, so those are fine). This
check applies in a sandbox too, which is attached writable: a sandbox is written with `--write`
or the Writer pane, and `--sql` stays a reader everywhere.

```bash
$ icelens --sql "SELECT 1; DROP TABLE demo.orders" --json
{"schema_version": 1, "error": {"kind": "IcelensError",
 "message": "refusing to run DROP: icelens reads. Pass --allow-writes to mean it."}}
```

`EXPLAIN ANALYZE` is the trap worth naming: DuckDB types it as `EXPLAIN`, and `ANALYZE` **runs**
the statement - `EXPLAIN ANALYZE DELETE FROM t` empties `t`. So every `EXPLAIN` has its tail
re-parsed, and one that does not explain a read is refused by name.

`--where` is a fragment, not a statement, and is held to that: `--where "false; SELECT 1"` makes
the query two statements and is refused. It filters the Data pane (`--pane data`) and nothing
else; it has no effect on `--sql`.

`--allow-writes` lifts both, attaching the catalog read-write. The SQL pane inside the app is
deliberately not guarded: a person typing into it is driving DuckDB, having meant to. A script
or an agent passing `--sql` is not.

### `--json`, for readers that are not people

`--json` prints **one object**, and the rows inside it keep their types. That is the difference
between it and the rendered table: the grid shows `2.5 KB` because that is what a person wants
to read, and the JSON says `2549` because that is what anything else wants.

```bash
$ icelens --demo --pane files demo.orders --json | jq '.rows[0] | {size, rows, seq}'
{ "size": 2549, "rows": 7, "seq": 5 }

$ icelens --demo --pane overview demo.orders --json | jq '.rows[0] | {schemas, current_schema_id}'
{ "schemas": 2, "current_schema_id": 1 }       # the pane shows "2 (current id 1)"
```

| key | what it is |
|---|---|
| `schema_version` | `1`. Bumped only when a key is removed or renamed, or a value's type changes |
| `kind` | the pane name, or `namespaces` / `tables` / `sql` / `explain` / `warehouses` / `catalogs` / `written` / `serve` |
| `note` | the line that goes to stderr for a person - **including how a pane was truncated** |
| `count`, `rows` | how many, and the rows themselves |
| `columns` | present only where the rows are an Arrow table passed through whole (`data`, `sql`, `partitions`): each column's Arrow type |
| `table`, `snapshot`, `where` | on a pane: what was asked for. `snapshot` is absent for a table with none |

Every key of every kind is written down in `tests/test_json_contract.py`, which fails if one is
renamed, removed or changes type.

Metadata timestamps are epoch milliseconds (one exception: `partitions` passes PyIceberg's own
table through, so its `last_updated_at` is a string and `columns` says so), byte counts are
bytes, counts are numbers, and a missing value is `null` rather than `"-"`. JSON has no date, no decimal and no `NaN`, so those arrive as
strings - `"NaN"`, `"Infinity"` and `"-Infinity"` included, because Python writes those three as
bare words that node, Go and serde all reject, and one of them would cost the caller the whole
document. It is also why `data` and `sql` publish `columns` -
a reader that gets `"73.34"` can look up
`decimal128(12, 2)` instead of guessing whether the column is text, and one that gets `"NaN"`
can see `double` rather than wondering. Panes whose display glues several facts into one sentence -
Overview and Layout - give each fact its own key instead.

Failures are JSON too, on stderr, with stdout left empty so a pipe is never fed half an answer:

```bash
$ icelens --pane overview core.nope --json
{"schema_version": 1, "error": {"kind": "IcelensError", "message": "cannot load core.nope: …"}}
```

A usage mistake - `--limit -5`, a pane that does not exist, `--pane` with no table - is JSON too
when `--json` is anywhere on the line, with `error.kind` `UsageError` and exit code `2`.
A `NeedsWarehouse` error carries the warehouses the catalog offers in `error.warehouses`. A
`--write` whose later statement fails carries what the earlier ones committed in
`error.written` - they stay committed, and a script should know - and
`icelens --explain <term> --json` returns the guide rail's whole entry - gloss, body, where it
lives in the spec, and what to read next - which is a local answer to "what is a manifest list"
that costs no network at all.

## Failure, on purpose

- No DuckDB extension, or a catalog it cannot attach: metadata panes work, rows come from
  PyIceberg, the status bar says why.
- A table that will not load: the error lands in the pane, not on the app.
- A namespace that will not list: it still appears in the tree.
- An expired snapshot id: a plain message naming the snapshot.

When the message is not enough, `--trace FILE` (or `ICELENS_TRACE=FILE`) writes down the
conversation behind it: every HTTP request PyIceberg made, the OAuth exchange and `/v1/config`
included, with its status and time; every statement icelens ran in DuckDB, with its time or its
error; and each decision about the route to the rows. It works for the app and for every headless
flag, appends, and is created readable by its owner only.

```text
07:10:32.203 icelens.http        11.1 ms  POST http://127.0.0.1:18181/api/catalog/v1/oauth/tokens -> 200
07:10:32.212 icelens.http         8.2 ms  GET http://127.0.0.1:18181/api/catalog/v1/config?warehouse=lake -> 200
07:08:24.160 icelens.duckdb       2.9 ms  ATTACH IF NOT EXISTS 'lake' AS icelens (TYPE ICEBERG, ENDPOINT '…', TOKEN '***')
07:08:24.160 icelens.catalog  route: DuckDB attached the catalog; it reads rows and runs SQL
```

It is meant to be pasted into a bug report, so no secret reaches it: the literals that carry a
credential in an `ATTACH` or `CREATE SECRET` are masked before anything else, and every line then
goes through the same scrubbing as an error message. DuckDB's own requests, made inside its
extensions, are not in it; `CALL enable_logging('HTTP')` in the SQL pane shows those.

## Limitations

- Read only against a real catalog: the connection is opened read-only and DuckDB enforces it.
  `--allow-writes` is the one way past that, and a sandbox (`--sandbox`, below) is the one place
  writing is on by default - it is a local catalog this tool made, holding nothing you can lose.
- The Data pane's `WHERE` is passed to the engine as written; a broken filter is an error in the
  pane. With DuckDB that is SQL. Without it - no extension, nothing it could read - the rows come
  from PyIceberg, whose filter language is narrower: comparisons, `and`/`or`/`not`, `in`,
  `is null`, `starts with`. A filter it cannot read says so, rather than failing as SQL would.
- Branch and tag *management* is out of scope; refs are shown, not edited.
- On the scanning route the SQL pane sees the tables that existed when it first ran; `r` reloads.
  Nested namespaces become one DuckDB schema there (`icelens."a.b"."t"`): DuckDB schemas do not
  nest.
- Without DuckDB's iceberg extension (offline, or in the browser) SQL runs over copies of the
  tables PyIceberg scanned: whole tables (up to 5 million rows each), refreshed when their snapshot
  moves, reads only, and no `AT (VERSION => …)` time travel. The status bar says "SQL over pyiceberg scans".
- The in-browser demo ([try it](https://harkanwals.github.io/icelens/try.html)) runs the demo
  catalog only, below the dependency floors above; what it needs and what was verified is in
  [PLATFORMS.md](PLATFORMS.md#in-the-browser).
- Some paths pass their unit tests and have never met the thing they are about - BigLake most of
  all. They are listed, with how to test each, in [UNVERIFIED.md](UNVERIFIED.md).
- Azure (ADLS): PyIceberg 0.12 cannot read an `abfss://` metadata file, so the metadata panes
  fail there, not only the rows ([PLATFORMS.md](PLATFORMS.md)).
- A v3 sandbox under a path with a non-ASCII character (`~/Leçons`) can be created, filled and
  read, but DuckDB 1.5.5 cannot `DELETE` or `UPDATE` there: it fails writing the deletion vector.
  Spaces are fine.
- Windows: local catalogs and sandboxes do not work yet - PyIceberg 0.12 cannot open a
  `file:///C:/…` path - and nothing else has been run there.

## What else exists

Checked before writing this, and worth knowing about:

- [**PyIceberg's own CLI**](https://py.iceberg.apache.org/cli/) (`pyiceberg list / describe /
  files`) - the closest thing to a baseline. Static prints of the *current* state; no history,
  no navigation, and `files` cannot be pointed at a past snapshot.
- [**iceberg-cli**](https://github.com/AlexMercedCoder/iceberg-cli) - PyIceberg plus catalog
  profiles and DataFusion querying. A CLI, aimed at administration rather than structure.
- [**apache-iceberg-ui**](https://github.com/AlexMercedCoder/apache-iceberg-ui) - a React web UI
  with a metadata viewer for schema, snapshots, files and manifests. The nearest thing in
  coverage; it wants Docker and a browser, and its views are per-table rather than pinned to one
  snapshot across every view.
- [**aws-tui**](https://github.com/thekaveh/aws-tui) - also Textual, also has Snapshots, History,
  Manifests, Files, Partitions and References views. Scoped to AWS: Glue catalogs only, no REST
  catalog, and it reads no rows - it prefills SQL into Athena instead of querying.
- [**Harlequin**](https://harlequin.sh/) - a terminal SQL IDE. Its DuckDB adapter can attach an
  Iceberg catalog, so you get SQL and a schema tree, but nothing about snapshots, manifests or
  what a commit changed.

Each is either tied to one catalog type or stops at metadata. What none of them do, and what this
is for: **one pinned snapshot that every view obeys**, a commit-level diff of the files a single
snapshot added and retired, the pointer chain rendered as a tree - and the same rows, the same SQL
and the same time travel **whatever the catalog is**, switchable with one keystroke, because
Iceberg's structure does not change when the catalog does and neither should the tool.

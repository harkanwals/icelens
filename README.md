# icelens

A terminal browser for Apache Iceberg catalogs. Point it at a catalog and walk what an Iceberg
table actually is - snapshots, the manifest list, manifests, data and delete files - and the rows
themselves. Pick a snapshot and every pane follows you there.

[Try it in your browser](https://harkanwals.github.io/icelens/try.html) first, if you like: the
real icelens on a demo catalog, nothing to install.

## Install

```bash
uv tool install icelens        # or: pipx install icelens
icelens --version
```

Python 3.10 or newer. Glue, S3 Tables, Hive and DynamoDB need PyIceberg's extra for each:
`uv tool install icelens --with 'pyiceberg[glue]'` (or `[hive]`, `[dynamodb]`).

Rows are read with DuckDB's iceberg extension, which icelens fetches on first use. Without a
network the metadata panes still work and rows come from PyIceberg; the status bar always says
which engine answered.

## Start

```bash
icelens --demo                 # a local catalog with one table and five commits to explore
icelens                        # the catalog this directory or ~/.pyiceberg.yaml is set up for
icelens demo.orders            # straight onto a table
icelens --uri http://localhost:8181/catalog --warehouse demo
```

With nothing configured, `icelens` asks what to open instead of failing.

## Connect to your catalog

icelens reads the same `~/.pyiceberg.yaml` PyIceberg does. One entry per catalog:

```yaml
catalog:
  lab:                                    # icelens --catalog lab
    type: rest
    uri: http://localhost:8181/catalog
    warehouse: analytics
  glue:
    type: glue
    warehouse: s3://my-bucket/warehouse   # credentials from the usual AWS chain
```

`icelens --catalogs` lists them and `ctrl+o` switches between them in the app. You can also pass
`--uri`, `--warehouse` and `-p key=value` (any PyIceberg property), set `ICELENS_*` variables, or
keep the settings in a `.env`. Keep secrets out of the YAML file: use PyIceberg's environment
variables or a `.env` only you can read. icelens never prints a credential.

A REST catalog with several warehouses (Lakekeeper, Polaris) gets a picker, and `ctrl+w` switches
warehouse. Recipes for Polaris, Snowflake, Databricks and BigLake are in the
[guide](https://harkanwals.github.io/icelens/docs/#polaris-snowflake-and-databricks-oauth-and-vended-credentials).

## What you see

| key | pane | what it answers |
|---|---|---|
| 1 | Overview | format version, location, refs, properties |
| 2 | Schema | the schema at the viewed snapshot, with field ids |
| 3 | Data | the rows; `w` filters them, `l` shows v3 row lineage |
| 4 | Snapshots | every commit; enter pins one and every pane re-reads at it |
| 5 | Commit | the files one snapshot added and retired |
| 6 | Manifests | the manifest list, one row per manifest |
| 7 | Files | every live data and delete file |
| 8 | Partitions | partitions with file and row counts |
| 9 | Layout | the chain from table metadata down to files |
| 0 | SQL | DuckDB SQL over the catalog: `icelens."<namespace>"."<table>"` |

`u` returns to the current snapshot, `,` and `.` step through history, `g` cycles the guide rail
that explains whatever the cursor is on, and `?` shows every key. Views appear in the tree beside
the tables.

icelens only reads a real catalog: it opens the connection read-only.

## A sandbox to write in

```bash
icelens --sandbox              # a local catalog you can write to; `e` opens the Writer pane
icelens --sandbox lesson --v3  # the same at format-version 3: deletion vectors, row lineage
icelens --demo --v3 --serve    # serve it over Iceberg REST for Spark, DuckDB or PyIceberg
```

Write `CREATE`, `INSERT` and `DELETE` statements and see exactly what each commit did to the
table. The [guide](https://harkanwals.github.io/icelens/docs/#the-sandbox-make-iceberg-do-it-in-front-of-you) walks through it.

## In scripts

Every pane prints, as a table, CSV or JSON:

```bash
icelens --list demo
icelens --pane snapshots demo.orders
icelens --pane files demo.orders --json
icelens --sql 'SELECT count(*) FROM icelens."demo"."orders"'
```

`--sql` refuses anything that writes. Exit codes are `0` fine, `1` refused by the catalog or the
query, and `2` bad usage. The `--json` format is documented in the
[guide](https://harkanwals.github.io/icelens/docs/#--json-for-readers-that-are-not-people).

## Where it works

Tested against Iceberg REST (Lakekeeper, Apache Polaris) and local SQL catalogs. Snowflake,
Databricks Unity, BigLake, Nessie, Glue, S3 Tables and Hive are supported in code but have not
been run against a real account. Linux and macOS only for now: local catalogs do not work on
Windows yet. [Platforms](https://harkanwals.github.io/icelens/docs/platforms.html) says what each needs and what has
been verified, and [Limitations](https://harkanwals.github.io/icelens/docs/#limitations) lists what does not work yet.

## More

- [The guide](https://harkanwals.github.io/icelens/docs/): every pane, key, flag and route in detail.
- [Platforms](https://harkanwals.github.io/icelens/docs/platforms.html): connecting to each catalog, and what to do when it fails.
- [Contributing](https://github.com/harkanwals/icelens/blob/main/CONTRIBUTING.md): how icelens is built and tested.

Apache-2.0.

# icelens

A terminal app for Apache Iceberg tables. It shows what a table is made of - its commits
(snapshots), the manifests and data and delete files each one points at, and the rows - as of any
commit you pick. Use it for two things:

- **Learn Iceberg.** `uvx icelens --demo` opens a local table with five commits, nothing to
  configure, and a guide beside every pane explains what the cursor is on.
- **Inspect your production tables.** Point it at your catalog to see why a table has the files it
  has, what a commit changed, and what the rows were before it. It only reads your catalog.

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
[guide](https://harkanwals.github.io/icelens/docs/#oauth-catalogs-polaris-snowflake-databricks).

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

Write `CREATE`, `INSERT` and `DELETE` statements and see what each commit did to the table. See
the [guide](https://harkanwals.github.io/icelens/docs/#the-sandbox).

## In scripts

Every pane prints, as a table, CSV or JSON:

```bash
icelens --list demo
icelens --pane snapshots demo.orders
icelens --pane files demo.orders --json
icelens --sql 'SELECT count(*) FROM icelens."demo"."orders"'
```

- These read the catalog you have configured. Add `--demo` to try them without one.
- `--sql` refuses anything that writes, and any table that is not the catalog's. It is still
  DuckDB, which can read local files and URLs, so run only SQL you have read. The
  [guide](https://harkanwals.github.io/icelens/docs/#--sql) says what is checked.
- Exit codes: `0` fine, `1` refused by the catalog or the query, `2` bad usage.
- The `--json` format is in the [guide](https://harkanwals.github.io/icelens/docs/#--json).

## With a coding agent

icelens ships an [Agent Skill](https://agentskills.io) that teaches a coding agent to drive it
through the headless flags. Install it where your agent looks for skills. For Claude Code:

```bash
mkdir -p ~/.claude/skills/icelens && icelens --skill > ~/.claude/skills/icelens/SKILL.md
```

`icelens --skill` prints the skill for the installed version, so rerun it after an upgrade.

## Where it works

Tested against Iceberg REST (Lakekeeper, Apache Polaris), Google BigLake and local SQL catalogs.
Snowflake, Databricks Unity, Nessie, Glue, S3 Tables and Hive are supported in code but have not
been run against a real account. Linux and macOS only for now: local catalogs do not work on
Windows yet. [Platforms](https://harkanwals.github.io/icelens/docs/platforms.html) says what each needs and what has
been verified, and [Limitations](https://harkanwals.github.io/icelens/docs/#limitations) lists what does not work yet.

## More

- [The guide](https://harkanwals.github.io/icelens/docs/): every pane, key and flag.
- [Platforms](https://harkanwals.github.io/icelens/docs/platforms.html): connecting to each catalog, and what to do when it fails.
- [Contributing](https://github.com/harkanwals/icelens/blob/main/CONTRIBUTING.md): how icelens is built and tested.

Apache-2.0.

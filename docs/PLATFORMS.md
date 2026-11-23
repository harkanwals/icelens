# Where icelens works

icelens reads any Iceberg catalog PyIceberg can reach. This page says which have been tested, how
to connect to each, and what to do when something goes wrong.

## At a glance

| Where your tables live | Status |
|---|---|
| A local SQL catalog, and icelens's own sandboxes | **Works.** Tested on every change. |
| Iceberg REST: Lakekeeper | **Works.** Tested live in CI. |
| Apache Polaris | **Works.** Tested live in CI. |
| Google BigLake (the Lakehouse runtime catalog) | **Works.** Run against a real Google Cloud project. |
| Snowflake (Open Catalog or Horizon) | **Should work.** Not yet run against a real account. |
| Databricks Unity Catalog | **Should work** on AWS and Google Cloud. Not yet run against a real workspace. |
| AWS Glue, S3 Tables, Hive, DynamoDB | **Should work** with PyIceberg's extra for each (see [Install](../README.md#install)). Untested. |
| Nessie | **Partly.** Run in Docker: add `-p header.X-Iceberg-Access-Delegation=` and your store keys, and rows and SQL work; the Files and Manifests panes fail. |
| Apache Gravitino | **Works** in Docker, with a `~/.pyiceberg.yaml` profile that sets `auth: {type: noop}` when the server has no login. |
| Anything on Azure storage (`abfss://`) | **Not yet.** The metadata panes fail as well as the rows. |
| Local catalogs on Windows | **Not yet.** PyIceberg cannot open a `file:///C:/…` path. |

"Should work" means the code follows the vendor's documentation and passes its tests, but has
not been run against the real service. If you try one, please open an issue saying what happened.

## Connecting

Each line is a complete command. Put the same settings in `~/.pyiceberg.yaml` to save typing them;
the guide's [Finding your catalog](GUIDE.md#finding-your-catalog) shows how.

### Apache Polaris, and Snowflake Open Catalog

```bash
icelens --uri https://HOST/api/catalog --warehouse CATALOG \
        -p credential=CLIENT_ID:CLIENT_SECRET -p scope=PRINCIPAL_ROLE:ALL
```

- The warehouse is the Polaris catalog's name. Leave it out and icelens lists the catalogs, if your
  login is allowed to see them (an administrator usually is).
- Your principal needs `TABLE_READ_DATA` (for example through `CATALOG_MANAGE_CONTENT`) to read rows.
  Without it, metadata opens and rows fail with a 403.
- On a Polaris without STS, icelens stops asking for per-table credentials after the first refusal
  and uses the storage keys you configured. The status bar says when that happens.

### Snowflake Horizon

```bash
icelens --uri https://ORG-ACCOUNT.snowflakecomputing.com/polaris/api/catalog \
        --warehouse DATABASE \
        -p credential=$SNOWFLAKE_PAT -p scope=session:role:ROLE
```

- Open Catalog and Horizon share a URL. What tells them apart is the login: Horizon takes a token
  with no client id, and its warehouse is a **database**.
- Schema names are usually UPPERCASE. icelens keeps their case.
- Catalogs synced from Snowflake into Open Catalog are two levels deep and read-only.

### Databricks Unity Catalog

```bash
# a personal access token
icelens --uri https://WORKSPACE/api/2.1/unity-catalog/iceberg-rest \
        --warehouse UC_CATALOG -p token=$DATABRICKS_PAT

# a service principal
icelens --uri https://WORKSPACE/api/2.1/unity-catalog/iceberg-rest \
        --warehouse UC_CATALOG -p credential=SP_ID:SP_SECRET \
        -p oauth2-server-uri=https://WORKSPACE/oidc/v1/token -p scope=all-apis
```

- The warehouse is a Unity Catalog **catalog**. Use the `iceberg-rest` path: the older `/iceberg`
  path is read-only, and newer workspaces reject it.
- To read rows, the metastore needs *External data access* turned on, and you need
  `EXTERNAL USE SCHEMA` on the schema.
- *Foreign* Iceberg tables come with no storage credentials, so their rows need keys of your own.

### Google BigLake

```bash
icelens --uri https://biglake.googleapis.com/iceberg/v1/restcatalog \
        --warehouse bl://projects/PROJECT_ID/catalogs/CATALOG_ID \
        -p token=$(gcloud auth print-access-token) \
        -p header.x-goog-user-project=PROJECT_ID
```

- The same Google token reads the Cloud Storage files, so no second credential is needed.
- The token lasts about an hour. When panes start failing, press `ctrl+o` and connect again.

## When something goes wrong

Start with the status bar: it says how rows are being read, and why.

| You see | What it means | What to do |
|---|---|---|
| `Please specify a warehouse` | The catalog serves more than one. | Pass `--warehouse NAME`, or pick from the list icelens offers. |
| Panes fail after about an hour | Your token expired. A fixed `token` is never refreshed. | Press `ctrl+o` and connect again. Logging in with `credential=CLIENT_ID:SECRET` refreshes by itself. |
| `attach refused, not authenticated` | DuckDB could not log in, though PyIceberg could. | Nothing: rows come another way, and the status bar names it. |
| Metadata opens, rows fail with 403 | Your login may see the table but not its files. | Ask for data access on the table (Polaris `TABLE_READ_DATA`, Databricks `EXTERNAL USE SCHEMA`). |
| `rows via pyiceberg (duckdb could not read it: …)` | DuckDB could not reach the files, or cannot read one of the table's column types (a `timestamptz_ns` column, which Trino writes). PyIceberg read them instead. | Nothing: the rows are right, just slower. |
| `SQL over pyiceberg scans` | DuckDB's iceberg extension is not installed, so SQL runs over copies of each table. Copies are refused above 5 million rows. | Run `python3 scripts/setup.py` to install it. |
| `catalog unreachable … ProxyError` from `--v3`, `--serve` or `--rest` on this machine | An HTTP proxy is set (`HTTP_PROXY`), and the catalog icelens runs on 127.0.0.1 is being asked for through it. | Set `NO_PROXY=127.0.0.1,localhost`. |
| An `abfss://` path in an error | Azure storage is not supported yet. | No workaround yet. |
| Rows fail with 403 on Nessie, though you gave store keys | Nessie hands out no credentials and DuckDB stops using yours. | Add `-p header.X-Iceberg-Access-Delegation=` (empty). |
| `Not implemented Error: Geography support` in the Data pane | DuckDB cannot read a table with a `geography` column yet. | The other panes work; there is no way to see its rows yet. |

## DuckDB

- **Tables DuckDB writes through a catalog** (`ATTACH … (TYPE ICEBERG)`, then `CREATE TABLE`):
  icelens reads them like any other, v3 included. A `variant` column shows as JSON text. A table
  with a `geography` column opens, but its rows do not: DuckDB cannot read that type yet.
- **A table opened by path**, as `iceberg_scan('s3://…/table')` reads one or
  `COPY … (FORMAT ICEBERG)` writes one: not yet. `icelens --uri <dir>` currently fails with a
  misleading "REST catalog" error.
- **DuckLake** is a different format and is not supported.

To try icelens with another engine, `icelens --demo --v3 --serve` starts a throwaway catalog and
prints what PyIceberg and DuckDB need to connect to it.

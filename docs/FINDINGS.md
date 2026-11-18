# Platform findings: the engineering record

How icelens was made to work against Polaris, Snowflake, Databricks, DuckDB and the browser, and
how each claim was checked. For what works where and how to connect, read
[PLATFORMS.md](PLATFORMS.md); this is the evidence behind it, for whoever changes the code.

It began as discovery, written before anything was built; the [Status](#status) table says what has been
fixed since, and each finding below is kept as it was found, because the why is the useful part.
Marks: what was run, what was read, what was argued.

| mark | meaning |
|---|---|
| ✅ | run against the real thing, on 2026-10-15 |
| ◐ | read in the vendor's docs or source, not run |
| ○ | argued only |

The ✅ rows come from one run: Apache Polaris 1.8.0 (`apache/polaris:latest`) in Docker,
RustFS as the object store with real STS, DuckDB 1.5.5 with iceberg extension `45163a28`, and
PyIceberg 0.12.0. The hermetic suite was green before anything was touched: 322 passed, 28 skipped.

---

## The short version

When this was written, metadata was fine everywhere PyIceberg could log in, and **rows were
not**, for one reason that covered Polaris, Snowflake Open Catalog and Databricks alike: those
catalogs authenticate with OAuth client credentials and hand out storage credentials per table,
and icelens passed neither to DuckDB. Both are fixed (#1, #2), and every row of this table is
green against Polaris 1.8.0 now; Snowflake and Databricks follow the same code but have not been
run against.

Before the fixes:

| against Polaris on S3, vended credentials | metadata panes (8) | Data | SQL |
|---|---|---|---|
| `-p credential=id:secret -p scope=PRINCIPAL_ROLE:ALL` (how these catalogs are configured) | ✅ | ❌ 403 | ❌ "table does not exist" |
| `-p token=<bearer>` | ✅ | ✅ | ✅ |

Same catalog, same tables, same tool: the only difference is which login icelens can translate
for DuckDB.

## Status

What happened to each finding below. "Tested" means a test that fails on the code before the
fix and passes after it. That was checked by running `tests/test_polaris.py` against `main`,
where 13 of its 14 tests fail.

| # | finding | status | tested in |
|---|---|---|---|
| 1 | OAuth never reaches DuckDB | **fixed**: `rest_login` | `test_polaris.py` (live), `test_routes.py` |
| 2 | scanning route has no vended credentials | **fixed**: per-table scoped secret | `test_the_scanning_route_gets_each_tables_vended_credentials` |
| 3 | no PyIceberg fallback | **fixed**: on I/O, HTTP, catalog and permission errors only | `test_pyiceberg_reads_what_duckdb_cannot_and_says_so`, `test_routes.py` |
| 4 | scanning-route failures invisible | **fixed**: status bar names a refused attach; SQL names unreadable tables | `test_a_refused_attach_is_named…`, `test_sql_names_the_tables_it_could_not_read` |
| 5 | nested namespaces | **fixed**: one schema name, `SUPPORT_NESTED_NAMESPACES` only where nesting was seen | `test_nested_namespaces_reach_duckdb`, `test_a_flat_catalog_is_attached_flat` |
| 6 | warehouse picker | **fixed**: Polaris catalogs, OAuth login, `/v1/config` at its real address | `test_the_warehouse_picker_lists_polaris_catalogs`, `test_routes.py` (fake server) |
| 7 | Azure | open: needs a decision on `adlfs`, and an Azure store to test against | - |
| 8 | vending refused | **fixed**: asks once, then stops asking on both halves | `test_a_catalog_that_vends_nothing_still_opens`, `test_sql_alone_recovers…` |
| 9 | tokens expire | partly: DuckDB now holds client credentials rather than a token; a static `token` still expires | - |
| 10 | a path called a REST catalog | open: belongs to opening tables by path | - |
| 11 | `ICELENS_TEST_CATALOG_URI` adopted as the catalog | **fixed**: `ICELENS_*` names are matched whole | `test_icelens_variables_are_matched_whole` |

\#11 was found while getting the live tests to run. `tests/live.py` documents
`ICELENS_TEST_CATALOG_URI` as the way to configure them for CI, and `discover` matched
`ICELENS_*` variables by suffix, so that setting became the catalog every hermetic test in
the run connected to.

The fixes were also run against **Lakekeeper 0.14.0** over RustFS, the stack the live tests were
written for. They were written against Lakekeeper 0.13.5. All 28 pass.

---

## The findings, in the order they bit

### 1. OAuth client credentials never reach DuckDB ✅

`_attach_sql` (`catalog.py`) sends `TOKEN` when there is one and `AUTHORIZATION_TYPE 'none'`
otherwise. A catalog configured with `credential=client_id:secret` - Polaris, Snowflake Open
Catalog, a Databricks service principal - therefore attaches with no auth at all, gets
`401 Unauthorized` from `/v1/config`, and falls back to scanning metadata files.

DuckDB 1.5.5 can do the OAuth exchange itself. Checked against the extension, not the docs:
`ATTACH … (TYPE ICEBERG, ENDPOINT …, CLIENT_ID …, CLIENT_SECRET …, OAUTH2_SERVER_URI …)` is
accepted, and so is `OAUTH2_SCOPE` in an `ICEBERG` secret. DuckDB attached Polaris this way and
wrote v3 tables through it (deletes, an update, `next-row-id` 101).

Fix: translate `credential`, `scope` and `oauth2-server-uri` (default `<uri>/v1/oauth/tokens`)
into those options. The scope has to go in a secret rather than on the `ATTACH`. Always pass
the scope and token URL explicitly, because the two clients' defaults differ: PyIceberg's scope
is `catalog`, DuckDB's is `PRINCIPAL_ROLE:ALL`. A credential with no colon is a bare secret
(Snowflake Horizon) and becomes `CLIENT_ID ''`.
Alternative: reuse the token PyIceberg already fetched. It works, but means reading PyIceberg's
private session state.

### 2. The scanning route has no storage credentials when a catalog vends them ✅

When the attach fails, `iceberg_scan('<metadata.json>')` runs with whatever `_create_secret`
(`catalog.py`) built from *static* properties. A vending catalog gives PyIceberg short-lived
keys in each table's load response (seen here: `s3.access-key-id`, `s3.secret-access-key`,
`s3.session-token`, `s3.endpoint`, `s3.path-style-access`, `s3.session-token-expires-at-ms`), and
DuckDB never sees them. Measured result: DuckDB aims at `lake.s3.us-east-1.amazonaws.com`, which
is not even the configured endpoint, and gets `403 InvalidAccessKeyId`.

Fix #1 removes most of this, because an attached DuckDB fetches vended credentials itself. This
route still matters for catalogs DuckDB will not attach. Fix: a per-table secret built from
`table.io.properties`, named after the table and scoped to its own two directories - its root
and its metadata's (`CREATE SECRET … SCOPE ['s3://…/t/', 's3://…/t/metadata/']`). Nothing wider
than a directory inside a bucket is ever handed over, so one table's keys never read another's.

### 3. A failed DuckDB read does not fall back to PyIceberg ✅

`Lake.rows` (`catalog.py`) uses PyIceberg only when DuckDB could not start at all. Once DuckDB
is up, a scan that fails is the pane's error. PyIceberg read those exact rows (90, with deletion
vectors applied) using the credentials it already held. Falling back on a storage or auth error,
and naming the route in the note, would have turned the 403 into a working pane.

### 4. Failures on the scanning route are invisible ✅

- The status bar hides `attach_error` whenever the mode is `bridge` (`app.py`). So "duckdb,
  scanning metadata files" never says that the attach failed on a 401, which is the fact the
  user needs.
- `_register_views` (`catalog.py`) skips any table it cannot scan, silently. The SQL pane
  then reports `Table with name items does not exist!` for a table the tree is showing.

### 5. Nested namespaces break the attach route ✅

Polaris nests namespaces; so does Nessie. `qualified("core.nested.deep")` (`catalog.py`)
produces `icelens."core"."nested"."deep"`, and DuckDB 1.5.5 cannot parse four-part names
(`Parser Error: NameListToString NOT IMPLEMENTED`). DuckDB lists nested schemas only when the
attach says `SUPPORT_NESTED_NAMESPACES true`, and then names them `"core.nested"`:
`icelens."core.nested"."deep"` read 3 rows. The scanning route already uses that spelling; the
attach route needs the option and the same spelling.

### 6. The warehouse picker only knows Lakekeeper ✅

Without a warehouse, Polaris answers `400 {"message": "Please specify a warehouse"}`.
`_needs_warehouse` recognises that, so `NeedsWarehouse` is raised correctly. The options list
comes back empty, though, and `--warehouses` says the catalog does not list them:

- `probe_warehouses` (`catalog.py`) asks Lakekeeper's `/management/v1/warehouse` (404 on
  Polaris). Polaris lists catalogs at `<base>/management/v1/catalogs`, the same `<base>` icelens
  already computes by trimming `/catalog`. It needs a bearer token, which with OAuth means doing
  the token exchange first, and the principal needs `service_admin` (a 403 should mean "ask for
  the name", not an error).
- The `/v1/config` fallback probes `<base>/v1/config` *after* trimming `/catalog`, which is not
  where the config endpoint is on Lakekeeper either. It only works by accident when the URI has
  no `/catalog` suffix.

### 7. Azure storage is not wired, for metadata or rows ◐

The Databricks and Snowflake sections below make Azure (`abfss://`) more likely than it has
been for this tool so far. Nothing here was run against Azure. The code paths were checked in
the installed libraries.

- **PyIceberg, so the metadata panes as well.** ✅ For `abfs[s]`/`wasb[s]`, PyIceberg 0.12 tries
  fsspec first. That route needs `adlfs`, which is not a dependency, so PyIceberg falls back to
  `PyArrowFileIO`. That reads only a bare `adls.sas-token` (`pyiceberg/io/pyarrow.py:569`), while
  vending catalogs key SAS tokens per account (`adls.sas-token.<account>.dfs.core.windows.net`).
  The fsspec route parses those from 0.11. Fix: `pyiceberg[adlfs]`, as an `icelens[azure]`
  extra or by default.
- **DuckDB, so rows.** icelens loads `iceberg`, `httpfs` and `aws`, but not `azure`
  (`catalog.py`), and `_create_secret` has no Azure case. Checked in 1.5.5: `TYPE AZURE`
  accepts `CONNECTION_STRING` and `PROVIDER ACCESS_TOKEN`, and refuses `SAS_TOKEN` by name, so a
  static SAS token has to go inside a connection string. An attached catalog's vended SAS tokens
  are parsed by the iceberg extension itself once `azure` is loaded, for the first storage
  account only. DuckDB's docs still list REST catalogs as S3 and GCS only.

### 8. Asking for vended credentials is not harmless ✅

`_rest_defaults` (`catalog.py`) adds `X-Iceberg-Access-Delegation: vended-credentials` on
the grounds that a catalog which does not vend "simply ignores" it. PyIceberg 0.12 sends it
anyway, by default (`pyiceberg/catalog/rest/__init__.py:881`), so icelens's copy changes
nothing. Either way, the claim is false for Polaris. A catalog configured with
`"stsUnavailable": true` - the usual way to run Polaris over an S3 store with no STS - answers
**every** table load with `400 Credential vending was requested for table core.t, but no
credentials are available`. Measured: icelens cannot open a single table there.

Sending the header as an empty string (`-p header.X-Iceberg-Access-Delegation=`) makes the load
succeed, with the user's own static keys. That covers PyIceberg only: DuckDB's attach still
requests vending unless it is given `ACCESS_DELEGATION_MODE 'none'`, which icelens never sends.
Fix: recognise that 400, and retry with delegation off on both halves, saying so in the
status bar. A flag to set it up front is a nice-to-have on top.

### 9. Long sessions outlive their token ◐

OAuth access tokens last about an hour on Polaris, Snowflake and Databricks alike.

- A static `token` is never refreshed, by PyIceberg or DuckDB.
- PyIceberg's legacy `credential` flow refreshes *reactively*: on a 401 or 419 it fetches a new
  token and retries once.
- DuckDB 1.5 refreshes when it holds client credentials, except with an empty `CLIENT_ID` -
  which is exactly Snowflake Horizon's shape (below). That bug is fixed only on DuckDB's main.

In the TUI that means panes that fail after an hour. Fix #1 makes most of this go away. For
the rest, re-attaching on a 401, as `ctrl+o` already does by hand, is the honest fallback;
`UNVERIFIED.md` #1 already predicts it for BigLake.

### 10. A path is reported as a REST catalog ✅

`icelens --uri /path/to/table` fails with `cannot open catalog 'icelens' (rest): Could not infer
the catalog type`. `catalog_type` (`catalog.py`) falls through to `"rest"` for any URI that
is not a known scheme. This is minor on its own, but see DuckDB below.

---

## Apache Polaris (self-hosted) ✅

Everything above was found against it, so its status is the list above. It now runs in CI on
every pull request (`tests/compose/stack.yml`, `make test-live`). The setup that first worked:

```bash
# application.properties
polaris.persistence.type=in-memory
polaris.realm-context.realms=POLARIS
polaris.readiness.ignore-severe-issues=true
polaris.features."ALLOW_INSECURE_STORAGE_TYPES"=true
polaris.features."SUPPORTED_CATALOG_STORAGE_TYPES"=["FILE","S3","GCS","AZURE"]

docker run --network host -e POLARIS_BOOTSTRAP_CREDENTIALS=POLARIS,root,s3cr3t \
  -e SMALLRYE_CONFIG_LOCATIONS=/polaris-conf/application.properties \
  -e AWS_ACCESS_KEY_ID=… -e AWS_SECRET_ACCESS_KEY=… -e AWS_REGION=us-east-1 \
  -v ./polaris-conf:/polaris-conf:ro apache/polaris:latest
```

Then: get a token from `POST /api/catalog/v1/oauth/tokens` (`client_credentials`, scope
`PRINCIPAL_ROLE:ALL`); create a catalog with `POST /api/management/v1/catalogs`; grant
`CATALOG_MANAGE_CONTENT` to its `catalog_admin` role. icelens connects with
`--uri http://host:8181/api/catalog --warehouse <catalog> -p credential=root:s3cr3t -p scope=PRINCIPAL_ROLE:ALL`.

Things that cost time and are worth not rediscovering:

- **S3 with RustFS needs `"kmsUnavailable": true`** in the catalog's `storageConfigInfo`. Polaris
  1.8 puts a KMS statement in the STS session policy, and RustFS refuses to parse it. The
  CREATE succeeds and every later table load is a 500, which looks like a Polaris bug and is not.
  RustFS does implement STS `AssumeRole`, so vending works once that flag is set.
- **MinIO is not used** (`minio/minio` is no longer on Docker Hub). RustFS is the S3 store the existing local stack uses anyway.
- **FILE storage needs the same path inside and outside the container**, and the directory has to
  be writable by the container's uid 10000. DuckDB writing to a local `file://` table location
  creates `data/` but not `metadata/`, so its first commit fails unless the directory exists. That
  only matters for local-file fixtures; object stores have no directories.
- Polaris accepted `CREATE TABLE … WITH ("format-version" = 3)` from DuckDB and stored a real v3
  table. This was the open question in `UNVERIFIED.md` #3, now answered for Polaris: v3 works.

---

## Snowflake ◐

Nothing here was run: it needs an account. Snowflake's docs could only be read through
search excerpts and quotes in GitHub issues. Its Iceberg REST catalog is Polaris, though, so
everything found against Polaris above applies to Snowflake too.

**There are two Snowflake catalogs, and they share a URL.** Both live at
`https://<org>-<account>.snowflakecomputing.com/polaris/api/catalog`:

| | Snowflake Open Catalog | Horizon Catalog (Iceberg REST) |
|---|---|---|
| what it is | managed Polaris, a separate account | built into an ordinary Snowflake account |
| status | closed to new customers; Snowflake points them at Horizon | reads GA, writes GA 2026-05 (v2 and v3) |
| `warehouse` | the catalog name | the **database** |
| namespaces | internal catalogs: any shape; synced external catalogs: `db.schema`, **two levels**, unless created with `CATALOG_SYNC_NAMESPACE_MODE=FLATTEN` | the schema, one level, usually UPPERCASE |
| login | `credential=<client_id>:<secret>`, `scope=PRINCIPAL_ROLE:<role>` or `PRINCIPAL_ROLE:ALL` | a PAT or key-pair JWT as the **secret with no client id**: `credential=<PAT>`, `scope=session:role:<ROLE>` |
| writable from outside | internal catalogs yes; external (synced) catalogs read-only | Snowflake-managed tables |
| list warehouses | the Polaris management API, admin principals only | no listing API found |

Most new Snowflake accounts use Horizon. icelens cannot tell the two apart by URI,
but it can by login: a `PRINCIPAL_ROLE:` scope with an `id:secret` pair is Open Catalog, and a
`session:role:` scope with a bare secret is Horizon. That matters for the error text, and for
what the README tells people to type.

What it means for the fixes above:

- **#1 has to handle a credential with no colon.** PyIceberg already does: a bare secret is sent
  with no `client_id` ✅ (`pyiceberg/catalog/rest/auth.py:100-102`). DuckDB needs it spelled
  `CLIENT_ID ''`, `CLIENT_SECRET '<PAT>'`, `OAUTH2_SCOPE 'session:role:R'`. In DuckDB 1.5 that
  empty `CLIENT_ID` also turns off token refresh (#9).
- **#5 matters for Open Catalog**: a catalog synced from Snowflake is two-level by default, so
  every table in it is a nested-namespace table.
- **#7 matters** wherever the Snowflake account is on Azure.
- **Uppercase identifiers ○.** icelens quotes every identifier, so `"CORE"."ITEMS"` keeps its case
  in DuckDB. Whether every pane survives a schema called `PUBLIC` next to one called `public`
  has not been tried.

**Polaris lessons that carry over:**

- A principal needs `TABLE_READ_DATA` (via `CATALOG_MANAGE_CONTENT` or finer grants) before a
  table load that asks for vended credentials is allowed. Without it the answer is a 403 that
  reads like "no access to the table", when the real cause is "no access to its data".
- A missing warehouse is a `400 Please specify a warehouse`, which icelens recognises ✅. An
  unknown or ungranted one is `404 Unable to find warehouse <name>` ✅, which it reports as a
  plain error; on Horizon that means a database name that is wrong or not granted.

**Only an account can settle:**

1. What Open Catalog and Horizon say for a missing or unknown warehouse.
2. Whether Horizon has any listing API.
3. Whether Horizon accepts an empty `client_id` sent as HTTP Basic, which is what DuckDB and
   PyIceberg's newer `auth:` manager do.
4. How long a Horizon token lives.
5. Whether DuckDB reads Azure-backed Snowflake tables through vended SAS tokens.
6. How tables with masking or row-access policies behave from outside Snowflake.

---

## Databricks Unity Catalog ◐

Nothing here was run: it needs a workspace. Databricks' own docs were read only through
search excerpts at the time, so treat every Databricks-side claim as ◐ at best. The
DuckDB and PyIceberg claims were read in their source, and the ones marked ✅ were checked
against the installed versions.

**Connecting.** The endpoint is `https://<workspace-host>/api/2.1/unity-catalog/iceberg-rest`
(read/write, DBR 16.4+). `warehouse` is the **Unity Catalog catalog name**, so one connection
is one UC catalog, and namespaces are single-level UC schemas. The older
`/api/2.1/unity-catalog/iceberg` path is read-only and UniForm-only, and newer workspaces reject it.
A workspace URL without the workspace id can answer with a `303` to a login page. That would look
like a broken catalog, and is worth naming in an error.

```bash
icelens --uri https://<workspace>/api/2.1/unity-catalog/iceberg-rest --warehouse <uc_catalog> \
        -p token=<PAT>                                       # works today, if rows can be reached
icelens … -p credential=<client_id>:<secret> \
          -p oauth2-server-uri=https://<workspace>/oidc/v1/token -p scope=all-apis   # service principal
```

**Auth.** A personal access token is `token`, which icelens already passes to DuckDB. A
service principal is OAuth M2M against `https://<workspace>/oidc/v1/token` with scope
`all-apis`: finding #1 again, and with a twist. **The two clients' defaults differ.** PyIceberg's
legacy OAuth defaults the scope to `catalog` ✅ (`pyiceberg/catalog/rest/__init__.py:689`).
DuckDB defaults it to `PRINCIPAL_ROLE:ALL` and the token URL to `<endpoint>/v1/oauth/tokens`,
which on Databricks is the wrong host path entirely. So the fix for #1 must pass the scope and
token URL explicitly, even when the user did not set them, or the two halves log in differently.

**What is exposed.** Managed Iceberg tables are read/write. UniForm (Delta with Iceberg reads)
tables are read-only. *Foreign* Iceberg tables are read-only **and get no vended credentials**,
so their rows need storage credentials of the user's own. Views are not exposed. v3 is in
Public Preview on DBR 18+.

**Storage credentials** need the metastore's *External data access* setting and
`EXTERNAL USE SCHEMA` on the schema. Vended keys arrive in the load-table `config`, by cloud:
`s3.access-key-id` / `s3.secret-access-key` / `s3.session-token` on AWS,
`gcs.oauth2.token` on GCP, and on Azure **`adls.sas-token.<account>.dfs.core.windows.net`**, keyed
by host. That last one is where it gets hard:

- **PyIceberg cannot read it with icelens's current dependencies.** ✅ For `abfss://` PyIceberg
  tries fsspec first (`adlfs`, not installed), then PyArrow, which reads only a bare
  `adls.sas-token` (`pyiceberg/io/pyarrow.py:569`) and ignores the per-host key. On Azure that
  breaks the **metadata panes too**, not just rows. The fsspec route parses per-host SAS keys from
  PyIceberg 0.11. Fix: `pyiceberg[adlfs]`, as an `icelens[azure]` extra or by default.
- **DuckDB** parses vended SAS tokens only with the `azure` extension loaded, which icelens does
  not load (#7), and uses only the first storage account.

**DuckDB support is unofficial.** DuckDB's docs have no Databricks page and list REST support as
S3, S3 Tables and GCS. Users report `ATTACH '<catalog>' (TYPE ICEBERG, SECRET s, ENDPOINT …)`
working with a PAT, and the Databricks-specific bugs land release by release: the catalog prefix
was URL-encoded wrongly until 1.5.0, an Azure endpoint-suffix fix is in 1.5.6, and a
credential-scope bug for metadata under `__metadata/` / `_external_metadata/` is fixed on `main`
only. **Use DuckDB 1.5.0 or later against Databricks, 1.5.6 for Azure.** icelens's own floor
(`pyproject.toml`) is the version the suite runs on. The PyIceberg fallback from #3 is what makes
the tool usable while DuckDB catches up.

**Only a workspace can settle:**

1. Which URL form avoids the 303 on your host.
2. The exact vended keys per cloud.
3. Whether DuckDB's Azure route works at all.
4. Whether DuckDB's credential scope covers data files for UniForm and v3 tables.
5. What a foreign table needs.
6. Whether v3 tables read correctly.
7. How a session past the token's hour behaves.
8. Whether row filters and column masks (which force server-side scan planning) return rows.
   PyIceberg 0.12 has a reported bug returning zero rows there.

---

## DuckDB Iceberg

DuckDB support can mean several things. They are ranked here by how much each would
change for a user, and the first is the only one that is a real gap.

### Reading 1: open a table by path, the way `iceberg_scan` does ✅ gap confirmed

DuckDB reads Iceberg tables with no catalog: `iceberg_scan('s3://bucket/path/table')` or
`iceberg_scan('…/metadata.json')`. Since 1.5.2, DuckDB also *writes* them:
`COPY (…) TO 'dir' (FORMAT ICEBERG)`. icelens cannot open either, because PyIceberg's
`load_catalog` is its only entry point.

Checked here with DuckDB 1.5.5:

- `COPY … TO dir (FORMAT ICEBERG)` writes `data/`, manifests, `<uuid>.metadata.json` and a
  `version-hint.text` holding that uuid with no newline. It is v2 every time:
  `"format-version" 3` and `FORMAT_VERSION 3` are both silently ignored, as `SANDBOX.md` found.
  DuckDB's main branch has a `format-version` option for this; 1.5.5 does not.
- PyIceberg 0.12 `StaticTable.from_metadata(<dir>)` opens that directory as-is (it reads the
  hint) and scans its 20 rows. So does `iceberg_scan(<dir>)`.
- `icelens --uri <dir>` fails, misreported as a REST catalog (#10 above).

What building it would take ○: a `Target` whose "catalog" is one static table (or a directory
walked for tables), metadata from `StaticTable`, rows from `iceberg_scan` with no ATTACH, and
the same credentials passed to both halves. It would be read-only by construction, and time travel
works there already (`snapshot_from_id`). A directory with no `version-hint.text` needs DuckDB's
`unsafe_enable_version_guessing` behaviour and should say so. **Moderate**; most of the bridge
route is already this.

### Reading 2: tables DuckDB writes through a catalog ✅ already works

DuckDB 1.4+ writes through REST catalogs, v3 from 1.5.1 (deletion vectors, row lineage),
`MERGE` from 1.5.3. icelens already reads those, and its `--sandbox --rest` writes them.
Polaris added to that list today. What is left is passing through the DuckDB-only attach options
that some catalogs need: `SUPPORT_NESTED_NAMESPACES`, `EXTRA_HTTP_HEADERS` (BigLake's
`x-goog-user-project` currently reaches PyIceberg and not DuckDB), `ACCESS_DELEGATION_MODE`.

### Reading 3: DuckLake ◐ not Iceberg

DuckLake keeps its metadata in a SQL database and exposes no `metadata.json` and no Iceberg
REST endpoint. `iceberg_to_ducklake()` copies *from* Iceberg into DuckLake (present in 1.5.5).
Browsing DuckLake would mean a second metadata backend. Out of scope.

### Reading 4: a local catalog to pair with DuckDB ◐

DuckDB's own test suite runs against `apache/iceberg-rest-fixture`, Lakekeeper, Polaris, Nessie
and Gravitino. Documenting one, or shipping a compose file, is cheap. Polaris is now proven here.

---

### Reading 5: the v3 features each writer produces ✅

Run 2026-11-13 with Spark 4.1.1 + Iceberg 1.12.0, DuckDB 1.5.5 and 2.0.0, and PyIceberg 0.12.0,
all through `--serve`; every pane read every table. Deletion vectors, row lineage, default values,
nanosecond timestamps and v3 partitioning work. Tables with `variant` or `geometry`/`geography`
(Spark, DuckDB) did not open at all: PyIceberg 0.12 cannot parse `variant`, and accepts only a
quoted `geometry('…')` where the spec and both writers use `geometry(OGC:CRS84)`. `icelens.opaque`
now keeps those types as the text they were written in, and serialises them back unchanged, so
every metadata pane opens them. Rows: DuckDB 1.5.5 reads a variant but cannot export it to Arrow,
so it is read as JSON text; it cannot read a table with a `geography` column at all (measured on
Spark 4.1's), which stays a gap. Also fixed: a nanosecond-timestamp traceback, DVs labelled
"position deletes", and "-0 rows" on every DV commit. Details: `docs/research/iceberg-features.md`.

Views (2026-11-13, Spark 4.1.1 + Iceberg 1.12.0 against `--serve`): `CREATE VIEW`, `CREATE OR
REPLACE VIEW`, `ALTER VIEW … SET TBLPROPERTIES`, `SHOW VIEWS`, `ALTER VIEW … RENAME` and a
`SELECT` from the view all ran; icelens lists the view and shows both versions. Before, the
served catalog had no view endpoints and Spark failed with "Server does not support endpoint".

Trino 484 (2026-11-13, Docker `trinodb/trino:484`, REST catalog against `--serve`, local files
via `fs.native-local.enabled`): v3 CTAS, `DELETE` and `UPDATE` (deletion vectors), `TIMESTAMP(9)`
with and without time zone, `VARIANT`, `month(d)` partitioning and `CREATE VIEW` all ran, and
every pane read every table. Two things showed up. DuckDB 1.5.5 cannot read a table with a
`timestamptz_ns` column at all - "Unrecognized primitive type", even for `SELECT id` - so the
Data pane now falls back to PyIceberg, which reads all nine digits. And Trino's `delete` snapshot
summary leaves `total-records` at 4 after removing a row, so the Snapshots pane's totals, which
are the summary's, say 4; the Commit pane, which reads the deletion vector, says -1.

## The browser (Pyodide) ✅ demo catalog

The site's home page, and `try.html` full screen, run icelens itself in the visitor's browser
(`site/demo.js`). Loading starts with the page on a screen at least 760 px wide; on a narrower
one, or where the browser reports Data Saver or a 2G/3G link, the visitor gets the preview and a
button instead of an unasked-for 29 MB. Pyodide 314.0.7 in a Web
Worker, the TUI drawn by xterm.js through `src/icelens/browser.py`, on the `--demo` catalog in
Pyodide's in-memory filesystem. Run on 2026-11-13, two ways: the built wheel under Pyodide in
Node (`make test-browser`, CI's `browser` job: 27 checks - the shims, `--list`, three panes,
`--sql` including a `USING` join, `--json`, the TUI drawing, a key, a resize, quitting, and that the
first frame needed neither pandas, SQLAlchemy nor DuckDB), and
`try.html` served locally and driven in headless Chrome over CDP (boots, draws the demo table,
`4` opens Snapshots, the SQL pane's default query answers 20 rows).

What Pyodide gives icelens is not what the other jobs test:

| | CPython (pyproject floors) | Pyodide 314.0.7 |
|---|---|---|
| PyIceberg | >= 0.12 | 0.11.1 |
| DuckDB | >= 1.5.5 | 1.5.1 |
| DuckDB's iceberg, avro, httpfs extensions | installed | none: extensions.duckdb.org publishes no `wasm_eh_pyodide` build (404) |
| threads | yes | `Thread.start()` raises |

So the wheel is installed with `deps=False`, below its own floors, and only the `browser` job
says that works. What had to change, each found by running it:

- **No iceberg extension**, so rows come from PyIceberg and SQL takes the `arrow` route: the
  tables a query names are scanned by PyIceberg and copied into a plain DuckDB
  (`Lake.sql_route`). It copies whole tables, which is fine for a demo and slow for a large one.
- **No threads.** PyIceberg's executor is swapped for a synchronous one (`install_shims`);
  Textual's thread workers run inline in the worker, so a slow scan blocks the worker, not the page.
- **`pyarrow._s3fs` is missing** from Pyodide's PyArrow and `pyiceberg.io.pyarrow` imports from
  it at module level; a stub stands in. Nothing in the demo reaches S3.
- **Pyodide's event loop calls `asyncio.tasks._set_task_name`**, which Python 3.14 removed;
  Textual names its tasks, so the app crashed on start until it was put back.
- **SQLAlchemy is not loaded**: the page's demo catalog is `icelens.memory`; `--demo` on the
  command line under Pyodide is still SQLite, and CI's check loads SQLAlchemy for that part.
- **Pyodide 314 is ES modules**: its `pyodide.js` fails under `importScripts` in Chrome, so the
  worker is a module worker and imports `pyodide.mjs`.

### How long it takes

Measured in headless Chrome over CDP against the site served locally (the probe reads the timing
marks `worker.js` posts), on 2026-11-13. Network throttling does not reach a worker this way, so
these are fast-connection numbers; on a slow one add the download at its speed.

| | first visit | return visit | downloaded before the first frame |
|---|---|---|---|
| v1: micropip, SQLite catalog, everything up front, on a click | 11.0 s after the click | 10.9 s | 39.6 MB |
| first speed pass | screen at 0.1 s, live at 6.5 s from opening the page | live at 5.2 s | 24.4 MB |
| now: compiled modules, prebuilt demo | screen at 0.1 s, live at 4.6 s | live at 3.7 s | 28.9 MB |

What changed, each measured before it was kept:

- **Nothing that the first frame does not import is downloaded first.** PyArrow's entry in
  Pyodide's lockfile lists pandas (4.1 MB, with pytz), which nothing here imports; `site/loader.mjs`
  edits it out. DuckDB (8.5 MB) answers only SQL and loads after the first frame.
- **No SQLAlchemy.** The demo's catalog is `icelens.memory`, a dict, not SQLite: importing
  SQLAlchemy was 0.8 s of the wait (`-X importtime` under Pyodide) and 1.9 MB.
- **One download, no PyPI round trips.** Textual and icelens are lockfile entries
  (`wheels/latest.json`, URLs and hashes from `uv.lock`), fetched with Pyodide's own packages
  instead of resolved one by one by micropip.
- **Loading starts with the page.** The worker is created in `<head>` and boots without waiting
  for a click, xterm.js, or a terminal size.
- **The first screen is shown at once.** `first-frame.json` is drawn dimmed while Python loads;
  keys typed meanwhile are kept and sent when the app is live.
- **Python is compiled at build time, not in the tab.** Pyodide's packages and standard library
  ship source and no bytecode, so the ~580 modules imported before the first frame were compiled
  in every visitor's tab: 1.9 s in Node, measured by compiling each one's source. The build
  (`tests/pyodide/prebuild.mjs`, run by `make site`) has Pyodide's own Python pack their code into
  `bytecode/<hash>.xz` (4.4 MB; `icelens.bytecode`), which the worker installs as an import hook
  before anything else is imported: imports 1.67 s to 0.43 s, seeding 1.01 s to 0.37 s, in Node.
  A module the bundle lacks, or whose file is not the one it was packed from, compiles from
  source as before, and CI's check fails if any module in the bundle went unused. The worker
  skips it where the browser reports a 2G/3G link or Data Saver: below roughly 20 Mbps the 4.4 MB
  costs more than the 1.8 s it saves, on a first visit only.
- **The demo is written at build time.** Its five commits are written once by the build, at the
  paths the page uses (`demo/<hash>.tar.xz`, 15 KB; `browser.pack_demo`/`restore_demo`), so the
  page unpacks them rather than writing them, and the preview is captured from the same files:
  it is the live app's first screen, ids and all.

Tried and dropped, or not tried, and why:

- **A Pyodide memory snapshot** (start from a saved heap with everything imported): the private
  `makeMemorySnapshot` refuses once any package is loaded ("Unexpected hiwire entry at index 6"),
  tried twice, the second time with a serializer that never got called.
- **zlib instead of xz for the bundle**: 0.06 s to unpack against 0.3 s, but 6.3 MB against 4.4;
  the bytes cost more than the CPU below about 60 Mbps.
- **Prefetching wheels during runtime start-up**: no difference that could be measured here.
- **A static site generator (Astro and the like)**: the pages already show at once (the
  preview is drawn 0.1 s after opening); every second of the wait is Python, which a generator
  does not touch.
- **Other ways to run Python in WebAssembly** (MicroPython, CPython's own WASI and Emscripten
  builds, PyScript): PyScript runs on Pyodide; the others have no PyArrow, NumPy or Pydantic,
  which icelens cannot do without. Pyodide is the one distribution that has them.

What is left is fixed cost, here: starting the runtime (1.0 s), unpacking and compiling the
WebAssembly of PyArrow and NumPy (1.2 s), running the imported modules' top-level code (Pydantic
models and Textual classes being built, about 0.9 s), and the first frame (0.2 s).

Not verified: Firefox and Safari, a phone, load time on a slow connection (about 29 MB before the
first frame, 8.5 MB more for SQL), whether the 2G/3G check skips the compiled modules where it should, mouse input, and any catalog but the demo. Writing in the browser's sandbox needs DuckDB's
extensions and so does not work; it fails in words.

## Publication, separate from platforms

Found while doing the above; none of it is about catalogs.

Done since: icelens is licensed Apache-2.0 and published on PyPI as `icelens`; the README's test counts and "no network" claim were corrected (tests that need
DuckDB's extensions skip where they cannot load, and CI proves it both ways); CI is the
repository's own; and `scripts/setup.py --extensions-from-pypi` installs the official, DuckDB-
signed extension binaries from third-party PyPI wheels where `extensions.duckdb.org` is
unreachable, refusing anything not signed with DuckDB's core key.

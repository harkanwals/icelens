# What has not been tested, and how to test it

Everything here passes its unit tests and none of it has met the thing it is about. The list
exists because the alternative is remembering, and because a reader who finds a bug in one of
these deserves to know it was a known gap rather than a claim.

What backs the code today: `uv run pytest` is hermetic - SQLite catalogs, local Parquet and
icelens's own REST catalog, so the attach-mode tests run everywhere - and skips the Polaris tests
and the ones that build and install the package. `make test-live` starts Lakekeeper and Polaris
in Docker and runs everything against them, and CI does the same on every pull request.

What none of that can reach, ranked by how likely it is to bite.

## 1. BigLake, end to end (blocked on a Google project)

The Cloud Storage work is unit-tested and its DuckDB syntax is verified against 1.5.5, but no
Google catalog has ever answered it. The command to try:

```bash
icelens --uri https://biglake.googleapis.com/iceberg/v1/restcatalog \
        --warehouse bl://projects/PROJECT_ID/catalogs/CATALOG_ID \
        -p token=$(gcloud auth print-access-token) \
        -p header.x-goog-user-project=PROJECT_ID
```

Four things to watch, in the order they would fail:

1. Does the catalog answer at all - namespaces and tables in the tree.
2. Do rows arrive. `bl://` is not a warehouse string DuckDB's iceberg extension accepts, so
   the attach should be refused and the status bar should say the rows came from the scanning
   route. If instead they come back as a 403, the `TYPE GCS` secret is not reaching the scan.
3. Does the token reuse work - that 403 is what it is there to prevent.
4. What an expired token looks like. It lasts about an hour and is read once at connect, so a
   long session ends in 401s; `ctrl+o` back to the profile should rebind it. Worth seeing how
   ugly it gets before then.

## 2. A `gs://` warehouse that is not Google's catalog

HMAC keys (`gcs.access-key-id` / `gcs.secret-access-key`) and the no-credentials case both
have unit tests and no bucket. The no-credentials case is the interesting one: icelens makes
no secret at all so that DuckDB's own chain can find application default credentials, and
whether it does has never been watched.

## 3. `--rest` against a catalog that is not Lakekeeper or Polaris

The v3 sandbox writes `CREATE TABLE ... WITH ("format-version" = 3)` and then reads the
version back off the table, so a server that ignores the clause should produce an error naming
the version it got rather than a quiet v2 table. It has run against Lakekeeper 0.13.5 and 0.14.0
and, by hand, against Polaris 1.8.0: a v3 create, a delete as a deletion vector, an update as an
overwrite, the namespace confinement refusing `DROP TABLE core.items`, and `--sandbox-reset`.
Nessie, Unity, Snowflake and S3 Tables are untried, and a server that refuses v3 outright is the
case most likely to read badly.

## 4. `--rest` on Glue or S3 Tables

DuckDB attaches both, so the sandbox has no reason not to work there, but `rest_sandbox_target`
has only been given REST catalogs. The namespace-creation step goes through PyIceberg, which
spells namespaces differently on Glue.

## 5. The ledger's row counts on a partitioned table

Counts are measured before and after each statement, and deletions are derived from what the
count did. Every test so far writes one unpartitioned table with one data file. A partitioned
overwrite that rewrites several files at once is the case where a derived number could be
wrong and still look plausible - which is the worst kind.

Half of this is now tested. `tests/test_ledger.py` deletes from a partitioned table in the
*offline* sandbox, touching one, two and three partition files, and checks that `+rows` minus
`-rows`, summed over the commits one statement made, is exactly how far the row count moved; it also feeds the attached side's derivation the
counts of a commit whose answer is known. The attached side itself - counted rows, merge-on-read,
several deletion vectors in one commit - has still only met one unpartitioned table.

## 6. A fresh install on another machine

`uvx --from <checkout> icelens --sandbox x --write "..."` was run once, in a clean
environment, to prove the `sql-sqlite` dependency is no longer missing from a real install.
It has not been run anywhere else, and packaging bugs are per-platform.

`tests/test_packaging.py` now does this properly - builds the sdist and the wheel, installs the
wheel into a fresh venv, runs `--version`, `--explain`, a sandbox write and the app - and CI runs
it on Linux, macOS and Windows. It stays on this list until those CI runs have been seen green:
Windows has never run any of this, and is allowed to fail there until it has.

Worth knowing while testing: `uvx --from <directory>` served a stale build of this repo for an
hour, surviving both `--refresh` and `uv cache clean icelens`, because the version string had
not changed. `uv run --project <directory> icelens` does not have that problem.

## 7. Snowflake, either catalog (blocked on an account)

Horizon and Open Catalog are Polaris underneath, so everything `tests/test_polaris.py` checks
should carry over - but Horizon logs in differently, and nothing here has met either. In order:

1. Does the login work: `-p credential=$PAT -p scope=session:role:ROLE` for Horizon, which sends
   the PAT as a secret with no client id, to PyIceberg and to DuckDB alike.
2. Does DuckDB attach, or is it refused - the status bar now says which, and why.
3. Do rows arrive on an Azure-hosted account. Probably not today: see 9.
4. Open Catalog's synced external catalogs are two levels deep (`db.schema`), so every table
   there is a nested-namespace table. The nested route is tested against Polaris, not there.
5. Uppercase identifiers. icelens quotes every name, so case should survive, but no pane has
   been read against a schema called `PUBLIC`.

## 8. Databricks Unity Catalog (blocked on a workspace)

1. Which URL form avoids the 303 to a login page on your workspace.
2. A service principal: `oauth2-server-uri=https://WORKSPACE/oidc/v1/token`, `scope=all-apis`.
   Both are passed to DuckDB explicitly, because its defaults are Polaris's, not Databricks's.
3. Whether DuckDB's credential scope covers the metadata Databricks keeps outside the table root;
   the scanning route's own per-table secret lists both directories in its scope.
4. Foreign Iceberg tables get no vended credentials at all, so their rows need store keys of
   your own.

## 9. Azure storage, anywhere

PyIceberg reads `abfss://` through fsspec only if `adlfs` is installed, and it is not a
dependency; its PyArrow fallback ignores the per-account SAS tokens catalogs vend. DuckDB needs
its `azure` extension, which icelens does not load. On Azure, expect the metadata panes to fail
as well as the rows. Deciding whether `adlfs` is a default or an extra comes first.

## 10. A session longer than its token

Catalog tokens last about an hour. A DuckDB attached with client credentials now fetches its
own, but a static `-p token=…` is read once, by both halves, and never refreshed. Nobody has
watched what the panes do in minute sixty-one.

## 11. icelens's own REST catalog, with clients other than DuckDB and PyIceberg

`icelens/rest.py` is checked against DuckDB 1.5.5 and PyIceberg 0.12.0 - the two clients icelens
uses - and against the spec where neither asks for something (`remove-snapshots`,
`set-statistics`, partition statistics). Spark, Trino and PyIceberg's newer releases have not
been pointed at it. It is a sandbox's catalog, not a general one: local files only, no views, no
credential vending, one process's token. Two icelens processes on one sandbox are kept honest by
SQLite's compare-and-swap, which is tested within one process and not across two.

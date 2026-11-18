# Contributing

```bash
python3 scripts/setup.py   # uv, the dev environment, DuckDB's extensions
make check                 # lint and the suite: no server, no network
```

[`AGENTS.md`](AGENTS.md) is the guide to working on icelens: the commands, what the tests need,
where things are, and the rules the code keeps. Coding agents read the same file, so it is the
one place to look and the one place to change.

## Before you open a pull request

- `make check` passes.
- If you changed how icelens talks to a catalog, `make test-live` passes too. It starts Lakekeeper
  and Polaris in Docker and runs the suite against them.
- A change in behaviour comes with a test that fails without it.
- A claim about a platform (Polaris, Snowflake, Databricks, BigLake, anything else) goes in
  `docs/PLATFORMS.md`, with what was run to verify it. What has not been run goes in
  `docs/UNVERIFIED.md` instead.
- A change to `--json` output is a breaking change; the pull request says so.

Work done with a coding agent is welcome under the same rules. You own what you submit: read the
diff and run the tests before you ask someone else to.

## Reporting a problem with a catalog

Say which catalog (product and version), how icelens connected (flags, or the `~/.pyiceberg.yaml`
entry with its secrets removed), what you expected and what the status bar or the error said.
Then run it again with `--trace trace.log` and attach the file: it records every request to the
catalog and every DuckDB statement, with credentials masked. Read it before you post it all the
same - table names, namespaces and hostnames are in it, by design.

## How icelens is built

- `catalog.py` - connection discovery, the catalog's *type*, the route to its rows, and the two
  clients. PyIceberg answers metadata questions
  because it speaks every catalog dialect; DuckDB answers row questions because that is what it
  is for. Neither is required for the other, and `Lake.engine` reports which one answered.
- `model.py` - one function per pane, each `View -> pa.Table`. A `View` is a table plus the
  snapshot you are looking at. Because every pane is an Arrow table, the grid, CSV export, the
  headless printer and the tests share one code path.
- `format.py` - the only place that decides how a byte count, a timestamp or a nested map looks.
- `rest.py` - a small Iceberg REST catalog over a sandbox's SQLite catalog, run in-process on
  127.0.0.1 so DuckDB can attach it and write v3. It writes table metadata itself; everything
  below the metadata file is the client's.
- `trace.py` - `--trace`: the requests, statements and route decisions behind a run, with
  secrets masked.
- `demo.py` and `welcome.py` - the first run: what `icelens` offers with nothing configured, and
  the demo tables it can make.
- `app.py` - the Textual UI. Every catalog call runs in a worker thread and results are cached
  per (table, snapshot, pane), because PyIceberg blocks on HTTP and on reading manifests out of
  object storage, and a UI that freezes while it does that is one people stop trusting.

Two deliberate departures from PyIceberg's `inspect` helpers: they mostly describe the *current*
snapshot, and `inspect.entries()` raises on v3 tables carrying delete files without column sizes
(PyIceberg 0.12). The Commit, Files and Layout panes therefore walk the manifest list themselves,
which is also what lets them answer "as of snapshot N".

## Treating the catalog as untrusted

A catalog is not a trusted input. Table names, column names, partition values and server error
text are all strings somebody else chose, and they end up on a terminal, in a generated SQL
statement and in a filename. Five rules, each with a test in `tests/test_hardening.py`:

- **Nothing from a catalog is ever markup.** Every cell is a `rich.Text`, every column label and
  tree label is a `Text`, and the note line is `markup=False`. A column legally named `[/]` used
  to be a `MarkupError` and a pane that would not draw; `[link=...]` used to be a live hyperlink
  this tool did not write.
- **Nothing from a catalog is ever a control code.** `format.safe` replaces C0/C1 characters,
  carriage returns and the bidi overrides. `rich`'s own `strip_control_codes` leaves `ESC` alone,
  so a file path can otherwise repaint the screen or reverse its own direction.
- **Generated SQL is one statement.** DuckDB takes no bind parameters in `ATTACH`, `CREATE SECRET`
  or a table-function argument, `execute()` runs every statement in the string, and `httpfs` can
  write to a URL. Values are escaped (`literal`, `quote`) and every generated statement goes
  through `one_statement` first. What you type into the SQL pane is exempt, deliberately - that is
  a person driving DuckDB on purpose.
- **Credentials never reach a message.** They are recorded by value when a target is bound and
  scrubbed out of any text on its way to a screen; a `user:pass@` in a URI is stripped from the
  title bar. An access key *id* is deliberately still shown.
- **Bounded reads and bounded writes.** Row limits are clamped (`LIMIT -1` means *no limit* in
  DuckDB), panes cap their listings, `.env` files are refused if oversized, group-writable or
  owned by someone else, and the upward search stops at `$HOME` or the first directory that is
  not yours, the warehouse probe follows no redirects (urllib copies the
  `Authorization` header across hosts), and `x` exports to a sanitised filename without ever
  overwriting an existing file.

## Tests

```bash
python3 scripts/setup.py  # once: uv, the dev environment, DuckDB's extensions
make check                # ruff, pyright, then the suite: no server, no network
make test-live            # the same against Lakekeeper and Polaris, started in Docker
make package              # build the sdist and wheel, install the wheel in a fresh venv, run it
make                      # the rest; each target is one command, listed in AGENTS.md
```

`AGENTS.md` is the contributor's guide - for people and for coding agents, which all read it.

**No server, and no network once DuckDB has its extensions.** Rows, SQL and sandbox writes go
through DuckDB's `iceberg`, `httpfs` and `aws` extensions, which DuckDB downloads from
`extensions.duckdb.org` the first time anything asks for them. The tests that need them are
marked `duckdb_extensions`; where they cannot be loaded those tests skip with DuckDB's reason, and
the rest still pass. `ICELENS_REQUIRE_DUCKDB_EXTENSIONS=1` turns those skips into failures, which CI
sets, and `ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS=1` runs the whole suite as a machine without them
would - which CI also does, so a new test that needs them and is not marked is caught.
`tests/test_offline.py` drives that machine on purpose: the `pyiceberg` route end to end, from
the metadata panes to `--json` to the status bar.

The suite builds a real Iceberg catalog in a temp directory (SQLite catalog, local files) with a
table that has three snapshots including a delete, so the panes are tested against genuine
manifests rather than fixtures. That catalog is also one DuckDB cannot attach, so the scanning
route - rows, SQL and time travel against a non-REST catalog - is what the suite exercises by
default. The UI tests drive the real app through Textual's pilot.

`tests/test_driving.py` is the one to run before you sit down with it. It drives the app the way a
person does and asserts what a screenshot cannot: every pane against both a table with history and
a table with no snapshot at all, the cursor over every column of every pane with the guide rail read
at each stop, every key the app binds pressed in every selection state, time travel into all ten
panes and back, a catalog that refuses to list and one that asks which warehouse, and the whole
thing at 60x18 and at 200x60. Textual swallows an exception raised inside a message handler, so
every step ends in `unbroken()` rather than trusting the absence of a traceback.

`tests/test_attach.py` is the other half: *attach* mode, which is what a real run uses and which a
SQLite catalog cannot reach - the ATTACH itself, `READ_ONLY` as DuckDB enforces it rather than as
a substring of a statement, `AT (VERSION => …)` time travel, and a v3 table written rather than
hand-forged. It runs against icelens's own REST catalog (`icelens/rest.py`, the one `--v3` uses),
so it runs everywhere DuckDB's extensions load, CI included. Point it at a live Lakekeeper with
`ICELENS_TEST_ENV_FILE` (a project's own `.env`, read through the same code `--env-file` uses) or
with `ICELENS_TEST_CATALOG_URI` and friends, and it builds a throwaway warehouse of its own there
instead, uses it, and deletes it. Creating a warehouse is not in the Iceberg REST spec, so that
part speaks Lakekeeper's admin API; everything after it is ordinary Iceberg.

`tests/test_rest_catalog.py` holds that catalog to what a catalog owes its clients - the token,
every requirement and update, a stale commit refused, a multi-table commit all or nothing, a
purge that cannot reach outside the sandbox, v3 row ids never reused - with PyIceberg and DuckDB
as the clients, because a catalog that only agrees with its own tests has not been tested.

It earned its place immediately: row lineage (`l`) on a v2 table came back as a column of NULLs
through an attached catalog, because DuckDB answers `_row_id` there instead of refusing it, and
the "could not, and here is why" fallback only ever fired on the scanning route.

`tests/test_polaris.py` does the same for the other shape of catalog - OAuth client credentials,
per-table vended credentials, nested namespaces - against Apache Polaris, and skips unless one
answers. `make test-live` starts one over RustFS with real STS, beside a Lakekeeper, and runs
the suite against both (`tests/compose/stack.yml`, `tests/compose/live.env`). Each test there is a defect that was real against
Polaris: 13 of its 14 fail on the code before it was written. `tests/test_routes.py` pins the
same rules with no server at all.

`tests/test_snapshots.py` holds the screens still - the welcome screen, the Schema and Writer
panes, help - each as the plain text a terminal shows, kept in `tests/snapshots/`. A change to one
is a diff to review rather than a surprise; `make snapshots` redraws them. Text rather than SVG,
so the diff reads in review; the first one written found help cut off with no way to scroll.

Two of the default tests are drift guards rather than behaviour tests: `test_every_real_column_is_explained`
runs every pane against the real table and resolves every column it produced, so a pane that grows
a column the corpus has no term for fails; `test_every_pane_prints_for_every_table` does the same
for the headless printer.

Three more files hold the parts of the tool other programs depend on:

- `tests/test_properties.py` states the hardening rules as properties and lets
  [hypothesis](https://hypothesis.readthedocs.io/) look for a counterexample: a quoted identifier
  is one identifier and a literal reads back as itself, checked by DuckDB's own parser; `only_reads`
  passes a mix of statements exactly when every one reads, whatever the comments and case; an
  attached sandbox writes inside its namespace or not at all; no remembered secret survives
  `scrub`; nothing in a cell can drive the terminal. `ICELENS_HYPOTHESIS_EXAMPLES` makes the hunt
  longer than the default 150 examples.
- `tests/test_json_contract.py` is the `--json` contract, every kind's envelope and row shape
  written out as data and compared key for key and type for type.
- `tests/test_arguments.py` drives every flag and every combination the CLI refuses, and checks
  the refusal names the flag.

# Working on icelens

Read this before changing anything, whether you are a person or an agent. It is the one set of
instructions for both: Claude Code, Codex, Copilot, Cursor, Gemini, Aider, Jules, Zed and the
rest read this file or a pointer to it, so add anything new here, never to one agent's file.

icelens is a terminal browser for Apache Iceberg catalogs, written in Python (3.10+) with Textual,
PyIceberg and DuckDB. `README.md` says what it does; this file says how to work on it.

## Setup

```bash
python3 scripts/setup.py                         # uv, `uv sync --extra dev`, DuckDB's extensions
python3 scripts/setup.py --extensions-from-pypi  # where extensions.duckdb.org is unreachable
python3 scripts/setup.py --check                 # change nothing; report what is ready
```

The script is idempotent and uses only the standard library. Every agent's setup hook runs it, so
when setup changes, change the script and not the hooks. It installs nothing outside the project
except uv (with `pip --user`, if uv is missing) and DuckDB's extensions (into `~/.duckdb`).

## Commands

| What | Command | `make` |
| --- | --- | --- |
| everything CI's main jobs check | | `make check` |
| tests, no server, no network | `uv run pytest -q` | `make test` |
| one file, or one test | `uv run pytest -q tests/test_rest_catalog.py -k stale` | |
| as a machine without DuckDB's extensions | `ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS=1 uv run pytest -q` | `make test-offline` |
| lint | `uv run ruff check .` | `make lint` |
| type check (basic mode, `src` and `scripts`) | `uv run pyright` | `make typecheck` |
| coverage | `uv run pytest -q --cov` | `make cov` |
| build and install the package, then run it | `ICELENS_TEST_PACKAGING=1 uv run pytest -q tests/test_packaging.py` | `make package` |
| start Lakekeeper and Polaris (Docker) | `docker compose -f tests/compose/stack.yml up -d --wait` | `make live-up` |
| the suite against them | `set -a; . tests/compose/live.env; set +a; uv run pytest -q` | `make test-live` |
| stop them | `docker compose -f tests/compose/stack.yml down -v` | `make live-down` |
| redraw the TUI snapshots after an intended change | `uv run pytest -q tests/test_snapshots.py --snapshot-update` | `make snapshots` |
| build the website into `_site/` (pages, docs, the demo's wheel; with Node, its compiled modules and prebuilt demo) | `python3 scripts/build_site.py` | `make site` |
| the built wheel under Pyodide in Node, as the site's demo runs it (needs Node) | | `make test-browser` |
| run the app on a throwaway catalog | `uv run icelens --demo` (`--v3` for format-version 3) | |
| a throwaway catalog for another engine | `uv run icelens --demo --v3 --serve` (prints how to connect) | |

The full suite takes a few minutes. While iterating, run the file you are changing, then the whole
suite once before you commit. Do not run `ruff format`: the code is formatted by hand, and a
reformatted file buries the change in its diff. `ruff check` is the rule.

## What the tests need, and what skips without it

- **DuckDB's extensions** (`iceberg`, `avro`, `httpfs`, `aws`). Tests that need them are marked
  `duckdb_extensions` and skip, with DuckDB's reason, where they will not load. CI sets
  `ICELENS_REQUIRE_DUCKDB_EXTENSIONS=1`, which turns those skips into failures. A new test that
  reads rows, runs SQL or writes to a sandbox must carry the mark; the `without-extensions` CI job
  catches one that does not.
- **A live catalog** (`tests/test_attach.py` in live mode, `tests/test_polaris.py`). These skip
  unless one answers. `make live-up` starts Lakekeeper and Polaris over RustFS on ports 18181,
  18282 and 19000. Without it, the attach suite runs against icelens's own REST catalog instead,
  so it still runs everywhere. `ICELENS_REQUIRE_LIVE_CATALOGS=1` (CI's live job) turns "no catalog
  answered" into a failure.
- **Network**: none, once the extensions are installed. A test must never reach the internet.

## Where things are

- `src/icelens/catalog.py`: finding the catalog, connecting, and choosing how rows are read
  (DuckDB `attach`, DuckDB scanning, or PyIceberg). `Lake.engine` reports which one answered.
- `src/icelens/model.py`: one function per pane, each `View -> pa.Table`. The grid, CSV export,
  the headless printer and the tests all read the same Arrow table.
- `src/icelens/format.py`: how a value looks. `safe()` strips control codes.
- `src/icelens/rest.py`: icelens's own Iceberg REST catalog, served on 127.0.0.1. `--v3`
  sandboxes and the hermetic attach tests use it.
- `src/icelens/trace.py`: `--trace`, and the wrapper that times DuckDB statements while it is on.
- `src/icelens/app.py`, `welcome.py`, `icelens.tcss`: the Textual UI. `cli.py`: flags and headless
  output. `demo.py`: the demo tables. `terms.py`: the explanations the guide rail shows.
- `src/icelens/browser.py`: icelens in a web page under Pyodide - the Textual driver xterm.js
  talks to, and the shims Pyodide needs (no threads, no `pyarrow._s3fs`). `memory.py`: the
  in-memory catalog the browser's demo uses instead of SQLite. `site/`: the website;
  `site/worker.js` runs icelens in a Web Worker, `site/loader.mjs` loads Python the same way for
  it and for `tests/pyodide/`, and `site/pyodide.json` says what is loaded when. `bytecode.py`: the demo's modules, compiled
  at build time by `tests/pyodide/prebuild.mjs`, which also writes the demo table and the preview. `scripts/build_site.py` renders the docs and builds the
  wheel; `.github/workflows/pages.yml` checks that wheel under Pyodide and deploys on `main`.
- `tests/conftest.py`: the fixtures. `lake` is a SQLite catalog with real history, `v3_lake` the
  same at v3, `attachable` a REST catalog DuckDB can attach (live if configured, local if not),
  `rest_lake` and `rest_sandbox` lakes on top of it.
- `docs/PLATFORMS.md`: for users - what works where, how to connect, what to do when it fails.
  `docs/FINDINGS.md`: the evidence behind it - what was verified, how, and the bugs found.
  `docs/UNVERIFIED.md`: what has not been verified.

## Rules the code keeps

Each of these has tests in `tests/test_hardening.py` and `tests/test_properties.py`; a change
that breaks one fails there.

- **Anything from a catalog is untrusted.** Table names, column names, values and server errors
  never become Rich markup (use `rich.Text`, `markup=False`) and pass through `format.safe()`
  before they reach the terminal.
- **Generated SQL is exactly one statement.** Identifiers go through `quote()`, values through
  `literal()`, and the statement through `one_statement()`. DuckDB takes no bind parameters in
  `ATTACH`, `CREATE SECRET` or table-function arguments, so these are the only guard.
- **Credentials never reach a message.** Record them with `remember_secrets()` when a target is
  bound; any text bound for the screen, a file or an exception goes through `scrub()`.
- **Every catalog call in the UI runs in a worker thread.** PyIceberg blocks on HTTP and object
  storage; the UI must not.
- **Failure is reported, not raised.** A pane that cannot load says why in the pane, and the
  status bar says which route answered and why it fell back. Tracebacks are bugs.

## Conventions

- Comments and docstrings explain *why*: the constraint, the server that behaves oddly, the bug a
  line prevents. Match the density of the code around you, and do not narrate what code does.
- A behaviour change needs a test that fails without it. Prefer the real thing over mocks: a real
  catalog in a temp directory, the real app driven through Textual's pilot, real HTTP to
  `rest.serve()`.
- Platform-specific behaviour gets a line in `docs/FINDINGS.md`, saying what was verified and
  how, and, if a user would notice it, a short one in `docs/PLATFORMS.md`, written for them: what
  works, how to connect, what to do. Do not claim support that has not been run.
- `--json` output is a contract (`tests/test_json_contract.py`). Changing a key or a type is a
  breaking change; say so in the commit.
- Dependency floors are tested, not guessed: CI's `minimums` job installs the lowest version of
  each direct dependency `pyproject.toml` allows and runs the suite. Lower a floor or raise a
  ceiling only after running the suite on that version. Change dependencies with `uv add` or
  `uv lock`, so `uv.lock` stays in step; CI runs `uv sync --locked`. The canary workflow
  (`.github/workflows/canary.yml`) runs the suite against DuckDB's pre-releases, PyIceberg's main
  branch and the newest releases past the pins, so an upstream break shows up there first.
- Commit subjects are one sentence, in the imperative, about the behaviour rather than the files:
  "Refuse a filter PyIceberg cannot read in words, not a traceback". The body says why.
- Never commit a `.env` or a token. Credentials in `tests/compose/` are throwaway fixtures for
  containers that live for one test run.

## Agents

Every agent gets these instructions and the same setup script. Only the wiring differs:

| Agent | Reads instructions from | Runs setup from |
| --- | --- | --- |
| Claude Code | `CLAUDE.md`, which imports this file | `.claude/settings.json` SessionStart hook, on the web |
| GitHub Copilot cloud agent, VS Code | this file | `.github/workflows/copilot-setup-steps.yml` |
| Cursor | this file | `.cursor/environment.json` |
| Gemini CLI | this file, via `.gemini/settings.json` | |
| Aider | this file, via `.aider.conf.yml` | |
| Codespaces, VS Code dev containers, Ona | this file | `.devcontainer/devcontainer.json` |
| Codex, Jules, Devin | this file | their environment settings: set the setup script to `python3 scripts/setup.py` |
| Zed, Windsurf, Amp, opencode, Warp, goose, Factory | this file | |

There is deliberately no `.github/copilot-instructions.md`, `GEMINI.md` or `.cursor/rules/`:
a second copy drifts, and Zed reads only the first instruction file it finds, in an order that
puts `copilot-instructions.md` ahead of this one.

## Agent notes

- Run `python3 scripts/setup.py --check` first if anything looks missing. If the extensions will
  not install, add `--extensions-from-pypi`.
- Docker is optional. Without it the live tests skip, which is expected, not a failure.
- To see what icelens asked a catalog or DuckDB, add `--trace FILE` to any command: every HTTP
  request, DuckDB statement and route decision, timed, with secrets masked. A bug that involves a
  catalog is diagnosed from that file, not by adding prints.
- The TUI cannot be driven from a plain shell. Drive it with Textual's pilot, as the tests do, and
  read a screen with `screen_text(app)` from `tests/snapshots.py`; or use the headless flags
  (`--list`, `--pane`, `--sql`, `--json`; see `README.md`), which print what a pane would show.
- Do not commit generated files: `.venv/`, coverage output, `dist/`, `icelens-*.csv` exports.

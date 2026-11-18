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

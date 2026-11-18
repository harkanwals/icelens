"""What the browser build must do, run inside Pyodide by run.mjs. Each phase returns its failures.

The phases follow what a visitor's tab has loaded, so each checks what is true at that point:

- `page`: only site/pyodide.json's `packages` - the first frame. The demo, its panes, keys,
  that what the page leaves out (pandas, SQLAlchemy, DuckDB) was not needed to get there, and
  that the site's demo files restored and its compiled-module bundle served every module it holds.
- `sql`: after `later` (DuckDB), the SQL the page's SQL pane runs.
- `cli`: after SQLAlchemy, which the page never loads, the headless commands on a SQLite sandbox,
  so the command line is known to work under Pyodide's versions too.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import re
import sys
import traceback

FAILED: list[str] = []
_CSI = re.compile(r"\x1b\[[0-9;?<>$]*[A-Za-z~]|\x1b\][^\x07]*\x07|\x1b[=>]")


def screen(chunks: list[str]) -> str:
    return " ".join(_CSI.sub(" ", "".join(chunks)).split())


def check(name: str, ok: bool, detail: str = "") -> None:
    # sys.__stdout__: while the app runs, Textual captures sys.stdout, and a check's verdict
    # written there would never reach CI's log.
    out = sys.__stdout__ or sys.stdout
    out.write(("ok    " if ok else "FAIL  ") + name + ("" if ok else f": {detail[:400]}") + "\n")
    if not ok:
        FAILED.append(name)


def cli(*argv: str) -> tuple[int, str, str]:
    from icelens import cli as icelens_cli

    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = icelens_cli.main(["--demo", "--sandbox-dir", "/tmp/cli", *argv])
    return code, out.getvalue(), err.getvalue()


async def settle(chunks: list[str], quiet: float = 0.5, limit: float = 30.0) -> None:
    loop = asyncio.get_running_loop()
    deadline, seen = loop.time() + limit, -1
    while loop.time() < deadline and len(chunks) != seen:
        seen = len(chunks)
        await asyncio.sleep(quiet)


async def page(loaded: list[str], dropped: list[str], bundled: int, restored: str) -> None:
    from icelens import browser, bytecode

    check("runs under Pyodide", browser.in_pyodide(), sys.platform)
    browser.install_shims()  # restore_demo has usually done it already; this must be a no-op then
    from pyiceberg.utils.concurrent import ExecutorFactory

    check("shims in place", "pyarrow._s3fs" in sys.modules and isinstance(ExecutorFactory.get_or_create(), browser.SyncExecutor))
    check("the dropped packages are not downloaded", not [n for n in dropped if n in loaded], str(loaded))
    check("Textual and icelens come with the first download", {"textual", "icelens"} <= set(loaded), str(loaded))
    check("the site's demo files restore", restored == "demo.orders", repr(restored))
    check("prepare finds the demo", browser.prepare() == "demo.orders")
    lake, table = browser.demo_lake()
    try:
        check("the demo's catalog is in memory", lake.target.catalog_type == "in-memory", lake.target.catalog_type)
        check("five snapshots", len(lake.table(table).snapshots()) == 5)
        rows, engine = lake.rows(table, limit=500)
        check("the Data pane reads 183 rows through PyIceberg", rows.num_rows == 183 and engine.startswith("pyiceberg"), engine)
    finally:
        lake.close()

    chunks: list[str] = []
    exits: list[str] = []
    session = await browser.start(chunks.append, 120, 36, on_exit=exits.append)
    await settle(chunks)
    check("the TUI draws the demo table", "demo.orders" in screen(chunks), screen(chunks)[:300])
    before = len(chunks)
    session.feed("4")
    await settle(chunks)
    check("a key switches pane", "5 snapshots" in screen(chunks[before:]), screen(chunks[before:])[:300])
    before = len(chunks)
    session.resize(100, 30)
    await settle(chunks)
    check("a resize redraws", len(chunks) > before)
    session.exit()
    await asyncio.wait_for(session.task, 20)
    check("quitting tells the page", exits == [""], repr(exits))
    for module in ("pandas", "sqlalchemy", "duckdb"):
        check(f"the first frame did not need {module}", module not in sys.modules)
    # The bundle is made from these same packages, so every module in it should have been used;
    # a module left over means its file moved, and the page compiled it from source after all.
    finder = next((f for f in sys.meta_path if isinstance(f, bytecode._Finder)), None)
    check("the site has a compiled-module bundle", bundled > 0, "wheels/latest.json names none")
    if finder is not None:
        check("every module in the bundle was used", not finder.modules and finder.served == bundled, f"{finder.served} served, unused: {sorted(finder.modules)[:20]}")


async def sql() -> None:
    from icelens import browser

    lake, table = browser.demo_lake()
    try:
        check("SQL takes the arrow route", lake.sql_route == "arrow", lake.sql_route)
        got = lake.query(f"SELECT count(*) AS n FROM icelens.{table}").to_pylist()
        check("SQL answers", got == [{"n": 183}], str(got))
        got = lake.query(f"SELECT count(*) AS n FROM icelens.{table} a JOIN icelens.{table} b USING (order_id)").to_pylist()
        # A USING join cannot be bound before its tables exist, so this is the check that DuckDB
        # finds the table names by parsing alone (json_serialize_sql) in Pyodide's build too.
        check("SQL with a USING join", got == [{"n": 183}], str(got))
    finally:
        lake.close()


async def command_line() -> None:
    code, out, err = cli("--list")
    check("--list", code == 0 and "demo" in out, out + err)
    code, out, err = cli("--pane", "snapshots", "demo.orders")
    check("--pane snapshots", code == 0 and out.count("append") == 4 and "overwrite" in out, out + err)
    code, out, err = cli("--pane", "manifests", "demo.orders")
    check("--pane manifests", code == 0 and "data" in out, out + err)
    code, out, err = cli("--pane", "data", "demo.orders")
    check("--pane data reads 183 rows", code == 0 and "183 rows" in err, err)
    code, out, err = cli("--sql", "SELECT count(*) AS n FROM icelens.demo.orders")
    check("--sql answers", code == 0 and "183" in out, out + err)
    code, out, err = cli("--json", "--sql", "SELECT region, count(*) AS n FROM icelens.demo.orders GROUP BY 1 ORDER BY 1")
    check("--sql --json", code == 0 and '"region"' in out, out + err)


async def main(phase: str, loaded: list[str] | None = None, dropped: list[str] | None = None, bundled: int = 0, restored: str = "") -> int:
    before = len(FAILED)
    try:
        if phase == "page":
            await page(loaded or [], dropped or [], bundled, restored)
        elif phase == "sql":
            await sql()
        else:
            await command_line()
    except BaseException:  # noqa: BLE001 - a crash is a failed check, with its traceback for CI's log
        check(f"{phase}: no exception", False, traceback.format_exc())
    return len(FAILED) - before

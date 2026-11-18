"""The browser build's driver and shims, run on CPython.

The same module runs under Pyodide in the site's Web Worker; CI's `pyodide` job runs it there
(`tests/pyodide/run.mjs`). Here it is driven exactly as the worker drives it - a `write` callback
out, `feed()` in - against the real demo catalog in a temp directory.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import Executor
from typing import Any

import pytest

from icelens import browser

_CSI = re.compile(r"\x1b\[[0-9;?<>$]*[A-Za-z~]|\x1b\][^\x07]*\x07|\x1b[=>]")


def text(chunks: list[str]) -> str:
    """What a terminal would show of `chunks`, flattened: escape codes out, whitespace squeezed."""
    return " ".join(_CSI.sub(" ", "".join(chunks)).split())


async def settle(chunks: list[str], quiet: float = 0.3, limit: float = 15.0) -> None:
    """Wait until the app has stopped drawing for `quiet` seconds."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit
    seen = -1
    while loop.time() < deadline:
        if len(chunks) == seen:
            return
        seen = len(chunks)
        await asyncio.sleep(quiet)


@pytest.fixture
def restore_executor() -> Any:
    from pyiceberg.utils.concurrent import ExecutorFactory

    saved = ExecutorFactory.__dict__["get_or_create"]
    yield
    ExecutorFactory.get_or_create = saved  # type: ignore[method-assign]


def test_shims_do_nothing_outside_pyodide() -> None:
    assert not browser.in_pyodide()
    assert browser.install_shims() == []


def test_shims_are_idempotent_and_leave_pyiceberg_working(tmp_path: Any, restore_executor: Any) -> None:
    from pyiceberg.utils.concurrent import ExecutorFactory

    first = browser.install_shims(force=True)
    assert "pyiceberg executor" in first
    assert "pyiceberg executor" not in browser.install_shims(force=True)
    assert isinstance(ExecutorFactory.get_or_create(), browser.SyncExecutor)
    # The demo writes five snapshots and scans them back through the synchronous executor:
    # the same path that runs in the browser, where a thread pool cannot start.
    lake, table = browser.demo_lake(tmp_path)
    try:
        rows, engine = lake._pyiceberg_rows(table, 500, None, None, None, False)
        assert rows.num_rows == 183
        assert len(lake.table(table).snapshots()) == 5
    finally:
        lake.close()


def test_prepare_seeds_the_demo_once_and_names_its_table(tmp_path: Any) -> None:
    assert browser.prepare(tmp_path) == "demo.orders"
    assert browser.prepare(tmp_path) == "demo.orders"  # the second finds it and leaves it alone
    lake, table = browser.demo_lake(tmp_path)
    try:
        assert len(lake.table(table).snapshots()) == 5
    finally:
        lake.close()


def test_sync_executor_runs_in_order_and_keeps_exceptions_in_the_future() -> None:
    pool: Executor = browser.SyncExecutor()
    seen: list[int] = []
    futures = [pool.submit(seen.append, n) for n in range(3)]
    assert seen == [0, 1, 2] and all(f.done() for f in futures)
    assert list(pool.map(lambda n: n * 2, [1, 2, 3])) == [2, 4, 6]
    failed = pool.submit(int, "not a number")
    with pytest.raises(ValueError):
        failed.result()


async def test_the_app_draws_through_write_and_answers_keys(tmp_path: Any) -> None:
    out: list[str] = []
    exits: list[str] = []
    session = await browser.start(out.append, 120, 36, on_exit=exits.append, home=tmp_path)
    try:
        assert session.running
        await settle(out)
        screen = text(out)
        assert "demo.orders" in screen
        assert "Snapshots" in screen  # the pane tabs
        assert out[0].startswith("\x1b[?1049h")  # alternate screen first, as a terminal expects

        before = len(out)
        session.feed("4")  # the Snapshots pane
        await settle(out)
        assert "5 snapshots" in text(out[before:])

        before = len(out)
        session.resize(90, 30)
        await settle(out)
        assert len(out) > before  # redrawn at the new size
        assert session.app.size == (90, 30)
    finally:
        session.exit()
        await asyncio.wait_for(session.task, 10)
    assert not session.running
    assert exits == [""]
    assert out[-1].endswith("\x1b[?1049l")  # and the terminal is handed back


async def test_the_app_takes_the_page_theme_and_follows_it(tmp_path: Any, monkeypatch: Any) -> None:
    # A Web Worker has no COLORTERM, and without it Rich falls back to 256 or 16 colours: the
    # theme's own colours would never reach xterm.js, which draws 24-bit colour.
    monkeypatch.delenv("COLORTERM", raising=False)
    out: list[str] = []
    session = await browser.start(out.append, 100, 30, home=tmp_path, dark=False)
    try:
        await settle(out)
        assert session.app.theme == "icelens-light" and "48;2;246;249;251" in "".join(out)
        before = len(out)
        session.set_theme(True)  # the page switched to dark
        await settle(out)
        assert session.app.theme == "icelens-dark" and "48;2;11;20;28" in "".join(out[before:])
    finally:
        session.exit()
        await asyncio.wait_for(session.task, 10)


async def test_input_split_across_feeds_and_a_lone_escape_still_arrive(tmp_path: Any) -> None:
    out: list[str] = []
    session = await browser.start(out.append, 100, 30, home=tmp_path)
    try:
        await settle(out)
        keys: list[str] = []
        driver = session._driver.current
        assert driver is not None
        original = driver.process_message

        def spy(message: Any) -> None:
            if hasattr(message, "key"):
                keys.append(message.key)
            original(message)

        driver.process_message = spy  # type: ignore[method-assign]
        session.feed("\x1b[")  # half of the down arrow
        session.feed("B")
        session.feed("\x1b")  # escape on its own: only the parser's timeout can say so
        await asyncio.sleep(0.5)
        assert "down" in keys
        assert "escape" in keys
    finally:
        session.exit()
        await asyncio.wait_for(session.task, 10)


async def test_a_crash_is_reported_as_a_crash_not_a_quit(tmp_path: Any, monkeypatch: Any) -> None:
    from icelens.app import IcelensApp

    def broken(self: Any) -> None:
        raise RuntimeError("the pane fell over")

    monkeypatch.setattr(IcelensApp, "on_mount", broken)
    exits: list[str] = []
    session = await browser.start(lambda _: None, 80, 24, on_exit=exits.append, home=tmp_path)
    await asyncio.wait_for(session.task, 10)
    assert exits == ["RuntimeError: the pane fell over"]


async def test_nothing_is_written_after_the_app_stops(tmp_path: Any) -> None:
    out: list[str] = []
    session = await browser.start(out.append, 80, 24, home=tmp_path)
    session.exit()
    await asyncio.wait_for(session.task, 10)
    count = len(out)
    session.feed("4")
    session.resize(100, 40)
    await asyncio.sleep(0.2)
    assert len(out) == count


async def test_the_first_frame_is_the_demo_and_stays_on_the_alternate_screen(tmp_path: Any) -> None:
    frame = await browser.first_frame(100, 30, tmp_path)
    assert frame.startswith("\x1b[?1049h")
    assert "\x1b[?1049l" not in frame  # the page goes on showing it, so it must not hand back
    assert "demo.orders" in text([frame])


def test_first_frame_command_prints_json(capsys: Any, monkeypatch: Any) -> None:
    import json
    import sys

    monkeypatch.delenv("COLORTERM", raising=False)  # as in the build's Pyodide
    monkeypatch.setitem(sys.modules, "duckdb", sys.modules.get("duckdb"))  # restored after
    assert browser.main(["first-frame", "90", "28"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["cols"], got["rows"]) == (90, 28) and "demo.orders" in text([got["ansi"]])
    # One frame per page theme, each in that theme's own background colour.
    assert "48;2;11;20;28" in got["ansi"] and "48;2;246;249;251" in got["light"]
    # As the page draws it: DuckDB is not loaded yet, so the rows are PyIceberg's.
    assert "read through pyiceberg" in text([got["ansi"]])
    assert browser.main([]) == 2


def test_the_published_frame_shows_no_path_of_the_machine_that_built_it(capsys: Any, monkeypatch: Any) -> None:
    # The site's frame is 120 wide, enough for the overview to show where the table lives, and it
    # is published as it is: under a fixed home, not a temporary folder of the build machine.
    import json
    import sys

    monkeypatch.setitem(sys.modules, "duckdb", sys.modules.get("duckdb"))  # restored after
    assert browser.main(["first-frame", "120", "36"]) == 0
    got = json.loads(capsys.readouterr().out)
    shown = text([got["ansi"]]) + text([got["light"]])
    assert "/tmp/icelens-frame/" in shown
    assert not any(m in shown for m in ("/var/folders/", "/Users/", "/home/"))


def _rewrite_pointer(blob: bytes, **changes: str) -> bytes:
    import io
    import json
    import tarfile

    src = tarfile.open(fileobj=io.BytesIO(blob), mode="r:xz")
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:xz") as tar:
        for member in src.getmembers():
            data = src.extractfile(member) if member.isfile() else None
            if member.name == "pointer.json" and data is not None:
                body = json.dumps({**json.loads(data.read()), **changes}).encode()
                member.size = len(body)
                data = io.BytesIO(body)
            tar.addfile(member, data)
    return out.getvalue()


def test_the_demo_packed_at_build_time_restores_as_written(tmp_path: Any) -> None:
    import shutil

    from icelens import memory

    home = tmp_path / "home"
    browser.prepare(home)
    lake, table = browser.demo_lake(home)
    written = lake.table(table).metadata_location
    lake.close()
    archive = tmp_path / "demo.tar.xz"
    archive.write_bytes(browser.pack_demo(home))
    # A fresh page: no catalog in memory, no files.
    memory.forget()
    shutil.rmtree(home)
    assert browser.restore_demo(str(archive), home) == "demo.orders"
    lake, table = browser.demo_lake(home)
    try:
        assert lake.table(table).metadata_location == written
        assert len(lake.table(table).snapshots()) == 5 and lake.rows(table)[0].num_rows == 183
    finally:
        lake.close()
    assert browser.restore_demo(str(archive), home) == "demo.orders"  # a second time changes nothing


def test_a_demo_archive_pointing_elsewhere_is_refused(tmp_path: Any) -> None:
    from icelens import memory

    home = tmp_path / "home"
    browser.prepare(home)
    blob = browser.pack_demo(home)
    archive = tmp_path / "demo.tar.xz"
    for changes in ({"metadata": "file:///etc/passwd"}, {"table": "other.table"}):
        memory.forget()
        archive.write_bytes(_rewrite_pointer(blob, **changes))
        assert browser.restore_demo(str(archive), home) is None
    # Packed for one home, the archive's metadata is outside any other's warehouse.
    archive.write_bytes(blob)
    assert browser.restore_demo(str(archive), tmp_path / "elsewhere") is None
    archive.write_bytes(b"not an archive")
    assert browser.restore_demo(str(archive), home) is None
    assert browser.restore_demo(str(tmp_path / "missing.tar.xz"), home) is None
    archive.write_bytes(_rewrite_pointer(blob))  # the control: rewritten but unchanged, it restores
    memory.forget()
    assert browser.restore_demo(str(archive), home) == "demo.orders"

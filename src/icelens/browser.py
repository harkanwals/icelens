"""icelens inside a web page: the TUI under Pyodide, drawn by xterm.js.

The site's `try.html` runs this module in a Web Worker. The page owns the terminal (xterm.js);
the worker owns Python. Bytes cross between them as strings: everything the app draws goes out
through the `write` callback given to `start()`, and everything the visitor types comes back in
through `Session.feed()`. Nothing here talks to a network - the catalog is the `--demo` one, in
Pyodide's in-memory filesystem, made fresh for every page load.

Pyodide is a CPython without threads and with a partial standard library, so three things
icelens leans on need help, and `install_shims()` gives it. Each was found by running the real
app under Pyodide 314.0.7, not guessed:

- `pyarrow._s3fs` is not built into Pyodide's PyArrow, and `pyiceberg.io.pyarrow` imports
  `S3RetryStrategy` from it at module level - so without a stand-in there is no FileIO at all.
  Nothing here reaches S3, so a name with nothing behind it is enough.
- `threading.Thread.start()` raises. PyIceberg fans manifest reads and writes out over a
  `ThreadPoolExecutor`, which starts a thread on its first `submit`; a synchronous executor
  answers the same calls in order. (Textual's thread workers survive on their own: Pyodide's
  event loop runs `run_in_executor` work inline.)
- Pyodide's event loop calls `asyncio.tasks._set_task_name`, which Python 3.14 removed, the
  first time anything creates a named task - and Textual names its tasks.

The shims are applied only where they are needed, so importing this module on an ordinary
CPython changes nothing; the tests drive the same driver there.
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from collections.abc import Callable, Iterable
from concurrent.futures import Executor, Future
from pathlib import Path
from typing import Any

from textual import events
from textual._xterm_parser import XTermParser
from textual.driver import Driver
from textual.geometry import Size
from textual.message import Message

#: Where the demo sandbox lives in Pyodide's in-memory filesystem. Gone on reload, by design.
HOME = "/tmp/icelens"
#: Where the build draws the site's preview frame (`main`), outside the browser.
FRAME_HOME = "/tmp/icelens-frame"

#: How long a lone ESC waits to see whether it starts a sequence; Textual's own drivers poll
#: their parser on a 100 ms select, so a key reaches the app no later than it would there.
_TICK = 0.1

#: The terminal modes Textual's Linux driver turns on that xterm.js understands: alternate
#: screen, hidden cursor, focus reports, mouse (any-event, SGR coordinates), bracketed paste.
_ENTER = "\x1b[?1049h\x1b[?25l\x1b[?1004h\x1b[?1000h\x1b[?1003h\x1b[?1015h\x1b[?1006h\x1b[?2004h"
_LEAVE = "\x1b[?2004l\x1b[?1006l\x1b[?1015l\x1b[?1003l\x1b[?1000l\x1b[?1004l\x1b[?25h\x1b[?1049l"


def in_pyodide() -> bool:
    return sys.platform == "emscripten"


class SyncExecutor(Executor):
    """An executor that runs each call as it is submitted, for a Python that cannot start threads.

    Exceptions land in the future rather than escaping `submit`, which is where a pool would
    have put them and where PyIceberg looks for them.
    """

    def submit(self, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Future[Any]:
        future: Future[Any] = Future()
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - delivered through the future, as a pool would
            future.set_exception(exc)
        return future


def install_shims(force: bool = False) -> list[str]:
    """Patch what Pyodide lacks. Idempotent. Returns the names of the shims it applied.

    `force` applies them on CPython too, so the tests can show they leave icelens working.
    """
    applied: list[str] = []
    if not (force or in_pyodide()):
        return applied
    try:
        import pyarrow._s3fs  # noqa: F401  # pyright: ignore[reportMissingModuleSource]
    except ImportError:
        stub = types.ModuleType("pyarrow._s3fs")
        stub.S3RetryStrategy = type("S3RetryStrategy", (), {})  # type: ignore[attr-defined]
        sys.modules["pyarrow._s3fs"] = stub
        applied.append("pyarrow._s3fs")
    from pyiceberg.utils.concurrent import ExecutorFactory

    if not getattr(ExecutorFactory.get_or_create, "_icelens_sync", False):
        def get_or_create() -> Executor:
            return SyncExecutor()

        get_or_create._icelens_sync = True  # type: ignore[attr-defined]
        ExecutorFactory.get_or_create = staticmethod(get_or_create)  # type: ignore[method-assign]
        applied.append("pyiceberg executor")
    tasks: Any = asyncio.tasks
    if not hasattr(tasks, "_set_task_name"):
        def _set_task_name(task: asyncio.Task[Any], name: str | None) -> None:
            if name is not None:
                task.set_name(name)

        tasks._set_task_name = _set_task_name
        applied.append("asyncio.tasks._set_task_name")
    return applied


class BrowserDriver(Driver):
    """A Textual driver whose terminal is a callback out and `feed()` in.

    Textual makes a driver itself, from a class, so the callback reaches it as a class
    attribute: `start()` builds a subclass with `sink` set. Input arrives in whatever pieces
    xterm.js hands over - a pasted block, half an escape sequence - and goes through the same
    `XTermParser` Textual's own drivers use, so keys, mouse and paste mean what they mean in a
    real terminal.
    """

    sink: Callable[[str], None] = staticmethod(lambda data: None)
    #: the most recent driver started, which is how a `Session` finds the one Textual made
    current: BrowserDriver | None = None

    def __init__(self, app: Any, *, debug: bool = False, mouse: bool = True, size: tuple[int, int] | None = None):
        super().__init__(app, debug=debug, mouse=mouse, size=size)
        self._parser = XTermParser(debug)
        self._ticking = False
        self._stopped = False

    @property
    def is_headless(self) -> bool:
        return False

    def write(self, data: str) -> None:
        if not self._stopped:
            type(self).sink(data)

    def flush(self) -> None:
        pass

    def _get_size(self) -> tuple[int, int]:
        return self._size or (80, 24)

    def start_application_mode(self) -> None:
        type(self).current = self
        self.write(_ENTER)
        self.resize(*self._get_size())

    def stop_application_mode(self) -> None:
        self.write(_LEAVE)
        self._stopped = True
        if type(self).current is self:
            type(self).current = None

    def disable_input(self) -> None:
        self._stopped = True

    def feed(self, data: str) -> None:
        """Input from the terminal, as typed. Called on the event loop's own thread."""
        if self._stopped or not data:
            return
        self._dispatch(self._parser.feed(data))
        self._schedule_tick()

    def resize(self, width: int, height: int) -> None:
        width, height = max(int(width), 1), max(int(height), 1)
        self._size = (width, height)
        size = Size(width, height)
        self.send_message(events.Resize(size, size))

    def _dispatch(self, messages: Iterable[Message]) -> None:
        for message in messages:
            self.process_message(message)

    def _schedule_tick(self) -> None:
        # A lone ESC is held until the parser's timeout says nothing followed it; a real
        # terminal's input thread polls for that, and here the event loop does it once.
        if self._ticking or self._parser._timeout_time is None:  # pyright: ignore[reportPrivateUsage]
            return
        self._ticking = True
        self._loop.call_later(_TICK, self._tick)

    def _tick(self) -> None:
        self._ticking = False
        if not self._stopped:
            self._dispatch(self._parser.tick())
            self._schedule_tick()


class Session:
    """One icelens running in a page. The worker keeps it and forwards input to it."""

    def __init__(self, app: Any, driver: type[BrowserDriver], task: asyncio.Task[Any]):
        self.app = app
        self._driver = driver
        self.task = task

    @property
    def running(self) -> bool:
        return not self.task.done()

    def feed(self, data: str) -> None:
        driver = self._driver.current
        if driver is not None:
            driver.feed(data)

    def resize(self, width: int, height: int) -> None:
        driver = self._driver.current
        if driver is not None:
            driver.resize(width, height)

    def set_theme(self, dark: bool) -> None:
        """Follow the page's light or dark mode."""
        if self.running:
            use_theme(self.app, dark)

    def exit(self) -> None:
        if self.running:
            self.app.exit()


def demo_lake(home: str | Path = HOME) -> tuple[Any, str]:
    """Open the demo sandbox under `home`, seeding it if it is empty. Returns the lake and table."""
    from icelens import demo
    from icelens.catalog import Lake
    from icelens.memory import memory_target

    # In memory rather than SQLite: SQLAlchemy is most of a second to import under Pyodide, and
    # a catalog that is gone on reload has no use for a database (see icelens.memory).
    lake = Lake(memory_target("demo", home), read_only=True)
    return lake, demo.seed(lake).table


def pack_demo(home: str | Path = HOME) -> bytes:
    """The seeded demo's warehouse, with where its table's metadata is, for `restore_demo`.

    Seeding is five commits written with PyIceberg in the visitor's tab, a third of a second
    or more under Pyodide; the build writes them once, at the same paths the page uses, and the
    page unpacks the files instead. It also makes the page's preview (captured from the same
    files) the same screen as the live app's first frame, ids and all.
    """
    import io
    import json
    import tarfile

    from icelens.memory import memory_target

    lake, table = demo_lake(home)
    try:
        location = lake.catalog.load_table(table).metadata_location
    finally:
        lake.close()
    warehouse = Path(memory_target("demo", home).properties["warehouse"].removeprefix("file://"))
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:xz") as tar:
        pointer = json.dumps({"table": table, "metadata": location}).encode()
        info = tarfile.TarInfo("pointer.json")
        info.size = len(pointer)
        tar.addfile(info, io.BytesIO(pointer))
        tar.add(warehouse, arcname="warehouse")
    return out.getvalue()


def restore_demo(archive: str, home: str | Path = HOME) -> str | None:
    """Unpack `pack_demo`'s warehouse under `home` and register its table. None if it will not do.

    The archive is the site's own, but it is still only trusted as far as the `data` filter lets
    it write (no links, nothing outside the warehouse), and only if its metadata is where this
    page's demo would have written it.
    """
    import json
    import tarfile

    from icelens import demo
    from icelens.catalog import Lake
    from icelens.memory import memory_target

    install_shims()  # opening the table reads it through PyArrow, which needs them under Pyodide
    target = memory_target("demo", home)
    warehouse = target.properties["warehouse"]
    try:
        with tarfile.open(archive, mode="r:xz") as tar:
            member = tar.extractfile("pointer.json")
            pointer = json.loads(member.read()) if member else {}
            location = str(pointer.get("metadata", ""))
            if pointer.get("table") != demo.TABLE or not location.startswith(warehouse + "/"):
                return None
            members = [m for m in tar.getmembers() if m.name == "warehouse" or m.name.startswith("warehouse/")]
            tar.extractall(Path(warehouse.removeprefix("file://")).parent, members=members, filter="data")
    except (OSError, ValueError, KeyError, tarfile.TarError):
        return None
    lake = Lake(target, read_only=True)
    try:
        catalog = lake.catalog
        if demo.NAMESPACE not in {ns[0] for ns in catalog.list_namespaces()}:
            catalog.create_namespace(demo.NAMESPACE)
        if (demo.NAMESPACE, demo.TABLE.split(".")[1]) not in set(catalog.list_tables(demo.NAMESPACE)):
            catalog.register_table(demo.TABLE, location)
    finally:
        lake.close()
    return demo.TABLE


def prepare(home: str | Path = HOME) -> str:
    """Everything before the first frame that does not need a terminal. Idempotent.

    Split from `start` so the page can run it while it is still drawing itself: the imports
    and the demo's five commits, unless `restore_demo` has already put them in place. Returns
    the demo table's name.
    """
    install_shims()
    import icelens.app  # noqa: F401 - imported for its cost, now rather than on the first frame

    lake, table = demo_lake(home)
    lake.close()
    return table


# The demo's colours, from the website's palette, so the app looks like part of the page it is
# on. icelens.tcss uses only Textual's theme variables, so a theme restyles all of it.
THEMES: dict[bool, dict[str, Any]] = {
    True: dict(
        name="icelens-dark", dark=True, primary="#6fd6f7", secondary="#8ab4f8", accent="#f5d67f",
        foreground="#e4ecf2", background="#0b141c", surface="#111c26", panel="#172431",
        success="#7dd3a8", warning="#f5d67f", error="#f7768e",
    ),
    False: dict(
        name="icelens-light", dark=False, primary="#0a7ea4", secondary="#3d6fb5", accent="#a86b00",
        foreground="#0f1b26", background="#f6f9fb", surface="#ffffff", panel="#e6eef4",
        success="#2f855a", warning="#a86b00", error="#c53030",
    ),
}


def use_theme(app: Any, dark: bool) -> None:
    """Give `app` the site's dark or light theme. Safe to call before the app runs and while it does."""
    from textual.theme import Theme

    spec = THEMES[bool(dark)]
    if spec["name"] not in app.available_themes:
        app.register_theme(Theme(**spec))
    app.theme = spec["name"]


async def start(
    write: Callable[[str], None],
    width: int = 120,
    height: int = 36,
    on_exit: Callable[[str], None] | None = None,
    home: str | Path = HOME,
    dark: bool = True,
) -> Session:
    """Seed the demo catalog and start the app on it, drawing through `write`.

    Returns once the app is running; the app itself carries on as a task on the event loop,
    which in a Web Worker is the worker's own. `on_exit` is told why the app stopped - empty
    for a quit, a line of text for a crash - so the page can offer to start it again.
    """
    install_shims()
    from icelens.app import IcelensApp
    from icelens.catalog import scrub
    from icelens.format import safe

    lake, table = demo_lake(home)
    driver = type("BoundBrowserDriver", (BrowserDriver,), {"sink": staticmethod(write)})
    # Rich picks its colours from the environment the app copies when it is made, and a Web
    # Worker has no COLORTERM: left alone, the themes reach xterm.js mapped onto its 16 ANSI
    # colours. xterm.js draws 24-bit colour, so the app is told so - and only this app.
    saved = os.environ.get("COLORTERM")
    os.environ["COLORTERM"] = "truecolor"
    try:
        app = IcelensApp(lake, ident=table)
        app.driver_class = driver
        use_theme(app, dark)
    except Exception:
        lake.close()
        raise
    finally:
        if saved is None:
            os.environ.pop("COLORTERM", None)
        else:
            os.environ["COLORTERM"] = saved

    def why(exc: BaseException) -> str:
        return safe(scrub(f"{type(exc).__name__}: {exc}"))

    async def run() -> None:
        reason = ""
        try:
            await app.run_async(size=(width, height))
            # Textual catches what the app raises, prints it, and returns as if from a quit.
            failed = getattr(app, "_exception", None)
            if failed is not None:
                reason = why(failed)
            elif app.return_code:
                reason = f"the app stopped with code {app.return_code}"
        except Exception as exc:  # noqa: BLE001 - reported to the page in words, never a traceback
            reason = why(exc)
        finally:
            lake.close()
            if on_exit is not None:
                on_exit(reason or (str(app.return_value) if app.return_value else ""))

    task = asyncio.get_running_loop().create_task(run(), name="icelens")
    # Let the app get as far as its first frame before the page is told it is running.
    for _ in range(100):
        if driver.current is not None or task.done():
            break
        await asyncio.sleep(0.02)
    return Session(app, driver, task)


async def first_frame(width: int, height: int, home: str | Path, dark: bool = True) -> str:
    """What the app draws first on a fresh demo, as the terminal bytes that draw it.

    The site shows this, dimmed, the moment the demo page opens, while Python is still loading:
    the build captures it once (`python -m icelens.browser first-frame`) so a visitor sees the
    real screen in a second rather than a progress bar for six.
    """
    out: list[str] = []
    session = await start(out.append, width, height, home=home, dark=dark)
    loop = asyncio.get_running_loop()
    # Done is the table's pane loaded, nothing still loading, and then quiet - not quiet alone.
    # The catalog is read in workers, and a pause before one of them longer than the wait used to
    # end the capture on a half-drawn screen: the tree, and "no table open" where the overview goes.
    app = session.app
    deadline, seen, quiet = loop.time() + 30, -1, 0
    while loop.time() < deadline and quiet < 2:
        await asyncio.sleep(0.25)
        loaded = bool(app._cache) or app.ident is None
        idle = loaded and all(worker.is_finished for worker in app.workers)
        quiet = quiet + 1 if idle and len(out) == seen else 0
        seen = len(out)
    session.exit()
    await asyncio.wait_for(session.task, 10)
    # Everything up to the hand-back: the page keeps showing the frame, so it must not leave
    # the alternate screen it drew in.
    frame = "".join(out)
    return frame[: frame.rfind(_LEAVE)] if _LEAVE in frame else frame


async def frames(width: int, height: int, home: str | Path) -> dict[str, Any]:
    """The preview the site shows: the first frame in each of the page's themes."""
    return {
        "cols": width,
        "rows": height,
        "ansi": await first_frame(width, height, home, dark=True),
        "light": await first_frame(width, height, home, dark=False),
    }


def main(argv: list[str] | None = None) -> int:
    """`python -m icelens.browser first-frame [WIDTH HEIGHT]`: JSON for the site's preview."""
    import json
    import shutil
    import tempfile

    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "first-frame":
        print("usage: python -m icelens.browser first-frame [WIDTH HEIGHT]", file=sys.stderr)
        return 2
    width, height = (int(args[1]), int(args[2])) if len(args) >= 3 else (120, 36)
    # The page's first frame is drawn before DuckDB has loaded, so rows come from PyIceberg and
    # the status bar says so. Captured with DuckDB in reach, the preview would claim otherwise.
    sys.modules["duckdb"] = None  # type: ignore[assignment]
    # Under /tmp, not the system's temporary directory: the table's location is on screen and the
    # frame is published, so $TMPDIR would put the build machine's own folders on the website.
    # A fresh directory each time, because a catalog opened earlier in this process may still hold
    # a connection to the last one's SQLite file.
    Path(FRAME_HOME).mkdir(parents=True, exist_ok=True)
    home = tempfile.mkdtemp(dir=FRAME_HOME)
    try:
        print(json.dumps(asyncio.run(frames(width, height, home))))
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

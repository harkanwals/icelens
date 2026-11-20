"""The website's screenshot: icelens on the demo table, drawn as HTML, with where its labels go.

`python scripts/screenshot.py [WIDTH HEIGHT]` prints JSON for `build_site.py`: the screen in the
site's dark and light themes, and the row and column of the text each label points at. It runs
the real app, headless, on a fresh demo sandbox, so the picture is the app as it is today; a
label whose text is no longer on screen fails the build rather than pointing at the wrong thing.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

#: Where the demo sandbox is made. The Overview pane shows the table's location, and the page is
#: published: a fixed path under /tmp keeps the build machine's own folders off the website.
HOME = Path("/tmp/icelens-screenshot")

#: What the screenshot labels: text the screen must show, whether the label goes just before it
#: or just after the phrase it starts, and what the label says.
CALLOUTS = (
    ("▼ sandbox 'demo'", "after", "The catalog: namespaces and their tables. <kbd>/</kbd> filters them, <kbd>ctrl+p</kbd> finds any table."),
    ("1 Overview", "before", "Ten panes, one key each: <kbd>1</kbd>&ndash;<kbd>9</kbd> and <kbd>0</kbd>. In Snapshots, <kbd>Enter</kbd> pins one and every pane re-reads as of it."),
    ("read through", "after", "Where these numbers came from: which engine read them."),
    ("property", "before", "The pane. Arrow keys move; <kbd>x</kbd> exports it to CSV, <kbd>c</kbd> copies a cell."),
    ("Table metadata", "before", "The guide rail: what the thing under the cursor is in Iceberg. <kbd>g</kbd> shortens it or hides it."),
    ("sandbox demo (", "before", "The status bar: which catalog this is, and how it is read. Under it, the keys that work here; <kbd>?</kbd> lists them all."),
)

#: The site's two themes, as Textual themes, so the screenshot matches the page around it.
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


def places(lines: list[str], callouts: tuple[tuple[str, str, str], ...] = CALLOUTS) -> list[list[float]]:
    """Each label's centre as (row, column), in the screen's character cells."""
    from rich.cells import cell_len

    found_at = []
    for needle, side, _ in callouts:
        # Not the title bar's row: it repeats the catalog's name.
        found = next(((row, line) for row, line in enumerate(lines) if row and needle in line), None)
        if found is None:
            raise RuntimeError(f"the screenshot no longer shows {needle!r}, which a label points at")
        row, line = found
        at = line.index(needle)
        col = cell_len(line[:at])
        # In the border before the text, or past the end of the phrase it starts: words with
        # single spaces between them, so "read through" is placed after "read through duckdb".
        phrase = re.match(r"\S+(?: \S+)*", line[at:])
        end = col + cell_len(phrase.group(0) if phrase else needle)
        found_at.append([row, col - 1.2 if side == "before" else end + 1.6])
    return found_at


async def capture(width: int, height: int, home: Path, dark: bool) -> dict[str, Any]:
    """The demo table's first screen, as `icelens --demo` opens it."""
    from rich.console import Console
    from textual.theme import Theme

    from icelens import demo
    from icelens.app import IcelensApp
    from icelens.catalog import Lake, sandbox_target

    lake = Lake(sandbox_target("demo", home), read_only=True)
    table = demo.seed(lake).table
    app = IcelensApp(lake, ident=table)
    spec = THEMES[dark]
    app.register_theme(Theme(**spec))
    app.theme = spec["name"]
    try:
        async with app.run_test(size=(width, height)) as pilot:
            # Loaded is the table's pane drawn and nothing still loading: the catalog is read in
            # worker threads, and a fixed wait ends on a half-drawn screen on a busy machine.
            for _ in range(240):
                await pilot.pause(0.05)
                if app._cache and all(worker.is_finished for worker in app.workers):
                    break
            await pilot.pause(0.2)
            console = Console(
                width=width, height=height, file=io.StringIO(), force_terminal=True,
                color_system="truecolor", record=True, legacy_windows=False, safe_box=False,
            )
            console.print(app.screen._compositor.render_update(full=True))
            lines = console.export_text(clear=False).splitlines()
            html = console.export_html(code_format="<style>{stylesheet}</style><pre>{code}</pre>")
    finally:
        lake.close()
    return {"html": html, "text": "\n".join(lines), "places": places(lines), "notes": [n for _, _, n in CALLOUTS]}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    width, height = (int(args[0]), int(args[1])) if len(args) >= 2 else (120, 36)
    # The same picture on every machine: rows read by PyIceberg, whether or not DuckDB's iceberg
    # extension happens to be installed where the site is built.
    sys.modules["duckdb"] = None  # type: ignore[assignment]

    async def both() -> dict[str, Any]:
        shots = {}
        for name, dark in (("dark", True), ("light", False)):
            shutil.rmtree(HOME, ignore_errors=True)
            shots[name] = await capture(width, height, HOME, dark)
        return shots

    try:
        print(json.dumps({"cols": width, "rows": height, **asyncio.run(both())}))
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The website's screenshots: a walk through the demo table, drawn as HTML, with labels.

`python scripts/screenshot.py [WIDTH HEIGHT]` prints JSON for `build_site.py`: each scene of the
walk in the site's dark and light themes, and the row and column of the text each label points
at. It runs the real app, headless, on a fresh demo sandbox, pressing the keys a person would, so
the pictures are the app as it is today; a label whose text is no longer on screen fails the
build rather than pointing at the wrong thing.
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

Callout = tuple[str, str, str]

#: The walk. Each scene is the keys pressed to reach it from the one before, and its labels: text
#: the screen must show, where the label goes (just before it, just after the phrase it starts,
#: or at the start of its row, where columns are too close together for a label between them),
#: and what the label says.
SCENES: tuple[dict[str, Any], ...] = (
    dict(name="overview", title="A table", keys=(), callouts=(
        ("▼ sandbox 'demo'", "after", "The catalog: namespaces and their tables. <kbd>/</kbd> filters them, <kbd>ctrl+p</kbd> finds any table."),
        ("1 Overview", "before", "Ten panes, one key each: <kbd>1</kbd>&ndash;<kbd>9</kbd> and <kbd>0</kbd>."),
        ("read through", "after", "Where these numbers came from: which engine read them."),
        ("property", "before", "The pane. Arrow keys move; <kbd>x</kbd> exports it to CSV, <kbd>c</kbd> copies a cell."),
        ("Table metadata", "before", "The guide rail: what the thing under the cursor is in Iceberg. <kbd>g</kbd> shortens it or hides it."),
        ("sandbox demo (", "before", "The status bar: which catalog this is, and how it is read. Under it, the keys that work here; <kbd>?</kbd> lists them all."),
    )),
    dict(name="history", title="Its history", keys=("4", "comma", "comma"), callouts=(
        ("snapshots - ◆", "after", "Every commit the table has had, oldest first. &#9670; is the current one."),
        ("overwrite", "row", "<kbd>,</kbd> and <kbd>.</kbd> step through history, and <kbd>Enter</kbd> pins a commit. Here, two steps back: the overwrite."),
        ("parent", "after", "<kbd>Enter</kbd> on a parent cell steps back to that commit."),
    )),
    dict(name="as-of", title="As it was", keys=("3",), callouts=(
        ("as of snapshot", "after", "Every pane now reads as of the pinned commit: these are the rows as they were then. <kbd>u</kbd> comes back to now."),
        ("WHERE", "before", "<kbd>w</kbd> filters the rows with a WHERE clause."),
    )),
    dict(name="commit", title="One commit", keys=("5",), callouts=(
        ("snapshot ", "before", "What the pinned commit did: its operation, when, and the rows it added and removed."),
        ("ADDED", "before", "The files it wrote&hellip;"),
        ("DELETED", "before", "&hellip;and the files it retired. They stay in storage, which is why the earlier commits can still be read."),
    )),
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


def places(lines: list[str], callouts: tuple[Callout, ...]) -> list[list[float]]:
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
        if side == "row":
            # The first text inside the pane's border: "││" is where the catalog meets the pane.
            box = line.rfind("││", 0, at)
            start = box + 2 + len(line[box + 2:]) - len(line[box + 2:].lstrip()) if box >= 0 else at
            found_at.append([row, cell_len(line[:start]) - 1.2])
            continue
        phrase = re.match(r"\S+(?: \S+)*", line[at:])
        end = col + cell_len(phrase.group(0) if phrase else needle)
        found_at.append([row, col - 1.2 if side == "before" else end + 1.6])
    return found_at


async def capture(width: int, height: int, home: Path, scenes: tuple[dict[str, Any], ...] = SCENES) -> list[dict[str, Any]]:
    """Each scene of the walk, in both themes: one session, so both show the same snapshot ids."""
    from rich.console import Console
    from textual.theme import Theme

    from icelens import demo
    from icelens.app import IcelensApp
    from icelens.catalog import Lake, sandbox_target

    lake = Lake(sandbox_target("demo", home), read_only=True)
    app = IcelensApp(lake, ident=demo.seed(lake).table)
    for spec in THEMES.values():
        app.register_theme(Theme(**spec))
    app.theme = THEMES[True]["name"]
    shots = []
    try:
        async with app.run_test(size=(width, height)) as pilot:
            async def settled() -> None:
                # Loaded is the pane drawn and nothing still loading: the catalog is read in
                # worker threads, and a fixed wait ends on a half-drawn screen on a busy machine.
                for _ in range(240):
                    await pilot.pause(0.05)
                    if app._cache and all(worker.is_finished for worker in app.workers):
                        break
                await pilot.pause(0.2)

            def draw() -> tuple[list[str], str]:
                console = Console(
                    width=width, height=height, file=io.StringIO(), force_terminal=True,
                    color_system="truecolor", record=True, legacy_windows=False, safe_box=False,
                )
                console.print(app.screen._compositor.render_update(full=True))
                return (console.export_text(clear=False).splitlines(),
                        console.export_html(code_format="<style>{stylesheet}</style><pre>{code}</pre>"))

            await settled()
            for scene in scenes:
                for key in scene["keys"]:
                    await pilot.press(key)
                    await settled()
                shot: dict[str, Any] = {k: scene[k] for k in ("name", "title", "keys")}
                for theme, dark in (("dark", True), ("light", False)):
                    app.theme = THEMES[dark]["name"]
                    await pilot.pause(0.2)
                    lines, html = draw()
                    shot[theme] = {"html": html, "text": "\n".join(lines), "places": places(lines, scene["callouts"])}
                app.theme = THEMES[True]["name"]
                shot["notes"] = [note for _, _, note in scene["callouts"]]
                shots.append(shot)
    finally:
        lake.close()
    return shots


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    width, height = (int(args[0]), int(args[1])) if len(args) >= 2 else (120, 36)
    # The same picture on every machine: rows read by PyIceberg, whether or not DuckDB's iceberg
    # extension happens to be installed where the site is built.
    sys.modules["duckdb"] = None  # type: ignore[assignment]

    shutil.rmtree(HOME, ignore_errors=True)
    try:
        print(json.dumps({"cols": width, "rows": height, "scenes": asyncio.run(capture(width, height, HOME))}))
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

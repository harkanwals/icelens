"""Screens as text, compared against the copies in `tests/snapshots/`.

A snapshot is what the terminal shows, as plain text: the layout, the labels, what is cut off
and where. Colours are not in it, deliberately - a text file diffs line by line in review, and
the question a snapshot answers is "did this screen change", not "is it still teal".

`pytest --snapshot-update` rewrites the copies; review the diff before committing it.
"""

from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console
from textual.app import App

HERE = Path(__file__).parent / "snapshots"


def screen_text(app: App) -> str:
    """The current screen as the terminal would show it, trailing spaces trimmed.

    Textual's own `export_screenshot` does exactly this and then writes SVG; this stops at text.
    """
    width, height = app.size
    console = Console(
        width=width,
        height=height,
        file=io.StringIO(),
        force_terminal=True,
        color_system=None,
        record=True,
        legacy_windows=False,
        safe_box=False,
    )
    console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
    return "\n".join(line.rstrip() for line in console.export_text().splitlines()) + "\n"


def assert_matches(name: str, text: str, update: bool) -> None:
    path = HERE / f"{name}.txt"
    if update:
        HERE.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return
    if not path.exists():
        raise AssertionError(f"no snapshot {path.name} yet: run pytest --snapshot-update and review it")
    expected = path.read_text(encoding="utf-8")
    if text != expected:
        import difflib

        diff = "".join(difflib.unified_diff(expected.splitlines(True), text.splitlines(True), "snapshot", "screen"))
        raise AssertionError(f"{path.name} changed; if that was intended, run pytest --snapshot-update\n{diff}")

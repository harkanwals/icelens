"""The website's walk-through replays the real app: the same keys, the same screens.

The page has no Python. It shows screens `scripts/walk.py` drew at build time and follows a table
of which key leads where, worked out from the app's own handlers. These tests press the same keys
in the real app and check that every screen it draws is the one the page would show.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

from icelens import demo
from icelens.app import IcelensApp
from icelens.catalog import Lake, sandbox_target

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "walk.py").is_file():
    pytest.skip("the website's scripts are not in this source tree", allow_module_level=True)
sys.path.insert(0, str(ROOT / "scripts"))

import walk  # noqa: E402

# The walk is conftest's walk_graph: four panes (Data is the one that scrolls down as well as
# across), every commit, twenty rows - past the Data pane's height, so it scrolls.
PILOT = {"ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right", "Enter": "enter",
         ",": "comma", ".": "full_stop", "u": "u", "1": "1", "3": "3", "4": "4", "5": "5", "?": "question_mark",
         "Escape": "escape", "Tab": "tab", "PageDown": "pagedown", "PageUp": "pageup", "Home": "home", "End": "end"}

Screen = list[list[tuple[str, str]]]


@pytest.fixture(scope="module", autouse=True)
def rows_as_the_site_reads_them() -> Any:
    # The walk is drawn with PyIceberg reading the rows, as scripts/walk.py does: in file order.
    # DuckDB reads files in parallel, and the same table comes back in another order each run.
    with pytest.MonkeyPatch.context() as patch:
        patch.setitem(sys.modules, "duckdb", None)
        yield


@pytest.fixture(scope="module")
def home(walk_home: Path) -> Path:
    return walk_home


@pytest.fixture(scope="module")
def graph(walk_graph: dict[str, Any]) -> dict[str, Any]:
    # The real app below opens this same sandbox, so both show the same ids and times.
    return walk_graph


def styled(rows: list[list[list[Any]]], styles: list[str]) -> Screen:
    """A screen as what it looks like: text, and the CSS each run of it is drawn in.

    A blank cell's text colour cannot be seen unless it is underlined or struck through, and
    Textual sets one on some blanks and not on others; it is left out of the comparison.
    """
    out = []
    for row in rows:
        runs: list[tuple[str, str]] = []
        for text, style in row:
            for ch in text:
                css = styles[style]
                if ch == " " and "text-decoration: " not in css:
                    css = "; ".join(p for p in css.split("; ") if not p.startswith(("color:", "text-decoration-color:")))
                if runs and runs[-1][1] == css:
                    runs[-1] = (runs[-1][0] + ch, css)
                else:
                    runs.append((ch, css))
        out.append(runs)
    return out


async def real_screens(home: Path, keys: list[Any], *, toasts: bool = False) -> list[Screen]:
    """What the real app draws after each key, from where the walk starts: a grid, focused."""
    lake = Lake(sandbox_target("demo", home), read_only=True)
    app = IcelensApp(lake, ident=demo.seed(lake).table)
    from textual.theme import Theme

    app.register_theme(Theme(**walk.THEME))
    app.theme = walk.THEME["name"]
    screens = walk.Screens(120, 36)
    out = []

    def drawn() -> Screen:
        return styled([screens.lines[i] for i in screens.draw(app)], screens.styles)

    try:
        async with app.run_test(size=(120, 36), notifications=True) as pilot:
            async def settled() -> None:
                for _ in range(400):
                    await pilot.pause(0.02)
                    if app._cache and all(worker.is_finished for worker in app.workers):
                        break
                await pilot.pause()

            await settled()
            await pilot.press("tab", "tab")  # the tree, then the tab strip, then the grid
            await settled()
            out.append(drawn())
            for key in keys:
                if isinstance(key, tuple):
                    _, x, y = key
                    # A mouse moves onto what it clicks, and stays there, as a visitor's does.
                    await pilot.hover(offset=(x, y))
                    await pilot.click(offset=(x, y))
                else:
                    await pilot.press(PILOT[key])
                await settled()
                await pilot.wait_for_scheduled_animations()  # at rest, as a visitor sees it
                if not toasts:
                    # Toasts come and go on a timer; these tests are about the screens beneath.
                    app.clear_notifications()
                    await settled()
                out.append(drawn())
    finally:
        lake.close()
    return out


def replay(graph: dict[str, Any], keys: list[Any], *, toasts: bool = False) -> list[Screen]:
    """What the page draws after each key, by its rule: walk.step, then walk.compose."""
    at, helping, shown, hover, mouse = graph["start"], None, [], None, None
    out = [styled(walk.compose(graph, at), graph["styles"])]
    for key in keys:
        before = at
        if helping is not None:
            if key in ("Escape", "?"):
                helping = None
            elif key in graph["help"]["next"]:
                helping = graph["help"]["next"][key][helping]
        elif key == "?":
            helping = 0
        elif isinstance(key, tuple):
            _, x, y = key
            hover, mouse = walk.hover_at(graph, at, y, x), (x, y)  # the mouse has moved onto it
            done = walk.click(graph, at, y, x)
            if done[0] == "key" and done[1] == "?":
                helping = 0
            elif done[0] == "key":
                at, note = walk.step(graph, at, done[1])
                if note is not None and toasts:
                    shown.append(note)
            elif done[0] == "state":
                at = done[1]
                if done[2] is not None and toasts:
                    shown.append(done[2])
        else:
            at, note = walk.step(graph, at, key)
            if note is not None and toasts:
                shown.append(note)
        hover = walk.hovered(graph, before, at, hover, None if isinstance(key, tuple) else key)
        if helping is not None:
            # A modal takes the mouse's hover away, and nothing is hovered again until it moves.
            hover, mouse = None, None
        out.append(styled(walk.compose(graph, at, help=helping, toasts=shown, hover=hover, mouse=mouse), graph["styles"]))
    return out


def first_difference(real: list[tuple[str, str]], page: list[tuple[str, str]]) -> str:
    cells = lambda runs: [(ch, css) for text, css in runs for ch in text]  # noqa: E731
    for col, (a, b) in enumerate(zip(cells(real), cells(page), strict=True)):
        if a != b:
            return f"column {col} {a[0]!r}: real {a[1]!r}, page {b[1]!r}"
    return "none"


def same(real: list[Screen], page: list[Screen], keys: list[Any]) -> None:
    for step, (a, b) in enumerate(zip(real, page, strict=True)):
        if a == b:
            continue
        rows = [(n, x, y) for n, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
        text = lambda runs: "".join(t for t, _ in runs)  # noqa: E731
        raise AssertionError(f"after {keys[max(0, step - 3):step]} (step {step}), the page draws a different screen:\n" + "\n".join(
            f"row {n} real: {text(x)}\nrow {n} page: {text(y)}" + (
                "" if text(x) != text(y) else "\n  same text, other styles: " + first_difference(x, y))
            for n, x, y in rows[:6]
        ))


async def test_the_tour_and_time_travel_show_what_the_app_shows(graph: dict[str, Any], home: Path) -> None:
    keys = ["ArrowDown", "ArrowRight", "4", ",", ",", "ArrowRight", "Enter", "5", "ArrowDown", "u", "1", ".", "4", "ArrowDown", "Enter"]
    same(await real_screens(home, keys), replay(graph, keys), keys)


async def test_a_random_walk_shows_what_the_app_shows(graph: dict[str, Any], home: Path) -> None:
    rng = random.Random(7)
    keys = [rng.choice(["ArrowUp", "ArrowDown", "ArrowDown", "ArrowLeft", "ArrowRight", "ArrowRight",
                        "Enter", "1", "4", "5", ",", ".", "u"]) for _ in range(60)]
    same(await real_screens(home, keys), replay(graph, keys), keys)


def test_the_page_can_read_the_walk(graph: dict[str, Any]) -> None:
    # Every state is a full screen of known lines, and every key leads to a state that exists.
    n = len(graph["states"])
    assert n == len(graph["move"]) == len(graph["fresh"]) == len(graph["group"]) == len(graph["enter_note"])
    for ids, move, note in zip(graph["states"], graph["move"], graph["enter_note"], strict=True):
        assert len(ids) == 36 and all(0 <= i < len(graph["lines"]) for i in ids)
        assert len(move) == len(walk.MOVES) and all(-1 <= j < n for j in move)
        assert -1 <= note < len(graph["notes"])
    assert len(graph["tint"]) <= len(graph["styles"]) and all(0 <= s < len(graph["styles"]) for s in graph["tint"])
    for pane in graph["groups"]:
        assert all(0 <= j < n for nxt in pane["next"] for j in nxt.values())
        assert all(0 <= k < len(graph["notes"]) for k in pane["toast"].values())
    json.dumps(graph)


def test_a_label_whose_text_is_gone_fails_the_capture() -> None:
    with pytest.raises(RuntimeError, match="no longer shows 'no such text'"):
        walk.places(["title", "a line on screen"], (("no such text", "after", "x"),))


def test_a_row_label_sits_at_the_start_of_its_row_inside_the_pane() -> None:
    lines = ["title", "│ catalog ││ 3  │  overwrite  123"]
    [(row, col)] = walk.places(lines, (("overwrite", "row", "x"),))
    assert (row, col) == (1, lines[1].index("3") - 1.2)


async def test_walking_back_across_a_wide_grid_keeps_the_scroll_the_app_keeps(graph: dict[str, Any], home: Path) -> None:
    # A grid wider than its pane scrolls to keep the cursor in view, and stays scrolled when the
    # cursor comes back until it would leave the view: the screen depends on the way there.
    keys = ["4", *["ArrowRight"] * 7, *["ArrowLeft"] * 6, "ArrowDown", *["ArrowLeft"] * 2,
            # then away and back, and a commit pinned while scrolled: each refills the grid
            *["ArrowRight"] * 6, "1", "4", *["ArrowRight"] * 6, ",", "ArrowLeft", "1", ".", "4", "ArrowRight"]
    same(await real_screens(home, keys), replay(graph, keys), keys)


async def test_help_opens_over_the_screen_it_was_opened_from(graph: dict[str, Any], home: Path) -> None:
    # A modal: the screen beneath stays, tinted, and keys other than the ones that close it do
    # nothing until it is closed.
    keys = ["?", "Escape", "4", "ArrowDown", "?", "ArrowDown", "ArrowDown", ",", "PageDown", "ArrowRight", "5",
            "End", "ArrowUp", "Home", "?", "ArrowDown", "?", "Escape"]
    same(await real_screens(home, keys), replay(graph, keys), keys)


async def test_what_the_app_says_shows_as_its_own_toasts_stacked(graph: dict[str, Any], home: Path) -> None:
    keys = ["4", ",", ",", "ArrowDown", "Enter", "u"]
    same(await real_screens(home, keys, toasts=True), replay(graph, keys, toasts=True), keys)


async def test_scrolling_down_a_long_grid_while_the_guide_changes_height_shows_what_the_app_shows(
    graph: dict[str, Any], home: Path,
) -> None:
    # The guide's height is the grid's: a longer entry shortens the grid, which scrolls less far.
    keys = ["3", *["ArrowDown"] * 18, *["ArrowRight"] * 4, *["ArrowUp"] * 9, *["ArrowLeft"] * 3, *["ArrowDown"] * 6,
            ",", *["ArrowDown"] * 4, "u", "4", "3", *["ArrowRight"] * 6]
    same(await real_screens(home, keys), replay(graph, keys), keys)


def at_after(graph: dict[str, Any], keys: list[Any]) -> int:
    at = graph["start"]
    for key in keys:
        if isinstance(key, tuple):
            done = walk.click(graph, at, key[2], key[1])
            at = done[1] if done[0] == "state" else walk.step(graph, at, done[1])[0] if done[0] == "key" else at
        else:
            at = walk.step(graph, at, key)[0]
    return at


def on_cell(graph: dict[str, Any], keys: list[Any], row: int, column: int) -> tuple[str, int, int]:
    """A click on the middle of a visible cell of the grid, as the walk has it laid out."""
    y0, r0, _n, columns = graph["layouts"][graph["layout"][at_after(graph, keys)]]["cells"]
    # A negative column counts from the right of what is shown: the last may be cut off.
    x0, x1 = columns[column][:2] if column < 0 else next((a, b) for a, b, c, _ in columns if c == column)
    return ("click", (x0 + x1) // 2, y0 + row - r0)


def on_tab(graph: dict[str, Any], keys: list[Any], pane: str) -> tuple[str, int, int]:
    y, _x0, _x1, tabs = graph["layouts"][graph["layout"][at_after(graph, keys)]]["tabs"]
    x0, x1 = next((a, b) for a, b, name in tabs if name == pane)
    return ("click", (x0 + x1) // 2, y)


def on_footer(graph: dict[str, Any], keys: list[Any], key: str) -> tuple[str, int, int]:
    y, footer = graph["layouts"][graph["layout"][at_after(graph, keys)]]["footer"]
    x0, x1 = next((a, b) for a, b, name in footer if name == key)
    return ("click", x0 + 1, y)


async def test_clicks_do_what_they_do_in_the_app(graph: dict[str, Any], home: Path) -> None:
    keys: list[Any] = []
    for step in (
        lambda k: on_cell(graph, k, 3, 1),           # a cell: the cursor goes there
        lambda k: "ArrowDown",
        lambda k: on_cell(graph, k, 4, 1),           # the cursor's own cell: Enter, which does nothing here
        lambda k: on_tab(graph, k, "snapshots"),     # a pane's name: it opens, the focus on the names
        lambda k: "ArrowRight",                      # which move along the strip
        lambda k: "ArrowLeft",
        lambda k: on_cell(graph, k, 2, 0),           # a cell, from the names: the grid takes the focus
        lambda k: on_cell(graph, k, 2, 0),           # the cursor's cell: Enter pins that commit
        lambda k: on_footer(graph, k, "u"),          # a footer key is pressed
        lambda k: on_tab(graph, k, "overview"),
        lambda k: "Tab",                             # into the pane's grid
        lambda k: on_tab(graph, k, "data"),
        lambda k: on_cell(graph, k, 0, 0),
        lambda k: on_cell(graph, k, 5, 3),
        lambda k: on_cell(graph, k, 9, -1),          # part shown at the edge: scrolled into view
        lambda k: "ArrowLeft",                       # the hover stays where the mouse left it
        lambda k: "ArrowUp",
        lambda k: "4",                               # another grid: no hover until the mouse moves
        lambda k: on_cell(graph, k, 1, 2),
        lambda k: ".",                               # a refill: the hover goes back to the first cell
        lambda k: on_tab(graph, k, "commit"),
        lambda k: ",",                               # the names keep the focus through a travel
        lambda k: on_footer(graph, k, "question_mark"),
        lambda k: "Escape",
    ):
        keys.append(step(keys))
    same(await real_screens(home, keys), replay(graph, keys), keys)


def test_the_two_themes_must_draw_the_same_walk(graph: dict[str, Any]) -> None:
    # The page changes theme in the middle of a walk without moving, so the light walk must be
    # the dark one in other colours; a light walk that reads differently is refused.
    same_walk = {key: graph[key] for key in ("move", "group", "fresh", "cursor", "layout", "enter_note", "notes", "start")}
    light = same_walk | {key: graph[key] for key in walk.SCREENS}
    assert walk.merged(graph, light)["light"]["states"] == graph["states"]
    lines = [[[text.replace("snapshot", "SNAPSHOT"), style] for text, style in line] for line in graph["lines"]]
    with pytest.raises(RuntimeError, match="reads differently in the light theme"):
        walk.merged(graph, light | {"lines": lines})
    with pytest.raises(RuntimeError, match="'move' differ"):
        walk.merged(graph, light | {"move": [[-1] * len(walk.MOVES)] * len(graph["move"])})

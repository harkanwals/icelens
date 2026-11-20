"""The website's walk-through: every screen of icelens on the demo table, and what leads between them.

`python scripts/walk.py [WIDTH HEIGHT]` prints JSON for `build_site.py`. It runs the real app,
headless, on a demo sandbox, in the site's dark and light themes at once, and draws every state a
visitor can reach: each pane, each cell of each grid and each way it can be scrolled there (the
guide follows the cursor), each commit pinned, the pane names focused, the help screen at each
offset over whatever it was opened from, and the app's own toasts. The page replays them; it runs
no Python.

What leads where comes from the app and from Textual, not from a model of them: Enter from the
app's cell handler, `,` `.` and `u` from its actions with what they would change recorded instead
of done, each scroll from Textual's own geometry, Tab from the app's focus chain, the hover colours
from hovering the real widgets. Keys and clicks the app answers in ways the walk does not follow
(typing, the tree, the SQL editor) are marked, and the page says so instead of pretending.
`tests/test_walk.py` presses the same keys and clicks in the real app and checks that every
screen, every style of every cell, is the one the page draws.

Screens are stored as lines of styled text, each distinct line once: most of a screen is the
same from one state to the next, so thousands of screens come to a small file.
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
HOME = Path("/tmp/icelens-walk")

#: How far down a grid the cursor may go. Only Data is longer, and its first rows make the point.
ROW_LIMIT = 30

#: The site's two themes as Textual themes: the page's terminal follows the page.
THEME: dict[str, Any] = dict(
    name="icelens-dark", dark=True, primary="#6fd6f7", secondary="#8ab4f8", accent="#f5d67f",
    foreground="#e4ecf2", background="#0b141c", surface="#111c26", panel="#172431",
    success="#7dd3a8", warning="#f5d67f", error="#f7768e",
)
LIGHT: dict[str, Any] = dict(
    name="icelens-light", dark=False, primary="#0a7ea4", secondary="#075f7c", accent="#a8641a",
    foreground="#0f1b26", background="#f6f9fb", surface="#ffffff", panel="#e6eef4",
    success="#1f8a5b", warning="#a8641a", error="#c2364a",
)
#: What differs between the two themes' walks: the screens. Every state, key and click is the same.
SCREENS = ("lines", "styles", "states", "tint", "hovered", "toasts", "help")

Callout = tuple[str, str, str]

#: The guided tour: chapters, each the keys pressed to reach it from the one before, and labels
#: on what it shows - text the screen must show, where the label goes (just before it, just after
#: the phrase it starts, or at the start of its row, where columns are too close for a label
#: between them), and what the label says.
TOUR: tuple[dict[str, Any], ...] = (
    dict(name="overview", title="A table", keys=(), callouts=(
        ("▼ sandbox 'demo'", "after", "The catalog: namespaces and their tables. <kbd>/</kbd> filters them, <kbd>ctrl+p</kbd> finds any table."),
        ("1 Overview", "before", "Ten panes, one key each: <kbd>1</kbd>&ndash;<kbd>9</kbd> and <kbd>0</kbd>."),
        ("read through", "after", "Where these numbers came from: which engine read them."),
        ("property", "before", "The pane. Arrow keys move the cursor; <kbd>x</kbd> exports it to CSV, <kbd>c</kbd> copies a cell."),
        ("guide", "after", "The guide: what the cell under the cursor is in Iceberg. It changes as the cursor moves."),
        ("sandbox demo (", "before", "The status bar: which catalog this is, and how it is read. Under it, the keys that work here."),
    )),
    dict(name="history", title="Its history", keys=("4", ",", ","), callouts=(
        ("snapshots - ◆", "after", "Every commit the table has had, oldest first. &#9670; is the current one."),
        ("overwrite", "row", "<kbd>,</kbd> and <kbd>.</kbd> step through history, and <kbd>Enter</kbd> pins a commit. Here, two steps back: the overwrite."),
        ("parent", "after", "<kbd>Enter</kbd> on a parent cell steps back to that commit."),
    )),
    dict(name="as-of", title="As it was", keys=("3",), callouts=(
        ("as of snapshot", "after", "Every pane now reads as of the pinned commit: these are the rows as they were then. <kbd>u</kbd> comes back to now."),
        ("WHERE", "before", "<kbd>w</kbd> filters the rows with a WHERE clause."),
    )),
    dict(name="commit", title="One commit", keys=("5", "ArrowDown"), callouts=(
        ("snapshot ", "before", "What the pinned commit did: its operation, when, and the rows it added and removed."),
        ("ADDED", "before", "The files it wrote&hellip;"),
        ("DELETED", "before", "&hellip;and the file it retired, under the cursor. The guide says what that means: it stays in storage, which is why earlier commits can still be read."),
    )),
)

#: The keys the page answers to, by their name in a browser's KeyboardEvent.key.
ARROWS = {"ArrowUp": (-1, 0), "ArrowDown": (1, 0), "ArrowLeft": (0, -1), "ArrowRight": (0, 1)}
#: The keys whose effect depends on the cell under the cursor, in the order the walk stores them.
MOVES = (*ARROWS, "Enter")
#: The footer's keys a click presses, by their names to Textual; the rest the page does not follow.
FOOTER_KEYS = {"question_mark": "?", "u": "u", "comma": ",", "full_stop": "."}
#: The keys the help screen's scrolling body answers, and their names to Textual.
HELP_KEYS = {"ArrowUp": "up", "ArrowDown": "down", "PageUp": "pageup", "PageDown": "pagedown",
             "Home": "home", "End": "end"}


def places(lines: list[str], callouts: tuple[Callout, ...]) -> list[list[float]]:
    """Each label's centre as (row, column), in the screen's character cells."""
    from rich.cells import cell_len

    found_at = []
    for needle, side, _ in callouts:
        # Not the title bar's row: it repeats the catalog's name.
        found = next(((row, line) for row, line in enumerate(lines) if row and needle in line), None)
        if found is None:
            raise RuntimeError(f"the walk no longer shows {needle!r}, which a label points at")
        row, line = found
        at = line.index(needle)
        col = cell_len(line[:at])
        if side == "row":
            # The first text inside the pane's border: "││" is where the catalog meets the pane.
            box = line.rfind("││", 0, at)
            start = box + 2 + len(line[box + 2:]) - len(line[box + 2:].lstrip()) if box >= 0 else at
            found_at.append([row, cell_len(line[:start]) - 1.2])
            continue
        # Past the end of the phrase it starts: words with single spaces between them, so
        # "read through" is placed after "read through pyiceberg".
        phrase = re.match(r"\S+(?: \S+)*", line[at:])
        end = col + cell_len(phrase.group(0) if phrase else needle)
        found_at.append([row, col - 1.2 if side == "before" else end + 1.6])
    return found_at


def scrolled(grid: Any, at: tuple[int, int], row: int, column: int) -> tuple[int, int]:
    """Where a grid is scrolled once its cursor moves to (row, column) from scroll `at`.

    What DataTable._scroll_cursor_into_view asks of Widget.scroll_to_region, worked out with
    Textual's own geometry instead of done: the scroll lands after a refresh, and waiting for one
    on every key from every state would take the build an hour. Which scroll a cell is seen at
    depends on the way there - back across a wide grid it stays scrolled - so the walk keeps the
    scroll in each state, and the tests check it against the real app.
    """
    from textual.coordinate import Coordinate
    from textual.geometry import Offset, Region, clamp

    window = grid.scrollable_content_region.at_offset(Offset(*at)).shrink(grid._get_fixed_offset())
    region = grid._get_cell_region(Coordinate(row, column))
    if window in region:
        return at
    delta = Region.get_scroll_to_visible(window, region, top=False)
    return clamp(at[0] + delta.x, 0, grid.max_scroll_x), clamp(at[1] + delta.y, 0, grid.max_scroll_y)


class Screens:
    """Screens as lines of (text, style) runs, each distinct style and line stored once."""

    def __init__(self, width: int, height: int) -> None:
        self.width, self.height = width, height
        self.styles: list[str] = []
        self.rich: list[Any] = []
        self.lines: list[list[list[Any]]] = []
        self._style_ids: dict[Any, int] = {}
        self._line_ids: dict[str, int] = {}

    def draw(self, app: Any) -> list[int]:
        from rich.cells import cell_len
        from rich.console import Console
        from rich.segment import Segment

        console = Console(
            width=self.width, height=self.height, file=io.StringIO(), force_terminal=True,
            color_system="truecolor", record=True, legacy_windows=False, safe_box=False,
        )
        # With the screens beneath, as the terminal gets it: a modal draws them, tinted.
        console.print(app.screen._compositor.render_update(full=True, screen_stack=app._background_screens))
        segments = list(console._record_buffer)
        ids = []
        for line in list(Segment.split_lines(segments))[: self.height]:
            runs: list[list[Any]] = []
            for segment in line:
                if segment.control:
                    continue
                if cell_len(segment.text) != len(segment.text):
                    # The page counts columns in characters, for clicks, labels and overlays.
                    raise RuntimeError(f"the walk draws {segment.text!r}, which is not one cell a character")
                style = self.style(segment.style)
                if runs and runs[-1][1] == style:
                    runs[-1][0] += segment.text
                else:
                    runs.append([segment.text, style])
            key = json.dumps(runs, ensure_ascii=False)
            if key not in self._line_ids:
                self._line_ids[key] = len(self.lines)
                self.lines.append(runs)
            ids.append(self._line_ids[key])
        return ids

    def style(self, style: Any) -> int:
        from rich.terminal_theme import DEFAULT_TERMINAL_THEME

        # By the style itself, not its CSS: two styles can look alike and tint apart beneath a
        # modal. Not by its meta, which carries each cell's coordinates.
        style = style.clear_meta_and_links() if style else None
        index = self._style_ids.setdefault(style, len(self._style_ids))
        if index == len(self.styles):
            self.styles.append(style.get_html_style(DEFAULT_TERMINAL_THEME) if style else "")
            self.rich.append(style)
        return index

    def tinted(self, color: Any) -> list[int]:
        """Each style as Textual draws it beneath a modal screen of this background colour."""
        from rich.segment import Segment
        from textual.renderables.background_screen import BackgroundScreen

        return [
            self.style(next(iter(BackgroundScreen.process_segments([Segment("x", style)], color))).style)
            for style in list(self.rich)
        ]

    def text(self, ids: list[int]) -> list[str]:
        return ["".join(run[0] for run in self.lines[i]) for i in ids]


async def capture(width: int = 120, height: int = 36, home: Path = HOME, *,
                  panes: tuple[str, ...] | None = None, row_limit: int = ROW_LIMIT,
                  theme: dict[str, Any] = THEME) -> dict[str, Any]:
    """Every state of the walk, and where each key leads from each one.

    `panes` and `row_limit` cut the walk down, for tests: only these panes, and fewer rows.
    """
    from collections import deque

    from textual.coordinate import Coordinate
    from textual.theme import Theme
    from textual.widgets import TabbedContent

    from icelens import demo, model
    from icelens.app import Grid, IcelensApp
    from icelens.catalog import Lake, sandbox_target

    lake = Lake(sandbox_target("demo", home), read_only=True)
    table = demo.seed(lake).table
    app = IcelensApp(lake, ident=table)
    app.register_theme(Theme(**theme))
    app.theme = theme["name"]
    # Every screen as the app comes to rest on it: the tab strip's underline slides, and a draw
    # in the middle of the slide is a screen the app only passes through.
    app.animation_level = "none"
    screens = Screens(width, height)
    states: list[dict[str, Any]] = []
    index: dict[tuple, int] = {}
    alias: dict[tuple, tuple] = {}
    layouts: list[dict[str, Any]] = []
    layout_ids: dict[str, int] = {}
    most: dict[tuple, tuple[int, int]] = {}
    into: dict[tuple[int, str], bool] = {}  # does Tab from a pane's name go into its grid  # how far each cell's grid can scroll, its guide shown
    notes: list[str] = []
    asked: dict[str, dict[str, Any]] = {}  # how the app raised each note: severity, timeout

    def add(key: tuple, ids: list[int], **extra: Any) -> int:
        index[key] = len(states)
        states.append({"key": key, "lines": ids, **extra})
        return index[key]

    try:
        # Notifications on: run_test turns them off, and the page shows the app's own toasts.
        async with app.run_test(size=(width, height), notifications=True) as pilot:
            async def settled() -> None:
                # Loaded is the pane drawn and nothing still loading: the catalog is read in
                # worker threads, and a fixed wait ends on a half-drawn screen on a busy machine.
                for _ in range(400):
                    await pilot.pause(0.02)
                    if app._cache and all(worker.is_finished for worker in app.workers):
                        break
                await pilot.pause()

            await settled()
            grid_panes = [p for p, _label, _key in app.panes if p not in ("sql", "writer")]
            if panes is not None:
                grid_panes = [p for p in grid_panes if p in panes]
            pane_keys = {key: p for p, _label, key in app.panes if p in grid_panes}
            # The pane names in the order the tab strip has them, and the ones the walk draws.
            from textual.widgets._tabbed_content import ContentTab as _Tab

            order = [str(t.id).removeprefix("--content-tab-tab-") for t in app.query(_Tab) if t.display]
            tab_panes = [p for p in order if p in grid_panes or (p == "sql" and panes is None)]
            tab_keys = {key: p for p, _label, key in app.panes if p in tab_panes}
            ids = [s.snapshot_id for s in model.ordered_snapshots(lake.table(table).metadata)]
            pinned: list[int | None] = [None, *ids]

            def pinned_row(ctx: int, pane: str) -> int:
                """Where a refilled grid puts its cursor: on the pin, in Snapshots."""
                sid = pinned[ctx]
                return ids.index(sid) if pane == "snapshots" and sid is not None and sid in ids else 0

            # What the app would do, recorded instead of done: travel() and notify() are swapped
            # for recorders while the app's own handler or action decides.
            async def would(action: Any) -> tuple[int | None, bool, str | None]:
                seen: dict[str, Any] = {}
                real_travel, real_notify, real_refresh = app.travel, app.notify, app.refresh_pane
                app.travel = lambda sid, note: seen.update(sid=sid, note=note)  # type: ignore[method-assign]
                def notify(message: Any, **options: Any) -> None:
                    seen.setdefault("note", str(message))
                    asked[str(message)] = options
                app.notify = notify  # type: ignore[method-assign]
                app.refresh_pane = lambda force=False: None  # type: ignore[method-assign]
                before = app.snapshot_id
                try:
                    result = action()
                    if asyncio.iscoroutine(result):
                        await result
                    moved = "sid" in seen or app.snapshot_id != before
                    sid = seen["sid"] if "sid" in seen else app.snapshot_id
                finally:
                    app.travel, app.notify, app.refresh_pane = real_travel, real_notify, real_refresh  # type: ignore[method-assign]
                    app.snapshot_id = before
                return sid, moved, seen.get("note")

            from textual.widgets import Footer
            from textual.widgets._footer import FooterKey
            from textual.widgets._tabbed_content import ContentTab, ContentTabs

            def interned(table: list[Any], ids: dict[str, int], value: Any) -> int:
                key = json.dumps(value)
                if key not in ids:
                    ids[key] = len(table)
                    table.append(value)
                return ids[key]

            def zones(grid: Any) -> int:
                """What a click can land on, as this screen is laid out: the grid's visible cells,
                the pane names, the footer's keys, and the other widgets a click would focus."""
                strip = app.query_one(ContentTabs)
                tabs = [[t.region.x, t.region.right, str(t.id).removeprefix("--content-tab-tab-")]
                        for t in strip.query(ContentTab) if t.display and t.region.width]
                footer = app.screen.query_one(Footer)
                keys = [[k.region.x, k.region.right, k.key] for k in footer.query(FooterKey) if k.region.width]
                others = [[w.region.y, w.region.bottom, w.region.x, w.region.right] for w in app.screen.focus_chain
                          if w is not grid and w is not strip and w.region.width]
                layout: dict[str, Any] = {
                    "tabs": [strip.region.y, strip.region.x, strip.region.right, tabs],
                    "footer": [footer.region.y, keys], "others": others, "pane": None,
                }
                if grid is None:
                    return interned(layouts, layout_ids, layout)
                sx, sy = round(grid.scroll_x), round(grid.scroll_y)
                window = grid.scrollable_content_region.shrink(grid._get_fixed_offset())
                origin = grid.content_region
                columns = []
                for c in range(len(grid.columns)):
                    region = grid._get_cell_region(Coordinate(0, c))
                    x0 = origin.x + region.x - sx
                    a, b = max(x0, window.x), min(x0 + region.width, window.right)
                    if a < b:
                        whole = x0 >= window.x and x0 + region.width <= window.right
                        columns.append([a, b, c, sx if whole else scrolled(grid, (sx, sy), 0, c)[0]])
                shown = [r for r in range(grid.row_count)
                         if window.y <= origin.y + grid._get_cell_region(Coordinate(r, 0)).y - sy < window.bottom]
                y0 = origin.y + grid._get_cell_region(Coordinate(shown[0], 0)).y - sy if shown else window.y
                box = grid.region
                layout.update(pane=[box.y, box.bottom, box.x, box.right], header=window.y - 1, view=[sx, sy],
                              cells=[y0, shown[0] if shown else 0, len(shown), columns])
                return interned(layouts, layout_ids, layout)

            def note(text: str | None) -> int | None:
                if text is None:
                    return None
                if text not in notes:
                    notes.append(text)
                return notes.index(text)

            travels: dict[int, dict[str, tuple[int | None, bool, int | None]]] = {}
            for ctx, sid in enumerate(pinned):
                app.snapshot_id = sid
                travels[ctx] = {}
                for key, action in ((",", lambda: app.action_step(-1)), (".", lambda: app.action_step(1)),
                                    ("u", app.action_untravel)):
                    target, moved, text = await would(action)
                    travels[ctx][key] = (target, moved, note(text))
                for pane in grid_panes:
                    app.clear_notifications()  # the screens beneath; toasts are drawn on their own
                    app.query_one("#tabs", TabbedContent).active = f"tab-{pane}"
                    await settled()
                    app.refresh_pane(force=False)
                    await settled()
                    grid = app.query_one(f"#grid-{pane}", Grid)
                    grid.focus()
                    await settled()
                    if grid.cursor_type != "cell":
                        raise RuntimeError(f"the {pane} grid moves a {grid.cursor_type} cursor; the walk follows cells")
                    home_row = pinned_row(ctx, pane)
                    rows, cols = min(grid.row_count, row_limit), len(grid.columns)
                    # How the pane arrives: refilled, so scrolled to the top and the cursor home.
                    landed = (home_row, 0, round(grid.scroll_x), round(grid.scroll_y))
                    selections: dict[tuple[int, int], tuple[int | None, bool, int | None]] = {}
                    # Arrived (the pane's own term stands in the guide until the cursor moves) and
                    # not (after a travel from a cell, the app has stopped explaining the pane).
                    todo = deque([(*landed, home_row == 0), (*landed, False)])
                    while todo:
                        r, c, sx, sy, fresh = todo.popleft()
                        wanted = (ctx, pane, "grid", r, c, fresh, sx, sy)
                        if wanted in index or wanted in alias:
                            continue
                        # The cell first, then its guide: the guide's height is the grid's, so
                        # how far the grid can scroll is only known once the guide has changed.
                        grid.cursor_coordinate = Coordinate(r, c)
                        await pilot.pause(0)
                        app._arrived = pane if fresh else None
                        app.update_guide()
                        await pilot.pause(0)
                        grid.scroll_to(sx, sy, animate=False, immediate=True)
                        await pilot.pause(0)
                        key = (ctx, pane, "grid", r, c, fresh, round(grid.scroll_x), round(grid.scroll_y))
                        if key != wanted:
                            alias[wanted] = key  # the scroll a move asked for, cut short by a shorter grid
                            if key in index:
                                continue
                        if (r, c) not in selections:
                            selected: tuple[int | None, bool, str | None] = (None, False, None)
                            if rows:
                                event = type("Selected", (), {"data_table": grid, "coordinate": Coordinate(r, c)})()
                                selected = await would(lambda event=event: app.cell_selected(event))  # type: ignore[arg-type]
                            selections[(r, c)] = (selected[0], selected[1], note(selected[2]))
                        drawn = screens.draw(app)
                        arrows: dict[str, tuple] = {}
                        beyond: list[str] = []
                        for name, (dr, dc) in ARROWS.items():
                            rr, cc = r + dr, c + dc
                            if 0 <= rr < rows and 0 <= cc < cols:
                                arrows[name] = (ctx, pane, "grid", rr, cc, False, *scrolled(grid, (sx, sy), rr, cc))
                                todo.append(arrows[name][3:5] + arrows[name][6:] + (False,))
                            elif rows <= rr < grid.row_count and 0 <= cc < cols:
                                beyond.append(name)  # the app goes on; the walk stops at ROW_LIMIT
                        most[(ctx, pane, r, c, fresh)] = (round(grid.max_scroll_x), round(grid.max_scroll_y))
                        add(key, drawn, enter=selections[(r, c)], arrows=arrows, beyond=beyond, landed=landed,
                            group=f"{ctx}/{pane}", layout=zones(grid), cursor=[r, c])
                # The pane names, focused, as a click on one leaves them: that pane refilled,
                # its guide explaining the pane. SQL is among them, though it has no grid.
                strip = app.query_one(ContentTabs)
                for pane in tab_panes:
                    app.clear_notifications()
                    app.query_one("#tabs", TabbedContent).active = f"tab-{pane}"
                    await settled()
                    app.refresh_pane(force=False)
                    await settled()
                    strip.focus()
                    app._arrived = pane
                    app.update_guide()
                    await settled()
                    grid = app.query_one(f"#grid-{pane}", Grid) if pane in grid_panes else None
                    add((ctx, pane, "tabs"), screens.draw(app), group=f"{ctx}/{pane}/tabs",
                        layout=zones(grid) if grid is not None else None, cursor=None)
                    # Where Tab goes from here is the pane's focus chain: the grid, or (Data) the
                    # WHERE box first, which the walk does not follow.
                    await pilot.press("tab")
                    await settled()
                    into[(ctx, pane)] = grid is not None and app.focused is grid
            app.snapshot_id = None
            app.query_one("#tabs", TabbedContent).active = "tab-overview"
            await settled()
            app.query_one(f"#grid-{grid_panes[0]}").focus()
            await settled()
            app.clear_notifications()
            await settled()
            await pilot.press("question_mark")
            await settled()
            # Help is a modal: the screen beneath shows through it, tinted, around its dialog,
            # which is opaque. Its body has the focus and scrolls, so the dialog is drawn at each
            # offset, and where each key the body answers leads is taken from pressing it.
            from textual.widgets._toast import Toast

            tint = screens.tinted(app.screen.styles.background)
            body = app.screen.query_one("#help-body")
            dialog = app.screen.query_one("#help").region
            offsets = list(range(round(body.max_scroll_y) + 1))
            help_frames, help_next = [], {k: [0] * len(offsets) for k in HELP_KEYS}
            for offset in offsets:
                body.scroll_to(y=offset, animate=False, immediate=True)
                await pilot.pause(0)
                drawn = screens.draw(app)
                help_frames.append([[y, dialog.x, cut(screens.lines[drawn[y]], dialog.x, dialog.right)]
                                    for y in range(dialog.y, dialog.bottom)])
                for k, name in HELP_KEYS.items():
                    body.scroll_to(y=offset, animate=False, immediate=True)
                    await pilot.pause(0)
                    await pilot.press(name)
                    await pilot.pause(0)
                    help_next[k][offset] = round(body.scroll_y)
            help_box = {"frames": help_frames, "next": help_next}
            await pilot.press("escape")
            await settled()
            # Each note as the app's toast, cut from the screen it is drawn on.
            toasts = []
            for text in notes:
                app.notify(text, **asked.get(text, {}))
                for _ in range(100):
                    await pilot.pause(0.02)
                    if app.query(Toast):
                        break
                await pilot.pause()
                [shown] = app.query(Toast)
                box, drawn = shown.region, screens.draw(app)
                toasts.append({"left": box.x, "bottom": box.bottom - 1, "timeout": shown._timeout, "rows": [
                    cut(screens.lines[drawn[y]], box.x, box.right) for y in range(box.y, box.bottom)
                ]})
                app.clear_notifications()
                for _ in range(100):
                    await pilot.pause(0.02)
                    if not app.query(Toast):
                        break
            # A cell under the mouse: DataTable lays its hover background beneath the cell's own
            # styles, and the cursor's over it. The header's is its own colour.
            from rich.style import Style

            grid = app.query_one(f"#grid-{grid_panes[0]}", Grid)
            plain = Style(bgcolor=grid.get_component_rich_style("datatable--hover").bgcolor)
            header = Style(bgcolor=(grid.get_component_rich_style("datatable--hover")
                                    + grid.get_component_rich_style("datatable--header-hover")).bgcolor)
            # A footer key under the mouse: its :hover background, as the app draws it.
            from textual.widgets._footer import FooterKey as _Key

            # A footer key or a pane's name under the mouse: their :hover colours, measured on
            # the app by hovering one, and laid over each style the way Textual lays them.
            async def lit(widget: Any) -> Any:
                x, y = widget.region.x + 1, widget.region.y
                await pilot.hover(offset=(width // 2, 0))
                await pilot.pause()
                before = screens.rich[cells(screens.lines[screens.draw(app)[y]])[x][1]] or Style()
                await pilot.hover(offset=(x, y))
                await pilot.pause()
                after = screens.rich[cells(screens.lines[screens.draw(app)[y]])[x][1]] or Style()
                await pilot.hover(offset=(width // 2, 0))
                await pilot.pause()
                return Style(color=after.color if after.color != before.color else None,
                             bgcolor=after.bgcolor if after.bgcolor != before.bgcolor else None)

            footer = await lit(next(k for k in app.screen.query(_Key) if k.region.width))
            tab = await lit(next(t for t in app.query(ContentTab) if t.region.width and not t.has_class("-active")))
            hover_styles = [[screens.style((style or Style()) + extra) for style in list(screens.rich)]
                            for extra in (plain, header, footer, tab)]
    finally:
        lake.close()

    landing = {state["key"][:2]: state["landed"] for state in states if state["key"][2] == "grid"}

    def resolved(key: tuple) -> int:
        key = alias[key] if key in alias else key
        return index[key] if key in index else -1

    def arrival(ctx: int, pane: str, fresh: bool) -> int:
        """A pane refilled - opened, or re-read as of another commit: the cursor home, unscrolled."""
        r, c, sx, sy = landing[(ctx, pane)]
        return resolved((ctx, pane, "grid", r, c, fresh and r == 0, sx, sy))

    def tabs_state(ctx: int, pane: str) -> int:
        return index.get((ctx, pane, "tabs"), -1)

    # What each state's keys do. The arrows and Enter depend on the cell, so each state has its
    # own; the pane keys and the travel keys only on the commit, the pane, where the focus is and
    # whether the pane is fresh, so each pane has those once.
    group_ids: dict[str, int] = {}
    groups: list[dict[str, Any]] = []
    for state in states:
        key = state["key"]
        ctx, pane, focus = key[0], key[1], key[2]
        fresh = key[5] if focus == "grid" else False
        if state["group"] not in group_ids:
            group_ids[state["group"]] = len(groups)
            nexts: list[dict[str, int]] = []
            toast: dict[str, int] = {}
            for was_fresh in (False, True):
                if focus == "grid":
                    nxt = {digit: arrival(ctx, other, True) for digit, other in pane_keys.items() if other != pane}
                else:
                    # Focus stays on the pane names: a pane key opens the pane, Tab goes into it.
                    nxt = {digit: tabs_state(ctx, other) for digit, other in tab_keys.items() if other != pane}
                    if into.get((ctx, pane)):
                        nxt["Tab"] = arrival(ctx, pane, True)
                for k, (sid, moved, text) in travels[ctx].items():
                    if moved and sid in pinned:
                        there = pinned.index(sid)
                        nxt[k] = arrival(there, pane, was_fresh) if focus == "grid" else tabs_state(there, pane)
                    if text is not None:
                        toast[k] = text
                nexts.append({k: v for k, v in nxt.items() if v >= 0})
            groups.append({"name": state["group"], "next": nexts, "toast": toast,
                           "tabs": {p: tabs_state(ctx, p) for p in tab_panes},
                           "home": arrival(ctx, pane, True) if pane in grid_panes else -1})
        move = [-1] * len(MOVES)
        if focus == "grid":
            for name, target in state["arrows"].items():
                move[MOVES.index(name)] = resolved(target)
            sid, moved, text = state["enter"]
            if moved and sid in pinned:
                move[MOVES.index("Enter")] = arrival(pinned.index(sid), pane, fresh)
            enter_note = text if moved and text is not None else -1
        else:
            # The strip's own keys: left and right open the neighbouring pane, round the ends.
            at = order.index(pane)
            move[MOVES.index("ArrowLeft")] = tabs_state(ctx, order[at - 1])
            move[MOVES.index("ArrowRight")] = tabs_state(ctx, order[(at + 1) % len(order)])
            enter_note = -1
        state.update(move=move, fresh=int(fresh), group=group_ids[state["group"]], enter_note=enter_note)

    # Where a click on each visible cell leads: the cursor there, the grid scrolled to show it
    # whole, then cut short if that cell's guide leaves the grid less room.
    for layout in layouts:
        layout["targets"] = []
    for state in states:
        lay = layouts[state["layout"]] if state["layout"] is not None else None
        if lay is None or lay["pane"] is None or lay["targets"]:
            continue
        ctx, pane = state["key"][0], state["key"][1]
        y0, r0, n, columns = lay["cells"]
        sy = lay["view"][1]
        targets = []
        for r in range(r0, r0 + n):
            for _a, _b, c, sx in columns:
                target = resolved((ctx, pane, "grid", r, c, False, sx, sy))
                if target < 0 and (ctx, pane, r, c, False) in most:
                    mx, my = most[(ctx, pane, r, c, False)]
                    target = resolved((ctx, pane, "grid", r, c, False, min(sx, mx), min(sy, my)))
                targets.append(target)
        lay["targets"] = targets

    walk: dict[str, Any] = {
        "cols": width, "rows": height, "lines": screens.lines, "moves": list(MOVES),
        "states": [s["lines"] for s in states], "move": [s["move"] for s in states],
        "fresh": [s["fresh"] for s in states], "group": [s["group"] for s in states], "groups": groups,
        "enter_note": [s["enter_note"] for s in states], "notes": notes, "toasts": toasts,
        "help": help_box, "tint": tint, "styles": screens.styles,
        "layout": [s["layout"] for s in states], "layouts": layouts, "cursor": [s["cursor"] for s in states],
        "beyond": [s.get("beyond", []) for s in states], "footer_keys": FOOTER_KEYS,
        "hovered": hover_styles,
    }
    walk["start"] = at = arrival(0, grid_panes[0], True)
    tour = []
    for chapter in TOUR:
        if any(k.isdigit() and k not in pane_keys for k in chapter["keys"]):
            break  # a cut-down walk, for tests, without a pane the tour visits
        for k in chapter["keys"]:
            at = step(walk, at, k)[0]
        text = screens.text(states[at]["lines"])
        tour.append({k: chapter[k] for k in ("name", "title", "keys")} | {
            "state": at, "places": places(text, chapter["callouts"]), "notes": [n for _, _, n in chapter["callouts"]],
        })
    walk["tour"] = tour
    return walk


def step(walk: dict[str, Any], at: int, key: str) -> tuple[int, int | None]:
    """Where `key` leads from state `at`, and the note the app shows: the page's rule, which its
    script follows too. The help screen is the page's to open and close."""
    group = walk["group"][at]
    if key in MOVES:
        to = walk["move"][at][MOVES.index(key)]
        note = walk["enter_note"][at] if key == "Enter" and to >= 0 else -1
        return (at if to < 0 else to), (None if note < 0 else note)
    pane = walk["groups"][group]
    return pane["next"][walk["fresh"][at]].get(key, at), pane["toast"].get(key)


def cells(runs: list[list[Any]]) -> list[tuple[str, int]]:
    return [(ch, style) for text, style in runs for ch in text]


def click(walk: dict[str, Any], at: int, y: int, x: int) -> tuple[Any, ...]:
    """What a click on screen cell (y, x) does in state `at`, by the app's rules; the page's
    script follows the same. A mouse-down focuses what it lands on, and then:

    ("key", k)        a footer key, which Textual presses on the mouse-down;
    ("state", s, n)   the walk moves to state s, and the app says note n (or None);
    ("unfollowed",)   the app would move the focus, or scroll, somewhere the walk does not go;
    ("none",)         nothing happens, in the app as on the page.
    """
    lay = walk["layouts"][walk["layout"][at]]
    group = walk["groups"][walk["group"][at]]
    on_strip = "/tabs" in group["name"]
    fy, keys = lay["footer"]
    if y == fy:
        for x0, x1, name in keys:
            if x0 <= x < x1:
                key = walk["footer_keys"].get(name)
                return ("key", key) if key else ("unfollowed",)
        return ("none",)
    ty, tx0, tx1, tabs = lay["tabs"]
    if y == ty and tx0 <= x < tx1:
        pane = group["name"].split("/")[1]
        for x0, x1, name in tabs:
            if x0 <= x < x1 and name != pane:
                target = group["tabs"].get(name, -1)
                return ("state", target, None) if target >= 0 else ("unfollowed",)
        # The open pane's name, or the strip between names: the focus moves to the strip.
        if on_strip:
            return ("none",)
        return ("state", group["tabs"][pane], None) if at == group["home"] else ("unfollowed",)
    if lay["pane"] is not None:
        top, bottom, left, right = lay["pane"]
        if top <= y < bottom and left <= x < right:
            y0, r0, n, columns = lay["cells"]
            here = group["home"] if on_strip else at  # the mouse-down has focused the grid
            for k, (x0, x1, _c, _sx) in enumerate(columns):
                if y0 <= y < y0 + n and x0 <= x < x1:
                    target = lay["targets"][(y - y0) * len(columns) + k]
                    if target == here or (on_strip and walk["cursor"][target][:2] == walk["cursor"][here][:2]):
                        # The cell under the cursor: DataTable selects it, as Enter does.
                        return ("state", *step(walk, here, "Enter"))
                    return ("state", target, None) if target >= 0 else ("unfollowed",)
            if y == lay["header"] and left <= x < right:
                return ("state", here, None) if here != at else ("none",)
            return ("unfollowed",)  # a scrollbar, or the grid past its rows
    for y0, y1, x0, x1 in lay["others"]:
        if y0 <= y < y1 and x0 <= x < x1:
            return ("unfollowed",)
    return ("none",)


def hover_at(walk: dict[str, Any], at: int, y: int, x: int) -> tuple[int, int] | None:
    """The grid cell under the mouse at screen cell (y, x), as DataTable reads it off the
    screen: (row, column), row -1 for the header; None off the grid's cells."""
    lay = walk["layouts"][walk["layout"][at]]
    if lay["pane"] is None:
        return None
    y0, r0, n, columns = lay["cells"]
    for x0, x1, c, _sx in columns:
        if x0 <= x < x1:
            if y == lay["header"]:
                return (-1, c)
            if y0 <= y < y0 + n:
                return (r0 + y - y0, c)
    return None


def hovered(walk: dict[str, Any], before: int, after: int, hover: tuple[int, int] | None,
            key: str | None = None) -> tuple[int, int] | None:
    """Where the hover is after a key (or a click, `key` None) took the walk from `before` to
    `after`: gone once the grid's own keys move or select (DataTable hides it), kept in the same
    grid otherwise, back on the first cell when the grid is refilled (DataTable.clear), and gone
    in another grid, which has not had the mouse move over it."""
    if hover is None or (key in MOVES and walk["cursor"][before] is not None):
        return None
    a, b = (walk["groups"][walk["group"][s]]["name"] for s in (before, after))
    if a == b:
        return hover
    if "/tabs" not in a and "/tabs" not in b and a.split("/")[1] == b.split("/")[1]:
        return (0, 0)
    return None


def cut(runs: list[list[Any]], start: int, end: int) -> list[list[Any]]:
    """The runs of a line between two columns."""
    out, at = [], 0
    for text, style in runs:
        a, b = max(start, at), min(end, at + len(text))
        if a < b:
            out.append([text[a - at:b - at], style])
        at += len(text)
    return out


def compose(walk: dict[str, Any], at: int, *, help: int | None = None, toasts: list[int] | tuple[int, ...] = (),
            hover: tuple[int, int] | None = None, mouse: tuple[int, int] | None = None) -> list[list[list[Any]]]:
    """The screen the page draws: state `at`, the help dialog over it if open (scrolled this
    far), then the notes
    still showing, oldest first, stacked up from the bottom as Textual's toast rack stacks them.
    The page's script draws by the same rule."""
    rows = [walk["lines"][i] for i in walk["states"][at]]

    def paste(y: int, left: int, inner: list[list[Any]]) -> None:
        if 0 <= y < len(rows):
            width = sum(len(text) for text, _ in inner)
            rows[y] = cut(rows[y], 0, left) + inner + cut(rows[y], left + width, walk["cols"])

    lay = walk["layouts"][walk["layout"][at]]
    if hover is not None and lay["pane"] is not None and hover != tuple(walk["cursor"][at] or ()):
        r, c = hover
        y0, r0, n, columns = lay["cells"]
        y = lay["header"] if r < 0 else y0 + r - r0
        span = next(((x0, x1) for x0, x1, cc, _ in columns if cc == c), None)
        if span is not None and (r < 0 or r0 <= r < r0 + n):
            mapped = walk["hovered"][1 if r < 0 else 0]
            row = rows[y]
            rows[y] = cut(row, 0, span[0]) + [[t, mapped[st]] for t, st in cut(row, *span)] + cut(row, span[1], walk["cols"])
    if mouse is not None and help is None:
        # Whatever footer key or pane name is under the mouse is drawn as hovered.
        fy, keys = lay["footer"]
        ty, _tx0, _tx1, tabs = lay["tabs"]
        pane = walk["groups"][walk["group"][at]]["name"].split("/")[1]
        names = [t for t in tabs if t[2] != pane]  # the open pane's name keeps its colours
        for y, spans, which in ((fy, keys, 2), (ty, names, 3)):
            span = next(((x0, x1) for x0, x1, _ in spans if y == mouse[1] and x0 <= mouse[0] < x1), None)
            if span is not None:
                mapped = walk["hovered"][which]
                row = rows[y]
                rows[y] = cut(row, 0, span[0]) + [[t, mapped[st]] for t, st in cut(row, *span)] + cut(row, span[1], walk["cols"])
    if help is not None:
        rows = [[[text, walk["tint"][style]] for text, style in row] for row in rows]
        for y, x, inner in walk["help"]["frames"][help]:
            paste(y, x, inner)
    y = None
    for note in reversed(toasts):
        toast = walk["toasts"][note]
        y = toast["bottom"] if y is None else y
        top = y - len(toast["rows"]) + 1
        for k, inner in enumerate(toast["rows"]):
            paste(top + k, toast["left"], inner)
        y = top - 2  # each toast has a row of margin above it
    return rows


def merged(dark: dict[str, Any], light: dict[str, Any]) -> dict[str, Any]:
    """The dark walk, with the light one's screens beside it, once they are the same walk: the
    same states, keys and clicks, and the same text in every state, so a page can change theme
    in the middle of one without moving."""
    for key in ("move", "group", "fresh", "cursor", "layout", "enter_note", "notes", "start"):
        if dark[key] != light[key]:
            raise RuntimeError(f"the light walk is not the dark one: its {key!r} differ")
    def text(walk: dict[str, Any], ids: list[int]) -> list[str]:
        return ["".join(t for t, _ in walk["lines"][i]) for i in ids]

    for n, (a, b) in enumerate(zip(dark["states"], light["states"], strict=True)):
        if text(dark, a) != text(light, b):
            rows = [(y, x, z) for y, (x, z) in enumerate(zip(text(dark, a), text(light, b), strict=True)) if x != z]
            raise RuntimeError(f"state {n} reads differently in the light theme: " + "; ".join(
                f"row {y}: dark {x.strip()!r}, light {z.strip()!r}" for y, x, z in rows[:3]))
    return dark | {"light": {key: light[key] for key in SCREENS}}


def drawn(theme: str, width: int, height: int) -> dict[str, Any]:
    """One theme's walk, in a process of its own: two at once take the time of one."""
    sys.modules["duckdb"] = None  # type: ignore[assignment]
    return asyncio.run(capture(width, height, HOME, theme=THEME if theme == "dark" else LIGHT))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    width, height = (int(args[0]), int(args[1])) if len(args) >= 2 else (120, 36)
    # The same screens on every machine: rows read by PyIceberg, whether or not DuckDB's iceberg
    # extension happens to be installed where the site is built.
    sys.modules["duckdb"] = None  # type: ignore[assignment]
    from concurrent.futures import ProcessPoolExecutor

    from icelens import demo
    from icelens.catalog import Lake, sandbox_target

    shutil.rmtree(HOME, ignore_errors=True)
    try:
        # One sandbox for both themes: the same commits, ids and times in each.
        lake = Lake(sandbox_target("demo", HOME))
        try:
            demo.seed(lake)
        finally:
            lake.close()
        with ProcessPoolExecutor(2) as pool:
            dark, light = (pool.submit(drawn, theme, width, height) for theme in ("dark", "light"))
            walk = merged(dark.result(), light.result())
        print(json.dumps(walk, ensure_ascii=False, separators=(",", ":")))
    finally:
        shutil.rmtree(HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The terminal UI: a catalog on the left, one table's anatomy on the right.

The layout is deliberately flat. A tree of namespaces and tables, ten panes over the selected
table, and one selected snapshot that every pane respects - so moving back through history is a
single keystroke and every pane follows you there, instead of each one having its own idea of
"now".

All catalog work happens in worker threads. PyIceberg blocks on HTTP and on reading manifests
out of object storage, and a UI that freezes while it does that is a UI people stop trusting.
Results are cached per (table, snapshot, pane), because flicking between panes should not
re-read a manifest list you already paid for.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.notifications import SeverityLevel
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from icelens import model, terms
from icelens.catalog import (
    IcelensError,
    Lake,
    NeedsWarehouse,
    Written,
    list_profiles,
    probe_warehouses,
    profile_target,
    qualified,
)
from icelens.format import csv_bytes, grid, pylist, safe, scalar
from icelens.model import Pane, View

#: pane id, tab label, shortcut key. The order is the reading order of a table:
#: what it is, what shape it has, what is in it, then how it got that way.
PANES: tuple[tuple[str, str, str], ...] = (
    ("overview", "Overview", "1"),
    ("schema", "Schema", "2"),
    ("data", "Data", "3"),
    ("snapshots", "Snapshots", "4"),
    ("commit", "Commit", "5"),
    ("manifests", "Manifests", "6"),
    ("files", "Files", "7"),
    ("partitions", "Partitions", "8"),
    ("layout", "Layout", "9"),
    ("sql", "SQL", "0"),
    ("writer", "Writer", "e"),
)

#: panes that are not a rendering of the selected table, so the tree and time travel leave
#: them alone. SQL shows whatever was asked for; Writer shows what this session has written.
FREE_PANES: frozenset[str] = frozenset({"sql", "writer"})

#: Grid columns that hold a snapshot id, so enter on one travels there. Snapshots names its
#: own `snapshot id` and `parent`; Manifests and Files both call theirs `added by` - the
#: snapshot that wrote the thing, which is the one worth seeing the table at.
SNAPSHOT_COLUMNS: tuple[str, ...] = ("snapshot id", "added by", "parent")

#: namespaces are listed eagerly up to this many; beyond it, tables load when you expand one
EAGER_NAMESPACES = 50


#: what the SQL pane says before it knows which table you are looking at
_SQL_HINT = '-- pick a table, or write SQL: icelens."<namespace>"."<table>"\nSELECT 1'

#: The Writer pane's opening statement. A worked example rather than a blank editor, because
#: the first question a sandbox has to answer is "what am I allowed to type here" - and the
#: three shapes below are the whole answer. It creates a table, so pressing ctrl+r on it as it
#: stands produces a snapshot, which is the fastest route to the thing this pane is for.
_WRITE_HINT = '''-- a sandbox: write here, and watch what Iceberg makes of it. ctrl+r runs it.
CREATE TABLE demo.sales AS
  SELECT i AS id, 'r' || (i % 4) AS region, i * 1.5 AS amount
  FROM range(1, 101) t(i);

-- then try these, one at a time, and read the ledger below:
--   INSERT INTO demo.sales SELECT i, 'r0', 9.99 FROM range(101, 111) t(i);
--   DELETE FROM demo.sales WHERE region = 'r0';'''

#: The attached sandbox writes v3, so its opening example is the v3 lesson: a DELETE that
#: leaves a deletion vector rather than rewriting the file, and an UPDATE, which the offline
#: sandbox cannot run at all. Table names are bare because the namespace is not a choice here.
_WRITE_HINT_REST = '''-- a v3 sandbox in {namespace}, written by DuckDB. ctrl+r runs it.
-- Tables are made here and nowhere else in this catalog.
CREATE TABLE sales AS
  SELECT i AS id, 'r' || (i % 4) AS region, i * 1.5 AS amount
  FROM range(1, 101) t(i);

-- then, one at a time - and watch pane 7 for what each one left behind:
--   DELETE FROM sales WHERE region = 'r0';      -- a deletion vector, not a rewrite
--   UPDATE sales SET amount = amount * 2 WHERE region = 'r1';
--   INSERT INTO sales SELECT i, 'r9', 0.5 FROM range(101, 111) t(i);'''


#: A v3 sandbox is the whole catalog, so its tables name their namespace, as offline ones do.
_WRITE_HINT_V3 = '''-- a v3 sandbox, written by DuckDB through icelens's own REST catalog. ctrl+r runs it.
CREATE TABLE demo.sales AS
  SELECT i AS id, 'r' || (i % 4) AS region, i * 1.5 AS amount
  FROM range(1, 101) t(i);

-- then, one at a time - and watch pane 7 for what each one left behind:
--   DELETE FROM demo.sales WHERE region = 'r0';      -- a deletion vector, not a rewrite
--   UPDATE demo.sales SET amount = amount * 2 WHERE region = 'r1';
--   INSERT INTO demo.sales SELECT i, 'r9', 0.5 FROM range(101, 111) t(i);
-- l in the Data pane shows each row's _row_id: watch which ones an UPDATE keeps.'''


@dataclass(frozen=True)
class NodeData:
    kind: str  # "catalog" | "namespace" | "table" | "view"
    name: str = ""


class Grid(DataTable):
    """A DataTable that keeps the Arrow table behind what it is showing.

    Export and copy need the real values, not the strings on screen, which are truncated and
    styled. Keeping the source beside the view is what makes `x` write a faithful CSV.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(zebra_stripes=True, cursor_type="cell", **kwargs)
        self.source: pa.Table | None = None

    def show(self, data: pa.Table, width: int) -> None:
        self.clear(columns=True)
        self.source = data
        columns, rows = grid(data, width)
        for index, name in enumerate(columns):
            # Text, not str: DataTable runs a str label through `Text.from_markup`, and a
            # column called `[/]` - a legal Iceberg field name - is a MarkupError, i.e. a
            # pane that will not draw at all.
            self.add_column(Text(safe(name)) if name.strip() else Text(" "), key=f"c{index}")
        for index, row in enumerate(rows):
            self.add_row(*row, key=f"r{index}")
        if rows:
            self.cursor_coordinate = Coordinate(0, 0)

    def column_name(self, index: int) -> str | None:
        if self.source is None or index >= len(self.source.column_names):
            return None
        return self.source.column_names[index]

    def value_at(self, row: int, column: int) -> Any:
        if self.source is None or row >= self.source.num_rows:
            return None
        return pylist(self.source.slice(row, 1).select([column]))[0][self.source.column_names[column]]


def _safe_name(text: str) -> str:
    """A table identifier, reduced to something that can only ever be one filename.

    Table names come from the catalog, and a catalog is free to hold `../../.ssh/authorized_keys`
    or a name made entirely of dots. Keep letters, digits, dash and underscore; everything else
    becomes an underscore.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")
    return cleaned[:64] or "catalog"


def _export_path(ident: str | None, pane: str) -> Path:
    """Where `x` writes, never overwriting: `-1`, `-2`, ... until a free name turns up."""
    stem = f"icelens-{_safe_name(ident or 'catalog')}-{_safe_name(pane)}"
    base = Path.cwd()
    candidate = base / f"{stem}.csv"
    for n in range(1, 1000):
        if not candidate.exists():
            return candidate
        candidate = base / f"{stem}-{n}.csv"
    return candidate


# A toast is two or three lines wide; a DuckDB error can be a page. Cut it here.
NOTIFY_CHARS = 240


class PaneBox(Vertical):
    """One pane: a note line saying where the numbers came from, and the grid itself."""

    def __init__(self, pane_id: str) -> None:
        super().__init__(id=f"box-{pane_id}")
        self.pane_id = pane_id

    def compose(self) -> ComposeResult:
        # markup=False: a note carries table names, partition values and catalog error text,
        # all of which are strings someone else chose. `[/]` in one of them is a MarkupError
        # and a crashed pane; `[link=...]` in one is a clickable hyperlink this tool did not
        # write. The note line is prose, so it has nothing to gain from markup anyway.
        yield Static("", id=f"note-{self.pane_id}", classes="note", markup=False)
        yield Grid(id=f"grid-{self.pane_id}")

    def update(self, pane: Pane, width: int) -> None:
        self.query_one(Static).update(safe(pane.note))
        self.query_one(Grid).show(pane.data, width)

    def say(self, message: str, error: bool = False) -> None:
        note = self.query_one(Static)
        note.update(safe(message))
        note.set_class(error, "error")


class GuideRail(Static):
    """The Iceberg vocabulary, docked under the panes, describing whatever the cursor is on.

    This is the tool's answer to the fact that Iceberg's structure is only legible to people
    who already know its nouns. It is not a help screen: it is always on, it is about the
    current cell rather than about Iceberg in general, and it changes as the cursor moves -
    one column right in Snapshots takes it from `operation` to `snapshot id`. Reading the
    table and learning the vocabulary are then the same activity, which is the only version
    of this that people actually do.

    Three states, cycled with `g`: the full entry (the default), a line, and off. The full entry
    is the default because someone opening icelens is usually learning what they are looking
    at; a reader who knows the nouns presses `g` once for the one-line rail.
    """

    #: how a state follows the one before it when `g` is pressed
    NEXT = {"deep": "brief", "brief": "off", "off": "deep"}

    def __init__(self) -> None:
        super().__init__("", id="guide", markup=True)
        self.term: terms.Term | None = None

    def show(self, term: terms.Term | None, mode: str) -> None:
        self.term = term
        self.set_class(mode == "deep", "deep")
        if mode == "off" or term is None:
            self.display = False
            return
        self.display = True
        if mode == "brief":
            self.update(f"[b]{escape(term.name)}[/b]  {escape(term.gloss)}   [dim]g for more[/dim]")
            return
        lines = [f"[b]{escape(term.name)}[/b]  -  {escape(term.gloss)}"]
        if term.body:
            lines.append(escape(term.body))
        if term.where:
            lines.append(f"[dim]where:[/dim] {escape(term.where)}")
        if term.see:
            names = ", ".join(terms.TERMS[key].name for key in term.see if key in terms.TERMS)
            lines.append(f"[dim]next:[/dim] {escape(names)}")
        self.update("\n".join(lines))


class TableCommands(Provider):
    """Fuzzy-jump to any loaded table from the command palette (ctrl+p)."""

    async def startup(self) -> None:
        self.tables = list(getattr(self.app, "known_tables", []))

    def _opener(self, ident: str) -> Callable[[], None]:
        app = self.app
        assert isinstance(app, IcelensApp)
        return lambda: app.open_table(ident)

    async def discover(self) -> Hits:
        for ident in self.tables[:20]:
            yield DiscoveryHit(ident, self._opener(ident), help="open table")

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for ident in self.tables:
            score = matcher.match(ident)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(ident),
                    self._opener(ident),
                    help="open table",
                )


class Picker(ModalScreen[str]):
    """Choose one of several things the tool found for you: warehouses, or whole catalogs.

    Options are `(value, label)`: the label can say more than the value - a catalog's type
    and address matter when you are deciding which one you meant - while the value is what
    comes back.
    """

    BINDINGS = [Binding("escape", "dismiss_none", "Cancel")]

    def __init__(self, title: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.title_text = title
        self.options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="picker"):
            yield Label(Text(self.title_text), classes="title")
            yield ListView(
                # Text: a catalog name and address out of ~/.pyiceberg.yaml, shown as written.
                *(ListItem(Label(Text(safe(label))), name=value) for value, label in self.options),
                id="warehouses",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name or "")

    def action_dismiss_none(self) -> None:
        self.dismiss("")


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape,q,question_mark", "dismiss_help", "Close")]

    TEXT = """\
[b]Moving around[/b]
  ↑ ↓ ← →      the tree and the grids        enter     open a table / pin a snapshot
  1 … 9 0      jump to a pane                tab       next pane
  /            filter the tree               ctrl+p    fuzzy-find any table
  escape       back to the tree              r         reload the selected table
  ctrl+o       switch catalog (~/.pyiceberg.yaml)   ctrl+w   switch warehouse

[b]Snapshots[/b]
  enter        view the whole table as of the snapshot in the cell under the cursor
  , .          step to the older / newer snapshot, in Snapshots-pane order
  u            back to the current snapshot
  enter works on any cell naming a snapshot: `snapshot id` or `parent` on Snapshots, and
  `added by` on Manifests and Files - so enter on a file shows the table as the commit that
  wrote it left it. Every pane follows the pinned snapshot: Schema shows the schema it was
  written with, Files and Layout show what existed then, Data reads the rows as of then.

[b]Rows[/b]
  [ ]          halve / double the row limit   w        filter rows (SQL WHERE, Data pane)
  x            export the focused grid to CSV c        copy the focused cell
  0            SQL over the whole catalog     ctrl+r   run the query
  l            row lineage: _row_id and _last_updated_sequence_number beside the rows.
               v3 only, and no engine shows them unless you name them.

[b]The guide[/b]
  g            the rail at the bottom: the full entry → a line → off
  It explains whatever the cursor is on and changes as you move: the pane, the column,
  the row, or the value in the cell. `icelens --explain <term>` is the same corpus.

[b]Reading the panes[/b]
  Overview   what metadata.json says about the table
  Schema     the schema of the viewed snapshot; ids, not names, are the identity
  Data       rows, through DuckDB when it can attach, PyIceberg otherwise
  Snapshots  every commit: ◆ current, │ ancestor, ~ off the current chain
  Commit     what one snapshot changed - the files it added and retired
  Manifests  the manifest list the snapshot points at
  Files      every live file at that snapshot, data and deletes
  Layout     the pointer chain: metadata → snapshot → manifest list → manifests → files
  Writer     sandbox only: write statements, and a ledger of what each one committed

[b]Writing (sandbox only)[/b]
  e            the Writer pane                 ctrl+r    commit what you typed
  escape       leave the editor, then 1 … 9 to read what the write did
  A sandbox is the only thing icelens will write to. Everywhere else it reads.
  `--sandbox` makes a local v2 catalog of its own. `--sandbox --rest` puts one in a
  namespace of a catalog you run, where DuckDB writes v3: deletion vectors, row
  lineage, UPDATE and MERGE - and where it can write in that namespace and no other.
"""

    def compose(self) -> ComposeResult:
        with Vertical(id="help"):
            # Taller than most terminals, so it scrolls, with the way out pinned below it.
            with VerticalScroll(id="help-body"):
                yield Static(self.TEXT, markup=True)
            yield Label("escape to close, ↑ ↓ to scroll", classes="dim")

    def on_mount(self) -> None:
        self.query_one("#help-body", VerticalScroll).focus()

    def action_dismiss_help(self) -> None:
        self.dismiss(None)


class IcelensApp(App[None]):
    CSS_PATH = "icelens.tcss"
    COMMANDS = App.COMMANDS | {TableCommands}
    # Below 80 columns (a split pane, a phone running the web demo) the 36-column catalog
    # beside the table leaves the table a sliver, so the screen stacks them instead.
    HORIZONTAL_BREAKPOINTS = [(0, "-narrow"), (80, "-wide")]
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("question_mark", "help", "Help", key_display="?"),
        Binding("slash", "focus_filter", "Filter", key_display="/"),
        Binding("r", "reload", "Reload"),
        Binding("u", "untravel", "Now"),
        Binding("x", "export", "Export"),
        Binding("c", "copy_cell", "Copy"),
        Binding("w", "focus_where", "Where"),
        Binding("g", "guide", "Guide"),
        Binding("l", "lineage", "Row ids"),
        Binding("comma", "step(-1)", "Older", key_display=",", show=False),
        Binding("full_stop", "step(1)", "Newer", key_display=".", show=False),
        Binding("left_square_bracket", "limit(0.5)", "Fewer", show=False),
        Binding("right_square_bracket", "limit(2.0)", "More", show=False),
        Binding("ctrl+r", "run_sql", "Run", priority=True),
        Binding("ctrl+o", "catalogs", "Catalogs", priority=True),
        Binding("ctrl+w", "warehouses", "Warehouse", priority=True),
        Binding("escape", "focus_tree", "Tree", show=False),
        *[Binding(key, f"pane('{pane}')", label, show=False) for pane, label, key in PANES],
    ]

    def __init__(self, lake: Lake, ident: str | None = None, limit: int = 200, width: int = 44) -> None:
        super().__init__()
        self.lake = lake
        self.ident: str | None = ident
        self.namespace: str | None = None
        self.snapshot_id: int | None = None
        self.limit = limit
        self.width = width
        self.where: str = ""
        #: v3 row lineage beside the data. Off by default: most catalogs out there are v2.
        self.lineage = False
        #: "deep" | "brief" | "off" - the guide rail is on, in full, by default, and is a
        #: feature of the layout rather than something you go and open.
        self.guide_mode = "deep"
        #: the pane just arrived at, until the cursor moves. Arriving somewhere and being told
        #: what the *column* is presumes you already know what the pane is - which is the
        #: assumption this whole rail exists to stop making.
        self._arrived: str | None = None
        #: a warehouse question already on screen. The tree worker and the pane worker both
        #: open the catalog, so both hit the same NeedsWarehouse within milliseconds of each
        #: other, and two stacked modals over one question is not a choice, it is a bug.
        self._asking_warehouse = False
        self.known_tables: list[str] = []
        #: views by namespace, listed beside the tables; a view has a definition and no panes
        self.known_views: dict[str, list[str]] = {}
        self.view_ident: str | None = None
        self._cache: dict[tuple, Pane] = {}
        self._sql_seed = ""
        self._written: list[Written] = []
        self._pending: str | None = ident

    @property
    def panes(self) -> tuple[tuple[str, str, str], ...]:
        """The panes this session actually has. One place decides, so nothing can disagree.

        The Writer only exists where writing is safe. Showing it greyed out against a
        production catalog would be an invitation to look for the way to enable it, and there
        is deliberately no way to enable it.
        """
        return tuple(pane for pane in PANES if pane[0] != "writer" or self.lake.is_sandbox)

    def _write_hint(self) -> str:
        """What the Writer pane opens on. Different sandboxes can do different things."""
        if self.lake.is_rest_sandbox:
            return _WRITE_HINT_REST.format(namespace=self.lake.sandbox_namespace)
        if self.lake.is_served_sandbox:
            return _WRITE_HINT_V3
        return _WRITE_HINT

    @property
    def cell_width(self) -> int:
        """The widest a cell is drawn. Narrow, half the screen: a 44-character property name
        on a 47-column phone would leave its value no room at all."""
        if not self.is_running or self.size.width >= 80:
            return self.width
        return min(self.width, max(12, (self.size.width - 6) // 2))

    # -- layout ----------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Input(placeholder="filter tables", id="filter")
                yield Tree("catalog", id="nav")
            with Vertical(id="content"):
                with TabbedContent(id="tabs"):
                    for pane, label, key in self.panes:
                        with TabPane(f"{key} {label}", id=f"tab-{pane}"):
                            if pane == "data":
                                yield Input(placeholder="WHERE … (press w)", id="where")
                            if pane == "sql":
                                yield TextArea(_SQL_HINT, id="sql", show_line_numbers=True)
                            if pane == "writer":
                                yield TextArea(self._write_hint(), id="write", show_line_numbers=True)
                            yield PaneBox(pane)
                yield GuideRail()
                yield Static("", id="status")
        yield Footer()

    def frame(self, ident: str | None) -> None:
        """Name the panels in their borders: the catalog, the open table, the guide."""
        self.query_one("#sidebar").border_title = "catalog"
        self.query_one("#guide").border_title = "guide"
        # A table's name is the catalog's, so it is Text, never markup, and made safe first.
        self.query_one("#tabs").border_title = Text(safe(ident)) if ident else "no table open"

    def on_mount(self) -> None:
        self.title = "icelens"
        self.sub_title = self.lake.target.label
        self.frame(None)
        self.query_one("#nav", Tree).root.data = NodeData("catalog")
        # the tree, not the filter box: otherwise the single-key pane shortcuts get typed
        # into an Input the moment the app starts.
        self.query_one("#nav", Tree).focus()
        self.update_guide()
        self.load_tree()

    # -- tree ------------------------------------------------------------------------
    @work(exclusive=True, group="tree")
    async def load_tree(self) -> None:
        tree = self.query_one("#nav", Tree)
        tree.root.set_label(Text(f"{self.lake.target.title}  (loading…)"))
        try:
            namespaces = await asyncio.to_thread(self.lake.namespaces)
        except NeedsWarehouse as exc:
            await self._ask_warehouse(exc)
            return
        except Exception as exc:  # noqa: BLE001 - every failure here is the user's to see
            tree.root.set_label(f"cannot list namespaces: {exc}")
            self.notify(str(exc), severity="error", timeout=12)
            return
        eager = len(namespaces) <= EAGER_NAMESPACES
        tables: dict[str, list[str]] = {}
        if eager:
            for namespace in namespaces:
                try:
                    tables[namespace] = await asyncio.to_thread(self.lake.tables, namespace)
                except Exception:  # noqa: BLE001 - a namespace that will not list still belongs in the tree
                    tables[namespace] = []
                self.known_views[namespace] = await asyncio.to_thread(self.lake.views, namespace)
        self.known_tables = sorted(ident for idents in tables.values() for ident in idents)
        tree.root.set_label(Text(self.lake.target.title))
        self._build_tree(namespaces, tables, "")
        if self._pending:
            self.open_table(self._pending)
            self._pending = None
        elif self.known_tables:
            self.status()

    def _build_tree(self, namespaces: list[str], tables: dict[str, list[str]], needle: str) -> None:
        tree = self.query_one("#nav", Tree)
        tree.clear()
        tree.root.data = NodeData("catalog")
        needle = needle.lower()
        for namespace in namespaces:
            idents = tables.get(namespace)
            views = self.known_views.get(namespace, [])
            if needle and idents is not None:
                idents = [i for i in idents if needle in i.lower()]
                views = [v for v in views if needle in v.lower()]
                if not idents and not views:
                    continue
            node = tree.root.add(
                # Text, not str: a namespace is named by whoever created it, and Tree renders
                # a str label through the markup parser.
                Text(f"{namespace}" + (f"  ({len(idents)})" if idents is not None else "")),
                data=NodeData("namespace", namespace),
                expand=bool(needle) or len(namespaces) <= 8,
            )
            for ident in idents or []:
                node.add_leaf(Text(ident.split(".")[-1]), data=NodeData("table", ident))
            self._add_views(node, views)
        tree.root.expand()

    @staticmethod
    def _add_views(node: Any, views: list[str]) -> None:
        for ident in views:
            node.add_leaf(Text.assemble(ident.split(".")[-1], ("  view", "dim italic")), data=NodeData("view", ident))

    @on(Tree.NodeExpanded, "#nav")
    async def expand_namespace(self, event: Tree.NodeExpanded) -> None:
        node = event.node
        data = node.data
        if not isinstance(data, NodeData) or data.kind != "namespace" or node.children:
            return
        try:
            idents = await asyncio.to_thread(self.lake.tables, data.name)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"{data.name}: {exc}", severity="error")
            return
        for ident in idents:
            node.add_leaf(Text(ident.split(".")[-1]), data=NodeData("table", ident))
        views = await asyncio.to_thread(self.lake.views, data.name)
        self.known_views[data.name] = views
        self._add_views(node, views)
        self.known_tables = sorted({*self.known_tables, *idents})

    @on(Tree.NodeHighlighted, "#nav")
    def highlight_node(self, event: Tree.NodeHighlighted) -> None:
        data = event.node.data
        self.update_guide()
        if not isinstance(data, NodeData):
            return
        if data.kind == "table" and data.name != self.ident:
            self.select_table(data.name)
        elif data.kind == "view" and data.name != self.view_ident:
            self.namespace = data.name.rsplit(".", 1)[0]
            self.ident = None
            self.view_ident = data.name
            self.snapshot_id = None
            self.sub_title = f"{self.lake.target.title}  ·  {data.name} (view)"
            self.refresh_pane(force=False)
        elif data.kind == "namespace":
            self.namespace = data.name
            self.view_ident = None
            self.ident = None
            self.snapshot_id = None
            self.refresh_pane(force=False)
            self.status()
        elif data.kind == "catalog":
            self.namespace = None
            self.view_ident = None
            self.ident = None
            self.refresh_pane(force=False)
            self.status()

    @on(Input.Changed, "#filter")
    def filter_tree(self, event: Input.Changed) -> None:
        namespaces = sorted({ident.rsplit(".", 1)[0] for ident in self.known_tables})
        tables: dict[str, list[str]] = {ns: [] for ns in namespaces}
        for ident in self.known_tables:
            tables[ident.rsplit(".", 1)[0]].append(ident)
        self._build_tree(namespaces, tables, event.value)

    # -- selection ---------------------------------------------------------------------
    def open_table(self, ident: str) -> None:
        """Select a table from anywhere - the palette, the CLI, a jump."""
        self.select_table(ident)
        tree = self.query_one("#nav", Tree)
        for node in tree.root.children:
            for leaf in node.children:
                if isinstance(leaf.data, NodeData) and leaf.data.name == ident:
                    node.expand()
                    tree.select_node(leaf)
                    # the cursor, not just the selection: `icelens demo.events` and a jump from
                    # the palette must leave the sidebar pointing at the table that is open, or
                    # the next arrow key starts from the root and the rail describes the catalog
                    tree.move_cursor(leaf)
                    tree.scroll_to_node(leaf)
                    return

    def _seed_sql(self, ident: str) -> None:
        """Point the SQL pane at the table you just opened, unless you have typed in it.

        A starter query naming a table that does not exist in this catalog is worse than no
        starter query: it is the first thing a new user runs, and it fails.
        """
        area = self.query_one("#sql", TextArea)
        if area.text.strip() not in ("", _SQL_HINT, self._sql_seed):
            return
        self._sql_seed = f"SELECT * FROM {qualified(ident)} LIMIT 20"
        area.text = self._sql_seed

    def select_table(self, ident: str) -> None:
        self._seed_sql(ident)
        # a new table refills every pane, so the pane is worth naming again
        self._arrived = self.active_pane
        self.ident = ident
        self.view_ident = None
        self.namespace = ident.rsplit(".", 1)[0]
        self.snapshot_id = None
        self.sub_title = f"{self.lake.target.title}  ·  {ident}"
        self.frame(ident)
        self.refresh_pane(force=False)
        self.status()

    @on(TabbedContent.TabActivated)
    def tab_changed(self, event: TabbedContent.TabActivated) -> None:
        self._arrived = self.active_pane
        self.refresh_pane(force=False)
        self.update_guide()

    @property
    def active_pane(self) -> str:
        tabs = self.query_one("#tabs", TabbedContent)
        active = tabs.active or "tab-overview"
        return active.removeprefix("tab-")

    def view(self) -> View:
        return View(
            self.lake,
            self.ident or "",
            snapshot_id=self.snapshot_id,
            limit=self.limit,
            where=self.where or None,
            lineage=self.lineage,
        )

    # -- panes -------------------------------------------------------------------------
    def refresh_pane(self, force: bool = False) -> None:
        pane_id = self.active_pane
        if pane_id in FREE_PANES:
            return
        box = self.query_one(f"#box-{pane_id}", PaneBox)
        if self.ident is None:
            self.load_catalog_pane(pane_id)
            return
        key = (self.ident, pane_id, self.snapshot_id, self.limit, self.where, self.lineage)
        if not force and key in self._cache:
            box.update(self._cache[key], self.cell_width)
            self.status()
            return
        box.say("loading…")
        self.load_pane(pane_id, key, force)

    @work(exclusive=True, group="pane")
    async def load_pane(self, pane_id: str, key: tuple, force: bool) -> None:
        box = self.query_one(f"#box-{pane_id}", PaneBox)
        try:
            if force:
                await asyncio.to_thread(self.lake.forget, self.ident)
            pane = await asyncio.to_thread(model.PANES[pane_id], self.view())
        except IcelensError as exc:
            box.say(str(exc), error=True)
            return
        except Exception as exc:  # noqa: BLE001 - a broken table must not take the app down
            box.say(f"{type(exc).__name__}: {exc}", error=True)
            return
        if key[0] != self.ident or key[1] != self.active_pane:
            return  # the selection moved on while this was loading
        self._cache[key] = pane
        box.update(pane, self.cell_width)
        self.status()

    @work(exclusive=True, group="pane")
    async def load_catalog_pane(self, pane_id: str) -> None:
        """With no table selected, every pane shows the view, namespace or catalog instead."""
        box = self.query_one(f"#box-{pane_id}", PaneBox)
        box.say("loading…")
        try:
            if self.view_ident:
                pane = await asyncio.to_thread(model.view_definition, self.lake, self.view_ident)
            elif self.namespace:
                pane = await asyncio.to_thread(model.namespace_overview, self.lake, self.namespace)
            else:
                pane = await asyncio.to_thread(model.catalog_overview, self.lake)
        except NeedsWarehouse as exc:
            await self._ask_warehouse(exc)
            return
        except Exception as exc:  # noqa: BLE001
            box.say(f"{type(exc).__name__}: {exc}", error=True)
            return
        box.update(pane, self.cell_width)
        self.status()

    # -- the guide rail ------------------------------------------------------------------
    def update_guide(self) -> None:
        """Point the rail at whatever has the cursor: a tree node, or a cell in a grid.

        Resolution is `terms.resolve`'s job, not this method's - here we only report what is
        under the cursor. The tree is checked first because when it has focus the grids still
        hold a cursor position, and the rail should describe what the reader is moving through.
        """
        rail = self.query_one("#guide", GuideRail)
        if self.guide_mode == "off":
            rail.show(None, "off")
            return
        focused = self.focused
        if isinstance(focused, Tree):
            node = focused.cursor_node
            data = node.data if node is not None else None
            kind = data.kind if isinstance(data, NodeData) else "catalog"
            rail.show(terms.for_tree(kind), self.guide_mode)
            return
        pane_id = self.active_pane
        if self._arrived == pane_id:
            # just arrived: say what the pane is. One keypress in any direction and the rail
            # is back to the cell, which is where it spends the rest of its life.
            rail.show(terms.resolve(pane_id), self.guide_mode)
            return
        column = row_label = value = None
        try:
            grid_widget = self.query_one(f"#grid-{pane_id}", Grid)
        except Exception:  # noqa: BLE001 - a pane with no grid on screen is not an error
            grid_widget = None
        if grid_widget is not None and grid_widget.source is not None and grid_widget.row_count:
            row, col = grid_widget.cursor_coordinate
            column = grid_widget.column_name(col)
            value = scalar(grid_widget.value_at(row, col))
            row_label = scalar(grid_widget.value_at(row, 0))
        rail.show(terms.resolve(pane_id, column, row_label, value), self.guide_mode)

    def action_guide(self) -> None:
        self.guide_mode = GuideRail.NEXT[self.guide_mode]
        self.update_guide()
        if self.guide_mode == "off":
            self.notify("guide off - g to bring it back")

    @on(DataTable.CellHighlighted)
    def cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Moving the cursor is the whole trigger: the rail follows the cell, not a request.

        A grid emits this when it is first filled and when it takes focus, both at (0, 0), and
        neither is somebody moving - so the pane's own term stands until the cursor leaves the
        first cell.
        """
        if event.coordinate != Coordinate(0, 0):
            self._arrived = None
        self.update_guide()

    @on(events.DescendantFocus)
    def focus_moved(self, event: events.DescendantFocus) -> None:
        """Focus decides what the rail is about: the tree describes nodes, a grid describes cells.

        Without this, stepping from the tree into a grid leaves the rail on the last thing the
        tree said, which is the one moment the reader is most likely to look at it.
        """
        self.update_guide()

    def notify(  # noqa: D102 - Textual's own docstring stands
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = False,
    ) -> None:
        # Every notification in this app is either a fixed string or an exception message, and
        # exception messages come from the catalog. Markup off by default, control codes
        # stripped, and cut to something a toast can hold.
        text = safe(" ".join(str(message).split()))
        super().notify(
            text if len(text) <= NOTIFY_CHARS else text[: NOTIFY_CHARS - 1] + "\u2026",
            title=safe(title),
            severity=severity,
            timeout=timeout,
            markup=markup,
        )

    def status(self) -> None:
        # Naming the catalog type and the route to the rows is not decoration: on a catalog
        # DuckDB cannot attach, "duckdb (bridge)" is the difference between a tool that looks
        # broken and one that has told you how it got the number.
        route = {"attach": "attached", "bridge": "scanning metadata files", "pyiceberg": "pyiceberg scans"}
        # The status line is the one place this app writes markup on purpose ([b] below), so
        # every value that came from a catalog, a config file or the keyboard is escaped first.
        bits = [
            f"{escape(safe(self.lake.target.catalog_type))} catalog",
            f"{escape(safe(self.lake.engine))}, {escape(route.get(self.lake.mode, safe(self.lake.mode)))}",
        ]
        if self.lake.is_sandbox:
            name = escape(safe(self.lake.target.sandbox))
            if self.lake.is_rest_sandbox:
                where = f"in {escape(safe(self.lake.sandbox_namespace))}, v3"
            elif self.lake.is_served_sandbox:
                where = "v3, served locally"
            else:
                where = "offline, v2"
            bits.insert(0, f"[b]sandbox {name}[/b] ({where}) - writing is on (press e)")
        if self.lake.attach_error and self.lake.mode != "bridge":
            bits.append(f"duckdb unavailable ({escape(safe(self.lake.attach_error))[:60]})")
            if self.lake.sql_route == "arrow":
                bits.append("SQL over pyiceberg scans")
        elif self.lake.attach_refused:
            # Scanning files is the plan for a Hive catalog and a symptom for a REST one: the
            # catalog turned DuckDB away, usually over a login, and that is the fact to show.
            bits.append(f"[b]{escape(safe(self.lake.attach_error or ''))[:90]}[/b]")
        if self.lake.delegation_note:
            bits.append(escape(safe(self.lake.delegation_note)))
        if self.ident:
            bits.append(f"limit {self.limit}")
        if self.where:
            bits.append(f"where {escape(safe(self.where))}")
        if self.lineage:
            bits.append("row lineage on")
        if self.snapshot_id is not None:
            bits.append(f"[b]time travel: snapshot {self.snapshot_id}[/b] (, . to step, u for now)")
        self.query_one("#status", Static).update("   ".join(bits))
        self.update_guide()

    # -- actions -----------------------------------------------------------------------
    def action_pane(self, pane_id: str) -> None:
        if pane_id not in {pane for pane, _label, _key in self.panes}:
            self.notify("writing needs a sandbox: icelens --sandbox (add --rest for v3)", severity="warning")
            return
        self.query_one("#tabs", TabbedContent).active = f"tab-{pane_id}"
        if pane_id == "sql":
            self.query_one("#sql", TextArea).focus()
        if pane_id == "writer":
            self.query_one("#write", TextArea).focus()

    @work
    async def action_catalogs(self) -> None:
        """Switch to another catalog from `~/.pyiceberg.yaml` without leaving the app.

        The point of the whole tool is that Iceberg's structure looks the same everywhere;
        the corollary is that moving between a REST catalog, a Glue one and a local SQL one
        should cost a keystroke, not a restart and a different set of flags.
        """
        profiles = list_profiles()
        if not profiles:
            self.notify("no catalogs in ~/.pyiceberg.yaml", severity="warning")
            return
        options = [
            (name, f"{name:<16} {props.get('type', 'rest'):<10} {props.get('uri', '')}")
            for name, props in sorted(profiles.items())
        ]
        chosen = await self.push_screen_wait(Picker("Which catalog?", options))
        if not chosen:
            return
        try:
            target = profile_target(chosen)
        except IcelensError as exc:
            self.notify(str(exc), severity="error")
            return
        self.lake.rebind(target)
        self.ident = None
        self.namespace = None
        self.snapshot_id = None
        self._cache.clear()
        self.known_tables = []
        self.known_views = {}
        self.view_ident = None
        self.sub_title = self.lake.target.label
        self.frame(None)
        self.notify(f"catalog {chosen}")
        self.load_tree()
        self.status()

    @work
    async def action_warehouses(self) -> None:
        """Switch warehouse inside the same REST catalog.

        One address can host dozens - a warehouse per project, per environment, per test run -
        and which one you are in is the difference between a full catalog and an empty one. Asking the
        catalog is the same probe that answers `--warehouses`, so this costs one request.
        """
        target = self.lake.target
        if target.catalog_type != "rest":
            self.notify(f"a {target.catalog_type} catalog has one warehouse", severity="warning")
            return
        options = await asyncio.to_thread(probe_warehouses, target.uri, properties=target.properties)
        if not options:
            self.notify("this catalog would not list its warehouses", severity="warning")
            return
        chosen = await self.push_screen_wait(Picker("Which warehouse?", [(w, w) for w in options]))
        if not chosen or chosen == target.warehouse:
            return
        self._use_warehouse(chosen)
        self.notify(f"warehouse {chosen}")

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_focus_filter(self) -> None:
        self.query_one("#filter", Input).focus()

    def action_focus_where(self) -> None:
        self.action_pane("data")
        self.query_one("#where", Input).focus()

    def action_focus_tree(self) -> None:
        self.query_one("#nav", Tree).focus()

    def action_reload(self) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k[0] != self.ident}
        self.refresh_pane(force=True)

    def action_untravel(self) -> None:
        if self.snapshot_id is None:
            return
        self.snapshot_id = None
        self.notify("back to the current snapshot")
        self.refresh_pane(force=False)
        self.status()

    def action_lineage(self) -> None:
        self.lineage = not self.lineage
        self._cache = {k: v for k, v in self._cache.items() if k[1] != "data"}
        if self.lineage and self.active_pane != "data":
            self.action_pane("data")
        else:
            self.refresh_pane(force=False)
        self.status()

    def travel(self, snapshot_id: int | None, note: str) -> None:
        """Pin (or unpin, with None) the snapshot every pane is read at."""
        self.snapshot_id = snapshot_id
        self.notify(note)
        self.status()
        self.refresh_pane(force=False)

    async def action_step(self, delta: int) -> None:
        """Walk the table's snapshots one at a time, in the order the Snapshots pane lists them.

        Landing on the current snapshot unpins rather than pinning it to the same id: the view
        is identical either way, and "now" is the more honest thing for the status bar to say.

        The metadata read happens in a thread even though it is usually a cache hit: `r` and a
        catalog switch both empty that cache, and `lake.table` then makes a REST round trip
        holding the lake's lock - 284 ms against a local catalog, unbounded against a sick one.
        Every pane load already goes through a thread for exactly this reason.
        """
        if not self.ident:
            self.notify("open a table first", severity="warning")
            return
        ident = self.ident
        try:
            meta = await asyncio.to_thread(lambda: self.lake.table(ident).metadata)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"{type(exc).__name__}: {exc}", severity="error")
            return
        if self.ident != ident:
            return  # a different table was opened while the metadata was being read
        ids = [snapshot.snapshot_id for snapshot in model.ordered_snapshots(meta)]
        if not ids:
            self.notify(f"{self.ident} has no snapshots to step through", severity="warning")
            return
        here = self.snapshot_id if self.snapshot_id is not None else meta.current_snapshot_id
        index = ids.index(here) if here in ids else len(ids) - 1
        target = index + delta
        if not 0 <= target < len(ids):
            self.notify("already at the oldest snapshot" if target < 0 else "already at the newest snapshot")
            return
        chosen = ids[target]
        where = f"snapshot {target + 1} of {len(ids)}"
        if chosen == meta.current_snapshot_id:
            self.travel(None, f"{where} - the current snapshot")
        else:
            self.travel(chosen, f"{where} - {chosen}, press u to come back")

    def action_limit(self, factor: float) -> None:
        self.limit = max(10, min(100_000, int(self.limit * factor)))
        self._cache = {k: v for k, v in self._cache.items() if k[1] != "data"}
        if self.active_pane == "data":
            self.refresh_pane(force=False)
        self.status()

    def action_export(self) -> None:
        grid_widget = self._focused_grid()
        if grid_widget is None or grid_widget.source is None:
            self.notify("nothing to export", severity="warning")
            return
        path = _export_path(self.ident, self.active_pane)
        try:
            # "xb", not "wb": a table called `../.bashrc` cannot escape _safe_name, but an
            # export must not silently overwrite anything either way. A clash gets a suffix.
            with path.open("xb") as handle:
                handle.write(csv_bytes(grid_widget.source))
        except OSError as exc:
            self.notify(f"export failed: {exc}", severity="error")
            return
        self.notify(f"wrote {grid_widget.source.num_rows} rows to {path.name}")

    def action_copy_cell(self) -> None:
        grid_widget = self._focused_grid()
        if grid_widget is None or grid_widget.source is None:
            return
        row, column = grid_widget.cursor_coordinate
        value = grid_widget.value_at(row, column)
        text = scalar(value)
        self.copy_to_clipboard(text)
        self.notify(f"copied: {text[:60]}")

    def action_run_sql(self) -> None:
        """ctrl+r runs whichever editor you are in - the Writer's, or the SQL pane's.

        One key for "run what I typed". Two would mean remembering which pane binds which,
        and the two editors are never both on screen.
        """
        if self.active_pane == "writer":
            self.run_write()
            return
        self.action_pane("sql")
        self.run_sql()

    @work(exclusive=True, group="write")
    async def run_write(self) -> None:
        """Commit the Writer's statements, then show what the table did about them."""
        box = self.query_one("#box-writer", PaneBox)
        sql = self.query_one("#write", TextArea).text.strip()
        if not sql:
            box.say("type a statement, then ctrl+r")
            return
        box.say("committing…")
        failure = ""
        try:
            fresh = await asyncio.to_thread(self.lake.write, sql)
        except IcelensError as exc:
            # The statements before the one that failed have committed, and the ledger is
            # where a commit is shown - so they go in it, and the failure is said under it.
            fresh, failure = list(exc.written), str(exc)
        except Exception as exc:  # noqa: BLE001 - a bad statement must not take the app down
            box.say(f"{type(exc).__name__}: {exc}", error=True)
            return
        self._written.extend(fresh)
        if fresh:
            box.update(model.written(self._written), self.cell_width)
        if failure:
            box.say(failure, error=True)
        if not fresh:
            if not failure:
                box.say("nothing committed - those statements read, or changed nothing")
            return
        # Every other pane is now stale, and the table may not have existed a second ago -
        # so the tree is reloaded and left pointing at what was just written. The Writer pane
        # stays on screen through all of it: `refresh_pane` leaves the free panes alone.
        self._cache.clear()
        self.snapshot_id = None
        await asyncio.to_thread(self.lake.forget, fresh[-1].table)
        self._pending = fresh[-1].table
        self.load_tree()
        self.status()

    @work(exclusive=True, group="sql")
    async def run_sql(self) -> None:
        box = self.query_one("#box-sql", PaneBox)
        sql = self.query_one("#sql", TextArea).text.strip()
        if not sql:
            box.say("type a query, then ctrl+r")
            return
        box.say("running…")
        try:
            data = await asyncio.to_thread(self.lake.query, sql)
        except IcelensError as exc:
            box.say(str(exc), error=True)
            return
        except Exception as exc:  # noqa: BLE001
            box.say(f"{type(exc).__name__}: {exc}", error=True)
            return
        box.update(Pane(data, f"{data.num_rows} rows  ·  {len(data.column_names)} columns"), self.cell_width)

    # -- grid interaction ---------------------------------------------------------------
    @on(DataTable.CellSelected)
    def cell_selected(self, event: DataTable.CellSelected) -> None:
        """Enter on a cell holding a snapshot id pins that snapshot for every pane.

        The cell under the cursor wins when it is one of those columns, so enter on a `parent`
        cell travels to the parent and not to the row's own snapshot; otherwise the row's first
        snapshot column is used, which is what makes enter work anywhere on a Files or Manifests
        row. The grids use a cell cursor, so enter posts CellSelected - RowSelected never fires.
        """
        table = event.data_table
        if not isinstance(table, Grid) or table.source is None:
            return
        row = event.coordinate.row
        if row >= table.source.num_rows:
            return
        columns = table.source.column_names
        under = table.column_name(event.coordinate.column)
        wanted = under if under in SNAPSHOT_COLUMNS else next((c for c in SNAPSHOT_COLUMNS if c in columns), None)
        if wanted is None:
            return
        try:
            snapshot_id = int(table.source.column(wanted)[row].as_py())
        except (TypeError, ValueError):
            return
        if snapshot_id == self.snapshot_id:
            return
        self.travel(snapshot_id, f"viewing snapshot {snapshot_id} - press u to come back")

    @on(Input.Submitted, "#where")
    def where_submitted(self, event: Input.Submitted) -> None:
        self.where = event.value.strip()
        self._cache = {k: v for k, v in self._cache.items() if k[1] != "data"}
        self.action_pane("data")
        self.refresh_pane(force=False)
        self.status()

    def _focused_grid(self) -> Grid | None:
        focused = self.focused
        if isinstance(focused, Grid):
            return focused
        try:
            return self.query_one(f"#grid-{self.active_pane}", Grid)
        except Exception:  # noqa: BLE001 - no grid on screen is not an error worth raising
            return None

    # -- warehouse picking ---------------------------------------------------------------
    async def _ask_warehouse(self, exc: NeedsWarehouse) -> None:
        if self._asking_warehouse:
            return
        if not exc.options:
            self.notify(str(exc), severity="error", timeout=15)
            return
        self._asking_warehouse = True
        try:
            chosen = await self.push_screen_wait(
                Picker("This catalog hosts several warehouses. Which one?", [(w, w) for w in exc.options])
            )
        finally:
            self._asking_warehouse = False
        if not chosen:
            self.notify("no warehouse chosen - ctrl+w to choose one", severity="warning", timeout=10)
            return
        self._use_warehouse(chosen)

    def _use_warehouse(self, name: str) -> None:
        self.lake.rebind(self.lake.target.with_warehouse(name, origin="picked in the app"))
        self.ident = None
        self.namespace = None
        self.snapshot_id = None
        self.known_tables = []
        self.known_views = {}
        self.view_ident = None
        self.sub_title = self.lake.target.label
        self.frame(None)
        self._cache.clear()
        self.load_tree()
        self.status()

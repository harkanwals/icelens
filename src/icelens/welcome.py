"""What `icelens` shows when nothing says which catalog to open.

The tool used to start anyway and greet a newcomer with PyIceberg's "URI missing, please
provide using --uri, the config or environment variable PYICEBERG_CATALOG__DEFAULT__URI" over
an empty tree. That is accurate and useless: the person running it for the first time
usually has no catalog to point at, and the tool can make one in about a second.

So it asks. It is a small app of its own, run before the main one, because the main app
decides at start-up which panes it has - the Writer pane exists only for a sandbox - and the
answer to this question decides whether this is one.
"""

from __future__ import annotations

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Label, ListItem, ListView, Static

from icelens.catalog import Sandbox
from icelens.format import safe

INTRO = (
    "No Iceberg catalog is configured here: nothing in --uri, the ICELENS_* variables, a .env "
    "or ~/.pyiceberg.yaml. You can still be looking at a real Iceberg table in about a second."
)

#: Printed after the screen closes rather than shown on it, so it can be copied.
CONNECT = """\
icelens opens any Iceberg catalog PyIceberg can reach. Tell it which, in any one of these ways:

  on the command line
    icelens --uri http://localhost:8181/catalog --warehouse WAREHOUSE

  in a .env in this directory or above it (a project's own variable names work)
    ACME_CATALOG_URI=http://localhost:8181/catalog
    ACME_WAREHOUSE=WAREHOUSE

  in ~/.pyiceberg.yaml, then `icelens --catalog lab` from anywhere
    catalog:
      lab:
        type: rest
        uri: http://localhost:8181/catalog
        warehouse: WAREHOUSE

Glue, Hive, SQL, Polaris, Snowflake and Databricks recipes are in the README.
To look around first, with nothing to set up: icelens --demo
"""


def choices(sandboxes: list[Sandbox], v3_writes: bool = True) -> list[tuple[str, str, str]]:
    """`(value, label, detail)` for each thing on offer, most likely first.

    A sandbox made last time comes first: somebody who has one is most likely back for it.
    Each demo is offered only while there is no sandbox of that name, because after that it
    is simply one of the sandboxes to open. The v3 one says so when DuckDB cannot write here,
    rather than failing after it is chosen.
    """
    made = {sandbox.name for sandbox in sandboxes}
    offered: list[tuple[str, str, str]] = []
    for sandbox in sandboxes:
        count = sandbox.tables
        tables = "tables unknown" if count is None else f"{count} table{'' if count == 1 else 's'}"
        offered.append(
            (f"sandbox:{sandbox.name}", f"Open sandbox {sandbox.name!r}", f"{tables}, v{sandbox.version} - yours to write to (press e)")
        )
    if "demo" not in made:
        offered.append(
            (
                "demo",
                "Make a sandbox with a demo table",
                "no server, no Docker: SQLite and Parquet under ~/.local/share/icelens, "
                "with five commits of history so every pane has something to show",
            )
        )
    if "demo-v3" not in made:
        detail = (
            "format-version 3: deletion vectors, row lineage, UPDATE and MERGE, written by DuckDB "
            "through a REST catalog icelens runs itself on 127.0.0.1"
        )
        if not v3_writes:
            detail += " - needs DuckDB's iceberg extension, which will not load here"
        offered.append(("demo-v3", "Make a v3 sandbox with a demo table", detail))
    offered.append(("empty", "Make an empty sandbox", "then press e and write to it with SQL"))
    offered.append(("connect", "Connect to a catalog you run", "prints how, and exits"))
    offered.append(("quit", "Quit", ""))
    return offered


def fresh_name(sandboxes: list[Sandbox]) -> str:
    """A name for a new, empty sandbox that is not one already made."""
    made = {sandbox.name for sandbox in sandboxes}
    for name in ("default", "scratch", *(f"scratch-{n}" for n in range(2, 1000))):
        if name not in made:
            return name
    raise RuntimeError("a thousand sandboxes is enough")


def duckdb_writes_iceberg() -> bool:
    """Whether DuckDB's iceberg extension loads here - what a v3 sandbox writes with.

    `LOAD` and never `INSTALL`: this runs before the screen is drawn, and a question asked of
    the network there would be a pause nobody could account for.
    """
    try:
        import duckdb

        duckdb.connect(":memory:").execute("LOAD iceberg")
    except Exception:  # noqa: BLE001 - not installed, no duckdb, anything: it will not write
        return False
    return True


class Welcome(App[str]):
    """Ask what to open. Returns the chosen value, or "" for quit."""

    CSS = """
    Screen { align: center middle; }
    #welcome { width: 88; max-width: 100%; height: auto; padding: 1 2; border: round $primary; }
    #brand { text-style: bold; margin-bottom: 1; }
    #intro { margin-bottom: 1; color: $text-muted; }
    #choices { height: auto; max-height: 20; }
    #choices > ListItem { padding: 0 1; margin-bottom: 1; }
    #choices Label { width: 1fr; }
    #foot { margin-top: 1; color: $text-muted; }
    """

    TITLE = "icelens"
    BINDINGS = [Binding("escape,q", "choose('')", "Quit")]

    def __init__(self, sandboxes: list[Sandbox], v3_writes: bool = True) -> None:
        super().__init__()
        self.offered = choices(sandboxes, v3_writes)

    def compose(self) -> ComposeResult:
        with Vertical(id="welcome"):
            yield Static(Text("icelens - a terminal browser for Apache Iceberg tables"), id="brand")
            yield Static(Text(INTRO), id="intro")
            items = []
            for value, label, detail in self.offered:
                # Text, never markup: a sandbox name is a directory name somebody chose.
                line = Text(safe(label), style="bold")
                if detail:
                    line.append("\n" + safe(detail), style="dim")
                items.append(ListItem(Label(line), name=value))
            yield ListView(*items, id="choices")
            yield Static(Text("enter to choose, escape to quit. A sandbox is format-version 2 unless it "
                              "is made v3: icelens --sandbox NAME --v3."), id="foot")

    def on_mount(self) -> None:
        self.query_one("#choices", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.action_choose(event.item.name or "")

    def action_choose(self, value: str) -> None:
        self.exit("" if value == "quit" else value)

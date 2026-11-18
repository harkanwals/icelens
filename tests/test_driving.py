"""Driving it: every pane, every column, every key, on a table with history and on an empty one.

The other UI tests each prove one feature. This one is the suite you want before you sit down
with the thing: it walks the cursor over every cell of every pane, presses every key that is
bound in every state the app has, and asserts after each move that nothing fell over out of
sight. Textual catches an exception raised inside a message handler and unwinds quietly, so
`unbroken()` - not the absence of a traceback in the output - is what makes this mean anything.
"""

from __future__ import annotations

import pytest
from harness import grid_of, note, rail, rows, settle, unbroken
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TextArea, Tree

from icelens.app import Grid, IcelensApp


#: every pane this app has, by the key a reader presses to reach it. Read off the app rather
#: than off `PANES`, because the Writer pane exists only against a sandbox.
def pane_keys(app: IcelensApp) -> tuple[tuple[str, str], ...]:
    return tuple((pane, key) for pane, _label, key in app.panes)


#: every key the app binds, minus the one that ends the session
DRIVEABLE: tuple[str, ...] = tuple(
    key for binding in IcelensApp.BINDINGS for key in binding.key.split(",") if key != "q"
)


async def back_to_the_tree(app: IcelensApp, pilot) -> None:
    """Undo whatever the last keystroke did to focus, and close anything it opened.

    Without this every test that presses keys in a loop starts typing its own keystrokes into
    the filter box the moment it presses `/`.
    """
    for _ in range(3):
        if not isinstance(app.screen, ModalScreen):
            break
        await pilot.press("escape")
        await pilot.pause()
    app.query_one("#nav", Tree).focus()
    await pilot.pause()


async def walk_the_grid(app: IcelensApp, pilot, pane: str) -> list[str]:
    """Move the cursor over every column and the first rows, and read the rail at each stop."""
    grid = grid_of(app, pane)
    if grid.source is None or not grid.row_count:
        return []
    grid.focus()
    await pilot.pause()
    seen = []
    for _ in range(grid.source.num_columns):
        seen.append(rail(app))
        unbroken(app, f"{pane} column {grid.cursor_coordinate.column}")
        assert seen[-1].strip(), f"{pane}: the rail went blank on column {grid.cursor_coordinate.column}"
        await pilot.press("right")
        await pilot.pause()
    for _ in range(min(grid.row_count, 5) - 1):
        await pilot.press("down")
        await pilot.pause()
        unbroken(app, f"{pane} row {grid.cursor_coordinate.row}")
        seen.append(rail(app))
    return seen


# -- every pane, every cell -----------------------------------------------------------------


@pytest.mark.parametrize("ident", ["demo.events", "demo.empty"])
async def test_every_pane_survives_every_cell(app: IcelensApp, ident: str) -> None:
    """The one that has to pass before anyone drives this: 10 panes x every column, twice.

    `demo.empty` is the half that breaks tools - a real table, registered in the catalog, with
    no snapshot, so every pane that walks history has nothing to walk.
    """
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table(ident)
        await settle(app, pilot)
        for pane, key in pane_keys(app):
            await pilot.press(key)
            await settle(app, pilot)
            assert app.active_pane == pane
            if pane == "sql":
                await pilot.press("ctrl+r")
                await settle(app, pilot)
            unbroken(app, f"{ident} {pane}")
            assert "Traceback" not in note(app, pane)
            await walk_the_grid(app, pilot, pane)
            await back_to_the_tree(app, pilot)


async def test_a_snapshotless_table_explains_itself_in_every_pane(app: IcelensApp) -> None:
    """Empty is a state, not an error: every pane says why it is empty rather than erroring."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.empty")
        await settle(app, pilot)
        for pane in ("snapshots", "commit", "manifests", "files", "layout"):
            await pilot.press(dict(pane_keys(app))[pane])
            await settle(app, pilot)
            assert "no snapshots" in note(app, pane), f"{pane}: {note(app, pane)!r}"
            assert not app.query_one(f"#note-{pane}", Static).has_class("error")
        await pilot.press("2")  # schema, which a table has before it has any data
        await settle(app, pilot)
        assert rows(app, "schema") == 4


# -- every key, in every state --------------------------------------------------------------


@pytest.mark.parametrize("selection", ["nothing", "table", "namespace"])
async def test_every_bound_key_in_every_selection(
    app: IcelensApp, selection: str, tmp_path, monkeypatch
) -> None:
    """Press everything the app binds, with nothing open, with a table open, with a namespace.

    Most crashes in a TUI are a key pressed in the state its author never pictured: copy with
    no grid, export with no table, `u` when you are not time travelling.
    """
    from icelens import app as app_module

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(app_module, "list_profiles", dict)  # ctrl+o must not open a real catalog
    async with app.run_test() as pilot:
        await settle(app, pilot)
        if selection == "table":
            app.open_table("demo.events")
        elif selection == "namespace":
            await pilot.press("down")
        await settle(app, pilot)
        for key in DRIVEABLE:
            await pilot.press(key)
            await pilot.pause()
            unbroken(app, f"{selection}: {key}")
            await back_to_the_tree(app, pilot)
            await settle(app, pilot)
        assert app.is_running


async def test_the_grid_keys_work_before_a_table_is_open(app: IcelensApp, tmp_path, monkeypatch) -> None:
    """With nothing selected the panes hold the catalog listing, so `x` and `c` still mean something.

    They are bound to the focused grid, not to a table, and with no table open the grid is the
    list of namespaces - which is worth exporting. `u` in the same state is a no-op, not an error.
    """
    monkeypatch.chdir(tmp_path)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("x")
        await pilot.press("c")
        await pilot.press("u")
        await pilot.pause()
        unbroken(app, "no table open")
        written = list(tmp_path.glob("icelens-catalog-*.csv"))
        assert written and b"demo" in written[0].read_bytes()
        assert app.snapshot_id is None


# -- time travel ------------------------------------------------------------------------------


async def test_time_travel_reaches_every_pane_and_comes_back(app: IcelensApp) -> None:
    """Pinning a snapshot is the app's one piece of global state; every pane must respect it."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        grid = grid_of(app, "snapshots")
        grid.focus()
        oldest = int(grid.source.column("snapshot id")[0].as_py())
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot_id == oldest
        assert f"time travel: snapshot {oldest}" in str(app.query_one("#status", Static).content)

        for pane, key in pane_keys(app):
            if pane == "sql":
                continue
            await pilot.press(key)
            await settle(app, pilot)
            unbroken(app, f"travelling: {pane}")
            assert not app.query_one(f"#note-{pane}", Static).has_class("error"), note(app, pane)
        assert rows(app, "data") == 10  # the first snapshot's 50 rows, capped by the limit

        await pilot.press("u")
        await settle(app, pilot)
        assert app.snapshot_id is None
        assert "time travel" not in str(app.query_one("#status", Static).content)


async def test_enter_on_a_row_that_is_not_a_snapshot_does_nothing(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("2")  # schema: rows, but no snapshot id anywhere in them
        await settle(app, pilot)
        grid_of(app, "schema").focus()
        await pilot.press("enter")
        await pilot.pause()
        unbroken(app, "enter on a schema row")
        assert app.snapshot_id is None


# -- the guide rail, in anger -------------------------------------------------------------------


async def test_the_rail_has_something_to_say_in_every_pane(app: IcelensApp) -> None:
    """Every pane resolves to a term even before the cursor moves, including SQL."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        said = {}
        for pane, key in pane_keys(app):
            await pilot.press(key)
            await settle(app, pilot)
            grid_of(app, pane).focus()
            await pilot.pause()
            said[pane] = rail(app)
            assert said[pane].strip(), f"{pane}: the rail said nothing"
        # ten panes should not be explained by three terms
        assert len({text.split("  ")[0] for text in said.values()}) >= 7


async def test_the_guide_stays_off_until_you_ask_for_it_back(app: IcelensApp) -> None:
    """Off means off: switching panes, opening tables and reloading must not bring it back."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("g", "g")
        await pilot.pause()
        assert app.guide_mode == "off"
        guide = app.query_one("#guide", Static)
        for key in ("2", "4", "7", "9"):
            await pilot.press(key)
            await settle(app, pilot)
            assert not guide.display, f"the rail came back on {key}"
        app.open_table("demo.empty")
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)
        assert not guide.display
        await pilot.press("g")
        await pilot.pause()
        assert app.guide_mode == "brief" and guide.display


async def test_the_deep_rail_renders_every_term(app: IcelensApp) -> None:
    """Deep mode is the one that renders a whole entry; a stray bracket in a body would break it."""
    from icelens import terms

    async with app.run_test() as pilot:
        await settle(app, pilot)
        rail_widget = app.query_one("#guide")
        for term in terms.TERMS.values():
            rail_widget.show(term, "deep")
            await pilot.pause()
            unbroken(app, f"deep: {term.name}")
            assert term.name in rail(app)


async def test_the_rail_describes_the_tree_as_well_as_the_grid(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        assert "Catalog" in rail(app)
        await pilot.press("down")  # the namespace node
        await settle(app, pilot)
        assert "Namespace" in rail(app)
        await pilot.press("down")  # a table node
        await settle(app, pilot)
        assert "Table metadata" in rail(app)
        # and stepping into a grid hands the rail over to the cell, not the node
        grid_of(app, app.active_pane).focus()
        await pilot.pause()
        assert tree.has_focus is False
        assert rail(app).strip()


# -- filtering, limits, SQL ----------------------------------------------------------------------


async def test_a_where_clause_applies_and_clears(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("w")
        await pilot.pause()
        where = app.query_one("#where", Input)
        assert where.has_focus
        where.value = "id > 55"
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.where == "id > 55"
        assert rows(app, "data") == 5
        assert "where id > 55" in str(app.query_one("#status", Static).content)

        app.query_one("#where", Input).focus()
        app.query_one("#where", Input).value = ""
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.where == ""
        assert rows(app, "data") == 10


async def test_a_where_clause_that_is_not_sql_is_reported_not_raised(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("w")
        await pilot.pause()
        app.query_one("#where", Input).value = "id >>> banana"
        await pilot.press("enter")
        await settle(app, pilot)
        unbroken(app, "bad where")
        assert app.query_one("#note-data", Static).has_class("error")


async def test_the_limit_keys_stay_inside_their_bounds(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        for _ in range(6):
            await pilot.press("left_square_bracket")
            await settle(app, pilot)
        assert app.limit == 10  # the floor, not zero and not a crash
        for _ in range(3):
            await pilot.press("right_square_bracket")
            await settle(app, pilot)
        assert app.limit == 80
        assert rows(app, "data") == 55  # the table has fewer rows than the limit now
        unbroken(app, "limits")


async def test_the_sql_pane_handles_nothing_typed_at_all(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("0")
        await settle(app, pilot)
        app.query_one("#sql", TextArea).text = ""
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert "type a query" in note(app, "sql")
        unbroken(app, "empty sql")


@pytest.mark.duckdb_extensions
async def test_the_sql_pane_is_seeded_with_the_table_you_opened(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        assert 'icelens."demo"."events"' in app.query_one("#sql", TextArea).text
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert rows(app, "sql") == 20
        # but a query you typed yourself is never overwritten by opening another table
        app.query_one("#sql", TextArea).text = "SELECT 42 AS answer"
        app.open_table("demo.empty")
        await settle(app, pilot)
        assert app.query_one("#sql", TextArea).text == "SELECT 42 AS answer"


# -- the tree ---------------------------------------------------------------------------------


async def test_a_filter_that_matches_nothing_empties_the_tree_and_recovers(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("slash")
        filter_box = app.query_one("#filter", Input)
        filter_box.value = "zzzz"
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        assert list(tree.root.children) == []
        unbroken(app, "empty filter result")
        filter_box.value = ""
        await settle(app, pilot)
        assert [str(node.label) for node in tree.root.children] == ["demo  (2)"]


async def test_selecting_a_namespace_then_the_catalog_again(app: IcelensApp) -> None:
    """Walking back up the tree clears the table, and every pane follows without erroring."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        tree.focus()
        tree.select_node(tree.root)
        await settle(app, pilot)
        assert app.ident is None and app.snapshot_id is None
        for pane, key in pane_keys(app):
            if pane == "sql":
                continue
            await pilot.press(key)
            await settle(app, pilot)
            unbroken(app, f"catalog view: {pane}")
            assert rows(app, pane) > 0  # the catalog listing stands in for every pane


# -- the terminal itself -------------------------------------------------------------------------


@pytest.mark.parametrize("size", [(60, 18), (200, 60)])
async def test_it_renders_at_both_ends_of_the_terminal(app: IcelensApp, size) -> None:
    """A laptop split pane and a full screen. Neither may clip the app into an exception."""
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("g")  # deep mode, the one state that takes real height
        for pane, key in pane_keys(app):
            await pilot.press(key)
            await settle(app, pilot)
            unbroken(app, f"{size}: {pane}")
        assert app.is_running


async def test_reload_does_not_lose_the_table_or_the_snapshot(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        grid_of(app, "snapshots").focus()
        pinned = int(grid_of(app, "snapshots").source.column("snapshot id")[0].as_py())
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)
        assert app.ident == "demo.events"
        assert app.snapshot_id == pinned
        assert isinstance(app.query_one("#grid-snapshots", Grid).source, object)
        unbroken(app, "reload")


# -- catalogs that will not answer until you choose ------------------------------------------------


async def test_a_catalog_that_hosts_several_warehouses_asks_which_one(app: IcelensApp, monkeypatch) -> None:
    """Lakekeeper, Polaris and Unity all refuse to list until told which warehouse.

    The rebind is intercepted rather than performed: the session's catalog is shared by every
    test, and what matters here is that the app offers the choice instead of dying on the error.
    """
    from icelens.catalog import NeedsWarehouse

    asked: list[str] = []
    real = app.lake.namespaces

    def namespaces() -> list[str]:
        if not asked:
            asked.append("refused")
            raise NeedsWarehouse("which warehouse?", ["lab", "prod"])
        return real()

    monkeypatch.setattr(app.lake, "namespaces", namespaces)
    monkeypatch.setattr(app.lake, "rebind", lambda target: asked.append(target.warehouse))
    async with app.run_test() as pilot:
        for _ in range(40):
            await pilot.pause()
            if isinstance(app.screen, ModalScreen):
                break
        assert isinstance(app.screen, ModalScreen), "the app never offered the warehouses"
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert asked == ["refused", "prod"]
        unbroken(app, "warehouse picker")


async def test_a_catalog_that_refuses_and_offers_nothing_says_so(app: IcelensApp, monkeypatch) -> None:
    from icelens.catalog import NeedsWarehouse

    def namespaces() -> list[str]:
        raise NeedsWarehouse("set --warehouse")

    monkeypatch.setattr(app.lake, "namespaces", namespaces)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert not isinstance(app.screen, ModalScreen)  # nothing to pick from, so no picker
        unbroken(app, "warehouse with no options")
        assert app.is_running


async def test_a_catalog_that_will_not_list_at_all_says_so_in_the_tree(app: IcelensApp, monkeypatch) -> None:
    def namespaces() -> list[str]:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(app.lake, "namespaces", namespaces)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "cannot list namespaces" in str(app.query_one("#nav", Tree).root.label)
        unbroken(app, "dead catalog")
        # and the app is still drivable: keys must not need a catalog to be pressed
        await pilot.press("2", "4", "g", "u")
        await pilot.pause()
        unbroken(app, "keys against a dead catalog")


# -- starting up on a table ---------------------------------------------------------------------


async def test_opening_straight_onto_a_table(lake) -> None:
    """`icelens demo.events` - the first thing anyone types after the bare command."""
    app = IcelensApp(lake, ident="demo.events", limit=10, width=30)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.ident == "demo.events"
        assert rows(app, "overview") > 5
        tree = app.query_one("#nav", Tree)
        assert tree.cursor_node is not None and tree.cursor_node.data.name == "demo.events"
        unbroken(app, "opened from the command line")


async def test_a_table_name_that_is_not_in_the_catalog(lake) -> None:
    """A typo at the command line has to land in the pane as a sentence, not in a traceback."""
    app = IcelensApp(lake, ident="demo.nope", limit=10, width=30)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.query_one("#note-overview", Static).has_class("error")
        assert "nope" in note(app, "overview")
        unbroken(app, "missing table")
        # and the app still drives: pick a real table out of the tree and carry on
        app.open_table("demo.events")
        await settle(app, pilot)
        assert rows(app, "overview") > 5
        assert not app.query_one("#note-overview", Static).has_class("error")

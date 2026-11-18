"""The app's less-travelled paths, driven by keystrokes: the ones a person meets on a bad day.

`test_app.py` proves each feature works and `test_driving.py` that nothing falls over. These are
what the app says when a thing does not work: a warehouse switch against a server that answers
(see `tests/stub.py`), a catalog too big to list eagerly, the command palette, the Writer pane given
nothing or only reads, a pane whose reader raises something nobody anticipated, an export with
nowhere to go, and every picker dismissed rather than answered. Each ends in `unbroken`, because
Textual swallows an exception raised in a handler and a test that only looks at widgets would not
notice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import note, settle, unbroken
from stub import StubCatalog
from textual.app import App
from textual.command import CommandPalette
from textual.content import Content
from textual.widgets import Static, TextArea, Tree

from icelens import model, terms
from icelens.app import Grid, GuideRail, IcelensApp, PaneBox, Picker
from icelens.catalog import IcelensError, Lake, NeedsWarehouse, Target, rest_sandbox_target, sandbox_target


def notices(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every notification the app raises, after its own cleaning, in order."""
    seen: list[str] = []
    original = App.notify

    def record(self, message, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(str(message))
        return original(self, message, **kwargs)

    monkeypatch.setattr(App, "notify", record)
    return seen


def tree_labels(app: IcelensApp) -> list[str]:
    return [str(node.label) for node in app.query_one("#nav", Tree).root.children]


async def until(app: IcelensApp, pilot, condition, what: str) -> None:
    """Wait for something a worker is *awaiting* - a picker - which `settle` would wait on forever."""
    for _ in range(300):
        await pilot.pause()
        if condition():
            return
    raise AssertionError(f"never saw {what}")


# -- warehouses, from a server that answers ----------------------------------------------------------


async def test_ctrl_w_switches_warehouse_and_the_tree_follows(monkeypatch) -> None:
    told = notices(monkeypatch)
    with StubCatalog({"wh-a": ["demo"], "wh-b": ["raw", "staged"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri, "warehouse": "wh-a"}))
        app = IcelensApp(lake)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert tree_labels(app) == ["demo  (0)"]
            await pilot.press("ctrl+w")
            await until(app, pilot, lambda: isinstance(app.screen, Picker), "the warehouse picker")
            assert [value for value, _ in app.screen.options] == ["wh-a", "wh-b"]
            await pilot.press("down", "enter")
            await settle(app, pilot)
            assert lake.target.warehouse == "wh-b"
            assert tree_labels(app) == ["raw  (0)", "staged  (0)"]
            assert "wh-b" in app.sub_title
            assert "warehouse wh-b" in told
            unbroken(app, "switching warehouse")
        lake.close()


async def test_ctrl_w_dismissed_or_answered_with_the_same_warehouse_changes_nothing() -> None:
    with StubCatalog({"wh-a": ["demo"], "wh-b": ["raw"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri, "warehouse": "wh-a"}))
        app = IcelensApp(lake)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            for keys in (("escape",), ("enter",)):  # cancelled; then the first row, which is wh-a
                await pilot.press("ctrl+w")
                await until(app, pilot, lambda: isinstance(app.screen, Picker), "the warehouse picker")
                await pilot.press(*keys)
                await settle(app, pilot)
                assert not isinstance(app.screen, Picker)
                assert lake.target.warehouse == "wh-a"
                assert tree_labels(app) == ["demo  (0)"]
            unbroken(app, "a picker put away")
        lake.close()


async def test_ctrl_w_on_a_catalog_that_will_not_list_its_warehouses(monkeypatch) -> None:
    told = notices(monkeypatch)
    with StubCatalog({"hidden": ["demo"]}, listed=False) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri, "warehouse": "hidden"}))
        app = IcelensApp(lake)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+w")
            await settle(app, pilot)
            assert not isinstance(app.screen, Picker)
            assert "this catalog would not list its warehouses" in told
            unbroken(app, "no warehouses to offer")
        lake.close()


async def test_a_catalog_that_wants_a_warehouse_and_gets_none(monkeypatch) -> None:
    """Dismissing the question leaves the app open and says how to ask again."""
    told = notices(monkeypatch)
    with StubCatalog({"wh-a": ["demo"], "wh-b": ["raw"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri}))
        app = IcelensApp(lake)
        async with app.run_test() as pilot:
            await until(app, pilot, lambda: isinstance(app.screen, Picker), "the warehouse question")
            await pilot.press("escape")
            await settle(app, pilot)
            assert any("no warehouse chosen - ctrl+w" in t for t in told)
            assert lake.target.warehouse == ""
            unbroken(app, "a warehouse question dismissed")
        lake.close()


async def test_a_catalog_that_wants_a_warehouse_and_will_not_say_which(monkeypatch) -> None:
    told = notices(monkeypatch)
    with StubCatalog({"hidden": ["demo"]}, listed=False) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri}))
        app = IcelensApp(lake)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            assert not isinstance(app.screen, Picker), "nothing to choose from is not a choice"
            assert any("needs a warehouse" in t for t in told)
            unbroken(app, "a warehouse needed and none offered")
        lake.close()


# -- a catalog too big to list up front ---------------------------------------------------------------


@pytest.fixture()
def crowded(tmp_path: Path) -> Lake:
    """Fifty-one namespaces: one more than the tree lists eagerly, so tables load on expand."""
    lake = Lake(Target(properties={"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}", "warehouse": tmp_path.as_uri()}))
    for index in range(51):
        lake.catalog.create_namespace(f"ns{index:02d}")
    lake.catalog.create_table("ns00.first", schema=_one_column())
    yield lake
    lake.close()


def _one_column():  # type: ignore[no-untyped-def]
    from pyiceberg.schema import Schema
    from pyiceberg.types import LongType, NestedField

    return Schema(NestedField(1, "id", LongType()))


async def test_a_big_catalog_lists_tables_when_a_namespace_is_opened(crowded: Lake) -> None:
    app = IcelensApp(crowded)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        tree = app.query_one("#nav", Tree)
        assert len(tree.root.children) == 51
        assert all(not node.children for node in tree.root.children), "nothing should be listed before it is asked for"
        assert app.known_tables == []
        first = tree.root.children[0]
        first.expand()
        await settle(app, pilot)
        assert [leaf.data.name for leaf in first.children] == ["ns00.first"]
        assert app.known_tables == ["ns00.first"]
        unbroken(app, "expanding a namespace")


async def test_a_namespace_that_will_not_expand_says_why(crowded: Lake, monkeypatch) -> None:
    told = notices(monkeypatch)

    def refuse(namespace: str) -> list[str]:
        raise IcelensError(f"cannot list the tables in {namespace}: access denied")

    app = IcelensApp(crowded)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        monkeypatch.setattr(crowded, "tables", refuse)
        app.query_one("#nav", Tree).root.children[3].expand()
        await settle(app, pilot)
        assert any("ns03" in t and "access denied" in t for t in told)
        unbroken(app, "a namespace that will not expand")


async def test_a_namespace_that_will_not_list_up_front_is_still_in_the_tree(lake: Lake, monkeypatch) -> None:
    fresh = Lake(lake.target)

    def refuse(namespace: str) -> list[str]:
        raise IcelensError("access denied")

    monkeypatch.setattr(fresh, "tables", refuse)
    app = IcelensApp(fresh)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert tree_labels(app) == ["demo  (0)"]
        unbroken(app, "a namespace that will not list")
    fresh.close()


# -- the command palette -----------------------------------------------------------------------------


async def test_ctrl_p_finds_a_table_by_part_of_its_name_and_opens_it(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+p")
        await until(app, pilot, lambda: isinstance(app.screen, CommandPalette), "the command palette")
        await pilot.press(*"evnts")  # fuzzy: not a substring, and still the table
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.ident == "demo.events"
        assert "demo.events" in app.sub_title
        unbroken(app, "the command palette")


# -- the Writer pane, given nothing to do ---------------------------------------------------------------


@pytest.fixture()
def sandbox(tmp_path: Path) -> Lake:
    lake = Lake(sandbox_target("paths", root=tmp_path))
    yield lake
    lake.close()


@pytest.mark.parametrize(
    ("typed", "said"),
    [("   ", "type a statement, then ctrl+r"), ("SELECT 1; EXPLAIN SELECT 2", "nothing committed - those statements read, or changed nothing")],
)
async def test_the_writer_says_when_there_is_nothing_to_commit(sandbox: Lake, typed: str, said: str) -> None:
    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        app.query_one("#write", TextArea).text = typed
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert said in note(app, "writer")
        assert app._written == []
        unbroken(app, "an empty commit")


@pytest.mark.duckdb_extensions
async def test_the_writer_keeps_what_committed_before_a_statement_failed(sandbox: Lake) -> None:
    """The first statement commits and stays committed; its record used to go down with the second."""
    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        app.query_one("#write", TextArea).text = "CREATE TABLE demo.t AS SELECT 1 AS x; INSERT INTO demo.t SELECT 'x'"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert [(w.kind, w.table, w.added) for w in app._written] == [("create", "demo.t", 1)]
        assert "append on demo.t failed" in note(app, "writer")
        assert "1 earlier statement committed before this one failed" in note(app, "writer")
        assert app.query_one("#box-writer", PaneBox).query_one(Static).has_class("error")
        unbroken(app, "a write that failed partway")


async def test_the_writer_survives_an_exception_nobody_anticipated(sandbox: Lake, monkeypatch) -> None:
    def explode(sql: str) -> list:
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(sandbox, "write", explode)
    app = IcelensApp(sandbox)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        app.query_one("#write", TextArea).text = "CREATE TABLE demo.t AS SELECT 1 AS x"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert note(app, "writer") == "RuntimeError: the disk went away"
        assert app.query_one("#box-writer", PaneBox).query_one(Static).has_class("error")
        unbroken(app, "a write that raised")


async def test_a_sandbox_on_a_catalog_opens_on_the_v3_lesson_and_says_where_it_writes(tmp_path: Path) -> None:
    base = Target(properties={"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}", "warehouse": tmp_path.as_uri()})
    lake = Lake(rest_sandbox_target("lesson", base), read_only=True)
    app = IcelensApp(lake)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        hint = app.query_one("#write", TextArea).text
        assert "a v3 sandbox in sandbox_lesson" in hint and "UPDATE sales" in hint
        status = str(app.query_one("#status", Static).content)
        assert "sandbox lesson" in status and "in sandbox_lesson, v3" in status
        unbroken(app, "a sandbox on a catalog")
    lake.close()


# -- panes and the SQL pane, when the reader raises -----------------------------------------------------


async def test_a_pane_whose_reader_raises_says_so_and_the_rest_still_work(app: IcelensApp, monkeypatch) -> None:
    def broken(view: model.View) -> model.Pane:
        raise ValueError("a manifest that is not Avro")

    monkeypatch.setitem(model.PANES, "files", broken)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("7")
        await settle(app, pilot)
        assert note(app, "files") == "ValueError: a manifest that is not Avro"
        await pilot.press("4")
        await settle(app, pilot)
        assert "3 snapshots" in note(app, "snapshots")
        unbroken(app, "a pane that raised")


async def test_the_sql_pane_survives_an_exception_nobody_anticipated(app: IcelensApp, monkeypatch) -> None:
    def explode(sql: str):  # type: ignore[no-untyped-def]
        raise ValueError("no such engine")

    monkeypatch.setattr(app.lake, "query", explode)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("0")
        app.query_one("#sql", TextArea).text = "SELECT 1"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert note(app, "sql") == "ValueError: no such engine"
        unbroken(app, "a query that raised")


async def test_stepping_through_a_table_whose_metadata_has_gone(tmp_path: Path, monkeypatch) -> None:
    told = notices(monkeypatch)
    lake = Lake(sandbox_target("gone", root=tmp_path))
    app = IcelensApp(lake)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.ident = "demo.vanished"  # selected, and then gone from under the app
        await pilot.press("comma")
        await settle(app, pilot)
        assert any("demo.vanished" in t for t in told)
        assert app.snapshot_id is None
        unbroken(app, "stepping a table that is not there")
    lake.close()


# -- the grid, at its edges ---------------------------------------------------------------------------


async def test_enter_on_a_cell_with_no_snapshot_in_it_goes_nowhere(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        grid = app.query_one("#grid-snapshots", Grid)
        grid.focus()
        grid.cursor_coordinate = grid.cursor_coordinate._replace(row=0, column=grid.source.column_names.index("parent"))
        await pilot.press("enter")  # the first snapshot's parent: there is none
        await settle(app, pilot)
        assert app.snapshot_id is None
        grid.cursor_coordinate = grid.cursor_coordinate._replace(row=0, column=grid.source.column_names.index("snapshot id"))
        await pilot.press("enter")
        await settle(app, pilot)
        pinned = app.snapshot_id
        assert pinned is not None
        await pilot.press("enter")  # the same one again: no second trip, no second notice
        await settle(app, pilot)
        assert app.snapshot_id == pinned
        assert grid.column_name(99) is None and grid.value_at(99, 0) is None
        unbroken(app, "enter on cells that are not a destination")


async def test_export_and_copy_with_nothing_to_give(app: IcelensApp, tmp_path: Path, monkeypatch) -> None:
    told = notices(monkeypatch)
    monkeypatch.chdir(tmp_path)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("0")  # the SQL pane, before anything has run: a grid with no source
        await settle(app, pilot)
        app.action_export()
        app.action_copy_cell()
        await pilot.pause()
        assert "nothing to export" in told
        assert not list(tmp_path.glob("icelens-*.csv"))
        unbroken(app, "exporting nothing")


async def test_an_export_with_nowhere_left_to_write_says_so_and_overwrites_nothing(app: IcelensApp, tmp_path: Path, monkeypatch) -> None:
    """Every name it would try is taken. It must fail loudly, never by writing over one of them."""
    told = notices(monkeypatch)
    monkeypatch.chdir(tmp_path)
    stem = "icelens-catalog-overview"
    (tmp_path / f"{stem}.csv").write_text("mine")
    for n in range(1, 1000):
        (tmp_path / f"{stem}-{n}.csv").write_text("mine")
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#grid-overview", Grid).focus()
        await pilot.press("x")
        await pilot.pause()
        assert any(t.startswith("export failed") for t in told)
        assert {p.read_text() for p in tmp_path.glob(f"{stem}*.csv")} == {"mine"}
        unbroken(app, "an export with nowhere to go")


async def test_the_limit_keys_off_the_data_pane_change_the_limit_and_nothing_else(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("right_square_bracket")
        await settle(app, pilot)
        assert app.limit == 20 and app.active_pane == "overview"
        assert "limit 20" in str(app.query_one("#status", Static).content)
        unbroken(app, "the limit keys on another pane")


# -- the pickers, dismissed --------------------------------------------------------------------------


async def test_ctrl_o_dismissed_or_failing_leaves_the_catalog_alone(app: IcelensApp, monkeypatch) -> None:
    from icelens import app as app_module

    told = notices(monkeypatch)
    before = app.lake.target

    def no_such(name: str) -> Target:
        raise IcelensError(f"no catalog named {name!r} in ~/.pyiceberg.yaml")

    monkeypatch.setattr(app_module, "list_profiles", lambda: {"lab": {"type": "rest", "uri": "http://x"}})
    monkeypatch.setattr(app_module, "profile_target", no_such)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+o")
        await until(app, pilot, lambda: isinstance(app.screen, Picker), "the catalog picker")
        await pilot.press("escape")
        await settle(app, pilot)
        assert app.lake.target is before
        await pilot.press("ctrl+o")
        await until(app, pilot, lambda: isinstance(app.screen, Picker), "the catalog picker")
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.lake.target is before
        assert "no catalog named 'lab' in ~/.pyiceberg.yaml" in told
        unbroken(app, "a catalog picker that went nowhere")


async def test_ctrl_o_with_no_profiles_says_so(app: IcelensApp, monkeypatch) -> None:
    from icelens import app as app_module

    told = notices(monkeypatch)
    monkeypatch.setattr(app_module, "list_profiles", lambda: {})
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+o")
        await settle(app, pilot)
        assert not isinstance(app.screen, Picker)
        assert "no catalogs in ~/.pyiceberg.yaml" in told


# -- the rail, for a term with nothing more to say -------------------------------------------------------


async def test_the_deep_rail_leaves_out_what_a_term_does_not_have(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        rail = app.query_one("#guide", GuideRail)
        bare = terms.Term(name="Bare [term]", gloss="A term with a gloss and nothing else at all.")
        rail.show(bare, "deep")
        await pilot.pause()
        shown = Content.from_markup(str(rail.content)).plain
        assert "Bare [term]" in shown and "nothing else at all" in shown
        assert "where:" not in shown and "next:" not in shown
        rail.show(None, "brief")
        await pilot.pause()
        assert not rail.display
        unbroken(app, "a bare term")


async def test_a_pane_that_needs_a_warehouse_hands_the_question_to_the_picker(app: IcelensApp, monkeypatch) -> None:
    """The catalog pane opens the catalog as well as the tree, so it can be the one that hears first."""
    asked: list[list[str]] = []

    async def ask(exc: NeedsWarehouse) -> None:
        asked.append(exc.options)

    def needs(lake: Lake) -> model.Pane:
        raise NeedsWarehouse("needs a warehouse", ["wh-a"])

    monkeypatch.setattr(model, "catalog_overview", needs)
    monkeypatch.setattr(app, "_ask_warehouse", ask)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert asked == [["wh-a"]]
        unbroken(app, "a pane that needed a warehouse")

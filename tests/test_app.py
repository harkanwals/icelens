"""The UI, driven by keystrokes the way a person drives it."""

from __future__ import annotations

import pytest
from harness import note, rail, rows, settle, unbroken
from textual.coordinate import Coordinate
from textual.widgets import Static, TextArea, Tree

from icelens.app import Grid, Help, IcelensApp, NodeData, PaneBox, Picker


async def test_the_tree_lists_the_catalog(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        tree = app.query_one("#nav")
        namespaces = [str(node.label) for node in tree.root.children]
        assert namespaces == ["demo  (2)"]
        leaves = [n.data.name for n in tree.root.children[0].children if isinstance(n.data, NodeData)]
        assert leaves == ["demo.empty", "demo.events"]
        assert app.known_tables == ["demo.empty", "demo.events"]
        # nothing selected yet, so the Overview pane describes the catalog itself
        assert "2 tables" in note(app, "overview")
        await pilot.press("down")  # onto the namespace node
        await settle(app, pilot)
        assert "namespace demo - 2 tables" in note(app, "overview")
        assert rows(app, "overview") == 2


async def test_opening_a_table_fills_every_pane(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        assert rows(app, "overview") > 5
        for key, pane in (("2", "schema"), ("4", "snapshots"), ("7", "files"), ("9", "layout")):
            await pilot.press(key)
            await settle(app, pilot)
            assert app.active_pane == pane
            assert rows(app, pane) > 0, f"{pane} came up empty"
            assert "Error" not in note(app, pane)


async def test_enter_on_a_snapshot_pins_it_everywhere(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        grid = app.query_one("#grid-snapshots", Grid)
        grid.focus()
        oldest = int(grid.source.column("snapshot id")[0].as_py())
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot_id == oldest
        await pilot.press("3")  # data, as of that snapshot
        await settle(app, pilot)
        assert rows(app, "data") == 10
        assert f"as of snapshot {oldest}" in note(app, "data")
        await pilot.press("u")
        await settle(app, pilot)
        assert app.snapshot_id is None


async def test_enter_on_a_file_travels_to_the_snapshot_that_wrote_it(app: IcelensApp) -> None:
    """Files names its snapshot column `added by`, and enter anywhere on the row should follow it."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("7")
        await settle(app, pilot)
        grid = app.query_one("#grid-files", Grid)
        grid.focus()
        grid.cursor_coordinate = Coordinate(0, 0)  # the `content` cell, not the snapshot one
        added_by = int(grid.source.column("added by")[0].as_py())
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot_id == added_by
        assert f"snapshot {added_by}" in app.query_one("#status", Static).content


async def test_enter_on_a_parent_cell_travels_to_the_parent(app: IcelensApp) -> None:
    """The column under the cursor wins, so enter on `parent` steps back one commit."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        grid = app.query_one("#grid-snapshots", Grid)
        grid.focus()
        column = grid.source.column_names.index("parent")
        grid.cursor_coordinate = Coordinate(1, column)  # the second snapshot: it has a parent
        parent = int(grid.source.column("parent")[1].as_py())
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.snapshot_id == parent
        assert parent == int(grid.source.column("snapshot id")[0].as_py())


async def test_stepping_walks_the_history_and_stops_at_both_ends(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        ids = [int(v) for v in app.query_one("#grid-snapshots", Grid).source.column("snapshot id").to_pylist()]
        assert len(ids) == 3
        await pilot.press("comma")  # from the current snapshot, back one
        await settle(app, pilot)
        assert app.snapshot_id == ids[1]
        await pilot.press("comma")
        await settle(app, pilot)
        assert app.snapshot_id == ids[0]
        await pilot.press("comma")  # nowhere older to go
        await settle(app, pilot)
        assert app.snapshot_id == ids[0]
        unbroken(app, "stepping off the oldest end")
        await pilot.press("full_stop")
        await settle(app, pilot)
        assert app.snapshot_id == ids[1]
        await pilot.press("full_stop")  # back onto the current snapshot: unpinned, not pinned
        await settle(app, pilot)
        assert app.snapshot_id is None
        await pilot.press("full_stop")
        await settle(app, pilot)
        assert app.snapshot_id is None
        unbroken(app, "stepping off the newest end")


async def test_stepping_every_pane_follows(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        assert rows(app, "data") == 10  # the app fixture limits to 10
        await pilot.press("comma", "comma")  # the first append: 50 rows, all of them id >= 1
        await settle(app, pilot)
        assert f"as of snapshot {app.snapshot_id}" in note(app, "data")
        await pilot.press("2")
        await settle(app, pilot)
        assert rows(app, "schema") == 4
        unbroken(app, "stepping with panes open")


async def test_stepping_without_a_table_says_so(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("comma")
        await settle(app, pilot)
        assert app.snapshot_id is None
        unbroken(app, "stepping with no table open")


async def test_stepping_a_table_with_no_snapshots(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.empty")
        await settle(app, pilot)
        await pilot.press("comma", "full_stop")
        await settle(app, pilot)
        assert app.snapshot_id is None
        unbroken(app, "stepping an empty table")


async def test_filtering_the_tree(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("slash")
        assert app.focused.id == "filter"
        await pilot.press("e", "m", "p", "t", "y")
        await settle(app, pilot)
        tree = app.query_one("#nav")
        leaves = [n.data.name for node in tree.root.children for n in node.children]
        assert leaves == ["demo.empty"]


async def test_row_limit_keys(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("3")
        await settle(app, pilot)
        assert rows(app, "data") == 10
        await pilot.press("right_square_bracket")
        await settle(app, pilot)
        assert app.limit == 20
        assert rows(app, "data") == 20


async def test_a_table_without_snapshots_does_not_break_the_ui(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.empty")
        await settle(app, pilot)
        await pilot.press("4")
        await settle(app, pilot)
        assert "no snapshots" in note(app, "snapshots")


@pytest.mark.duckdb_extensions
async def test_sql_pane_runs_against_the_bridged_catalog(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("0")
        app.query_one("#sql", TextArea).text = 'SELECT count(*) AS n FROM icelens."demo"."events"'
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert "1 rows" in note(app, "sql")
        assert not app.query_one("#box-sql", PaneBox).query_one("Static").has_class("error")


async def test_sql_pane_shows_a_bad_query_in_the_pane(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("0")
        app.query_one("#sql", TextArea).text = "SELECT * FROM nope"
        await pilot.press("ctrl+r")
        await settle(app, pilot)
        assert app.query_one("#box-sql", PaneBox).query_one("Static").has_class("error")


async def test_help_opens_and_closes(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("question_mark")
        await pilot.pause()
        assert isinstance(app.screen, Help)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, Help)


async def test_export_writes_the_full_value(app: IcelensApp, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("9")  # layout, which holds long file paths
        await settle(app, pilot)
        await pilot.press("x")
        await pilot.pause()
        written = list(tmp_path.glob("icelens-*.csv"))
        assert written and written[0].read_bytes().count(b"\n") > 3


async def test_switching_catalogs_from_inside_the_app(app: IcelensApp, monkeypatch) -> None:
    """ctrl+o lists the catalogs in ~/.pyiceberg.yaml and rebinds the app to the chosen one.

    The target handed back is the same test catalog, so what this asserts is the mechanism:
    the picker opens, a choice rebinds the Lake, and the tree reloads against it.
    """
    from icelens import app as app_module

    same = app.lake.target
    monkeypatch.setattr(app_module, "list_profiles", lambda: {"lab": dict(same.properties), "prod": {}})
    monkeypatch.setattr(app_module, "profile_target", lambda name: same)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert isinstance(app.screen, Picker)
        assert len(app.screen.options) == 2
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert not isinstance(app.screen, Picker)
        tree = app.query_one("#nav", Tree)
        assert [str(node.label) for node in tree.root.children] == ["demo  (2)"]


@pytest.mark.duckdb_extensions
async def test_the_status_bar_names_the_catalog_type_and_the_route(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        status = str(app.query_one("#status", Static).content)
        assert "sql catalog" in status
        assert "duckdb, scanning metadata files" in status


async def test_the_guide_follows_the_cursor(app: IcelensApp) -> None:
    """The rail is the feature: it describes the cell you are on, and changes when you move."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # nothing selected, tree focused: the thing being navigated is the catalog itself
        assert "Catalog" in rail(app)

        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")  # Snapshots
        await settle(app, pilot)
        grid = app.query_one("#grid-snapshots", Grid)
        grid.focus()
        await pilot.pause()

        seen = []
        for _ in range(len(grid.source.column_names)):
            await pilot.pause()
            seen.append((grid.column_name(grid.cursor_coordinate.column), rail(app)))
            await pilot.press("right")
        by_column = dict(seen)
        assert "Snapshot" in by_column["#"]
        assert "append" in by_column["operation"].lower()
        assert "Snapshot id" in by_column["snapshot id"]
        assert "Sequence number" in by_column["seq"]
        assert "Schema id" in by_column["schema"]
        # four different terms at least, or it is not following anything
        assert len({text.split("  ")[0] for _, text in seen}) >= 4



async def test_arriving_at_a_pane_names_the_pane_then_hands_back_to_the_cell(app: IcelensApp) -> None:
    """Being told what the *column* means presumes you know what the pane is. On arrival, say that."""
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("6")  # Manifests
        await settle(app, pilot)
        grid = app.query_one("#grid-manifests", Grid)
        grid.focus()
        await pilot.pause()
        # the pane's own term, not the term for whatever happens to be in column zero
        assert "Manifest" in rail(app)
        assert "File content" not in rail(app)

        await pilot.press("right")
        await pilot.pause()
        assert "Snapshot id" in rail(app)  # "added by": the cell, from here on

        await pilot.press("left")
        await pilot.pause()
        assert "File content" in rail(app)  # back on column zero, and still the cell

        # a different table refills every pane, so the pane is worth naming again
        app.open_table("demo.events")
        await settle(app, pilot)
        grid.focus()
        await pilot.pause()
        assert "Manifest" in rail(app)
        assert "File content" not in rail(app)


async def test_the_guide_reads_the_row_in_a_key_value_pane(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("1")
        await settle(app, pilot)
        grid = app.query_one("#grid-overview", Grid)
        grid.focus()
        labels = [grid.value_at(r, 0) for r in range(grid.source.num_rows)]
        wanted = labels.index("format version")
        for _ in range(wanted):
            await pilot.press("down")
        await pilot.pause()
        assert "Format version" in rail(app)
        assert "1, 2 or 3" in rail(app) or "revision of the Iceberg spec" in rail(app)


async def test_g_cycles_the_guide(app: IcelensApp) -> None:
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        guide = app.query_one("#guide", Static)
        # It opens in full: someone new to icelens is the reader the rail is for.
        assert app.guide_mode == "deep" and guide.display
        deep = rail(app)
        assert "next:" in deep  # the trail of related terms is a deep-mode thing

        await pilot.press("g")
        await pilot.pause()
        assert app.guide_mode == "brief"
        assert len(rail(app)) < len(deep) and "g for more" in rail(app)

        await pilot.press("g")
        await pilot.pause()
        assert app.guide_mode == "off" and not guide.display

        await pilot.press("g")
        await pilot.pause()
        assert app.guide_mode == "deep" and guide.display


async def test_a_catalog_that_needs_a_warehouse_asks_instead_of_drawing_an_empty_tree(
    app: IcelensApp, monkeypatch
) -> None:
    """A REST catalog hosting several warehouses must produce a question, not a blank screen."""
    from icelens.catalog import NeedsWarehouse

    real = app.lake.namespaces
    asked: list[str] = []

    def once(*args, **kwargs):
        if not asked:
            asked.append("raised")
            raise NeedsWarehouse("needs a warehouse", ["wh-a", "wh-b"])
        return real(*args, **kwargs)

    picked: list[str] = []
    monkeypatch.setattr(app.lake, "namespaces", once)
    monkeypatch.setattr(app, "_use_warehouse", lambda name: picked.append(name))

    async with app.run_test() as pilot:
        # not `settle`: the tree worker is *awaiting the answer*, so it stays running until
        # the question is answered - which is the whole shape of this feature.
        for _ in range(200):
            await pilot.pause()
            if isinstance(app.screen, Picker):
                break
        picker = app.screen
        assert isinstance(picker, Picker), f"expected the warehouse picker, got {type(picker).__name__}"
        assert [value for value, _ in picker.options] == ["wh-a", "wh-b"]

        # a second worker hitting the same NeedsWarehouse must not stack a second modal
        depth = len(app.screen_stack)
        await app._ask_warehouse(NeedsWarehouse("needs a warehouse", ["wh-a", "wh-b"]))
        assert len(app.screen_stack) == depth

        # down, then enter: the second warehouse, so this proves a choice was made rather
        # than the first row being taken by default
        await pilot.press("down", "enter")
        await settle(app, pilot)
        assert picked == ["wh-b"]


async def test_l_toggles_row_lineage_and_lands_on_the_data_pane(app: IcelensApp) -> None:
    """The toggle is only ever about the Data pane, so pressing it goes there.

    `demo.events` is v2, so the columns cannot appear - and the note has to say that rather
    than leave a person wondering whether they pressed the key.
    """
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.open_table("demo.events")
        await settle(app, pilot)
        await pilot.press("4")  # somewhere that is not Data
        await settle(app, pilot)

        await pilot.press("l")
        await settle(app, pilot)
        assert app.lineage is True
        assert app.active_pane == "data"
        assert "row lineage is v3 only" in note(app, "data")
        assert "row lineage on" in str(app.query_one("#status", Static).render())

        await pilot.press("l")
        await settle(app, pilot)
        assert app.lineage is False
        assert "row lineage" not in note(app, "data")


async def test_a_narrow_screen_stacks_the_catalog_above_the_table(app: IcelensApp) -> None:
    # A phone running the web demo is about 47 columns; side by side, the 36-column catalog
    # left the table and the guide four columns between them.
    async with app.run_test(size=(47, 40)) as pilot:
        await settle(app, pilot)
        sidebar, content = app.query_one("#sidebar").region, app.query_one("#content").region
        assert sidebar.bottom <= content.y
        assert content.width == 47
        # and no one cell takes the row: the fixture's 30-character cap becomes 20
        app.open_table("demo.events")
        await settle(app, pilot)
        grid = app.query_one("#grid-overview", Grid)
        assert max(column.content_width for column in grid.columns.values()) <= 20

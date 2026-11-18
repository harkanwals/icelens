"""What the screens look like, held still: a change to one is a diff somebody has to approve.

The behaviour tests (`test_driving.py` and friends) say a pane works; these say it still looks the
way it did - nothing cut off at a size it used to fit, no label lost, no column moved. Only
screens whose content is the same on every run are here: no timestamps, no snapshot ids, no
temp paths. Anything that shows DuckDB's route needs its extensions, and is marked so.

`pytest tests/test_snapshots.py --snapshot-update` rewrites them; read the diff before committing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import settle
from snapshots import assert_matches, screen_text

from icelens import demo
from icelens.app import IcelensApp
from icelens.catalog import Lake, Sandbox, sandbox_target
from icelens.welcome import Welcome


@pytest.fixture()
def update(request: pytest.FixtureRequest) -> bool:
    return bool(request.config.getoption("--snapshot-update"))


@pytest.fixture(scope="module")
def demo_lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    lake = Lake(sandbox_target("demo", root=tmp_path_factory.mktemp("snapshots")), read_only=False)
    demo.seed(lake)
    yield lake
    lake.close()


# -- the first run -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "sandboxes"),
    [
        ("welcome-first-run", []),
        ("welcome-with-sandboxes", [Sandbox("demo", 1, 2), Sandbox("scratch", 0, 3)]),
    ],
)
async def test_welcome(name: str, sandboxes: list[Sandbox], update: bool) -> None:
    app = Welcome(sandboxes, v3_writes=True)
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        assert_matches(name, screen_text(app), update)


async def test_welcome_where_duckdb_cannot_write_v3(update: bool) -> None:
    app = Welcome([], v3_writes=False)
    async with app.run_test(size=(100, 34)) as pilot:
        await pilot.pause()
        assert_matches("welcome-without-v3", screen_text(app), update)


# -- the app -----------------------------------------------------------------------------------


@pytest.mark.duckdb_extensions
@pytest.mark.parametrize(("name", "size"), [("schema", (120, 34)), ("schema-narrow", (60, 20))])
async def test_schema_pane(demo_lake: Lake, name: str, size: tuple[int, int], update: bool) -> None:
    app = IcelensApp(demo_lake, ident=demo.TABLE)
    async with app.run_test(size=size) as pilot:
        await settle(app, pilot)
        await pilot.press("2")
        await settle(app, pilot)
        assert_matches(name, screen_text(app), update)


@pytest.mark.duckdb_extensions
async def test_writer_pane_on_a_sandbox(demo_lake: Lake, update: bool) -> None:
    app = IcelensApp(demo_lake, ident=demo.TABLE)
    async with app.run_test(size=(120, 34)) as pilot:
        await settle(app, pilot)
        await pilot.press("e")
        await settle(app, pilot)
        assert_matches("writer", screen_text(app), update)


@pytest.mark.duckdb_extensions
async def test_help_fits_its_lines_and_scrolls_to_the_end(demo_lake: Lake, update: bool) -> None:
    """It once wrapped every long line at 120 columns, and cut off its last third with no scroll."""
    app = IcelensApp(demo_lake, ident=demo.TABLE)
    async with app.run_test(size=(120, 40)) as pilot:
        await settle(app, pilot)
        await pilot.press("2")  # Overview shows file names, which differ per run; Schema does not
        await settle(app, pilot)
        await pilot.press("question_mark")
        await settle(app, pilot)
        assert_matches("help", screen_text(app), update)
        await pilot.press("end")
        await settle(app, pilot)
        assert_matches("help-end", screen_text(app), update)


def test_every_snapshot_file_has_a_test() -> None:
    """A snapshot nothing compares against is a picture of a screen that may no longer exist."""
    names = {p.stem for p in (Path(__file__).parent / "snapshots").glob("*.txt")}
    used = {"welcome-first-run", "welcome-with-sandboxes", "welcome-without-v3", "schema", "schema-narrow", "writer", "help", "help-end"}
    assert names == used

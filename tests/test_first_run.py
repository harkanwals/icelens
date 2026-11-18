"""The first thirty seconds: running `icelens` with nothing configured.

It used to start anyway and greet a newcomer with PyIceberg's "URI missing" over an empty
tree. Now a terminal gets asked what to open - a sandbox with a demo table, by default -
and anything without a terminal gets told how to ask for one. The demo itself is pinned
commit by commit, because the README and the guide rail describe it and a demo that drifts
from its description teaches the wrong thing.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from headless import clean_environment, run

from icelens import cli, demo, model
from icelens.catalog import Lake, NoCatalog, Sandbox, Target, discover, list_sandboxes, sandbox_target
from icelens.welcome import CONNECT, Welcome, choices, fresh_name


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A machine with nothing on it: no variables, no ~/.pyiceberg.yaml, no .env, no sandboxes."""
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture()
def seeded(tmp_path: Path) -> Lake:
    lake = Lake(sandbox_target("demo", tmp_path / "sandboxes"))
    demo.seed(lake)
    yield lake
    lake.close()


# -- the demo -------------------------------------------------------------------------


def test_the_demo_has_the_history_it_describes(seeded: Lake) -> None:
    metadata = seeded.table(demo.TABLE).metadata
    history = [
        (str(s.summary.operation).rsplit(".", 1)[-1].lower(), s.summary.get("added-records"), s.summary.get("deleted-records"))
        for s in sorted(metadata.snapshots, key=lambda s: s.sequence_number)
    ]
    assert history == [
        ("append", "120", None),
        ("append", "30", None),
        ("overwrite", "108", "120"),  # deleting 12 test rows rewrote all 120: copy-on-write
        ("append", "20", None),
        ("append", "25", None),
    ]
    assert sorted(metadata.refs) == ["first-load", "main"]
    assert metadata.refs["first-load"].snapshot_id == min(metadata.snapshots, key=lambda s: s.sequence_number).snapshot_id
    assert (len(metadata.schemas), len(metadata.partition_specs), metadata.format_version) == (2, 2, 2)


def test_the_demos_rename_keeps_its_field_id(seeded: Lake) -> None:
    metadata = seeded.table(demo.TABLE).metadata
    before, after = (dict((f.name, f.field_id) for f in schema.fields) for schema in metadata.schemas)
    assert before["customer"] == after["customer_id"] == 4
    assert "customer" not in after and after["channel"] == 6


def test_every_pane_has_something_to_say_about_the_demo(seeded: Lake) -> None:
    for name, pane in model.PANES.items():
        result = pane(model.View(seeded, demo.TABLE, limit=500))
        assert result.data.num_rows > 0, name
    assert model.PANES["data"](model.View(seeded, demo.TABLE, limit=500)).data.num_rows == 183


def test_the_schema_pane_partitions_as_the_viewed_snapshot_was_written(seeded: Lake) -> None:
    """The demo is partitioned by region from snapshot 5. Viewed at snapshot 1 the pane showed
    `region` as an identity partition all the same - today's spec, put on yesterday's table."""
    ordered = sorted(seeded.table(demo.TABLE).metadata.snapshots, key=lambda s: s.sequence_number)

    def partition(snapshot: int | None) -> dict[str, str | None]:
        rows = model.schema(model.View(seeded, demo.TABLE, snapshot_id=snapshot)).records()
        return {row["name"]: row["partition"] for row in rows if row["partition"]}

    assert partition(ordered[0].snapshot_id) == {}
    assert partition(ordered[3].snapshot_id) == {}, "snapshot 4 came just before the spec changed"
    assert partition(ordered[4].snapshot_id) == {"region": "identity"}
    assert partition(None) == {"region": "identity"}


def test_time_travel_to_the_tag_finds_the_first_load(seeded: Lake) -> None:
    tagged = seeded.table(demo.TABLE).metadata.refs["first-load"].snapshot_id
    rows, _ = seeded.rows(demo.TABLE, limit=1000, snapshot_id=tagged)
    assert rows.num_rows == 120
    assert rows.column("region").to_pylist().count("test") == 12


def test_the_demo_is_made_once(seeded: Lake) -> None:
    again = demo.seed(seeded)
    assert (again.created, again.snapshots, again.rows) == (False, 5, 183)
    assert again.line() == "already there: demo.orders - 5 snapshots, 183 rows"


def test_the_demo_is_written_only_into_a_sandbox_of_this_tools_own(tmp_path: Path) -> None:
    from icelens.catalog import rest_sandbox_target

    real = Lake(Target({"type": "sql", "uri": f"sqlite:///{tmp_path / 'c.db'}", "warehouse": tmp_path.as_uri()}))
    somebody_elses = Lake(rest_sandbox_target("x", Target({"type": "rest", "uri": "http://127.0.0.1:1/catalog"})))
    for lake in (real, somebody_elses):
        with pytest.raises(ValueError, match="sandbox of this tool's own"):
            demo.seed(lake)


# -- nothing configured ---------------------------------------------------------------


def test_nothing_configured_is_recognised() -> None:
    assert discover(search_dotenv=False).unconfigured
    assert not Target({"type": "glue"}).unconfigured  # a Glue catalog needs no URI
    assert not Target({"uri": "http://x.example/c"}).unconfigured
    assert not Target({"warehouse": "w"}).unconfigured


def test_a_headless_run_says_how_to_get_a_catalog(capsys: pytest.CaptureFixture[str]) -> None:
    """Not PyIceberg's "URI missing, please provide … PYICEBERG_CATALOG__DEFAULT__URI"."""
    ran = run(capsys, ["--list"])
    assert ran.code == 1 and ran.out == ""
    assert "icelens --demo" in ran.err and "URI missing" not in ran.err


def test_nothing_configured_has_its_own_json_kind(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    ran = run(capsys, ["--pane", "overview", "demo.orders", "--json"])
    assert ran.code == 1 and ran.out == ""
    assert json.loads(ran.err)["error"]["kind"] == "NoCatalog"


def test_the_app_without_a_terminal_says_it_too(capsys: pytest.CaptureFixture[str]) -> None:
    ran = run(capsys, [])
    assert ran.code == 1 and "icelens --demo" in ran.err


# -- --demo ---------------------------------------------------------------------------


def test_demo_makes_a_local_catalog_and_fills_it(capsys: pytest.CaptureFixture[str], home: Path) -> None:
    ran = run(capsys, ["--demo", "--list"])
    assert ran.code == 0, ran.err
    assert "made demo.orders - 5 snapshots, 183 rows" in ran.err
    assert "sandbox 'demo' - 1 namespaces, 1 tables" in ran.err  # a note, so stderr
    assert ran.out.splitlines()[1].split()[:2] == ["demo", "1"]
    assert (home / ".local" / "share" / "icelens" / "sandboxes" / "demo" / "catalog.db").is_file()


def test_demo_goes_into_a_named_sandbox_too(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    ran = run(capsys, ["--sandbox", "lesson", "--demo", "--sandbox-dir", str(tmp_path / "s"), "--pane", "snapshots", "demo.orders", "--json"])
    assert ran.code == 0, ran.err
    assert ran.err == ""  # --json: the "made demo.orders" line is a person's, not a program's
    assert [sandbox.name for sandbox in list_sandboxes(tmp_path / "s")] == ["lesson"]


def test_demo_and_reset_start_the_history_again(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    where = ["--sandbox-dir", str(tmp_path / "s")]
    assert run(capsys, ["--demo", *where, "--write", "DELETE FROM demo.orders WHERE order_id < 50"]).code == 0
    ran = run(capsys, ["--demo", "--sandbox-reset", *where, "--list", "demo"])
    assert "made demo.orders" in ran.err


def test_demo_refuses_a_rest_sandbox(capsys: pytest.CaptureFixture[str]) -> None:
    ran = run(capsys, ["--demo", "--rest", "--uri", "http://x.example/c"])
    assert ran.code == 1 and "--demo" in ran.err and "--rest" in ran.err


def test_demo_refuses_connection_settings(capsys: pytest.CaptureFixture[str]) -> None:
    ran = run(capsys, ["--demo", "--uri", "http://x.example/c"])
    assert ran.code == 1 and "takes no connection settings" in ran.err


# -- sandboxes made before -----------------------------------------------------------


def test_local_catalogs_are_listed_with_their_tables(tmp_path: Path, seeded: Lake) -> None:
    root = tmp_path / "sandboxes"
    assert Lake(sandbox_target("empty", root)).namespaces() == []  # made, and never written to
    (root / "not-a-sandbox").mkdir()
    (root / "broken").mkdir()
    (root / "broken" / "catalog.db").write_bytes(b"not sqlite at all")
    assert list_sandboxes(root) == [Sandbox("broken", None, 2), Sandbox("demo", 1, 2), Sandbox("empty", 0, 2)]
    assert list_sandboxes(tmp_path / "nowhere") == []


def test_listing_never_writes_to_a_catalog(tmp_path: Path, seeded: Lake) -> None:
    database = tmp_path / "sandboxes" / "demo" / "catalog.db"
    before = database.stat().st_mtime_ns
    list_sandboxes(tmp_path / "sandboxes")
    assert database.stat().st_mtime_ns == before
    with sqlite3.connect(database) as con:  # and leaves it a working catalog
        assert con.execute("SELECT count(*) FROM iceberg_tables").fetchone() == (1,)


# -- the welcome screen ---------------------------------------------------------------


def test_with_nothing_made_the_demos_come_first() -> None:
    assert [value for value, _, _ in choices([])] == ["demo", "demo-v3", "empty", "connect", "quit"]


def test_a_sandbox_made_before_comes_first_and_no_demo_is_made_twice() -> None:
    offered = choices([Sandbox("demo", 1, 2), Sandbox("demo-v3", 1, 3), Sandbox("teaching", 3, 2)])
    assert [value for value, _, _ in offered] == [
        "sandbox:demo", "sandbox:demo-v3", "sandbox:teaching", "empty", "connect", "quit",
    ]
    assert offered[1][2].startswith("1 table, v3")
    assert offered[2][2].startswith("3 tables, v2")
    assert choices([Sandbox("x", None, 2)])[0][2].startswith("tables unknown")


def test_the_v3_demo_says_when_duckdb_cannot_write_here() -> None:
    [v3] = [detail for value, _, detail in choices([], v3_writes=False) if value == "demo-v3"]
    assert "needs DuckDB's iceberg extension" in v3
    [v3] = [detail for value, _, detail in choices([], v3_writes=True) if value == "demo-v3"]
    assert "needs" not in v3


def test_an_empty_sandbox_gets_a_name_nobody_has_used() -> None:
    assert fresh_name([]) == "default"
    assert fresh_name([Sandbox("default", 3, 2), Sandbox("scratch", 0, 3)]) == "scratch-2"


async def test_enter_takes_the_first_choice() -> None:
    app = Welcome([])
    async with app.run_test() as pilot:
        await pilot.press("enter")
    assert app.return_value == "demo"


async def test_the_arrow_keys_reach_the_rest() -> None:
    app = Welcome([Sandbox("teaching", 3, 2)])
    async with app.run_test() as pilot:
        await pilot.press("down", "down", "down", "down", "enter")
    assert app.return_value == "connect"


@pytest.mark.parametrize("key", ["escape", "q"])
async def test_escape_or_q_quits(key: str) -> None:
    app = Welcome([])
    async with app.run_test() as pilot:
        await pilot.press(key)
    assert app.return_value == ""


async def test_choosing_quit_quits() -> None:
    app = Welcome([])
    async with app.run_test() as pilot:
        await pilot.press("down", "down", "down", "down", "enter")
    assert app.return_value == ""


async def test_a_sandbox_name_is_never_markup() -> None:
    """A directory called `[b]x[/b]` or `[/]` is text, not a style and not a crash."""
    app = Welcome([Sandbox("[/]", 1, 2), Sandbox("[link=http://x]y", 2, 3)])
    async with app.run_test() as pilot:
        await pilot.pause()
        labels = [str(label.content) for label in app.query("Label")]
    assert any("[/]" in text for text in labels)
    assert any("[link=http://x]y" in text for text in labels)


# -- what the answer opens ------------------------------------------------------------


class _Opened:
    """Stands in for the main app: records what it was asked to open."""

    calls: list[tuple[str, bool, str | None]] = []

    def __init__(self, lake: Lake, ident: str | None = None, **_: object) -> None:
        self.lake, self.ident = lake, ident

    def run(self) -> None:
        tables = list_sandboxes(Path.home() / ".local" / "share" / "icelens" / "sandboxes")
        _Opened.calls.append((self.lake.target.sandbox, self.lake.is_sandbox, self.ident))
        _Opened.tables = [(s.name, s.tables) for s in tables]  # type: ignore[attr-defined]


@pytest.fixture()
def answered(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    import icelens.app

    _Opened.calls = []
    monkeypatch.setattr(icelens.app, "IcelensApp", _Opened)
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    return monkeypatch


def _answer(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setattr(Welcome, "run", lambda self: value)


def test_choosing_the_demo_opens_it_on_the_table(answered: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _answer(answered, "demo")
    assert cli.main([]) == 0
    assert _Opened.calls == [("demo", True, "demo.orders")]
    assert _Opened.tables == [("demo", 1)]  # type: ignore[attr-defined]


def test_choosing_empty_opens_an_empty_local_catalog(answered: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _answer(answered, "empty")
    assert cli.main([]) == 0
    assert _Opened.calls == [("default", True, None)]
    assert _Opened.tables == [("default", 0)]  # type: ignore[attr-defined]


@pytest.mark.duckdb_extensions
def test_choosing_a_catalog_made_before_opens_it(answered: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(capsys, ["--sandbox", "teaching", "--write", "CREATE TABLE t.x AS SELECT 1 AS a"]).code == 0
    _answer(answered, "sandbox:teaching")
    assert cli.main([]) == 0
    assert _Opened.calls == [("teaching", True, None)]


def test_choosing_connect_prints_how_and_opens_nothing(answered: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _answer(answered, "connect")
    assert cli.main([]) == 0
    assert capsys.readouterr().out == CONNECT + "\n"
    assert _Opened.calls == []


def test_quitting_opens_nothing_and_makes_nothing(answered: pytest.MonkeyPatch, home: Path) -> None:
    _answer(answered, "")
    assert cli.main([]) == 0
    assert _Opened.calls == []
    assert not (home / ".local").exists()


def test_a_configured_catalog_is_never_asked_about(answered: pytest.MonkeyPatch, tmp_path: Path) -> None:
    asked: list[bool] = []
    answered.setattr(Welcome, "run", lambda self: asked.append(True) or "")
    database = tmp_path / "real.db"
    assert cli.main(["--uri", f"sqlite:///{database}", "--warehouse", tmp_path.as_uri(), "-p", "type=sql"]) == 0
    assert asked == [] and len(_Opened.calls) == 1


def test_no_catalog_is_an_icelens_error() -> None:
    assert "icelens --demo" in str(NoCatalog())


@pytest.mark.duckdb_extensions
def test_choosing_the_v3_demo_opens_a_v3_sandbox_on_its_table(answered: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    _answer(answered, "demo-v3")
    assert cli.main([]) == 0
    assert _Opened.calls == [("demo-v3", True, "demo.orders")]
    [made] = list_sandboxes(Path.home() / ".local" / "share" / "icelens" / "sandboxes")
    assert (made.name, made.tables, made.version) == ("demo-v3", 1, 3)

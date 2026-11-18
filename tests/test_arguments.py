"""Every flag the CLI takes, every combination it refuses, and the flag each refusal names.

The rule these hold the CLI to is the one its own comments state: a flag that cannot do what it
says is refused by name, rather than ignored. Ignoring one reads as something else having gone
wrong - a connection that quietly fell back to local, a .env that was never read, a catalog
"emptied" that was never reached - and the error that eventually surfaces names the wrong thing.

Exit codes are the README's: 0 fine, 1 refused, 2 bad usage. The JSON shape of each failure is
`test_json_contract.py`'s business; this file is about the words.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest
from headless import Ran, argv, clean_environment, run, strict

from icelens.catalog import Lake
from icelens.cli import PANE_NAMES, build_parser


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """No ICELENS_* settings, no ~/.pyiceberg.yaml, no .env, and a home the .env walk stops at."""
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def refused(ran: Ran, *words: str, code: int = 1) -> str:
    """A refusal: the exit code, nothing on stdout, and stderr naming what it has to name."""
    assert ran.code == code, (ran.code, ran.out, ran.err)
    assert ran.out == "", ran.out
    assert "Traceback" not in ran.err
    for word in words:
        assert word in ran.err, f"{word!r} not in {ran.err!r}"
    return ran.err


def namespaces(ran: Ran) -> list[str]:
    assert ran.code == 0, ran.err
    return [row["namespace"] for row in strict(ran.out)["rows"]]


# -- bad usage: argparse's, exit 2 -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "named"),
    [
        (["--sandbox", "s", "--serve", "--port", "0"], "--port"),
        (["--sandbox", "s", "--serve", "--port", "70000"], "--port"),
        (["--sandbox", "s", "--serve", "--port", "http"], "--port"),
        (["--limit", "-5", "--pane", "data", "demo.events"], "--limit"),
        (["--limit", "ten", "--pane", "data", "demo.events"], "--limit"),
        (["--width", "wide", "--pane", "files", "demo.events"], "--width"),
        (["--snapshot", "latest", "--pane", "overview", "demo.events"], "--snapshot"),
        (["--pane", "nonsense", "demo.events"], "--pane"),
        (["--pane", "overview"], "--pane needs a table"),
        (["--list", "demo", "-p"], "-p/--property"),
        (["--no-such-flag"], "--no-such-flag"),
        (["demo.events", "demo.empty"], "unrecognized arguments: demo.empty"),
    ],
)
def test_bad_usage_exits_2_and_names_the_flag(lake: Lake, capsys, args: list[str], named: str) -> None:
    refused(run(capsys, argv(lake, *args)), named, code=2)


def test_an_unknown_pane_lists_the_ones_there_are(lake: Lake, capsys) -> None:
    err = refused(run(capsys, argv(lake, "--pane", "nonsense", "demo.events")), code=2)
    assert all(pane in err for pane in PANE_NAMES)


# -- refusals: exit 1, in words that name the flag -----------------------------------------------------


@pytest.mark.parametrize(
    "connection",
    [["--uri", "http://localhost:8181/catalog"], ["--warehouse", "w"], ["--catalog", "lab"], ["-p", "s3.endpoint=x"], ["--env-file", "{env}"]],
)
def test_an_offline_sandbox_takes_no_connection_setting(home: Path, capsys, connection: list[str]) -> None:
    """Every one of them would otherwise be ignored, which looks like a connection that failed over."""
    env = home / "real.env"
    env.write_text("ACME_CATALOG_URI=http://localhost:8181/catalog\n")
    connection = [part.format(env=env) for part in connection]
    ran = run(capsys, ["--sandbox", "s", "--sandbox-dir", str(home / "sandboxes"), *connection, "--list"])
    refused(ran, "--sandbox makes its own catalog", "takes no connection settings", "--rest")
    assert not (home / "sandboxes").exists(), "a refused command must not have made the sandbox"


@pytest.mark.parametrize("flag", [["--sandbox-reset"], ["--sandbox-dir", "{home}"], ["--write", "SELECT 1"], ["--rest"]])
def test_a_sandbox_flag_needs_a_sandbox(lake: Lake, home: Path, capsys, flag: list[str]) -> None:
    flag = [part.format(home=home) for part in flag]
    refused(run(capsys, argv(lake, *flag, "--list")), "--sandbox-reset, --sandbox-dir, --rest and --write need --sandbox")


def test_rest_and_a_sandbox_dir_do_not_go_together(home: Path, capsys) -> None:
    refused(run(capsys, ["--sandbox", "--rest", "--sandbox-dir", str(home), "--list"]), "--sandbox-dir is for an offline sandbox")


def test_rest_without_a_catalog_says_which_settings_it_needs(capsys) -> None:
    refused(run(capsys, ["--sandbox", "lesson", "--rest", "--list"]), "--rest puts the sandbox inside a catalog", "--uri", "--catalog")


@pytest.mark.parametrize("which", ["missing", "directory"])
def test_an_env_file_that_is_not_a_file(home: Path, capsys, which: str) -> None:
    path = home / "nope.env" if which == "missing" else home
    refused(run(capsys, ["--env-file", str(path), "--list"]), "no such --env-file", str(path))


def test_no_env_and_an_env_file_contradict_each_other(lake: Lake, home: Path, capsys) -> None:
    """--no-env used to win in silence, and the file the caller named was never read."""
    env = home / "lake.env"
    env.write_text(f"ACME_CATALOG_URI={lake.target.uri}\nACME_WAREHOUSE={lake.target.warehouse}\n")
    env.chmod(0o600)
    refused(run(capsys, ["--no-env", "--env-file", str(env), "--list"]), "--no-env", "--env-file")


@pytest.mark.parametrize("pair", ["nonsense", "s3.endpoint"])
def test_a_property_must_be_key_equals_value(lake: Lake, capsys, pair: str) -> None:
    refused(run(capsys, argv(lake, "-p", pair, "--list")), "--property expects KEY=VALUE", repr(pair))


@pytest.mark.parametrize("name", ["../escape", "a/b", "has space", ".", ".."])
def test_a_sandbox_name_is_a_directory_name(home: Path, capsys, name: str) -> None:
    ran = run(capsys, ["--sandbox", name, "--sandbox-dir", str(home / "sandboxes"), "--list"])
    refused(ran, "a sandbox name is letters, digits, dot, dash or underscore", repr(name))


def test_a_reset_refuses_a_directory_that_is_not_a_sandbox(home: Path, capsys) -> None:
    mine = home / "sandboxes" / "thesis"
    mine.mkdir(parents=True)
    (mine / "chapter-1.tex").write_text("years of work")
    ran = run(capsys, ["--sandbox", "thesis", "--sandbox-dir", str(home / "sandboxes"), "--sandbox-reset", "--list"])
    refused(ran, "does not look like a sandbox", "chapter-1.tex", "not touching it")
    assert (mine / "chapter-1.tex").read_text() == "years of work"


def test_listing_a_namespace_that_is_not_there(lake: Lake, capsys) -> None:
    """It used to be PyIceberg's NoSuchNamespaceError, raw, and a traceback."""
    refused(run(capsys, argv(lake, "--list", "nope")), "cannot list the tables in nope", "does not exist")


def test_a_profile_that_is_not_there_is_named(capsys) -> None:
    refused(run(capsys, ["--catalog", "nope", "--list"]), "'nope'")


def test_resetting_a_rest_sandbox_on_a_catalog_that_is_down_says_so_and_claims_nothing(capsys) -> None:
    """It used to print "emptied sandbox_lesson, 0 tables" first - about a catalog it never reached."""
    ran = run(capsys, ["--sandbox", "lesson", "--rest", "--uri", "http://127.0.0.1:1/catalog", "--sandbox-reset", "--list"])
    err = refused(ran, "unreachable")
    assert "emptied" not in err


# -- what needs no catalog at all -----------------------------------------------------------------------


def test_version_wins_over_everything_else_on_the_line(capsys) -> None:
    from importlib.metadata import version

    ran = run(capsys, ["--version", "-p", "nonsense", "--env-file", "/no/such/file", "--sandbox", "../bad", "--list"])
    assert (ran.code, ran.out.strip(), ran.err) == (0, version("icelens"), "")


@pytest.mark.parametrize("asked", [["--explain", "snapshot"], ["--explain"], ["--catalogs"]])
def test_the_vocabulary_and_the_catalog_list_are_answered_before_connecting(capsys, asked: list[str]) -> None:
    """A dead catalog address on the same line is never even tried."""
    ran = run(capsys, ["--uri", "http://127.0.0.1:1/catalog", *asked])
    assert "unreachable" not in ran.err
    assert ran.code in (0, 1) and (ran.out or "no catalogs" in ran.err)


def test_help_lists_every_flag_the_parser_has(capsys) -> None:
    """A drift guard: a flag added without help text is a flag nobody finds."""
    ran = run(capsys, ["--help"])
    assert ran.code == 0
    for action in build_parser()._actions:
        for spelling in action.option_strings:
            assert spelling in ran.out, f"{spelling} is missing from --help"
        if action.option_strings and action.help is not argparse.SUPPRESS:
            assert action.help, f"{action.option_strings} has no help text"


# -- one question per run -----------------------------------------------------------------------------


def test_one_run_answers_one_question_in_a_fixed_order(lake: Lake, capsys) -> None:
    """--warehouses, then --list, then --sql, then --pane: the first one asked is the one answered."""
    ran = run(capsys, argv(lake, "--warehouses", "--list", "--pane", "overview", "demo.events", "--json"))
    assert strict(ran.out)["kind"] == "warehouses"
    ran = run(capsys, argv(lake, "--list", "--pane", "overview", "demo.events", "--json"))
    assert strict(ran.out)["kind"] == "namespaces"


def test_json_beats_csv_and_csv_beats_the_table(lake: Lake, capsys) -> None:
    both = run(capsys, argv(lake, "--pane", "snapshots", "demo.events", "--csv", "--json"))
    assert strict(both.out)["kind"] == "snapshots"
    csv = run(capsys, argv(lake, "--pane", "snapshots", "demo.events", "--csv"))
    header = csv.out.splitlines()[0]
    assert header.startswith('"#",') and "snapshot id" in header
    assert csv.err == "", "the note is for a person reading a table, not for a CSV"


# -- every way to name the catalog reaches the same one ----------------------------------------------


def _dotenv(path: Path, lake: Lake) -> Path:
    """A project's own .env, in its own names - nothing here says ICELENS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# the project's\nACME_CATALOG_URI={lake.target.uri}\nACME_WAREHOUSE={lake.target.warehouse}\nUNRELATED=1\n")
    path.chmod(0o600)
    return path


def test_properties_on_the_command_line(lake: Lake, capsys) -> None:
    assert namespaces(run(capsys, argv(lake, "--list", "--json"))) == ["demo"]


def test_a_later_property_wins(lake: Lake, home: Path, capsys) -> None:
    dead = f"sqlite:///{home / 'missing' / 'nowhere.db'}"
    assert namespaces(run(capsys, argv(lake, "-p", f"uri={dead}", "-p", f"uri={lake.target.uri}", "--list", "--json"))) == ["demo"]
    assert run(capsys, argv(lake, "-p", f"uri={dead}", "--list", "--json")).code == 1


def test_an_env_file_named_on_the_command_line(lake: Lake, home: Path, capsys) -> None:
    env = _dotenv(home / "elsewhere" / "lake.env", lake)
    assert namespaces(run(capsys, ["--env-file", str(env), "--list", "--json"])) == ["demo"]


def test_the_nearest_env_upwards_from_where_you_stand(lake: Lake, home: Path, capsys, monkeypatch) -> None:
    _dotenv(home / "project" / ".env", lake)
    deep = home / "project" / "src" / "pkg"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert namespaces(run(capsys, ["--list", "--json"])) == ["demo"]
    # and --no-env is the way to not have it
    ran = run(capsys, ["--no-env", "--list", "--json"])
    assert ran.code == 1 and strict(ran.err)["error"]["kind"] == "NoCatalog"


def test_the_walk_stops_at_home(lake: Lake, home: Path, capsys, monkeypatch) -> None:
    """Above home are shared roots, where a .env is nobody's in particular."""
    _dotenv(home / ".env", lake)
    inside = home / "me"
    inside.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: inside))
    monkeypatch.chdir(inside)
    assert run(capsys, ["--list", "--json"]).code == 1


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_a_world_writable_env_is_not_read(lake: Lake, home: Path, capsys) -> None:
    env = _dotenv(home / "shared.env", lake)
    env.chmod(0o666)
    assert run(capsys, ["--env-file", str(env), "--list", "--json"]).code == 1


def test_icelens_environment_variables(lake: Lake, capsys, monkeypatch) -> None:
    monkeypatch.setenv("ICELENS_CATALOG_URI", lake.target.uri)
    monkeypatch.setenv("ICELENS_WAREHOUSE", lake.target.warehouse)
    assert namespaces(run(capsys, ["--no-env", "--list", "--json"])) == ["demo"]


def test_a_named_profile(lake: Lake, home: Path, capsys) -> None:
    """Named `icelens` because a SQL catalog keys its tables by catalog name, and that is the
    name the session catalog was made under: a profile called `lab` over the same file is a
    different, empty catalog - which is PyIceberg being right, not this being wrong.
    """
    (home / ".pyiceberg.yaml").write_text(
        f"catalog:\n  icelens:\n    type: sql\n    uri: {lake.target.uri}\n    warehouse: {lake.target.warehouse}\n"
    )
    assert namespaces(run(capsys, ["--catalog", "icelens", "--list", "--json"])) == ["demo"]
    listed = strict(run(capsys, ["--catalogs", "--json"]).out)["rows"]
    assert [(row["catalog"], row["type"]) for row in listed] == [("icelens", "sql")]


def test_a_profile_named_on_the_command_line_beats_the_env_beside_it(lake: Lake, home: Path, capsys) -> None:
    """`--catalog icelens` in a directory whose .env names another catalog opened that one."""
    from icelens.catalog import Target

    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    other = Lake(Target({"type": "sql", "uri": f"sqlite:///{elsewhere / 'c.db'}", "warehouse": elsewhere.as_uri()}))
    other.catalog.create_namespace("not_this_one")
    other.close()
    _dotenv(home / ".env", other)
    (home / ".pyiceberg.yaml").write_text(
        f"catalog:\n  icelens:\n    type: sql\n    uri: {lake.target.uri}\n    warehouse: {lake.target.warehouse}\n"
    )
    assert namespaces(run(capsys, ["--list", "--json"])) == ["not_this_one"]
    assert namespaces(run(capsys, ["--catalog", "icelens", "--list", "--json"])) == ["demo"]


def test_an_env_that_names_no_catalog_does_not_hide_the_default_profile(home: Path, capsys) -> None:
    """A .env holding only a region made the default profile unreachable: no catalog at all."""
    from icelens.catalog import Target

    (home / "wh").mkdir()
    props = {"type": "sql", "uri": f"sqlite:///{home / 'default.db'}", "warehouse": (home / "wh").as_uri()}
    made = Lake(Target(props, profile="default"))  # a SQL catalog keys its tables by catalog name
    made.catalog.create_namespace("from_default")
    made.close()
    (home / ".pyiceberg.yaml").write_text("catalog:\n  default:\n" + "".join(f"    {k}: {v}\n" for k, v in props.items()))
    env = home / ".env"
    env.write_text("AWS_REGION=eu-west-1\n")
    env.chmod(0o600)
    assert namespaces(run(capsys, ["--list", "--json"])) == ["from_default"]


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_a_refused_env_is_named_rather_than_ignored(lake: Lake, home: Path, capsys, monkeypatch) -> None:
    """A .env other users can write is refused, and the reader used to learn only that nothing
    was configured - while looking at the file that configures it."""
    _dotenv(home / ".env", lake).chmod(0o666)
    ran = run(capsys, ["--list"])
    err = refused(ran, "no Iceberg catalog is configured here", f"did not read {home / '.env'}", "other users can write to it")
    assert err.count("did not read") == 1
    error = strict(run(capsys, ["--list", "--json"]).err)["error"]
    assert error["kind"] == "NoCatalog" and "other users can write to it" in error["message"]
    # Configured some other way, it is context: said once on stderr, and never under --json.
    monkeypatch.setenv("ICELENS_CATALOG_URI", lake.target.uri)
    monkeypatch.setenv("ICELENS_WAREHOUSE", lake.target.warehouse)
    ran = run(capsys, ["--list"])
    assert ran.code == 0 and ran.err.count(f"icelens: did not read {home / '.env'}") == 1
    ran = run(capsys, ["--list", "--json"])
    assert ran.code == 0 and ran.err == ""


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_an_env_file_named_and_refused_says_why(lake: Lake, home: Path, capsys) -> None:
    env = _dotenv(home / "shared.env", lake)
    env.chmod(0o666)
    refused(run(capsys, ["--env-file", str(env), "--list"]), f"will not read {env}", "other users can write to it")


def test_neither_a_property_nor_a_profile_can_make_a_catalog_a_sandbox(lake: Lake, home: Path, capsys, monkeypatch) -> None:
    """`-p icelens.sandbox=x`, or the key in a profile, made a real catalog count as scratch: the
    app's SQL pane went read-write, and `icelens.sandbox.served=v3` let writes reach any namespace."""
    import sys

    from icelens.app import IcelensApp

    started: list[IcelensApp] = []
    monkeypatch.setattr(IcelensApp, "run", lambda self: started.append(self))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    hostile = ["-p", "icelens.sandbox=x", "-p", "icelens.sandbox.served=v3"]
    assert run(capsys, argv(lake, *hostile, "demo.events")).code == 0
    (home / ".pyiceberg.yaml").write_text(
        f"catalog:\n  icelens:\n    type: sql\n    uri: {lake.target.uri}\n    warehouse: {lake.target.warehouse}\n"
        "    icelens.sandbox: x\n    icelens.sandbox.served: v3\n"
    )
    assert run(capsys, ["--catalog", "icelens", "demo.events"]).code == 0
    for app in started:
        assert app.lake.read_only and not app.lake.is_sandbox and not app.lake.is_served_sandbox
    assert len(started) == 2


@pytest.mark.parametrize("version", [[], ["--v3"]])
def test_sql_in_a_sandbox_reads_and_writing_is_what_write_is_for(home: Path, capsys, version: list[str]) -> None:
    """`COPY (SELECT 1) TO '/tmp/x.csv'` wrote the file: the read-only guard let a sandbox's SQL by."""
    out = home / "x.csv"
    ran = run(capsys, ["--sandbox", "s", *version, "--sandbox-dir", str(home / "sandboxes"), "--sql", f"COPY (SELECT 1) TO '{out}'"])
    refused(ran, "refusing to run COPY", "--write")
    assert not out.exists()


# -- the display flags --------------------------------------------------------------------------------


def test_width_cuts_every_cell_and_says_so(lake: Lake, capsys) -> None:
    ran = run(capsys, argv(lake, "--pane", "files", "demo.events", "--width", "8"))
    assert ran.code == 0
    lines = ran.out.splitlines()[1:]
    assert lines and any("…" in line for line in lines)
    for line in lines:
        for cell in line.split():
            assert len(cell) <= 8, cell


def test_limit_is_the_number_of_rows_read(lake: Lake, capsys) -> None:
    for limit, expected in (("0", 0), ("3", 3), ("1000", 55)):
        body = strict(run(capsys, argv(lake, "--pane", "data", "demo.events", "--limit", limit, "--json")).out)
        assert body["count"] == expected
        assert f"(limit {limit})" in body["note"]


@pytest.mark.duckdb_extensions
def test_where_and_csv_together(lake: Lake, capsys) -> None:
    ran = run(capsys, argv(lake, "--pane", "data", "demo.events", "--where", "id >= 58", "--csv"))
    header, *rows = ran.out.strip().splitlines()
    assert header == '"id","name","ingest_date","measured_at"'
    assert sorted(int(row.split(",")[0]) for row in rows) == [58, 59, 60]


# -- the sandbox flags ---------------------------------------------------------------------------------


def test_a_sandbox_with_no_name_is_called_default(home: Path, capsys) -> None:
    ran = run(capsys, ["--sandbox", "--sandbox-dir", str(home / "sandboxes"), "--list", "--json"])
    assert ran.code == 0
    assert sorted(p.name for p in (home / "sandboxes" / "default").iterdir()) == ["catalog.db", "warehouse"]


def test_a_sandbox_defaults_to_somewhere_under_home(home: Path, capsys) -> None:
    ran = run(capsys, ["--sandbox", "lesson", "--list", "--json"])
    assert ran.code == 0
    assert (home / ".local" / "share" / "icelens" / "sandboxes" / "lesson" / "catalog.db").exists()


@pytest.mark.duckdb_extensions
def test_a_relative_sandbox_dir_is_relative_to_where_you_stand(home: Path, capsys) -> None:
    """`file:///` + a relative path is a path from the root: tables went under `/lessons`."""
    ran = run(capsys, ["--sandbox", "s", "--sandbox-dir", "lessons", "--write", "CREATE TABLE demo.t AS SELECT 1 AS x"])
    assert ran.code == 0, ran.err
    assert list((home / "lessons" / "s" / "warehouse" / "demo").rglob("*.parquet"))
    assert not Path("/lessons").exists()


@pytest.mark.parametrize("odd", ["a#b", "why?"])
def test_a_sandbox_cannot_live_where_a_file_uri_would_cut_the_path_short(home: Path, capsys, odd: str) -> None:
    refused(run(capsys, ["--sandbox", "s", "--sandbox-dir", str(home / odd), "--list"]), "# or ?", odd)
    assert not (home / odd).exists()


def test_a_demo_that_cannot_be_written_says_so_in_one_line(home: Path, capsys, monkeypatch) -> None:
    from icelens import demo

    def broken(lake: Lake) -> None:
        raise PermissionError(13, "Permission denied", str(home / "sandboxes" / "demo" / "warehouse"))

    monkeypatch.setattr(demo, "seed", broken)
    refused(run(capsys, ["--demo", "--sandbox-dir", str(home / "sandboxes"), "--list"]), "cannot write the demo table", "Permission denied")
    error = strict(run(capsys, ["--demo", "--sandbox-dir", str(home / "sandboxes"), "--list", "--json"]).err)["error"]
    assert error["kind"] == "IcelensError" and "cannot write the demo table" in error["message"]


@pytest.mark.duckdb_extensions
def test_write_reset_and_read_again(home: Path, capsys) -> None:
    common = ["--sandbox", "cycle", "--sandbox-dir", str(home / "sandboxes")]
    assert run(capsys, [*common, "--write", "CREATE TABLE demo.t AS SELECT 1 AS x"]).code == 0
    assert namespaces(run(capsys, [*common, "--list", "--json"])) == ["demo"]
    assert namespaces(run(capsys, [*common, "--sandbox-reset", "--list", "--json"])) == []


def test_a_rest_sandbox_reset_says_what_it_emptied(home: Path, capsys) -> None:
    """On stderr, because a reset is context and stdout belongs to whatever comes next.

    A SQLite catalog of its own stands in for the REST one: --rest asks only that the catalog
    be identifiable, and emptying one namespace is something any catalog can do.
    """
    from icelens.catalog import Target

    warehouse = home / "warehouse"
    warehouse.mkdir()
    own = Lake(Target(properties={"type": "sql", "uri": f"sqlite:///{home / 'own.db'}", "warehouse": warehouse.as_uri()}))
    own.catalog.create_namespace("sandbox_rested")
    ran = run(capsys, argv(own, "--sandbox", "rested", "--rest", "--sandbox-reset", "--list"))
    assert ran.code == 0, ran.err
    assert "icelens: emptied sandbox_rested, 0 tables" in ran.err
    # Under --json stderr is the error object's alone, and a failure after the reset must leave
    # exactly that one line there.
    ran = run(capsys, argv(own, "--sandbox", "rested", "--rest", "--sandbox-reset", "--list", "--json"))
    assert (ran.code, ran.err) == (0, "")
    assert namespaces(ran) == ["sandbox_rested"]
    ran = run(capsys, argv(own, "--sandbox", "rested", "--rest", "--sandbox-reset", "--pane", "schema", "nope.missing", "--json"))
    own.close()
    assert ran.code == 1 and ran.error["error"]["kind"] == "IcelensError"  # one line, the error's


# -- the half that is for a person --------------------------------------------------------------------


def test_catalogs_for_a_person(home: Path, capsys) -> None:
    (home / ".pyiceberg.yaml").write_text(
        "catalog:\n  lab:\n    uri: https://alice:hunter2@lab.example/catalog\n  local:\n    type: sql\n    warehouse: file:///w\n"
    )
    ran = run(capsys, ["--catalogs"])
    assert ran.code == 0
    lines = ran.out.splitlines()
    assert lines[0].split() == ["lab", "rest", "https://lab.example/catalog"]
    assert lines[1].split() == ["local", "sql", "file:///w"]
    assert "hunter2" not in ran.out  # the command people paste into an issue


def test_no_catalogs_for_a_person(capsys) -> None:
    refused(run(capsys, ["--catalogs"]), "no catalogs in ~/.pyiceberg.yaml")


def test_warehouses_for_a_person(capsys) -> None:
    from stub import StubCatalog

    with StubCatalog({"wh-a": [], "wh-b": []}) as stub:
        ran = run(capsys, ["--uri", stub.uri, "--warehouses"])
        assert (ran.code, ran.out) == (0, "wh-a\nwh-b\n")
        # and asked for a listing without choosing, the choices come with the refusal
        err = refused(run(capsys, ["--uri", stub.uri, "--list"]), "needs a warehouse", "--warehouse NAME")
        assert err.splitlines()[-3:] == ["warehouses this catalog offers:", "  wh-a", "  wh-b"]
    with StubCatalog({"hidden": []}, listed=False) as stub:
        refused(run(capsys, ["--uri", stub.uri, "--warehouses"]), "this catalog does not list its warehouses")


def test_explain_shows_five_and_counts_the_rest(capsys) -> None:
    ran = run(capsys, ["--explain", "file"])
    assert ran.code == 0
    assert ran.err.startswith("(") and ran.err.rstrip().endswith("more matches)")
    assert ran.out.count("\n  where: ") + ran.out.count("\n  next:  ") >= 5


def test_the_flags_reach_the_app_on_a_terminal(lake: Lake, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one thing about the UI this file can check: what the command line hands it."""
    import sys

    from icelens.app import IcelensApp

    started: list[IcelensApp] = []
    monkeypatch.setattr(IcelensApp, "run", lambda self: started.append(self))
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    ran = run(capsys, argv(lake, "demo.events", "--limit", "7", "--width", "12"))
    assert ran.code == 0
    (app,) = started
    assert (app.ident, app.limit, app.width) == ("demo.events", 7, 12)
    assert app.lake.read_only, "the app is read-only unless --allow-writes says otherwise"


# -- --serve ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "named"),
    [
        (["--port", "8999", "--sandbox", "s"], "--port is where --serve listens"),
        (["--serve"], "--serve shares a sandbox"),
        (["--serve", "--sandbox", "s", "--list"], "--serve keeps running"),
        (["--serve", "--sandbox", "s", "--write", "SELECT 1"], "--serve keeps running"),
        (["--serve", "--sandbox", "--rest", "--uri", "http://127.0.0.1:1/catalog"], "--rest puts the sandbox in a catalog you already run"),
    ],
)
def test_serve_is_refused_where_it_cannot_mean_anything(home: Path, capsys, flags: list[str], named: str) -> None:
    refused(run(capsys, [*flags, "--sandbox-dir", str(home / "sandboxes")] if "--rest" not in flags else flags), named)


def test_a_serve_token_anyone_could_guess_is_refused(home: Path, capsys, monkeypatch) -> None:
    """The token is all that stands between the sandbox and every other process on the machine."""
    from icelens import cli

    monkeypatch.setattr(cli, "_wait_for_interrupt", lambda stop: None)
    monkeypatch.setenv("ICELENS_SERVE_TOKEN", "hunter2")
    refused(run(capsys, ["--sandbox", "s", "--sandbox-dir", str(home / "sandboxes"), "--serve"]), "ICELENS_SERVE_TOKEN is 7 characters", "16")
    monkeypatch.setenv("ICELENS_SERVE_TOKEN", "a-token-of-sixteen")
    ran = run(capsys, ["--sandbox", "s", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--json"])
    assert ran.code == 0, ran.err
    assert strict(ran.out)["rows"][0]["token"] == "a-token-of-sixteen"

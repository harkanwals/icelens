"""`icelens --serve`: a sandbox's catalog, served for other engines, as a real process.

What has to hold is what a person pastes into another tool: the printed PyIceberg settings
open the catalog, the printed DuckDB `ATTACH` attaches it verbatim and can write, another icelens
can browse it - and ctrl+c or SIGTERM stops it cleanly. So the server here is a subprocess and
the clients are the real ones, not a function call that returns a dict.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from headless import clean_environment, run

from icelens import cli
from icelens.catalog import sandbox_home

posix_only = pytest.mark.skipif(os.name != "posix", reason="signals are delivered differently on Windows")


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Served:
    """An `icelens --serve` process, and what it printed first."""

    def __init__(self, *args: str, env: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", "import sys; from icelens.cli import main; sys.exit(main())", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, **(env or {})},
        )
        assert self.process.stdout is not None
        self.first = self.process.stdout.readline()
        if not self.first:
            raise AssertionError(f"--serve printed nothing: {self.process.communicate(timeout=30)}")

    @property
    def details(self) -> dict[str, Any]:
        body = json.loads(self.first)
        assert body["kind"] == "serve" and body["count"] == 1
        return body["rows"][0]

    def stop(self, sig: int = signal.SIGTERM) -> tuple[int, str, str]:
        """Signal it, and return its exit code and everything it printed after the first line.

        Read through the same file objects, not `communicate()`: `readline()` above has already
        pulled the rest of stdout into the file's buffer, where `communicate()` never looks.
        """
        assert self.process.stdout is not None and self.process.stderr is not None
        self.process.send_signal(sig)
        out, err = self.process.stdout.read(), self.process.stderr.read()
        return self.process.wait(timeout=30), out, err

    def kill(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
            self.process.communicate(timeout=30)


@pytest.fixture()
def served_demo(home: Path) -> Served:
    served = Served("--demo", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--json")
    yield served
    served.kill()


# -- what a client gets ------------------------------------------------------------------------


@posix_only
def test_pyiceberg_opens_what_was_printed_and_reads_the_demo(served_demo: Served) -> None:
    from pyiceberg.catalog import load_catalog

    details = served_demo.details
    assert details["sandbox"] == "demo" and details["format_version"] == 2
    catalog = load_catalog("served", **details["pyiceberg"])
    assert ("demo", "orders") in catalog.list_tables("demo")
    table = catalog.load_table("demo.orders")
    assert len(table.metadata.snapshots) >= 5
    assert table.scan().to_arrow().num_rows > 0
    code, _, err = served_demo.stop()
    assert code == 0, err


@posix_only
def test_another_icelens_browses_it(served_demo: Served, capsys) -> None:
    d = served_demo.details
    ran = run(capsys, ["--no-env", "--uri", d["uri"], "--warehouse", d["warehouse"], "-p", f"token={d['token']}", "--list", "--json"])
    assert ran.code == 0, ran.err
    assert [row["namespace"] for row in json.loads(ran.out)["rows"]] == ["demo"]


@posix_only
@pytest.mark.duckdb_extensions
def test_duckdb_attaches_with_the_printed_statement_and_writes_v3(home: Path) -> None:
    import duckdb

    served = Served("--demo", "--v3", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--json")
    try:
        details = served.details
        assert details["format_version"] == 3
        assert 'WITH ("format-version" = 3)' in cli._serving_text(details)
        con = duckdb.connect()
        con.execute("LOAD iceberg")
        con.execute(details["duckdb"])  # verbatim: what a user will paste
        alias = details["duckdb"].split(" AS ", 1)[1].split(" (", 1)[0]
        before = con.execute(f"SELECT count(*) FROM {alias}.demo.orders").fetchone()[0]
        con.execute(f"INSERT INTO {alias}.demo.orders SELECT * FROM {alias}.demo.orders LIMIT 1")
        assert con.execute(f"SELECT count(*) FROM {alias}.demo.orders").fetchone()[0] == before + 1
        # What the printed note says: DuckDB asks for v2 unless the CREATE says otherwise.
        con.execute(f"CREATE TABLE {alias}.demo.plain AS SELECT 1 AS id")
        con.execute(f'CREATE TABLE {alias}.demo.asked WITH ("format-version" = 3) AS SELECT 1 AS id')
        con.close()
        db = sqlite3.connect(Path(details["warehouse_path"]).parent / "catalog.db")
        for table, version in (("plain", 2), ("asked", 3)):
            [location] = db.execute("SELECT metadata_location FROM iceberg_tables WHERE table_name = ?", (table,)).fetchone()
            assert json.loads(Path(location.removeprefix("file://")).read_text())["format-version"] == version
        code, _, err = served.stop()
        assert code == 0, err
    finally:
        served.kill()


# -- how it starts and stops -------------------------------------------------------------------


@posix_only
def test_ctrl_c_stops_it_and_says_so(home: Path) -> None:
    served = Served("--sandbox", "plain", "--sandbox-dir", str(home / "sandboxes"), "--serve")
    try:
        assert served.first.startswith("icelens: serving sandbox 'plain' (format-version 2)")
        code, out, err = served.stop(signal.SIGINT)
        assert code == 0
        assert "token" in out and "ATTACH 'sandbox' AS \"plain\"" in out
        assert "format-version\" = 3" not in out  # the v3 note is for v3 sandboxes only
        assert "stopped serving sandbox 'plain'" in err and "Traceback" not in err
    finally:
        served.kill()


@posix_only
def test_sigterm_the_moment_the_address_is_printed_still_stops_cleanly(home: Path) -> None:
    """A supervisor reads the address and may signal at once; the handler has to be there first."""
    for attempt in range(8):
        served = Served("--sandbox", f"t{attempt}", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--json")
        try:
            assert served.stop(signal.SIGTERM)[0] == 0, f"attempt {attempt} died on the signal"
        finally:
            served.kill()


@posix_only
def test_a_port_and_a_token_can_be_fixed(home: Path) -> None:
    port = free_port()
    served = Served(
        "--sandbox", "fixed", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--port", str(port), "--json",
        env={"ICELENS_SERVE_TOKEN": "a-token-for-a-config-file"},
    )
    try:
        assert served.details["uri"] == f"http://127.0.0.1:{port}"
        assert served.details["token"] == "a-token-for-a-config-file"
        assert served.stop()[0] == 0
    finally:
        served.kill()


@pytest.mark.parametrize("port", ["0", "65536", "-1", "http"])
def test_a_port_that_is_not_one_is_bad_usage(home: Path, capsys, port: str) -> None:
    ran = run(capsys, ["--sandbox", "p", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--port", port])
    assert ran.code == 2 and "--port" in ran.err


def test_a_port_in_use_is_refused_in_words(home: Path, capsys) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        ran = run(capsys, ["--sandbox", "busy", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--port", str(port)])
    assert ran.code == 1
    assert f"cannot listen on 127.0.0.1:{port}" in ran.err and "Traceback" not in ran.err


def test_the_served_catalog_listens_on_loopback_only(home: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens import cli, rest

    # Other tests' sandboxes may be served in this process too; this one is found by its home.
    loop = sandbox_home("loop", home / "sandboxes")
    seen: list[Any] = []
    monkeypatch.setattr(cli, "_wait_for_interrupt", lambda stop: seen.append(rest._running.get(loop)))
    assert run(capsys, ["--sandbox", "loop", "--sandbox-dir", str(home / "sandboxes"), "--serve", "--json"]).code == 0
    (running,) = seen
    assert running is not None and running._server.server_address[0] == "127.0.0.1"
    assert loop not in rest._running  # and it was stopped on the way out

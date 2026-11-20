"""`--trace FILE`: the conversation with the catalog written down, and no secret in it.

The trace exists for bug reports, which get pasted into public issues. So most of what is
tested here is what must *not* be in the file: a bearer token, an OAuth secret, the literal in a
`CREATE SECRET`. The rest is that the file says what a person debugging a catalog needs: which
route the rows took, every request with its status, every DuckDB statement with its time.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

import pytest
from conftest import Attachable
from headless import argv, clean_environment, run
from hypothesis import given
from hypothesis import strategies as st

from icelens import trace
from icelens.catalog import Lake, remember_secrets, sandbox_target


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    yield tmp_path
    trace.stop()


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


# -- what the file says ------------------------------------------------------------------------


def test_a_trace_says_what_was_opened_and_which_route_read_it(lake: Lake, home: Path, capsys) -> None:
    path = home / "trace.log"
    assert run(capsys, argv(lake, "--pane", "data", "demo.events", "--limit", "3", "--trace", str(path))).code == 0
    text = path.read_text()
    assert "trace started: python" in text and "pyiceberg" in text
    assert "catalog: " in text
    assert "pyiceberg: opening" in text
    # A SQLite catalog cannot be attached, so the trace has to say how the rows were read instead.
    assert "rows: 3 of demo.events via " in text


@pytest.mark.duckdb_extensions
def test_every_duckdb_statement_is_timed(lake: Lake, home: Path, capsys) -> None:
    path = home / "trace.log"
    assert run(capsys, argv(lake, "--sql", "SELECT 41 + 1 AS answer", "--trace", str(path))).code == 0
    statements = [line for line in lines(path) if " icelens.duckdb " in line]
    assert any("LOAD iceberg" in line for line in statements)
    assert any(" ms  SELECT 41 + 1 AS answer" in line for line in statements)


@pytest.mark.duckdb_extensions
def test_a_failed_statement_is_traced_with_its_error(lake: Lake, home: Path, capsys) -> None:
    path = home / "trace.log"
    # A column, not a table: a table the catalog does not have is refused before DuckDB is asked.
    assert run(capsys, argv(lake, "--sql", 'SELECT no_such_column FROM icelens."demo"."events"', "--trace", str(path))).code == 1
    failed = [line for line in lines(path) if " failed " in line]
    assert failed and "no_such_column" in failed[0] and "BinderException" in failed[0]
    assert any("stopped: " in line for line in lines(path))


def test_requests_to_a_rest_catalog_are_traced_with_status_and_time(attachable: Attachable, home: Path, capsys) -> None:
    path = home / "trace.log"
    assert run(capsys, [*attachable.cli_args(), "--list", "--trace", str(path)]).code == 0
    http = [line for line in lines(path) if " icelens.http " in line]
    assert any(" ms  GET " in line and "/v1/config" in line and line.endswith("-> 200") for line in http), http


def test_icelens_trace_in_the_environment_does_what_the_flag_does(
    lake: Lake, home: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = home / "from-env.log"
    monkeypatch.setenv("ICELENS_TRACE", str(path))
    assert run(capsys, argv(lake, "--list")).code == 0
    assert "trace started" in path.read_text()


def test_a_trace_appends_rather_than_overwrites(lake: Lake, home: Path, capsys) -> None:
    path = home / "trace.log"
    for _ in range(2):
        assert run(capsys, argv(lake, "--list", "--trace", str(path))).code == 0
    assert path.read_text().count("trace started") == 2


# -- what the file must never say --------------------------------------------------------------


def test_the_file_is_readable_by_its_owner_alone(lake: Lake, home: Path, capsys) -> None:
    path = home / "trace.log"
    assert run(capsys, argv(lake, "--list", "--trace", str(path))).code == 0
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_bearer_token_never_reaches_the_trace(attachable: Attachable, home: Path, capsys) -> None:
    token = attachable.properties.get("token")
    if not token:
        pytest.skip("this catalog logs in without a token")
    path = home / "trace.log"
    # --list goes through PyIceberg's HTTP, --sql through DuckDB's ATTACH: both carry the token.
    for action in (["--list"], ["--sql", "SELECT 1"]):
        assert run(capsys, [*attachable.cli_args(), *action, "--trace", str(path)]).code in (0, 1)
    text = path.read_text()
    assert token not in text
    assert "icelens.http" in text  # it did talk to the catalog, so the absence above means something


@pytest.mark.duckdb_extensions
def test_the_attach_statement_is_traced_with_its_token_masked(attachable: Attachable, home: Path, capsys) -> None:
    path = home / "trace.log"
    assert run(capsys, [*attachable.cli_args(), "--sql", "SELECT 1", "--trace", str(path)]).code == 0
    attach = [line for line in lines(path) if " ATTACH " in line and "TYPE ICEBERG" in line]
    assert attach, lines(path)
    if attachable.properties.get("token"):
        assert "TOKEN '***'" in attach[0]


def test_a_remembered_secret_is_scrubbed_from_any_line(home: Path) -> None:
    path = home / "trace.log"
    trace.start(path)
    remember_secrets({"credential": "client-id:hunter2-is-not-a-password"})
    logging.getLogger("icelens.test").info("the server said: bad secret hunter2-is-not-a-password")
    trace.stop()
    assert "hunter2-is-not-a-password" not in path.read_text()


@given(
    keyword=st.sampled_from(trace._SENSITIVE),
    value=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40),
)
def test_redact_leaves_no_credential_literal_standing(keyword: str, value: str) -> None:
    literal = "'" + value.replace("'", "''") + "'"
    for sql in (
        f"CREATE SECRET s (TYPE S3, KEY_ID 'AKIA-shown-on-purpose', {keyword} {literal}, ENDPOINT 'http://store:9000')",
        f"ATTACH 'warehouse' AS icelens (TYPE ICEBERG, ENDPOINT 'http://store:9000', {keyword} {literal})",
    ):
        out = trace.redact(sql)
        # The exact masked form, so a quote inside the value ('ab''cd') cannot leave a tail behind.
        assert f"{keyword} '***'" in out
        assert "http://store:9000" in out
        assert out.count("'") == sql.count("'") - literal.count("'") + 2


#: Written out here rather than read from trace.py, so a keyword dropped there is a failure here.
MUST_MASK = ("SECRET", "TOKEN", "CLIENT_SECRET", "SESSION_TOKEN", "BEARER_TOKEN", "ACCOUNT_KEY", "CONNECTION_STRING", "PASSWORD")


@pytest.mark.parametrize("keyword", MUST_MASK)
def test_every_credential_keyword_is_masked_in_an_attach(keyword: str) -> None:
    out = trace.redact(f"ATTACH 'w' AS i (TYPE ICEBERG, ENDPOINT 'http://h', {keyword} 'the-value')")
    assert "the-value" not in out and f"{keyword} '***'" in out


# -- what it costs when it is off --------------------------------------------------------------


def test_untraced_lakes_get_duckdb_and_requests_as_they_come(home: Path) -> None:
    import requests

    send = requests.Session.send
    assert not trace.active()
    assert trace.connection(sentinel := object()) is sentinel
    trace.start(home / "trace.log")
    assert requests.Session.send is not send
    trace.stop()
    assert requests.Session.send is send


def test_main_stops_the_trace_it_started(lake: Lake, home: Path, capsys) -> None:
    levels = {name: logging.getLogger(name).level for name in ("icelens", "pyiceberg")}
    assert run(capsys, argv(lake, "--list", "--trace", str(home / "trace.log"))).code == 0
    assert not trace.active()
    # and leaves the loggers as it found them, so a later run is not logging at DEBUG for nobody
    assert {name: logging.getLogger(name).level for name in levels} == levels


def test_a_trace_that_cannot_be_written_is_bad_usage(lake: Lake, home: Path, capsys) -> None:
    ran = run(capsys, argv(lake, "--list", "--trace", str(home / "no" / "such" / "dir" / "trace.log")))
    assert ran.code == 2
    assert "--trace cannot write" in ran.err and "Traceback" not in ran.err


@pytest.mark.duckdb_extensions
def test_a_traced_offline_sandbox_write_shows_the_file_duckdb_wrote(home: Path, capsys) -> None:
    path = home / "trace.log"
    sandboxes = home / "sandboxes"
    Lake(sandbox_target("t", root=sandboxes)).close()
    ran = run(
        capsys,
        ["--sandbox", "t", "--sandbox-dir", str(sandboxes), "--write", "CREATE TABLE t.x AS SELECT 1 AS a", "--trace", str(path)],
    )
    assert ran.code == 0, ran.err
    # An offline sandbox is written by DuckDB to Parquet and committed by PyIceberg's add_files.
    assert any(" ms  COPY (" in line and ".parquet" in line for line in lines(path))


# -- the review's cases: secrets in places the first version did not look ------------------------


@pytest.mark.parametrize(
    ("sql", "secret"),
    [
        ("CREATE SECRET s (TYPE S3, SECRET $$dollar-quoted-secret$$, KEY_ID 'AKIA')", "dollar-quoted-secret"),
        ("CREATE SECRET s (TYPE S3, SECRET $tag$tagged-secret$tag$)", "tagged-secret"),
        ("CREATE SECRET h (TYPE HTTP, EXTRA_HTTP_HEADERS MAP {'Authorization': 'Bearer header-secret'})", "header-secret"),
        ("CREATE SECRET g (TYPE GCS, BEARER_TOKEN 'ya29.gcs-secret')", "gcs-secret"),
        ("ATTACH 'dbname=x user=y password=pg-secret host=h' AS pg (TYPE postgres)", "pg-secret"),
        ("ATTACH 'w' AS i (TYPE ICEBERG, CLIENT_SECRET $x$oauth-secret$x$)", "oauth-secret"),
        ("SELECT * FROM read_parquet('https://b.s3.amazonaws.com/f.parquet?X-Amz-Signature=sig-secret&x=1')", "sig-secret"),
    ],
)
def test_redact_masks_every_shape_a_credential_takes_in_sql(sql: str, secret: str) -> None:
    out = trace.redact(sql)
    assert secret not in out
    assert "***" in out


def test_redact_keeps_what_a_reader_of_the_trace_needs() -> None:
    out = trace.redact("CREATE SECRET s (TYPE S3, KEY_ID 'AKIA-id', SECRET 'x', ENDPOINT 'http://store:9000', SCOPE 's3://b/t/')")
    assert all(kept in out for kept in ("TYPE S3", "'AKIA-id'", "'http://store:9000'", "'s3://b/t/'"))
    assert trace.redact("SELECT * FROM t WHERE region = 'north'") == "SELECT * FROM t WHERE region = 'north'"


def test_a_password_in_the_catalog_url_never_reaches_the_trace() -> None:
    # requests percent-encodes userinfo, so the remembered secret no longer matches it verbatim.
    url = trace.safe_url("http://admin:Pa%5Ess%7Bw0rd%7D@127.0.0.1:8181/v1/config?warehouse=w&token=abc")
    assert "admin" not in url and "Pa%5Ess" not in url and "token=abc" not in url
    assert url.startswith("http://127.0.0.1:8181/v1/config?warehouse=w")


@pytest.mark.skipif(os.name != "posix", reason="symlinks and modes are POSIX here")
def test_a_trace_path_that_is_a_symlink_is_refused(home: Path) -> None:
    victim = home / "victim_rc"
    victim.write_text("original\n")
    link = home / "trace.log"
    link.symlink_to(victim)
    with pytest.raises(OSError):
        trace.start(link)
    assert victim.read_text() == "original\n"


@pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX here")
def test_an_existing_trace_file_is_made_private(home: Path) -> None:
    path = home / "trace.log"
    path.write_text("")
    path.chmod(0o644)
    trace.start(path)
    trace.stop()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

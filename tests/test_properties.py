"""The hardening rules, asserted as rules rather than as the examples that motivated them.

`test_hardening.py` pins each rule to the case that made it necessary. This file asserts the
rule itself, against inputs nobody thought of: a quoted identifier is one identifier for *every*
name, a literal reads back as the value it was for *every* value, `only_reads` lets no write
through for *any* mix of statements, comments and case, an attached sandbox writes inside its
namespace or not at all, and no remembered secret survives `scrub` whatever surrounds it.

Where an invariant can be checked by DuckDB's own parser rather than by a regex in this file, it
is: the parser is what the quoting has to satisfy, and a test that re-implemented it would only
prove the two regexes agree.

Hypothesis keeps the examples it found failing in `.hypothesis/` and replays them first. The
example count is bounded so the suite stays quick; `ICELENS_HYPOTHESIS_EXAMPLES` raises it for a
longer hunt.
"""

from __future__ import annotations

import os
import stat
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from icelens import catalog
from icelens.catalog import (
    MAX_DOTENV_BYTES,
    MAX_ROWS,
    IcelensError,
    Target,
    _uncommented,
    classify,
    literal,
    one_statement,
    only_reads,
    quote,
    read_dotenv,
    redact_uri,
    remember_secrets,
    route,
    route_attached,
    row_limit,
    sandbox_table,
    scrub,
)
from icelens.format import REPLACEMENT, cell, safe, scalar

EXAMPLES = int(os.environ.get("ICELENS_HYPOTHESIS_EXAMPLES", "150"))
bounded = settings(max_examples=EXAMPLES, deadline=None, suppress_health_check=[HealthCheck.too_slow])

#: one connection for the whole module: these tests ask DuckDB to parse and evaluate, never to
#: load an extension, so they run the same on a machine that has none
_DUCK = duckdb.connect()


# -- what the strategies generate ----------------------------------------------------------------

#: anything DuckDB can hold in a string: no lone surrogates (not UTF-8) and no NUL (refused by name)
_chars = st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00")

#: the fragments a SQL tokenizer cares about, so generated text hits them far more often than
#: uniform unicode ever would
_SQL_BITS = ("'", "''", '"', '""', ";", "--", "/*", "*/", "\n", "\\", "$$", " ", "x", "DROP TABLE t", "E'", "\t", "(", ")")

hostile = st.one_of(
    st.text(_chars, max_size=40),
    st.lists(st.sampled_from(_SQL_BITS), max_size=12).map("".join),
    st.lists(st.one_of(st.sampled_from(_SQL_BITS), st.text(_chars, max_size=4)), max_size=10).map("".join),
)


# -- quote and literal ---------------------------------------------------------------------------


@bounded
@given(hostile.filter(bool))
def test_a_quoted_name_is_one_identifier_and_reads_back_as_itself(name: str) -> None:
    """Whatever the catalog calls a column, DuckDB sees exactly that name and nothing after it."""
    sql = f"SELECT 1 AS {quote(name)}"
    assert len(duckdb.extract_statements(sql)) == 1
    assert _DUCK.execute(sql).description[0][0] == name


@bounded
@given(st.text(_chars, max_size=10), st.text(_chars, max_size=10))
def test_a_name_with_a_nul_in_it_is_refused_by_name(before: str, after: str) -> None:
    with pytest.raises(IcelensError, match="NUL"):
        quote(f"{before}\x00{after}")
    with pytest.raises(IcelensError, match="NUL"):
        literal(f"{before}\x00{after}")


@bounded
@given(hostile)
def test_a_literal_reads_back_as_the_value_it_was(value: str) -> None:
    sql = f"SELECT {literal(value)} AS v"
    assert len(duckdb.extract_statements(sql)) == 1
    assert _DUCK.execute(sql).fetchone()[0] == value


#: every shape of statement icelens builds out of configuration, with a literal where it puts one
_GENERATED = (
    "ATTACH IF NOT EXISTS {v} AS icelens (TYPE ICEBERG, ENDPOINT {v}, TOKEN {v}, READ_ONLY)",
    "CREATE OR REPLACE SECRET icelens_s3 (TYPE S3, KEY_ID {v}, SECRET {v}, REGION {v}, ENDPOINT {v})",
    "CREATE OR REPLACE SECRET icelens_gcs (TYPE GCS, BEARER_TOKEN {v})",
    "SELECT * FROM iceberg_scan({v}, snapshot_from_id => 1)",
    "COPY (SELECT 1) TO {v} (FORMAT PARQUET)",
)


@bounded
@given(hostile, st.sampled_from(_GENERATED))
def test_a_literal_can_never_end_the_statement_it_sits_in(value: str, shape: str) -> None:
    """A warehouse name, a token or a path cannot close its quote and start a second statement."""
    sql = shape.format(v=literal(value))
    assert len(duckdb.extract_statements(sql)) == 1
    assert one_statement(sql) == sql


# -- one_statement and only_reads ----------------------------------------------------------------

#: statements DuckDB types as reads, and that `only_reads` must therefore let through
READS = (
    "SELECT 1",
    "SELECT * FROM range(3)",
    "WITH a AS (SELECT 1 AS n) SELECT * FROM a",
    "VALUES (1), (2)",
    "FROM range(2)",
    "DESCRIBE SELECT 1",
    "SUMMARIZE SELECT 1",
    "SHOW TABLES",
    "PRAGMA database_size",
    "SELECT 1 UNION ALL SELECT 2",
    "EXPLAIN SELECT 1",
    "EXPLAIN WITH a AS (SELECT 1) SELECT * FROM a",
)

#: statements that change something - a table, a file, a setting, the session - every one of
#: which `only_reads` must refuse, alone or hidden among reads
WRITES = (
    "DROP TABLE t",
    "CREATE TABLE t AS SELECT 1",
    "CREATE OR REPLACE VIEW v AS SELECT 1",
    "CREATE MACRO m() AS 1",
    "CREATE SECRET s (TYPE S3)",
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET a = 1",
    "DELETE FROM t",
    "TRUNCATE t",
    "MERGE INTO t USING s ON true WHEN MATCHED THEN DELETE",
    "ALTER TABLE t ADD COLUMN c INT",
    "COMMENT ON TABLE t IS 'x'",
    "COPY (SELECT 1) TO '/tmp/icelens-should-not-exist.csv'",
    "COPY t FROM '/tmp/icelens-should-not-exist.csv'",
    "EXPORT DATABASE '/tmp/icelens-should-not-exist'",
    "CALL foo()",
    "CHECKPOINT",
    "ATTACH 'x.db' AS x",
    "DETACH x",
    "SET threads = 1",
    "RESET threads",
    "PRAGMA enable_profiling",
    "INSTALL iceberg",
    "LOAD iceberg",
    "USE memory",
    "VACUUM",
    "BEGIN TRANSACTION",
    "PREPARE p AS SELECT 1",
    "EXPLAIN ANALYZE DELETE FROM t",
    "EXPLAIN (ANALYZE) DELETE FROM t",
    "EXPLAIN COPY (SELECT 1) TO '/tmp/icelens-should-not-exist.csv'",
)

#: what can go between two tokens without changing what DuckDB thinks the statement is
_GAPS = (" ", "  ", "\n", "\t", " /* c */ ", "/**/", " /* ; DROP TABLE x */ ", " -- c\n", " -- ; DELETE FROM t\n")


def _dress(statement: str, data: st.DataObject) -> str:
    """The same statement, in a different hand: case flipped, comments and whitespace between words.

    Never before the first word: a comment ahead of `EXPLAIN` is refused on purpose (see
    `catalog._EXPLAINING`), and this is about spellings a guard has to see through, not about
    the one it deliberately declines to guess at.
    """
    words = statement.split(" ")
    out = []
    for index, word in enumerate(words):
        if not word.startswith("'"):
            word = data.draw(st.sampled_from((word, word.lower(), word.upper(), word.swapcase())))
        out.append(word)
        if index < len(words) - 1:
            out.append(data.draw(st.sampled_from(_GAPS)))
    return "".join(out)


@bounded
@given(st.lists(st.sampled_from(READS + WRITES), min_size=1, max_size=5), st.data())
def test_only_reads_lets_a_mix_through_exactly_when_every_statement_reads(picked: list[str], data) -> None:
    sql = data.draw(st.sampled_from((";", "; ", ";\n", " ;\n\n"))).join(_dress(s, data) for s in picked)
    writes = [s for s in picked if s in WRITES]
    if not writes:
        assert only_reads(sql) == sql
        return
    try:
        only_reads(sql)
    except IcelensError as refused:
        assert "--allow-writes" in str(refused)
        return
    # let through: safe only if DuckDB cannot parse it, because then `execute` cannot run it
    with pytest.raises(duckdb.Error):
        duckdb.extract_statements(sql)


@bounded
@given(
    st.sampled_from(WRITES[:13]),
    st.sampled_from(("EXPLAIN", "explain", "ExPlAiN")),
    st.sampled_from(("ANALYZE", "analyze", "AnAlYzE", "(ANALYZE)", "(ANALYZE true)", "(ANALYZE, FORMAT json)")),
    st.sampled_from(_GAPS),
    st.sampled_from(_GAPS),
    st.sampled_from(("", "/* lead */ ", "-- lead\n")),
)
def test_explain_analyze_never_passes_however_it_is_spelled(write: str, explain: str, analyze: str, gap: str, gap2: str, lead: str) -> None:
    """`ANALYZE` *runs* what it explains, so the tail is re-parsed and must be a read.

    DuckDB 1.5.5 also accepts the Postgres option spelling, `EXPLAIN (ANALYZE) DELETE FROM t`,
    and measured, it deletes; that spelling is covered here alongside the bare keyword. A
    spelling DuckDB cannot parse at all (`EXPLAIN ANALYZE COMMENT ON …`) is passed through, and
    that is safe for exactly one reason, asserted here: nothing can run it either.
    """
    sql = f"{lead}{explain}{gap}{analyze}{gap2}{write}"
    try:
        only_reads(sql)
    except IcelensError:
        return
    with pytest.raises(duckdb.Error):
        duckdb.extract_statements(sql)


@bounded
@given(st.lists(st.sampled_from(READS + WRITES[:20]), min_size=1, max_size=4))
def test_one_statement_counts_statements_the_way_duckdb_does(picked: list[str]) -> None:
    sql = "; ".join(picked)
    if len(picked) == 1:
        assert one_statement(sql) == sql
    else:
        with pytest.raises(IcelensError, match="more than one statement"):
            one_statement(sql)


@bounded
@given(hostile)
def test_the_guards_answer_anything_with_the_sql_or_a_refusal(text: str) -> None:
    """Whatever arrives, a guard returns the SQL unchanged or raises IcelensError - never anything else.

    And SQL a guard lets through without understanding is SQL DuckDB cannot run either: the
    pass-through is only safe because `execute` uses the same parser and fails the same way.
    """
    for guard in (one_statement, only_reads):
        try:
            assert guard(text) == text
        except IcelensError:
            continue
    try:
        kinds = {s.type.name for s in duckdb.extract_statements(text)}
    except Exception:  # noqa: BLE001 - unparseable: the guard passed it through, and nothing will run it
        return
    if kinds - {"SELECT", "EXPLAIN"}:
        with pytest.raises(IcelensError):
            only_reads(text)


# -- the offline router, and the comments around a statement --------------------------------------

_word = st.from_regex(r"[a-z][a-z0-9_]{0,8}", fullmatch=True)
_table = st.tuples(_word, _word).map(lambda parts: f"ns_{parts[0]}.t_{parts[1]}")
#: comment text that cannot close its own comment or start a new line
_remark = st.text(st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="*/"), max_size=12)
_leading = st.lists(st.one_of(_remark.map(lambda t: f"-- {t}\n"), _remark.map(lambda t: f"/* {t} */\n")), max_size=3).map("".join)
_trailing = st.lists(st.one_of(_remark.map(lambda t: f"\n-- {t}"), _remark.map(lambda t: f"\n/* {t} */")), max_size=3).map("".join)


@st.composite
def _sandbox_writes(draw) -> tuple[str, str, str, str]:
    """(statement, kind, table, payload) for one of the three shapes the offline sandbox commits."""
    table = draw(_table)
    number = draw(st.integers(0, 10**6))
    kind = draw(st.sampled_from(("append", "delete", "create")))
    if kind == "append":
        return f"INSERT INTO {table} SELECT {number}", kind, table, f"SELECT {number}"
    if kind == "delete":
        return f"DELETE FROM {table} WHERE id < {number}", kind, table, f"id < {number}"
    return f"CREATE TABLE {table} AS SELECT {number} AS id", kind, table, f"SELECT {number} AS id"


@bounded
@given(_sandbox_writes(), _leading, _trailing, st.booleans())
def test_comments_around_a_statement_never_change_what_it_writes(shape, lead: str, trail: str, terminated: bool) -> None:
    """The Writer pane opens on a commented example; the comments must never reach the commit."""
    statement, kind, table, payload = shape
    (write,) = route(f"{lead}{statement}{';' if terminated else ''}{trail}")
    assert (write.kind, write.table, write.payload) == (kind, table, payload)
    assert write.sql == statement


@bounded
@given(_sandbox_writes(), _leading, _trailing)
def test_uncommenting_is_idempotent_and_keeps_the_statement_whole(shape, lead: str, trail: str) -> None:
    statement = shape[0]
    once = _uncommented(f"{lead}{statement};{trail}")
    assert once == statement
    assert _uncommented(once) == once


@bounded
@given(hostile)
def test_the_offline_router_classifies_or_refuses_anything(text: str) -> None:
    """Never a stray exception, and never a write it does not know how to commit."""
    try:
        writes = route(text)
    except IcelensError:
        return
    assert {w.kind for w in writes} <= {"read", "append", "delete", "create"}
    for w in writes:
        assert (w.kind == "read") == (w.table == "")


@bounded
@given(st.lists(st.sampled_from(("DELETE FROM", "delete from", "DELETE  FROM")), min_size=1, max_size=1), _table, _trailing)
def test_a_delete_with_no_where_is_never_a_sandbox_write(verb: list[str], table: str, trail: str) -> None:
    """Deleting everything by accident is the classic; a sandbox wants `WHERE true` said out loud."""
    with pytest.raises(IcelensError, match="It cannot do this one"):
        route(f"{verb[0]} {table}{trail}")


# -- the attached sandbox: one namespace, and nowhere else ----------------------------------------

NAMESPACE = "sandbox_demo"

#: pieces a hostile table name is made of: the alias, the sandbox's own namespace, somebody
#: else's, and the quoting and dots that decide which is which
_NAME_BITS = ("sandbox_demo", "core", "icelens", "sales", '"', ".", '""', "x")
_VERBS = (
    "INSERT INTO {} SELECT 1",
    "DELETE FROM {} WHERE true",
    "UPDATE {} SET a = 1",
    "CREATE TABLE {} AS SELECT 1",
    "CREATE OR REPLACE TABLE {} AS SELECT 1",
    "CREATE TABLE IF NOT EXISTS {} AS SELECT 1",
    "DROP TABLE {}",
    "DROP TABLE IF EXISTS {}",
    "ALTER TABLE {} ADD COLUMN c INT",
    "MERGE INTO {} USING s ON true WHEN MATCHED THEN DELETE",
)


@bounded
@given(hostile)
def test_sandbox_table_confines_any_name_or_refuses_it(ident: str) -> None:
    try:
        name = sandbox_table(ident, NAMESPACE)
    except IcelensError:
        return
    assert name.split(".")[0] == NAMESPACE


@bounded
@given(st.lists(st.sampled_from(_NAME_BITS), min_size=1, max_size=6).map("".join), st.sampled_from(_VERBS))
def test_an_attached_sandbox_writes_inside_its_namespace_or_not_at_all(ident: str, verb: str) -> None:
    """The statement DuckDB is handed names the sandbox namespace, ahead of anything that was typed."""
    try:
        jobs = route_attached(verb.format(ident), NAMESPACE)
    except IcelensError:
        return
    prefix = verb.split("{}")[0]
    for job in jobs:
        assert job.kind != "read"
        assert job.table.split(".")[0] == NAMESPACE
        assert job.payload.startswith(f'{prefix}icelens."{NAMESPACE}".')


@pytest.mark.parametrize(
    "sql",
    [
        "ALTER TABLE sales SET SCHEMA core",  # moving a table out is a write elsewhere
        "WITH c AS (SELECT 1 AS id) INSERT INTO crm.accounts SELECT * FROM c",
        "WITH c AS (SELECT 1) DELETE FROM crm.accounts",
        "DROP TABLE sales, crm.accounts",
        "DROP SCHEMA core CASCADE",
        "INSERT OR REPLACE INTO crm.accounts SELECT 1",
        "CREATE TEMP TABLE core_x AS SELECT 1",
        "TRUNCATE crm.accounts",
        "COMMENT ON TABLE crm.accounts IS 'x'",
        "COPY crm.accounts TO '/tmp/icelens-should-not-exist.csv'",
        "ATTACH 'elsewhere.db' AS elsewhere",
    ],
)
def test_the_ways_round_the_namespace_that_do_not_work(sql: str) -> None:
    """Each of these names a table outside the sandbox somewhere the shape match does not look.

    They are refused - by DuckDB's parser, or by a shape that does not match - rather than
    rewritten, which is the only outcome that is safe without understanding the statement.
    """
    with pytest.raises(IcelensError):
        route_attached(sql, NAMESPACE)


def test_explain_analyze_is_a_read_to_the_router_and_so_is_never_run() -> None:
    """The router does not run reads at all, so the one read that would write never reaches DuckDB."""
    (job,) = route_attached("EXPLAIN ANALYZE DELETE FROM crm.accounts", NAMESPACE)
    assert job.kind == "read"


# -- credentials ---------------------------------------------------------------------------------


@contextmanager
def remembered_only(*properties: dict[str, str]) -> Iterator[None]:
    """`remember_secrets` with nothing else remembered, and nothing left behind afterwards.

    The set is module state, and a random string remembered here would be scrubbed out of every
    later test's output - `demo.events` included, if hypothesis ever drew it.
    """
    saved = catalog._secrets
    catalog._secrets = set()
    try:
        for props in properties:
            remember_secrets(props)
        yield
    finally:
        catalog._secrets = saved


#: secret material: long enough to be remembered, and with no `*`, which is what `scrub` writes
_secret = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00*"), min_size=6, max_size=24)
_SECRET_KEYS = ("token", "s3.secret-access-key", "credential", "password", "gcs.oauth2.token", "header.Authorization", "adls.private-key")
_PLAIN_KEYS = ("uri", "warehouse", "s3.endpoint", "s3.access-key-id", "s3.region", "type")


@bounded
@given(st.lists(_secret, min_size=1, max_size=4), st.lists(st.text(_chars, max_size=12), min_size=5, max_size=5), st.sampled_from(_SECRET_KEYS))
def test_no_remembered_secret_survives_scrub(secrets: list[str], around: list[str], key: str) -> None:
    text = around[0] + "".join(s + a for s, a in zip(secrets, around[1:] + around[:1], strict=False))
    with remembered_only({f"{key}.{i}": s for i, s in enumerate(secrets)}):
        out = scrub(text)
    for secret in secrets:
        assert secret not in out


#: `scrub` walked its set in hash order, so whether this held changed with PYTHONHASHSEED; it
#: scrubs longest first now. On any one run hash order could happen to be the right one, which
#: is why `test_hardening` pins the wrong order as well.
@bounded
@given(_secret, st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00*"), min_size=1, max_size=8), st.booleans())
def test_a_secret_is_masked_whole_when_a_shorter_one_is_inside_it(base: str, extra: str, before: bool) -> None:
    """A credential and the secret half of it are both remembered; the credential must vanish whole.

    `credential=root:s3cr3t` beside `s3.secret-access-key=s3cr3t` is the realistic pair. Masking
    the short one first leaves `root:********` - the half that was not secret, but also every
    character of the long one that the short one did not cover.
    """
    longer = extra + base if before else base + extra
    with remembered_only({"token": base, "credential": longer}):
        assert scrub(longer) == "********"


@bounded
@given(st.sampled_from(_PLAIN_KEYS), st.text(_chars, max_size=30).filter(lambda v: "://" not in v))
def test_a_value_that_is_not_a_credential_is_not_remembered(key: str, value: str) -> None:
    """An access key *id* is deliberately shown: it identifies a key rather than being one."""
    with remembered_only({key: value}):
        assert scrub(f"<{value}>") == f"<{value}>"


@bounded
@given(st.sampled_from(_SECRET_KEYS), st.text(_chars, max_size=5))
def test_a_value_too_short_to_be_a_credential_is_not_scrubbed_everywhere(key: str, value: str) -> None:
    """`token=x` would otherwise blank every `x` in every message the tool ever printed."""
    with remembered_only({key: value}):
        assert scrub(f"<{value}>") == f"<{value}>"


#: userinfo as RFC 3986 allows it: unreserved and sub-delims, `:` between user and password, and
#: `@` inside the password, which is invalid and common enough that `_userinfo` copes with it
_userpart = st.text("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._~!$&'()*+,;=%", min_size=1, max_size=12)
_host = st.from_regex(r"[a-z][a-z0-9-]{0,10}(\.[a-z]{2,5}){0,2}(:[0-9]{2,5})?", fullmatch=True)
_path = st.from_regex(r"(/[A-Za-z0-9_.-]{0,8}){0,3}", fullmatch=True)


@bounded
@given(st.sampled_from(("http", "https", "thrift", "postgresql")), _userpart, _userpart, _host, _path, st.booleans())
def test_a_password_in_a_uri_reaches_no_label_and_no_message(scheme: str, user: str, password: str, host: str, path: str, at_sign: bool) -> None:
    if at_sign:
        password = f"{password}@{password}"
    uri = f"{scheme}://{user}:{password}@{host}{path}"
    assert redact_uri(uri) == f"{scheme}://{host}{path}"
    target = Target({"uri": uri, "warehouse": "wh"})
    # equality rather than `password not in label`: hypothesis is entitled to draw a password
    # that is also the host name, and the host is meant to be shown
    assert target.label == f"wh @ {scheme}://{host}{path}"
    assert target.redacted()["uri"] == f"{scheme}://{host}{path}"
    with remembered_only({"uri": uri}):
        assert f"{user}:{password}" not in scrub(f"GET {uri} failed")


@bounded
@given(_userpart.filter(lambda user: len(user) < 6), _host)
def test_a_short_bare_user_name_is_not_scrubbed_out_of_every_message(user: str, host: str) -> None:
    """No password and too short to be a token: blanking it would blank that string everywhere."""
    with remembered_only({"uri": f"https://{user}@{host}/catalog"}):
        assert scrub(f"<{user}>") == f"<{user}>"


@bounded
@given(st.dictionaries(st.sampled_from(_SECRET_KEYS + _PLAIN_KEYS + ("header.X-Custom", "client.region")), st.text(_chars, max_size=20), max_size=8))
def test_redacted_blanks_every_secret_key_and_nothing_else(properties: dict[str, str]) -> None:
    shown = Target(properties).redacted()
    assert set(shown) == set(properties)
    for key, value in properties.items():
        secretish = any(word in key.lower() for word in catalog._SECRET_KEYS)
        assert shown[key] == ("********" if secretish else redact_uri(value))


@bounded
@given(hostile, st.sampled_from(("cannot load demo.t", "cannot list namespaces", "cannot open catalog 'x' (rest)")))
def test_a_classified_error_is_one_line_and_carries_no_credential(message: str, what: str) -> None:
    with remembered_only({"token": "tok-1234567890"}):
        error = classify(RuntimeError(f"{message} tok-1234567890"), what)
    assert isinstance(error, IcelensError)
    text = str(error)
    assert text.startswith(f"{what}: ")
    assert "tok-1234567890" not in text
    assert len(text[len(what):].splitlines()) <= 1


# -- .env ----------------------------------------------------------------------------------------

_env_key = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,20}", fullmatch=True)
#: a value that means the same thing however it is quoted: no line breaks, no `#`, no quotes,
#: nothing that `strip` would take off the ends
_env_value = st.text(
    st.characters(blacklist_categories=("Cs", "Cc", "Zl", "Zp"), blacklist_characters="#'\"\x00"), max_size=30
).map(str.strip)


@bounded
@given(st.dictionaries(_env_key, _env_value, max_size=8), st.data())
def test_a_dotenv_reads_back_as_it_was_written(values: dict[str, str], data) -> None:
    lines = ["# written by a test"]
    for key, value in values.items():
        quote_mark = data.draw(st.sampled_from(("", "'", '"')))
        spacing = data.draw(st.sampled_from(("", " ")))
        comment = data.draw(st.sampled_from(("", "  # a note")))
        lines.append(f"{spacing}{key}{spacing}={spacing}{quote_mark}{value}{quote_mark}{comment}")
        lines.append(data.draw(st.sampled_from(("", "   ", "# between"))))
    with tempfile.TemporaryDirectory() as home:
        path = Path(home) / ".env"
        path.write_text("\n".join(lines), encoding="utf-8")
        path.chmod(0o600)
        assert read_dotenv(path) == values


def test_a_dotenv_at_the_size_limit_is_read_and_one_byte_over_is_not(tmp_path: Path) -> None:
    # Bytes, not text: on Windows `write_text` turns the `\n` into `\r\n`, one byte over.
    line = b"ICELENS_CATALOG_URI=http://x\n"
    path = tmp_path / ".env"
    path.write_bytes(line + b"#" * (MAX_DOTENV_BYTES - len(line)))
    path.chmod(0o600)
    assert path.stat().st_size == MAX_DOTENV_BYTES
    assert read_dotenv(path) == {"ICELENS_CATALOG_URI": "http://x"}
    path.write_bytes(line + b"#" * (MAX_DOTENV_BYTES - len(line) + 1))
    assert read_dotenv(path) == {}


@pytest.mark.skipif(os.name != "posix", reason="mode bits")
@pytest.mark.parametrize(
    ("mode", "read"),
    [(0o600, True), (0o644, True), (0o400, True), (0o620, False), (0o602, False), (0o666, False), (0o777, False)],
)
def test_a_dotenv_anyone_else_can_write_is_refused(tmp_path: Path, mode: int, read: bool) -> None:
    """Group-writable and world-writable are each enough: either is somebody else choosing the catalog."""
    path = tmp_path / ".env"
    path.write_text("ICELENS_CATALOG_URI=http://x\n")
    path.chmod(mode)
    assert bool(read_dotenv(path)) is read


def _returns_promptly(path: Path) -> dict[str, str]:
    """`read_dotenv`, on a thread with a deadline: a fifo that was opened would never come back."""
    found: list[dict[str, str]] = []
    worker = threading.Thread(target=lambda: found.append(read_dotenv(path)), daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), f"read_dotenv blocked on {path}"
    return found[0]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs fifos")
def test_a_fifo_is_not_a_dotenv_and_is_never_opened(tmp_path: Path) -> None:
    fifo = tmp_path / ".env"
    os.mkfifo(fifo, 0o600)
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    assert _returns_promptly(fifo) == {}


@pytest.mark.skipif(os.name != "posix", reason="symlinks and modes")
def test_a_symlink_is_judged_by_what_it_points_at(tmp_path: Path) -> None:
    real = tmp_path / "real.env"
    real.write_text("ICELENS_WAREHOUSE=wh\n")
    real.chmod(0o600)
    link = tmp_path / ".env"
    link.symlink_to(real)
    assert read_dotenv(link) == {"ICELENS_WAREHOUSE": "wh"}
    real.chmod(0o666)  # the link is fine; what it points at is not
    assert read_dotenv(link) == {}
    dangling = tmp_path / "dangling.env"
    dangling.symlink_to(tmp_path / "nowhere")
    assert read_dotenv(dangling) == {}
    if hasattr(os, "mkfifo"):
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo, 0o600)
        to_fifo = tmp_path / "to-fifo.env"
        to_fifo.symlink_to(fifo)
        assert _returns_promptly(to_fifo) == {}


@pytest.mark.skipif(not Path("/dev/null").exists(), reason="needs /dev/null")
def test_a_device_is_not_a_dotenv() -> None:
    assert _returns_promptly(Path("/dev/null")) == {}


# -- the row limit -------------------------------------------------------------------------------


@bounded
@given(
    st.one_of(
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.decimals(allow_nan=True, allow_infinity=True),
        st.text(max_size=12),
        st.booleans(),
        st.none(),
        st.lists(st.integers(), max_size=2),
    )
)
def test_a_row_limit_is_always_a_bounded_whole_number(value: object) -> None:
    """Whatever a config file or a caller hands over, the engine gets 0..MAX_ROWS - never -1, never an error."""
    limit = row_limit(value)
    assert type(limit) is int
    assert 0 <= limit <= MAX_ROWS


@bounded
@given(st.integers())
def test_an_integer_row_limit_is_clamped_not_reinterpreted(value: int) -> None:
    assert row_limit(value) == max(0, min(MAX_ROWS, value))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(float("inf"), MAX_ROWS), (float("-inf"), 0), (Decimal("Infinity"), MAX_ROWS), (Decimal("-Infinity"), 0), (float("nan"), 200)],
)
def test_an_infinite_row_limit_is_clamped_like_any_other_number(value: object, expected: int) -> None:
    """`int(inf)` raises OverflowError, which is neither of the two errors the clamp used to catch."""
    assert row_limit(value) == expected


# -- what reaches the terminal -------------------------------------------------------------------

#: every character `safe` exists to remove, written out from the rule rather than from its regex:
#: C0 controls except tab and newline, DEL, C1, the zero-width and invisible formatting
#: characters, every bidi embedding, override, isolate and mark, and the byte-order mark
_DRIVING = frozenset(
    [chr(c) for c in range(0x00, 0x20) if chr(c) not in "\t\n"]
    + [chr(c) for c in range(0x7F, 0xA0)]
    + [chr(c) for c in range(0x200B, 0x2010)]
    + [chr(c) for c in range(0x202A, 0x202F)]
    + [chr(c) for c in range(0x2060, 0x2065)]
    + [chr(c) for c in range(0x2066, 0x206A)]
    + ["؜", "﻿"]
)

_terminal_text = st.one_of(
    st.text(st.characters(blacklist_categories=("Cs",)), max_size=40),
    st.lists(st.one_of(st.sampled_from(sorted(_DRIVING)), st.sampled_from(("a", "[/]", "\x1b[2J", " ", "é", "漢"))), max_size=20).map("".join),
)


@bounded
@given(_terminal_text)
def test_safe_replaces_every_driving_character_and_touches_nothing_else(text: str) -> None:
    shown = safe(text)
    assert len(shown) == len(text)
    for before, after in zip(text, shown, strict=True):
        assert after == (REPLACEMENT if before in _DRIVING else before)
    assert safe(shown) == shown


#: what a Parquet cell can hold: scalars, and lists, maps and structs of them, keys included.
#: `deferred` rather than `recursive`: the latter's check that its extend function recurses
#: misfires on Python 3.10 and warns about a strategy that demonstrably nests.
_cell_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.floats(allow_nan=False), _terminal_text, st.binary(max_size=20))
_cell_values = st.deferred(
    lambda: st.one_of(
        _cell_scalars,
        st.lists(_cell_values, max_size=3),
        st.dictionaries(_terminal_text, _cell_values, max_size=3),
        st.lists(st.tuples(_terminal_text, _cell_values), min_size=1, max_size=3),  # how Arrow hands back a map
    )
)


@bounded
@given(_cell_values)
def test_no_value_in_a_cell_can_drive_the_terminal(value: object) -> None:
    """A map key or a struct field name is as much the data file's choosing as a value is."""
    text = scalar(value)
    assert not set(text) & _DRIVING
    assert "\n" not in text and "\t" not in text


@bounded
@given(_cell_values, st.integers(-5, 80))
def test_a_cell_is_never_wider_than_asked_and_says_when_it_was_cut(value: object, width: int) -> None:
    shown = cell(value, width).plain
    limit = max(4, width)
    assert len(shown) <= limit
    full = scalar(value) if value is not None else "NULL"
    assert shown == full or (shown.endswith("…") and full.startswith(shown[:-1]))

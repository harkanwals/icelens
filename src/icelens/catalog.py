"""Reaching an Iceberg catalog, and reading it two ways.

Metadata comes from **PyIceberg**: it speaks every catalog dialect (REST, Glue, Hive, SQL) and
hands back the snapshots, manifests and specs that make a table an Iceberg table rather than a
pile of Parquet. Rows come from **DuckDB** with the catalog ATTACHed, because scanning is what
DuckDB is for and because the SQL pane has to talk to something.

Neither client is required for the other. Without the DuckDB iceberg extension - or against a
catalog DuckDB cannot attach, such as Hive - every metadata pane still works and the row panes
fall back to a PyIceberg scan. `Lake.engine` says which one answered, and the app shows it,
because "where did this number come from" is the whole point of a tool like this.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa

from icelens import trace

_log = logging.getLogger("icelens.catalog")

#: the alias the catalog is attached under in DuckDB, so tables are `icelens.<ns>.<table>`
ALIAS = "icelens"
# The most rows the `arrow` route copies from one table (see `_arrow_scan`).
ARROW_ROW_LIMIT = 5_000_000

#: The v3 row-lineage columns, in the order they are shown. DuckDB computes them on read;
#: they are not columns of the schema and not stored in the data files.
LINEAGE_COLUMNS = ("_row_id", "_last_updated_sequence_number")


#: `.env` key suffixes this understands, so a project's own variable names work unedited.
#: Matched on the suffix because everyone prefixes them (MYAPP_, ICEBERG_, ACME_).
_DOTENV_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("CATALOG_URI", "uri"),
    ("CATALOG_URL", "uri"),
    ("REST_URI", "uri"),
    ("WAREHOUSE", "warehouse"),
    ("S3_ENDPOINT", "s3.endpoint"),
    ("S3_REGION", "s3.region"),
    ("AWS_REGION", "s3.region"),
    ("S3_ACCESS_KEY", "s3.access-key-id"),
    ("ACCESS_KEY_ID", "s3.access-key-id"),
    ("S3_SECRET_KEY", "s3.secret-access-key"),
    ("SECRET_ACCESS_KEY", "s3.secret-access-key"),
)


class IcelensError(RuntimeError):
    """Something the user can fix: no catalog, no table, a query that will not parse."""

    #: What a multi-statement write committed before this failure stopped it. Those commits
    #: stand - Iceberg has no transaction across statements - so they are reported, not lost.
    written: tuple[Written, ...] = ()


class CatalogUnreachable(IcelensError):
    """Nothing answered: wrong host, no route, nothing listening, or it timed out."""


class CatalogAuthError(IcelensError):
    """The catalog wants credentials it did not get, or got expired ones."""


class CatalogPermissionDenied(IcelensError):
    """The credentials are good and the answer is still no."""


def classify(exc: BaseException, what: str) -> IcelensError:
    """Turn whatever a catalog client raised into one of four things a user can act on.

    Worth the trouble because the four have different fixes - log in, ask for a grant, fix
    the address, fix the request - and because every dialect spells them differently:
    PyIceberg's own exceptions over REST, botocore's over Glue, thrift's over Hive. A tool
    that says "cannot open catalog: ClientError" for all four has told you nothing.
    """
    text = scrub(_one_line(exc))
    low = text.lower()
    name = type(exc).__name__.lower()
    if "nosuch" in name or "notfound" in name:
        # Said by the exception's type, before its words are read: "Table does not exist:
        # demo.expired_sessions" names a table, and a name is not a reason to log in again.
        return IcelensError(f"{what}: {text}")
    if "unauthorized" in name or _says(low, ("401", "unauthorized", "not authenticated", "invalid token", "expired")):
        return CatalogAuthError(f"{what}: not authenticated - {text}")
    if "forbidden" in name or _says(low, ("403", "forbidden", "access denied", "accessdenied", "not authorized")):
        return CatalogPermissionDenied(f"{what}: access denied - {text}")
    if _says(
        low,
        (
            "connection refused",
            "failed to establish",
            "name or service not known",
            "nodename nor servname",
            "temporary failure in name resolution",
            "max retries",
            "timed out",
            "timeout",
            "unreachable",
            "could not connect",
        ),
    ):
        return CatalogUnreachable(f"{what}: catalog unreachable - {text}")
    return IcelensError(f"{what}: {text}")


def _refusal(exc: BaseException) -> str:
    """Why a catalog said no, in the words `classify` would use, then the server's own."""
    label = {
        CatalogAuthError: "not authenticated",
        CatalogPermissionDenied: "access denied",
        CatalogUnreachable: "unreachable",
    }.get(type(classify(exc, "")))
    detail = _one_line(exc)
    return f"{label}: {detail}" if label else detail


def _says(text: str, words: tuple[str, ...]) -> bool:
    """Whether `text` says one of `words`, rather than containing it inside a name.

    Error text quotes table names, and a table is free to be called `orders_2401` or
    `raw.expired_sessions`. So a match may not follow a letter, a digit, a dot or a dash, nor
    run on into an underscore or a digit. The edges are deliberately uneven: botocore spells
    its reasons `AccessDeniedException` and `ExpiredTokenException`, and OAuth servers spell
    theirs `token_expired`, so a letter after the word and an underscore before it still count.
    The exception's *type* settles the commonest case before any of this is read.
    """
    return any(re.search(rf"(?<![^\W_])(?<![.-]){re.escape(word)}(?![_\d])", text) for word in words)


def _unreadable_type(exc: BaseException) -> bool:
    """Whether PyIceberg refused a table for a column type it cannot parse: `variant`, or a
    `geometry(OGC:CRS84)` spelled the spec's way, unquoted, as Java and DuckDB write it."""
    return bool(re.match(r"Unsupported field type: |Could not parse \S+ into a \w+Type", _one_line(exc)))


def _pyiceberg_version() -> str:
    from importlib.metadata import version

    return version("pyiceberg")


def _one_line(exc: BaseException) -> str:
    # PyIceberg parses table metadata with pydantic, whose first line is only "1 validation
    # error for TableResponse"; the reason ("Unsupported field type: 'variant'") is on a later
    # one. Measured on v3 tables Spark 4.1 and DuckDB wrote with a variant column.
    if type(exc).__name__ == "ValidationError":
        try:
            first: Any = exc.errors()[0]  # pyright: ignore[reportAttributeAccessIssue]
            reason = str(first.get("msg", "")).removeprefix("Value error, ").strip()
        except Exception:  # noqa: BLE001 - a malformed error is no reason to lose the original
            reason = ""
        if reason:
            return reason.splitlines()[0][:200]
    text = str(exc).strip()
    return text.splitlines()[0][:200] if text else type(exc).__name__


#: property names whose *values* must never reach a screen, a log or a bug report.
#: An access key *id* is deliberately not on the list: it identifies a key rather than being one,
#: and blanking it makes "which credentials am I using" unanswerable, which is how people end up
#: pasting the real secret into a chat window to find out.
#: Azure spells its three the most ways: `adls.account-key`, a connection string that carries the
#: key inside it, and SAS tokens keyed per account (`adls.sas-token.<account>`).
_SECRET_KEYS = (
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "private-key",
    "private_key",
    "account-key",
    "connection-string",
    "sas",
)

#: the live secrets to scrub out of message text, set when a Target is bound
_secrets: set[str] = set()


def remember_secrets(properties: dict[str, str]) -> None:
    """Record which literal strings are credentials, so no message can ever echo one.

    Error text is the leak nobody looks for. DuckDB quotes the statement it failed on - and
    that statement is `ATTACH '…' (TOKEN '…')`; PyIceberg and botocore echo request URLs that
    may carry a signature. All of it lands in a status bar, a pane note, or a pasted bug
    report. Scrubbing by value rather than by key catches it wherever it surfaces.
    """
    for key, value in properties.items():
        if not isinstance(value, str):
            continue  # a FileIO's properties can hold objects - PyIceberg keeps its auth manager there
        if value and len(value) >= 6 and any(s in key.lower() for s in _SECRET_KEYS):
            _secrets.add(str(value))
            # `credential` is `client_id:client_secret`, and DuckDB is handed the two halves
            # separately - so the secret half has to be scrubbable on its own, or an error
            # quoting `CLIENT_SECRET '…'` would print it.
            _, colon, half = str(value).partition(":")
            if colon and len(half) >= 6:
                _secrets.add(half)
            # An Azure connection string is `AccountName=…;AccountKey=…;…`, and an error from the
            # SDK quotes the key on its own, so the key and any signature are remembered apart.
            for part in value.split(";"):
                name, equals, piece = part.partition("=")
                if equals and name.strip().lower() in ("accountkey", "sharedaccesssignature") and len(piece) >= 6:
                    _secrets.add(piece.strip())
        if value and "://" in value:
            userinfo = _userinfo(value)
            # A password, or something long enough to be a token used as a user name. A bare
            # short user name is neither, and remembering it blanked that string out of every
            # message: `http://x@host` turned `demo.taxi_trips` into `demo.ta********i_trips`.
            if userinfo and (":" in userinfo or len(userinfo) >= 6):
                _secrets.add(userinfo)


def _userinfo(uri: str) -> str:
    """The `user:pass` of a URI, which people paste into `uri` far more often than they admit."""
    _, _, rest = uri.partition("://")
    host = rest.split("/", 1)[0]
    return host.rsplit("@", 1)[0] if "@" in host else ""


def scrub(text: str) -> str:
    """Whatever it was, with every known credential replaced by `********`.

    Longest first. One secret can sit inside another - `secret` inside `half-secret-5512` -
    and a set has no order of its own: replacing the short one first left the long one as
    `half-********-5512`, partly printed, on some runs and not others.
    """
    for secret in sorted(_secrets, key=len, reverse=True):
        if secret in text:
            text = text.replace(secret, "********")
    return text


class NoCatalog(IcelensError):
    """Nothing says which catalog to open: no flag, no variable, no `.env`, no profile.

    Its own class because it is the first thing a newcomer meets, and the honest answer is
    not PyIceberg's "URI missing" - it is that there is a catalog to be had without setting
    anything up, and how to ask for it.
    """

    def __init__(self, ignored: str = "") -> None:
        # `ignored` is a `.env` that was there and refused. Without it the reader is told to
        # write the file they are looking at, which is how "it ignores my .env" gets reported.
        super().__init__(
            "no Iceberg catalog is configured here. To try icelens with nothing to set up, "
            "`icelens --demo` makes a sandbox with a table in it - no server, no Docker. "
            "To open yours: --uri and --warehouse, a .env in this directory, or --catalog NAME "
            "from ~/.pyiceberg.yaml." + (f" icelens {ignored}." if ignored else "")
        )


class NeedsWarehouse(IcelensError):
    """The catalog will not answer without being told which warehouse to open.

    Carries whatever the server was willing to list, so the caller can offer a choice
    instead of a dead end. REST catalogs that host several warehouses (Lakekeeper, Polaris,
    Unity) all behave this way, and "specify the warehouse parameter" is a poor error
    message when the server can be asked what the options are.
    """

    def __init__(self, message: str, options: list[str] | None = None):
        super().__init__(message)
        self.options = options or []


def quote(name: str) -> str:
    """A SQL identifier, escaped rather than validated.

    Escaping beats a regex in both directions. Iceberg puts almost no restrictions on a table
    or column name, so a whitelist rejects names that are perfectly legal upstream; and a
    double-quoted identifier with `"` doubled is unambiguous to DuckDB whatever is inside it,
    so there is nothing a whitelist would catch that escaping does not.
    """
    if not name:
        raise IcelensError("empty identifier")
    if "\x00" in name:
        raise IcelensError("identifier contains a NUL byte")
    return '"' + name.replace('"', '""') + '"'


#: kept as the spelling of a *plain* identifier, for messages and tests; not a security check
_quoted = quote


def literal(value: str) -> str:
    """A SQL string literal, single-quoted and escaped.

    DuckDB takes no bind parameters in `ATTACH`, `CREATE SECRET` or a table function argument,
    which is exactly where this tool has to put a warehouse name, an endpoint, a token and a
    metadata-file path. Those values come from `~/.pyiceberg.yaml`, from `ICELENS_*`, from a
    `.env` found by walking *up* from the working directory, and from the catalog itself - so
    at least one of them is routinely not written by the person running the tool.

    That matters more than it would in most places: `duckdb.execute()` accepts several
    statements in one string, and the `httpfs` extension this tool loads can both read and
    write remote URLs. An unescaped quote in a warehouse name is therefore not a broken
    query, it is `COPY … TO 'http://…'` running as the user.
    """
    text = str(value)
    if "\x00" in text:
        raise IcelensError("value contains a NUL byte")
    return "'" + text.replace("'", "''") + "'"


def one_statement(sql: str) -> str:
    """The SQL back, having checked it is a single statement. Raises if it is not.

    Used on the statements icelens *generates*, never on what the user types into the SQL
    pane: a person driving DuckDB directly may run as many statements as they like, but a
    query assembled out of configuration must be exactly the one it looks like.

    SQL that DuckDB cannot parse is passed through rather than refused, and that is not a
    hole: `execute` parses it with the same parser, so it will not run either. The case worth
    stopping is the one that parses cleanly - into two statements.
    """
    try:
        import duckdb

        statements = duckdb.extract_statements(sql)
    except ImportError:
        return sql
    except Exception:  # noqa: BLE001 - a parse failure is DuckDB's to report, not ours
        return sql
    if len(statements) > 1:
        raise IcelensError("refusing to run more than one statement in a generated query")
    return sql


#: Statement types the read-only guard lets through. DuckDB types `DESCRIBE`, `SHOW`,
#: `SUMMARIZE` and a CTE as `SELECT`, so the set stays this small on purpose: anything it does
#: not name can change something, `COPY ... TO`, `CALL` and `ATTACH` included.
#:
#: `EXPLAIN` is here because `EXPLAIN SELECT` is a read worth having, but DuckDB types
#: `EXPLAIN ANALYZE <anything>` as `EXPLAIN` too - and `ANALYZE` *runs* the statement. Measured:
#: `EXPLAIN ANALYZE DELETE FROM t` empties `t`. So every EXPLAIN has its tail re-parsed below.
READS_ONLY: frozenset[str] = frozenset({"SELECT", "EXPLAIN"})

#: `EXPLAIN` at the very start of a statement, and nothing else. Deliberately strict: a leading
#: comment, or a comment between the two words, makes this fail to match and the statement is
#: then refused rather than guessed at. `EXPLAIN/*x*/ANALYZE DELETE FROM t` is a real spelling
#: DuckDB accepts, and no keyword scan that tolerates comments is worth trusting.
_EXPLAINING = re.compile(r"^\s*EXPLAIN\b(?P<tail>.*)$", re.IGNORECASE | re.DOTALL)


def _statement_types(sql: str) -> list[str] | None:
    """Every statement's type name, or None when DuckDB cannot parse the SQL."""
    try:
        import duckdb

        return [statement.type.name for statement in duckdb.extract_statements(sql)]
    except ImportError:
        return None
    except Exception:  # noqa: BLE001 - a parse failure is DuckDB's to report, not ours
        return None


def _explained_reads(statement: Any) -> bool:
    """True when an EXPLAIN statement explains something that only reads.

    The check is the whole statement minus its leading `EXPLAIN`, handed back to the parser.
    `EXPLAIN ANALYZE DELETE FROM t` leaves `ANALYZE DELETE FROM t`, which does not parse, so it
    is refused - which is the point. `EXPLAIN SELECT 1` leaves `SELECT 1`, which does.
    """
    match = _EXPLAINING.match(getattr(statement, "query", "") or "")
    if match is None:
        return False
    types = _statement_types(match.group("tail"))
    return bool(types) and not set(types) - READS_ONLY


def only_reads(sql: str) -> str:
    """The SQL back, having checked every statement in it only reads. Raises if one does not.

    icelens is a browser, but DuckDB attached to a real catalog will run `DROP TABLE` through
    the same connection the panes read with, and the request would look no different. `--sql`
    is the half of the tool a script or an agent drives, so it is guarded by default. The SQL
    pane in the app stays exempt, for the reason `one_statement` gives: a person typing into
    it is driving DuckDB, having meant to.

    This is the second of two guards, and the weaker one. The first is `READ_ONLY` on the
    ATTACH itself, which DuckDB enforces in the engine; this one exists to refuse in English
    before the engine refuses in its own words, and to cover what a read-only attach does not:
    `COPY ... TO '/tmp/x.csv'` writes a local file without touching the catalog at all.

    SQL DuckDB cannot parse is passed through rather than refused - `execute` will reject it
    with a better message than this function could invent.
    """
    offenders = _writes_in(sql)
    if offenders:
        raise IcelensError(
            f"refusing to run {', '.join(sorted(offenders))}: icelens reads. Pass --allow-writes to mean it."
        )
    return sql


def _writes_in(sql: str) -> set[str]:
    """The kinds of statement in `sql` that are not reads; empty when DuckDB cannot say."""
    try:
        import duckdb

        statements = duckdb.extract_statements(sql)
    except ImportError:
        return set()
    except Exception:  # noqa: BLE001 - a parse failure is DuckDB's to report, not ours
        return set()
    offenders = set()
    for statement in statements:
        kind = statement.type.name
        if kind not in READS_ONLY:
            offenders.add(kind)
        elif kind == "EXPLAIN" and not _explained_reads(statement):
            offenders.add("EXPLAIN ANALYZE")
    return offenders


#: The three statement shapes a sandbox accepts. Narrow on purpose: DuckDB's parser says what
#: *kind* a statement is, but not what it is against, and guessing a table name out of SQL that
#: nearly matches is how a write lands somewhere nobody meant. Anything these do not match is
#: refused with the statement quoted back, rather than interpreted generously.
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+(?P<table>[\w.\"]+)\s+(?P<query>(SELECT|WITH|VALUES|FROM)\b.*)$", re.I | re.S)
_DELETE = re.compile(r"^\s*DELETE\s+FROM\s+(?P<table>[\w.\"]+)\s+WHERE\s+(?P<where>.+?)\s*$", re.I | re.S)
_CREATE = re.compile(r"^\s*CREATE\s+TABLE\s+(?P<table>[\w.\"]+)\s+AS\s+(?P<query>.+?)\s*$", re.I | re.S)

#: DuckDB hands a statement back with the comments around it still attached, and the Writer
#: pane opens on a commented example - so every shape above would fail to match the one
#: statement that pane most wants to run, and the trailing comments would end up inside the
#: query the statement carries. Comments *inside* a statement are SQL DuckDB has already
#: parsed and are left alone; the trailing rule additionally insists on a line of its own, so
#: a `--` that is really part of a string literal is not mistaken for a comment.
_LEADING_COMMENT = re.compile(r"^\s*(?:--[^\n]*(?:\n|$)|/\*.*?\*/)\s*", re.S)
_TRAILING_COMMENT = re.compile(r"\n[ \t]*(?:--[^\n]*|/\*.*?\*/)\s*$", re.S)


def _uncommented(sql: str) -> str:
    """A statement with the comments around it removed, and its terminating `;` with them."""
    for pattern in (_LEADING_COMMENT, _TRAILING_COMMENT):
        while (shorter := pattern.sub("", sql, count=1)) != sql:
            sql = shorter
    return sql.strip().rstrip(";").strip()


@dataclass(frozen=True)
class Write:
    """One statement, classified: what it does, to which table, with what."""

    kind: str
    table: str
    payload: str
    sql: str


@dataclass(frozen=True)
class Written:
    """What a commit actually did, read back off the snapshot it produced."""

    kind: str
    table: str
    operation: str
    snapshot_id: int
    added: int
    deleted: int
    records: int
    files: int
    engine: str
    sql: str

    def line(self) -> str:
        """The one line the app puts under the pane after a write."""
        parts = [f"{self.operation} on {self.table}"]
        if self.added:
            parts.append(f"+{self.added} rows")
        if self.deleted:
            parts.append(f"-{self.deleted} rows")
        parts.append(f"snapshot {self.snapshot_id}")
        return " · ".join(parts) + f" · {self.engine}"


def strip_alias(ident: str, alias: str = ALIAS) -> str:
    """`icelens."demo"."sales"` -> `demo.sales`, so a name reads the same in both panes."""
    parts = [part.strip('"') for part in re.findall(r'"[^"]*"|[^.]+', ident)]
    if parts and parts[0] == alias:
        parts = parts[1:]
    return ".".join(parts)


def _statements(sql: str) -> list[Any]:
    """Every statement in `sql`, as DuckDB's own parser splits it.

    Both routers go through here so the offline sandbox and the attached one can never
    disagree about where one statement ends and the next begins.
    """
    try:
        import duckdb

        return list(duckdb.extract_statements(sql))
    except ImportError as exc:
        raise IcelensError("writing needs DuckDB, which is not installed here") from exc
    except Exception as exc:  # noqa: BLE001 - a parse failure is DuckDB's to report
        raise IcelensError(f"DuckDB cannot parse that: {_one_line(exc)}") from exc


def route(sql: str) -> list[Write]:
    """Classify every statement in `sql` into the writes a sandbox knows how to commit.

    DuckDB's own parser decides the statement kind - the same `extract_statements` call
    `only_reads` already makes, so the guard and the router cannot disagree about what a
    statement is. The shape *within* a kind is matched here, narrowly, and a statement that
    does not match is refused rather than guessed at.
    """
    writes: list[Write] = []
    for statement in _statements(sql):
        text = _uncommented(statement.query)
        kind = statement.type.name
        if kind in READS_ONLY:
            writes.append(Write("read", "", "", text))
            continue
        if kind == "INSERT" and (m := _INSERT.match(text)):
            writes.append(Write("append", strip_alias(m["table"]), m["query"].strip(), text))
        elif kind == "DELETE" and (m := _DELETE.match(text)):
            writes.append(Write("delete", strip_alias(m["table"]), m["where"].strip(), text))
        elif kind == "CREATE" and (m := _CREATE.match(text)):
            writes.append(Write("create", strip_alias(m["table"]), m["query"].strip(), text))
        else:
            raise IcelensError(
                f"a sandbox writes with INSERT INTO t SELECT …, DELETE FROM t WHERE …, or "
                f"CREATE TABLE t AS SELECT …. It cannot do this one: {text[:120]}"
            )
    return writes


#: An attached sandbox does not have to guess how to *perform* a statement - DuckDB performs
#: it - so these shapes only have to find the table it is against, and the vocabulary can be
#: as wide as DuckDB's. `UPDATE` and `MERGE` are here and absent from the offline router for
#: exactly that reason: nothing in this file has to know how to merge.
_ATTACH_SHAPES: dict[str, re.Pattern[str]] = {
    "INSERT": re.compile(r"^\s*INSERT\s+INTO\s+(?P<table>[\w.\"]+)", re.I),
    "DELETE": re.compile(r"^\s*DELETE\s+FROM\s+(?P<table>[\w.\"]+)", re.I),
    "UPDATE": re.compile(r"^\s*UPDATE\s+(?P<table>[\w.\"]+)", re.I),
    "CREATE": re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?P<table>[\w.\"]+)", re.I),
    "MERGE_INTO": re.compile(r"^\s*MERGE\s+INTO\s+(?P<table>[\w.\"]+)", re.I),
    "DROP": re.compile(r"^\s*DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?P<table>[\w.\"]+)", re.I),
    "ALTER": re.compile(r"^\s*ALTER\s+TABLE\s+(?P<table>[\w.\"]+)", re.I),
}

#: What each of those is called in the ledger. The snapshot's own `operation` is reported
#: beside it, and the two disagreeing is informative: `UPDATE` arrives as an `overwrite`.
_ATTACH_KINDS = {
    "INSERT": "insert",
    "DELETE": "delete",
    "UPDATE": "update",
    "CREATE": "create",
    "MERGE_INTO": "merge",
    "DROP": "drop",
    "ALTER": "alter",
}

#: A `WITH (...)` that follows the table name immediately belongs to the CREATE, not to a CTE
#: - `CREATE TABLE t AS WITH cte AS (...)` puts its `WITH` after the `AS`, which this misses
#: on purpose.
_CREATE_WITH = re.compile(r"^\s*WITH\s*\(", re.I)


#: What the ledger calls the writer on the attached side. The offline side names both halves
#: of its split; this side has only one half, which is the point of it.
_ATTACHED_ENGINE = "duckdb through the catalog"


def sandbox_table(ident: str, namespace: str | None) -> str:
    """`sales` -> `sandbox_demo.sales`, and a name outside the sandbox namespace is refused.

    This is the whole safety story of an attached sandbox. The catalog on the other end is
    somebody's real one, holding tables a lesson has no business touching, so the sandbox
    gets a namespace of its own and cannot write a byte outside it. A bare name is taken to
    mean that namespace, which is also how it stays pleasant to type.
    """
    name = strip_alias(ident)
    parts = [part.strip('"') for part in re.findall(r'"[^"]*"|[^.]+', name)]
    if not parts or not all(parts):
        raise IcelensError(f"that is not a table name: {ident!r}")
    if namespace is None:
        # A v3 sandbox is a catalog of this tool's own, so every namespace in it is the
        # sandbox's - but a table still has to say which one, as it does offline.
        if len(parts) == 1:
            raise IcelensError(f"say which namespace {parts[0]} goes in: demo.{parts[0]}, say")
        return ".".join(parts)
    if len(parts) == 1:
        return f"{namespace}.{parts[0]}"
    if ".".join(parts[:-1]) != namespace:
        raise IcelensError(
            f"a sandbox on a real catalog writes only inside {namespace}, and {name} is "
            f"somewhere else. Write {namespace}.{parts[-1]} - or just {parts[-1]}."
        )
    return ".".join(parts)


def route_attached(sql: str, namespace: str | None) -> list[Write]:
    """Classify statements for a sandbox DuckDB can attach, and qualify them for it.

    Nothing here is rewritten except the table name: the statement DuckDB runs is the
    statement that was typed, with `sales` turned into `icelens."sandbox_x"."sales"` so it
    lands in the sandbox namespace and nowhere else. `Write.payload` carries that qualified
    form; `Write.sql` keeps the original, because that is what belongs in the ledger.
    """
    writes: list[Write] = []
    for statement in _statements(sql):
        text = _uncommented(statement.query)
        kind = statement.type.name
        if kind in READS_ONLY:
            writes.append(Write("read", "", "", text))
            continue
        shape = _ATTACH_SHAPES.get(kind)
        match = shape.match(text) if shape else None
        if match is None:
            raise IcelensError(
                f"a sandbox DuckDB writes runs INSERT, DELETE, UPDATE, MERGE, CREATE "
                f"TABLE, ALTER TABLE and DROP TABLE. It cannot do this one: {text[:120]}"
            )
        table = sandbox_table(match["table"], namespace)
        start, end = match.span("table")
        writes.append(Write(_ATTACH_KINDS[kind], table, text[:start] + qualified(table) + text[end:], text))
    return writes


def with_format_version(sql: str, after: int, version: int = 3) -> str:
    """Ask DuckDB for a format version on a CREATE that did not name one.

    v3 is the reason an attached sandbox exists at all - deletion vectors and row lineage
    are v3-only - so it is the default here rather than something to remember. A CREATE that
    says `WITH ("format-version" = 2)` is left alone: asking for v2 on purpose is a lesson
    too, and the two tables side by side are the clearest way to show the difference.

    `after` is where the table name ends. The clause goes there for `CREATE TABLE t AS …`, and
    after the column list for `CREATE TABLE t (id INTEGER)`: DuckDB parses `t WITH (…) (id …)`
    as an error, so every column-list CREATE failed in a v3 sandbox. A WITH already after the
    column list is the user's, and is left alone like the other.
    """
    rest = sql[after:]
    if _CREATE_WITH.match(rest):
        return sql
    if rest.lstrip().startswith("("):
        close = _closing_paren(sql, after + len(rest) - len(rest.lstrip()))
        if close is None:
            return sql  # unbalanced: DuckDB will say so better than a guess here would
        if _CREATE_WITH.match(sql[close + 1:]):
            return sql
        after = close + 1
    return f'{sql[:after]} WITH ("format-version" = {version}){sql[after:]}'


def _closing_paren(sql: str, start: int) -> int | None:
    """The index of the `)` that closes the `(` at `start`, skipping quoted text; None if none.

    Quotes are skipped whole, doubled quotes included, so a column called `"a)b"` or a default
    of `')'` does not end the list early.
    """
    depth, index = 0, start
    while index < len(sql):
        char = sql[index]
        if char in "'\"":
            end = index + 1
            while end < len(sql):
                if sql[end] == char:
                    if sql[end + 1:end + 2] != char:
                        break
                    end += 1
                end += 1
            index = end
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _local(location: str) -> Path:
    """A `file://` table location as a path. Sandboxes are local; nothing else writes here.

    The inverse of `_file_uri`, so it does not percent-decode either. A drive letter keeps its
    place: `file:///C:/x` is `C:/x`, not a directory called `C:` at the root of the current drive.
    """
    if not location.startswith("file://"):
        raise IcelensError(f"a sandbox writes to local files, and this table lives at {location}")
    path = urllib.parse.urlsplit(location).path
    if re.match(r"/[A-Za-z]:", path):
        path = path[1:]
    return Path(path)


def _file_uri(path: Path) -> str:
    """A local path as the `file://` URI PyIceberg reads back as that same path.

    Not `Path.as_uri()`, which percent-encodes: PyIceberg 0.12 does not decode a `file://`
    path, so a sandbox under `~/My Lessons` had every table written to a sibling directory
    literally called `My%20Lessons`, and the Parquet icelens wrote was then looked for at
    `My%2520Lessons` - so nothing could be written there at all, and a reset left the misplaced
    directory behind. The path goes in as it is, with forward slashes, so a Windows drive letter
    reads `file:///C:/...`.
    """
    return "file:///" + path.as_posix().lstrip("/")


def _sandbox_name(name: str) -> str:
    """A sandbox name is a directory name and half a namespace name, so it has to be both."""
    if not name or not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
        raise IcelensError(f"a sandbox name is letters, digits, dot, dash or underscore: {name!r}")
    return name


def _sandbox_home(name: str, root: Path | str | None = None) -> Path:
    """Where one named sandbox lives. The name is a directory name, so it has to be one.

    Absolute, because the warehouse becomes a `file://` URI and a relative path has no URI of
    its own: `--sandbox-dir lessons` put every table under `/lessons`. And no `#` or `?`, which
    end the path part of a URI - PyIceberg would write the files somewhere shorter than asked.
    """
    _sandbox_name(name)
    base = Path(root).expanduser() if root else Path.home() / ".local" / "share" / "icelens" / "sandboxes"
    base = base.absolute()
    if "#" in str(base) or "?" in str(base):
        raise IcelensError(f"a sandbox cannot live under a path with # or ? in it, which a file URI cuts short: {base}")
    return base / name


def sandbox_home(name: str, root: Path | str | None = None) -> Path:
    """The directory a named sandbox lives in, whether or not it exists yet."""
    return _sandbox_home(name, root).resolve()


def make_sandbox_home(name: str, root: Path | str | None = None) -> Path:
    """`sandbox_home`, made, with the directory of sandboxes private to this user if it is new.

    A sandbox holds whatever was written into it, and on a v3 one the catalog's own database;
    under a default umask a new directory is readable by everybody on the machine.
    """
    home = _sandbox_home(name, root)
    if not home.parent.exists():
        home.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    (home / "warehouse").mkdir(parents=True, exist_ok=True)
    return home.resolve()


#: The file that remembers what kind of sandbox a directory is. Absent means the original kind.
SANDBOX_MARK = "sandbox.json"


@dataclass(frozen=True)
class Sandbox:
    """One sandbox made before: its name, how many tables it holds, and its format version."""

    name: str
    tables: int | None
    version: int


def sandbox_version(home: Path) -> int:
    """2 for a sandbox PyIceberg writes, 3 for one served through icelens's own REST catalog."""
    import json

    try:
        return int(json.loads((home / SANDBOX_MARK).read_text()).get("format-version", 2))
    except (OSError, ValueError, AttributeError):
        return 2


def sandbox_target(name: str = "default", root: Path | str | None = None, version: int | None = None) -> Target:
    """A scratch catalog of this tool's own making: local files, no server to install.

    Persistent rather than temporary, and named, because the history you built by hand is the
    thing worth coming back to. Nothing here needs credentials, a network or a container, so
    the first write is available about a second after the tool starts.

    Two kinds, decided once per sandbox and remembered in `sandbox.json`:

    - v2, the original: a SQLite catalog PyIceberg opens directly. DuckDB runs the query,
      PyIceberg commits the Parquet, and nothing can write v3 this way.
    - v3: the same SQLite catalog, served over Iceberg REST by `icelens.rest` on 127.0.0.1,
      so DuckDB can attach it and write format-version 3 - deletion vectors, row lineage,
      `UPDATE` and `MERGE` - with nothing to install and nothing listening beyond this process.

    `version=3` makes a sandbox v3 and keeps it so. A v2 sandbox's tables survive the change:
    the server reads the catalog PyIceberg wrote, and DuckDB writes to v2 tables too.
    """
    import json

    if version is not None and version not in (2, 3):
        raise IcelensError(f"a sandbox is format-version 2 or 3, not {version}")
    home = _sandbox_home(name, root)
    make_sandbox_home(name, root)
    if version == 3 and sandbox_version(home) < 3:
        (home / SANDBOX_MARK).write_text(json.dumps({"format-version": 3}) + "\n")
    if sandbox_version(home) >= 3:
        from icelens import rest

        running = rest.serve(home)
        return Target(
            properties={
                "type": "rest",
                "uri": running.uri,
                "warehouse": rest.PREFIX,
                "token": running.token,
                # Local files: there is nothing to vend, and asking would only cost a round trip.
                _DELEGATION: "",
                "icelens.sandbox": name,
                "icelens.sandbox.served": "v3",
            },
            profile=None,
            origin=f"sandbox {name!r}",
        )
    return Target(
        properties={
            "type": "sql",
            "uri": f"sqlite:///{home / 'catalog.db'}",
            "warehouse": _file_uri(home / "warehouse"),
            "icelens.sandbox": name,
        },
        profile=None,
        origin=f"sandbox {name!r}",
    )


def rest_sandbox_target(name: str, base: Target) -> Target:
    """A sandbox that lives in a namespace of a catalog somebody is already running.

    The offline sandbox cannot teach the v3 material - PyIceberg 0.12 will not serialise a v3
    metadata document (`NotImplementedError: Writing V3 is not yet supported`), and DuckDB
    writes Iceberg to a bare directory only at v2, silently ignoring a format version asked
    for by `COPY`. Through an attached REST catalog DuckDB writes v3 properly, which is the
    one route to deletion vectors and row lineage that exists today.

    The price is that the catalog is real, so the sandbox confines itself to one namespace
    named after it and refuses to write anywhere else. See `sandbox_table`.
    """
    _sandbox_name(name)
    return Target(
        properties={**base.properties, "icelens.sandbox": name, "icelens.sandbox.namespace": f"sandbox_{name}"},
        profile=base.profile,
        origin=f"sandbox {name!r} on {base.origin}",
    )


def list_sandboxes(root: Path | str | None = None) -> list[Sandbox]:
    """The sandboxes this tool has made before: name, how many tables, which format version.

    Read straight out of each `catalog.db`, read-only, rather than by opening a catalog on
    every one: this runs before anything is on screen, and must not start a v3 sandbox's
    server just to count its tables. A count that cannot be read is None, not a reason to
    leave the sandbox out.
    """
    import sqlite3

    base = Path(root).expanduser() if root else _sandbox_home("default").parent
    found: list[Sandbox] = []
    if not base.is_dir():
        return found
    for home in sorted(base.iterdir()):
        database = home / "catalog.db"
        if not database.is_file():
            continue
        try:
            with sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True) as con:
                count: int | None = int(con.execute("SELECT count(*) FROM iceberg_tables").fetchone()[0])
        except sqlite3.Error:
            count = None
        found.append(Sandbox(home.name, count, sandbox_version(home)))
    return found


def reset_sandbox(name: str = "default", root: Path | str | None = None) -> None:
    """Empty one sandbox, and refuse to empty anything that is not one.

    A reset deletes a directory tree, so it checks the shape of what it is about to delete
    first: a sandbox is a directory holding `catalog.db` and `warehouse`, and anything else
    under that path is somebody's, not ours.
    """
    import shutil

    home = _sandbox_home(name, root)
    if not home.exists():
        return
    unexpected = {child.name for child in home.iterdir()} - {"catalog.db", "warehouse", SANDBOX_MARK}
    if unexpected:
        raise IcelensError(f"{home} does not look like a sandbox (it holds {', '.join(sorted(unexpected))}); not touching it")
    # A reset empties the sandbox; it does not change what kind it is. And a v3 one's server
    # holds the catalog open, so it is stopped before the catalog goes from under it.
    mark = (home / SANDBOX_MARK).read_text() if (home / SANDBOX_MARK).is_file() else None
    from icelens import rest

    rest.stop(home)
    shutil.rmtree(home)
    if mark is not None:
        home.mkdir(parents=True)
        (home / SANDBOX_MARK).write_text(mark)


def qualified(ident: str, alias: str = ALIAS) -> str:
    """`crm.accounts` -> `icelens."crm"."accounts"`; `a.b.t` -> `icelens."a.b"."t"`.

    A nested namespace is one schema name to DuckDB, dots and all. That is how an attach with
    `SUPPORT_NESTED_NAMESPACES` lists it and how the scanning route registers it, and DuckDB
    cannot parse the four-part name it would otherwise be (`NameListToString NOT IMPLEMENTED`,
    measured against Polaris).
    """
    *namespace, table = ident.split(".")
    if not namespace:
        raise IcelensError(f"table identifier {ident!r} must be <namespace>.<table>")
    return ".".join([alias, quote(".".join(namespace)), quote(table)])


def is_nested(ident: str) -> bool:
    """Whether a table identifier sits in a namespace inside another namespace."""
    return ident.count(".") >= 2


#: a `.env` larger than this is not a `.env`; refuse it rather than read it into memory
MAX_DOTENV_BYTES = 256 * 1024


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a `.env` file without importing anything. Missing, odd or unsafe file: empty dict.

    Four refusals, because this file is found by walking *up* from the working directory and
    so is not always one the user wrote: anything that is not a regular file (a fifo or a
    device would block or never end), anything over `MAX_DOTENV_BYTES`, anything owned by
    another user, and anything writable by other people - its contents become this process's
    catalog address and credentials. `_read_dotenv` also says which.
    """
    return _read_dotenv(path)[0]


def _read_dotenv(path: Path) -> tuple[dict[str, str], str]:
    """The values, and why the file was refused when it was: empty for one that is not there.

    Not being a regular file is not reported: a `.env/` directory is somebody's virtualenv.
    """
    values: dict[str, str] = {}
    try:
        stat = path.stat()
        if not path.is_file():
            return values, ""
        if stat.st_size > MAX_DOTENV_BYTES:
            return values, f"it is over {MAX_DOTENV_BYTES // 1024} KiB, which no .env is"
        if hasattr(os, "getuid") and stat.st_uid != os.getuid():
            return values, "it belongs to another user"
        if os.name == "posix" and stat.st_mode & 0o022:
            return values, "other users can write to it (chmod go-w fixes that)"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values, ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = re.split(r"\s+#", value, maxsplit=1)[0]
        values[key.strip()] = value.strip().strip("'\"")
    return values, ""


@dataclass(frozen=True)
class Target:
    """Where to connect, how it was worked out, and what to call it on screen."""

    properties: dict[str, str] = field(default_factory=dict)
    profile: str | None = None
    origin: str = "arguments"
    #: a `.env` that was found and not read, and why: said once, rather than ignored in silence
    ignored: str = ""

    @property
    def uri(self) -> str:
        return self.properties.get("uri", "")

    @property
    def warehouse(self) -> str:
        return self.properties.get("warehouse", "")

    @property
    def unconfigured(self) -> bool:
        """Nothing was found to say which catalog this is: no address, no type, no warehouse.

        A Glue catalog needs no URI and a REST one may need no warehouse, so any one of the
        three counts as configured. Defaults icelens adds itself never do.
        """
        return not any(self.properties.get(key) for key in ("uri", "type", "warehouse"))

    @property
    def sandbox(self) -> str:
        """The sandbox's name, or empty for every real catalog.

        Carried as a property rather than inferred from the URI, because "is this scratch"
        decides whether writing is allowed, and a decision that important must be something
        the tool set on purpose - not something a local `sqlite://` path can imply.
        """
        return self.properties.get("icelens.sandbox", "")

    @property
    def sandbox_namespace(self) -> str:
        """Set only for a sandbox on a catalog somebody else runs; empty for an offline one.

        Its presence is what tells the lake it is writing into a real catalog, and so what
        turns on both the namespace confinement and the guard over the SQL pane.
        """
        return self.properties.get("icelens.sandbox.namespace", "")

    @property
    def catalog_type(self) -> str:
        """Which dialect this is: rest, glue, sql, hive, dynamodb, in-memory, or unknown.

        PyIceberg works this out internally and keeps it to itself. icelens needs it in the
        open, because the catalog's type decides how - and whether - DuckDB can reach the
        rows, and that decision has to be explainable in the status bar rather than showing
        up as a stack trace.
        """
        declared = self.properties.get("type", "").strip().lower()
        if declared:
            return declared
        if self.properties.get("py-catalog-impl") == "icelens.memory.MemoryCatalog":
            # The in-browser demo's catalog (icelens.memory): PyIceberg forbids naming a type
            # beside an implementation, so this is the one dialect known by its class.
            return "in-memory"
        uri = self.uri
        scheme = uri.split("://", 1)[0].lower() if "://" in uri else ""
        if scheme in ("http", "https"):
            return "rest"
        if scheme == "thrift":
            return "hive"
        if scheme.startswith(("sqlite", "postgres", "mysql", "mariadb", "oracle", "mssql")):
            return "sql"
        if ":s3tables:" in self.warehouse or self.warehouse.startswith("arn:aws"):
            return "glue"
        return "rest" if uri else "unknown"

    @property
    def label(self) -> str:
        """The name to put in a title bar - never carrying a credential.

        A URI with `user:pass@host` in it is common enough in a `.env`, and the label is the
        one string this tool puts somewhere persistent: a terminal title, a screenshot, a
        pasted status line.
        """
        if self.sandbox and not self.sandbox_namespace:
            # Not `file:///…/warehouse @ sqlite:////…/catalog.db`: two paths into a directory
            # this tool made say nothing that the name it was given does not say better.
            return f"sandbox {self.sandbox!r}"
        uri = redact_uri(self.uri)
        if self.warehouse and uri:
            return f"{self.warehouse} @ {uri}"
        return self.warehouse or uri or self.profile or "catalog"

    @property
    def title(self) -> str:
        """The short name the tree and the title bar use: the warehouse, or the label when the
        warehouse is a path into a directory this tool made."""
        if self.sandbox and not self.sandbox_namespace:
            return self.label
        return self.warehouse or self.label

    def describe(self) -> str:
        bits = [self.label, f"via {self.origin}"]
        if self.profile:
            bits.insert(1, f"profile {self.profile!r}")
        return "  ".join(bits)

    def with_warehouse(self, warehouse: str, origin: str | None = None) -> Target:
        properties = dict(self.properties)
        properties["warehouse"] = warehouse
        return Target(properties, self.profile, origin or self.origin)

    def redacted(self) -> dict[str, str]:
        """The properties with anything secret-looking blanked, safe to display.

        Two passes, because credentials arrive in two shapes: named by their key (`token`,
        `s3.secret-access-key`), and buried in the value of a key that is not secret at all
        (`uri: https://user:pass@host`). The key list is `_SECRET_KEYS`, the same one that
        decides what `scrub` will strip out of an error message.
        """
        return {
            key: ("********" if any(s in key.lower() for s in _SECRET_KEYS) else redact_uri(value))
            for key, value in self.properties.items()
        }


def redact_uri(uri: str) -> str:
    """`https://user:pass@host/x` -> `https://host/x`. Anything else, unchanged."""
    userinfo = _userinfo(uri)
    return uri.replace(f"{userinfo}@", "", 1) if userinfo else uri


def without_own_keys(props: dict[str, str]) -> dict[str, str]:
    """`props` less every `icelens.` key, which only `sandbox_target` and its REST twin may set.

    Those keys decide whether writing is allowed: `icelens.sandbox` turns read-only off, and
    `icelens.sandbox.served` lets a write land in any namespace. A `-p`, a profile or a `.env`
    that could set one could make somebody's real catalog writable by calling it scratch.
    """
    kept = {}
    for key, value in props.items():
        if key.lower().startswith("icelens."):
            _log.info("ignoring %s: icelens sets it on catalogs it made, and nothing else can", key)
            continue
        kept[key] = value
    return kept


def list_profiles() -> dict[str, dict[str, str]]:
    """The named catalogs in `~/.pyiceberg.yaml`, read exactly the way PyIceberg reads them.

    This file is the multi-catalog story Iceberg already has and no tool surfaces: one
    place, many catalogs, different types. Reading it through PyIceberg's own `Config`
    means `PYICEBERG_HOME` and the `PYICEBERG_CATALOG__*` environment overrides keep
    working, and costs no new dependency. `icelens.` keys are dropped: see `without_own_keys`.
    """
    try:
        from pyiceberg.utils.config import Config

        catalogs = Config().config.get("catalog") or {}
    except Exception:  # noqa: BLE001 - a broken config file must not stop the tool starting
        return {}
    if not isinstance(catalogs, dict):
        return {}
    return {
        str(name): without_own_keys({str(k): str(v) for k, v in props.items() if not isinstance(v, (dict, list))})
        for name, props in catalogs.items()
        if isinstance(props, dict)
    }


def profile_target(name: str) -> Target:
    """A Target for one named profile, for switching catalogs without restarting."""
    props = dict(list_profiles().get(name, {}))
    if not props:
        raise IcelensError(f"no catalog named {name!r} in ~/.pyiceberg.yaml")
    target = _rest_defaults(Target(properties=props, profile=name, origin="~/.pyiceberg.yaml"))
    remember_secrets(target.properties)
    return target


#: Google's Iceberg REST endpoints. Matched on the *host*, never as a substring of the whole
#: URI: `https://elsewhere.example/?x=googleapis.com` is not Google, and the difference decides
#: whether this tool hands a catalog token to an unrelated object store.
_GOOGLE_HOSTS = ("googleapis.com",)


def _is_google_catalog(uri: str) -> bool:
    """Is this URI a Google REST catalog endpoint (BigLake / Lakehouse runtime)?"""
    if not uri.startswith(("http://", "https://")):
        return False
    host = (urllib.parse.urlsplit(uri).hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in _GOOGLE_HOSTS)


#: PyIceberg's scope when a REST catalog is given client credentials and no `scope`. DuckDB's own
#: default is `PRINCIPAL_ROLE:ALL`; passing this one explicitly is what keeps the two halves
#: logged in as the same thing.
PYICEBERG_DEFAULT_SCOPE = "catalog"

_DELEGATION = "header.X-Iceberg-Access-Delegation"


def _header_key(props: dict[str, str], key: str) -> str | None:
    """The property that sets this header, however its name is cased; None when none does.

    PyIceberg hands `header.*` to a case-insensitive header map, so `header.x-iceberg-access-delegation`
    and the canonical spelling are one header - and when both are set, whichever comes last in
    the dict wins. A lookup by the exact key would miss the one the user wrote.
    """
    if key in props:
        return key
    wanted = key.lower()
    return next((k for k in props if k.startswith("header.") and k.lower() == wanted), None)


def _bearer(props: dict[str, str]) -> str:
    """The token in a `header.Authorization: Bearer …`, or empty."""
    key = _header_key(props, "header.Authorization")
    value = props.get(key, "") if key else ""
    return value[len("Bearer "):].strip() if value[: len("Bearer ")].lower() == "bearer " else ""


def rest_login(props: dict[str, str]) -> list[str]:
    """The ATTACH options that log DuckDB in to a REST catalog the way PyIceberg logs in.

    The two clients have to agree, or the metadata panes work and the rows do not, which is
    exactly what happened against Polaris: PyIceberg did the OAuth exchange with
    `credential=id:secret`, DuckDB was told `AUTHORIZATION_TYPE 'none'` and got a 401.

    PyIceberg's order is followed, as measured on 0.12 rather than as documented: client
    credentials win - the exchange runs whenever `credential` is set, and replaces any `token`
    - and a token is used only without them. Handing DuckDB the token when both were set gave
    it a credential PyIceberg had not used, often a stale one, and a 401 to fall back from.

    A credential with no colon is a bare secret, which is how Snowflake's Horizon catalog takes
    a PAT - PyIceberg sends it with no client id, so DuckDB gets an empty one. The token URL and
    the scope are always spelled out, because the two clients default them differently.
    """
    credential = props.get("credential", "")
    if not credential:
        token = props.get("token") or _bearer(props)
        return [f"TOKEN {literal(token)}"] if token else ["AUTHORIZATION_TYPE 'none'"]
    client_id, colon, secret = credential.partition(":")
    if not colon:
        client_id, secret = "", credential
    server = props.get("oauth2-server-uri") or f"{props.get('uri', '').rstrip('/')}/v1/oauth/tokens"
    return [
        f"CLIENT_ID {literal(client_id)}",
        f"CLIENT_SECRET {literal(secret)}",
        f"OAUTH2_SERVER_URI {literal(server)}",
        f"OAUTH2_SCOPE {literal(props.get('scope') or PYICEBERG_DEFAULT_SCOPE)}",
    ]


def asks_for_vended_credentials(props: dict[str, str]) -> bool:
    """Whether this connection asks the catalog to hand out storage credentials per table.

    PyIceberg asks by default, so only an explicit header that says something else - empty,
    usually - turns it off. DuckDB knows two modes, so anything but vending is `none` there.
    """
    key = _header_key(props, _DELEGATION)
    return "vended-credentials" in (props[key] if key else "vended-credentials")


def without_vended_credentials(target: Target) -> Target:
    """The same connection, no longer asking the catalog for storage credentials."""
    props = {k: v for k, v in target.properties.items() if k.lower() != _DELEGATION.lower()}
    return Target({**props, _DELEGATION: ""}, target.profile, target.origin)


def refused_to_vend(exc: BaseException) -> bool:
    """A catalog that was asked for storage credentials and has none to give.

    Polaris over an S3 store with no STS (`stsUnavailable`) answers every table load this way
    when asked - and PyIceberg always asks - so without recognising it no table opens at all.
    """
    text = str(exc).lower()
    return "credential vending was requested" in text or "no credentials are available" in text


def _rest_defaults(target: Target) -> Target:
    """The conveniences a REST catalog almost always wants, applied in one place."""
    props = dict(target.properties)
    if props.get("uri", "").startswith(("http://", "https://")):
        # Ask a vending catalog (Lakekeeper, Polaris, Unity) for per-table credentials.
        # PyIceberg asks anyway; saying so here keeps the DuckDB half in step with it. Not
        # always harmless: Polaris with no STS refuses every table that asks, which
        # `Lake.table` recognises and answers by asking again without. A header the user
        # set in any case is theirs: adding the canonical spelling beside it overrode it.
        if _header_key(props, _DELEGATION) is None:
            props[_DELEGATION] = "vended-credentials"
        props.setdefault("type", "rest")
        if not props.get("token") and (bearer := _bearer(props)):
            # PyIceberg 0.12 replaces a configured Authorization header with its own, which
            # without a token says `Bearer None`; DuckDB was handed the header's token. As a
            # `token` both halves send the same one.
            props["token"] = bearer
        if props.get("credential"):
            # PyIceberg finds the token URL by itself today, warns that it will stop, and
            # DuckDB has to be told it anyway. One explicit value serves both halves.
            props.setdefault("oauth2-server-uri", f"{props['uri'].rstrip('/')}/v1/oauth/tokens")
        if props.get("s3.endpoint"):
            props.setdefault("s3.path-style-access", "true")
    return Target(props, target.profile, target.origin, target.ignored)


def _dotenv_candidates(env_file: Path | str | None) -> list[Path]:
    """The `.env` files worth reading, nearest first.

    Walking up matters: a project keeps its `.env` at the repository root, and people run
    tools from wherever they happen to be standing inside it.

    It stops at the first directory that is not this user's, and at the home directory when
    it started under it. Beyond either are shared roots - `/tmp`, `/`, a mounted volume, a
    colleague's checkout - where a `.env` is nobody's in particular, and adopting one silently
    points the tool at a catalog the user never chose. From `/tmp/x` the old walk reached
    `/tmp/.env` and `/.env`, which anyone on the machine can create.
    """
    if env_file is not None:
        return [Path(env_file).expanduser()]
    here = Path.cwd().resolve()
    home = Path.home().resolve()
    me = os.getuid() if hasattr(os, "getuid") else None
    walked: list[Path] = []
    for parent in (here, *here.parents):
        if me is not None:
            try:
                if parent.stat().st_uid != me:
                    break
            except OSError:
                break
        walked.append(parent / ".env")
        if parent == home:
            break
    return walked


def discover(
    uri: str | None = None,
    warehouse: str | None = None,
    profile: str | None = None,
    properties: dict[str, str] | None = None,
    env_file: Path | str | None = None,
    search_dotenv: bool = True,
) -> Target:
    """Work out which catalog to open, preferring what the user was most explicit about.

    Highest first, each overriding the ones below it key by key:

    1. the command line: `uri`, `warehouse`, and `properties` (`-p`);
    2. `ICELENS_*` environment variables;
    3. a `.env`: `env_file`, or else the nearest one walking up from the working directory.
       Not searched for when `profile` names a catalog: a catalog asked for by name is more
       explicit than a file that happens to sit nearby, and `--catalog lab` used to open the
       `.env`'s catalog instead;
    4. the profile `profile` names in `~/.pyiceberg.yaml` - or, when nothing above says which
       catalog this is (no `uri` and no `type`), the `default` one. Not only when nothing at
       all was found: a `.env` holding just `AWS_REGION`, or `-p token=…`, hid the default
       profile and left no catalog to open.

    The `.env` step is what makes the tool start with no arguments inside a project that
    already has a lakehouse configured - it reads that project's own variable names rather
    than demanding its own. A `.env` that is there and refused is named in `Target.ignored`.
    No source here may set an `icelens.` key: see `without_own_keys`.
    """
    props: dict[str, str] = {}
    origins: list[str] = []
    ignored = ""

    if search_dotenv and (env_file is not None or not profile):
        for candidate in _dotenv_candidates(env_file):
            values, refusal = _read_dotenv(candidate)
            if refusal and env_file is not None:
                raise IcelensError(f"will not read {candidate}: {refusal}")
            if refusal:
                ignored = ignored or f"did not read {candidate}: {refusal}"
                continue
            found = _from_dotenv(values)
            if found:
                props.update(found)
                origins.append(str(candidate))
                break

    env_props = _from_dotenv(dict(os.environ), prefix="ICELENS_")
    if env_props:
        props.update(env_props)
        origins.append("ICELENS_* environment")

    if properties:
        props.update(properties)
    if uri:
        props["uri"] = uri.rstrip("/")
    if warehouse:
        props["warehouse"] = warehouse
    if uri or warehouse or properties:
        origins.append("command line")

    if not profile and not (props.get("uri") or props.get("type")):
        profile = "default"

    if profile:
        # Merge the profile in *underneath* anything explicit, instead of leaving the file to
        # `load_catalog`: the DuckDB half has to know this catalog's type and address to work
        # out whether it can attach it, scan it, or neither, and it cannot ask PyIceberg that.
        merged = dict(list_profiles().get(profile, {}))
        if merged:
            origins.append(f"profile {profile!r} in ~/.pyiceberg.yaml")
        merged.update(props)
        props = merged

    target = _rest_defaults(Target(properties=without_own_keys(props), profile=profile, origin=""))
    origin = " + ".join(reversed(origins)) or "pyiceberg config"
    remember_secrets(target.properties)
    return Target(target.properties, profile, origin, ignored)


def _from_dotenv(values: dict[str, str], prefix: str = "") -> dict[str, str]:
    """Pick the catalog settings out of a pile of environment variables.

    A `.env` is matched on the suffix alone, so a project's own names work unedited. With a
    prefix the name must be the prefix and a suffix and nothing between: the process
    environment is shared, and suffix matching there adopted `ICELENS_TEST_CATALOG_URI` - the
    live tests' setting - as the catalog every other test in the run then connected to.
    """
    found: dict[str, str] = {}
    for key, value in values.items():
        if not value or (prefix and not key.startswith(prefix)):
            continue
        for suffix, prop in _DOTENV_SUFFIXES:
            matches = key.upper() == prefix + suffix if prefix else key.upper().endswith(suffix)
            if matches and prop not in found:
                found[prop] = value.rstrip("/") if prop == "uri" else value
                break
    return found


#: a config document larger than this is not one; do not read the rest of it
MAX_PROBE_BYTES = 1024 * 1024


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects outright while a bearer token is on the request.

    `urllib` copies every header onto the redirected request, `Authorization` included and
    regardless of host - so a catalog that answers `302 Location: http://attacker/` is handed
    the token. Nothing this function asks for is worth following a redirect to get.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_no_redirects = urllib.request.build_opener(_NoRedirects)


def probe_warehouses(
    uri: str,
    token: str | None = None,
    timeout: float = 5.0,
    properties: dict[str, str] | None = None,
) -> list[str]:
    """Ask a REST catalog which warehouses it has. Best effort; never raises.

    There is no line in the Iceberg REST spec for this, so it is a matter of knowing where
    each implementation keeps its administrative API. Both Lakekeeper's and Polaris's sit
    *beside* the catalog API rather than under it - `…/catalog` and `…/management` - which is
    why the `/catalog` suffix is trimmed for those two and not for `/v1/config`, which really
    is under it. Snowflake Open Catalog is Polaris, at the same relative path.

    `properties` are the connection's, so a catalog that wants OAuth client credentials gets
    the same login PyIceberg would have done. Listing usually needs an administrator: a
    principal that may not list gets a 403, and the answer is then "ask for the name", which
    is what an empty list means to every caller.
    """
    if not uri.lower().startswith(("http://", "https://")):
        # urllib happily opens file:// and ftp://; this function exists to talk to a REST
        # catalog, and any other scheme here is a configuration mistake at best.
        return []
    props = dict(properties or {})
    if not token and props.get("credential"):
        # Client credentials first, as PyIceberg logs in: see `rest_login`. A configured token
        # is still worth a try when the exchange fails, which costs nothing here.
        token = _client_credentials_token(uri, props, timeout)
    token = token or props.get("token") or _bearer(props)
    root = uri.rstrip("/")
    base = root[: -len("/catalog")] if root.endswith("/catalog") else root
    names: list[str] = []
    for url, extract in (
        (f"{base}/management/v1/warehouse", lambda body: [w.get("name") for w in body.get("warehouses", [])]),
        (f"{base}/management/v1/catalogs", lambda body: [c.get("name") for c in body.get("catalogs", [])]),
        (f"{root}/v1/config", lambda body: [body.get("defaults", {}).get("warehouse")]),
    ):
        body = _get_json(url, token, timeout)
        try:
            found = [name for name in extract(body) if isinstance(name, str) and name]
        except (AttributeError, TypeError):
            continue  # an answer of some other shape: not this implementation's API
        names.extend(found)
        if names:
            break
    return sorted(dict.fromkeys(names))


def _get_json(url: str, token: str | None, timeout: float) -> Any:
    """GET a small JSON document with no redirects followed. `{}` for anything that fails."""
    import json

    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with _no_redirects.open(request, timeout=timeout) as response:
            _log.info("probe GET %s -> %s", url, response.status)
            return json.loads(response.read(MAX_PROBE_BYTES) or b"{}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        # HTTPException is not an OSError: something on the port that is not HTTP at all - an
        # SSH banner, say - answers with `BadStatusLine`, which was a traceback.
        _log.info("probe GET %s -> %s", url, _one_line(exc))
        return {}


def _client_credentials_token(uri: str, props: dict[str, str], timeout: float) -> str | None:
    """Do the OAuth client-credentials exchange PyIceberg would do, for the probe alone.

    The secret goes to the token URL PyIceberg would send it to and nowhere else: the
    configured `oauth2-server-uri`, or the catalog's own `/v1/oauth/tokens`. No redirects are
    followed, for the reason `_NoRedirects` gives. None on any failure - a probe that cannot
    log in lists nothing, and the caller asks for a name instead.
    """
    import json

    client_id, colon, secret = props.get("credential", "").partition(":")
    if not colon:
        client_id, secret = "", client_id
    server = props.get("oauth2-server-uri") or f"{uri.rstrip('/')}/v1/oauth/tokens"
    if not server.lower().startswith(("http://", "https://")):
        return None
    form = {"grant_type": "client_credentials", "client_secret": secret, "scope": props.get("scope") or PYICEBERG_DEFAULT_SCOPE}
    if client_id:
        form["client_id"] = client_id
    request = urllib.request.Request(
        server,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _no_redirects.open(request, timeout=timeout) as response:
            _log.info("probe POST %s -> %s", server, response.status)
            body = json.loads(response.read(MAX_PROBE_BYTES) or b"{}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        _log.info("probe POST %s -> %s", server, _one_line(exc))
        return None
    token = body.get("access_token") if isinstance(body, dict) else None
    return token if isinstance(token, str) and token else None


#: the largest number of rows any pane will ask an engine for, however it was asked
MAX_ROWS = 1_000_000


def row_limit(limit: Any) -> int:
    """A row limit that is always a non-negative, bounded integer.

    `LIMIT -1` is "no limit" in DuckDB, so a negative number arriving from a config file or a
    stray `--limit` is not a smaller read, it is the whole table streamed into memory.

    Zero is left alone, because `LIMIT 0` is a real question - "what are the columns?" - and
    answering it with one row was the kind of quiet disobedience that makes a tool untrustworthy
    to script against. Callers that show what they read say what this returned, not what they
    asked for.
    """
    try:
        value = int(limit)
    except OverflowError:
        # An infinity: not a number `int` will take, but still a direction, so it is clamped
        # like any other number rather than treated as nonsense.
        return MAX_ROWS if limit > 0 else 0
    except (TypeError, ValueError):
        return 200
    return max(0, min(MAX_ROWS, value))


#: the name a PyIceberg scan is registered under while the `arrow` route copies it in
_SCANNED = "icelens_pyiceberg_scan"


def _no_such(exc: BaseException | None) -> bool:
    """Whether a catalog said a table or namespace does not exist, by the exception's type.

    Not a file that is missing: a table whose metadata file has gone exists, and is broken,
    and saying it does not exist would hide why.
    """
    if exc is None or isinstance(exc, OSError):
        return False
    name = type(exc).__name__.lower()
    return "nosuch" in name or "notfound" in name


def _references(con: Any, sql: str) -> list[tuple[str, str, str]]:
    """Every table one statement reads, as (catalog, schema, table) with the quoting undone.

    `json_serialize_sql` first, because it only parses. `get_table_names` also binds, and
    binding a query whose tables do not exist yet fails on `JOIN ... USING (id)` - measured on
    DuckDB 1.5.1 and 1.5.5 - which is every join written the short way. It stays as the
    fallback for a build without the json extension.
    """
    try:
        parsed = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        if not parsed.get("error"):
            found: list[tuple[str, str, str]] = []
            _base_tables(parsed, found)
            return found
    except Exception:  # noqa: BLE001 - no json extension, or a statement it cannot serialize
        pass
    try:
        names = con.get_table_names(sql, qualified=True)
    except Exception:  # noqa: BLE001 - DuckDB will refuse the SQL itself, in its own words
        return []
    out = []
    for name in names:
        parts = _name_parts(name)
        if len(parts) == 3:
            out.append((parts[0], parts[1], parts[2]))
        elif len(parts) == 2:
            out.append(("", parts[0], parts[1]))
    return out


def _base_tables(node: Any, found: list[tuple[str, str, str]]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "BASE_TABLE":
            found.append((node.get("catalog_name") or "", node.get("schema_name") or "", node.get("table_name") or ""))
        for value in node.values():
            _base_tables(value, found)
    elif isinstance(node, list):
        for value in node:
            _base_tables(value, found)


def _name_parts(name: str) -> list[str]:
    """`icelens."a.b"."we""ird" AS w` -> `['icelens', 'a.b', 'we"ird']`: how `get_table_names` spells one."""
    parts, current, quoted, i = [], "", False, 0
    while i < len(name):
        char = name[i]
        if quoted:
            if char == '"' and name[i + 1 : i + 2] == '"':
                current += '"'
                i += 1
            elif char == '"':
                quoted = False
            else:
                current += char
        elif char == '"':
            quoted = True
        elif char == ".":
            parts.append(current)
            current = ""
        elif char.isspace():
            break  # the alias that follows is not part of the name
        else:
            current += char
        i += 1
    parts.append(current)
    return parts


class Lake:
    """One open catalog: PyIceberg for metadata, DuckDB for rows.

    Everything here is blocking - REST calls, manifest reads, scans - so the app calls it
    from worker threads. One lock serialises access because a DuckDB connection is not
    safe to use from two threads at once, and a half-read manifest is not worth the
    concurrency.
    """

    def __init__(self, target: Target, read_only: bool = False):
        """`read_only` attaches the catalog with DuckDB's own `READ_ONLY`, and refuses any
        statement that is not a read before it gets that far.

        It is a property of the whole open catalog rather than of one call, because the
        connection is shared: a guard that only some callers pass is a guard with a way round
        it, which is exactly what `--where` turned out to be.
        """
        self.target = target
        # A sandbox attaches writable - that is what makes it a sandbox. What `read_only`
        # still buys, when the sandbox is somebody's real catalog, is the SQL pane: see
        # `sql_is_guarded`. An offline sandbox has nothing to protect and keeps neither.
        self.read_only = read_only and not target.sandbox
        self._asked_read_only = read_only
        remember_secrets(target.properties)
        self._lock = threading.RLock()
        self._catalog: Any = None
        self._con: Any = None
        self._attached: bool | None = None
        self._attach_error: str | None = None
        self._mode = "pyiceberg"
        self._views_ready = False
        self._namespace_ready = False
        self._tables: dict[str, Any] = {}
        #: whether DuckDB is attached with SUPPORT_NESTED_NAMESPACES (see `_duck`)
        self._nested = False
        #: the namespace walk, kept: it decides how DuckDB attaches, and the tree asks for it too
        self._namespace_cache: list[str] | None = None
        #: tables the scanning route could not give a SQL name, and why
        self._unreadable: dict[str, str] = {}
        #: an ATTACH was tried and the catalog said no, as opposed to one never being possible
        self._attach_refused = False
        #: set when the catalog refused to vend credentials and the lake stopped asking
        self.delegation_note = ""
        #: DuckDB is attached the old way and should be reattached at its next use
        self._reattach = False
        #: the `arrow` route's own DuckDB: no extensions, nothing attached (see `_arrow`)
        self._arrow_con: Any = None
        #: what the `arrow` route has copied in: table -> the snapshot id it was scanned at
        self._arrow_scans: dict[str, int | None] = {}

    @property
    def sandbox_namespace(self) -> str:
        """The one namespace an attached sandbox may write to. Empty for an offline one."""
        return self.target.sandbox_namespace

    @property
    def is_served_sandbox(self) -> bool:
        """A v3 sandbox: this tool's own catalog, served over REST so DuckDB can write v3."""
        return self.target.properties.get("icelens.sandbox.served") == "v3"

    @property
    def writes_through_duckdb(self) -> bool:
        """Whether DuckDB commits this sandbox's writes, rather than PyIceberg's add_files."""
        return self.is_rest_sandbox or self.is_served_sandbox

    @property
    def is_rest_sandbox(self) -> bool:
        """A sandbox that lives in somebody's running catalog, so DuckDB can write it v3."""
        return bool(self.target.sandbox_namespace)

    @property
    def sql_is_guarded(self) -> bool:
        """Whether the SQL pane still refuses writes on a lake the Writer pane may write to.

        An attached sandbox has to attach writable, which would otherwise hand the SQL pane
        a loaded gun pointed at a real catalog - `DROP TABLE crm.accounts` is a read-only
        lake away. So the sanctioned write path is the Writer pane, which is confined to the
        sandbox namespace, and the SQL pane stays a reader unless `--allow-writes` says
        otherwise.
        """
        return self._asked_read_only and self.is_rest_sandbox

    @property
    def is_sandbox(self) -> bool:
        """A scratch catalog this tool made, and the only kind it will write to."""
        return bool(self.target.sandbox)

    # -- clients ---------------------------------------------------------------------
    @property
    def catalog(self) -> Any:
        if self._catalog is None:
            from pyiceberg.catalog import load_catalog

            name = self.target.profile or "icelens"
            _log.info("pyiceberg: opening %s as a %s catalog", self.target.label, self.target.catalog_type)
            try:
                self._catalog = load_catalog(name, **self.target.properties)
            except Exception as exc:  # noqa: BLE001 - every catalog type raises its own
                _log.info("pyiceberg: %s", _one_line(exc))
                if _needs_warehouse(exc) and not self.target.warehouse:
                    options = probe_warehouses(self.target.uri, properties=self.target.properties)
                    if len(options) == 1:
                        self.target = self.target.with_warehouse(options[0], origin="probed from the catalog")
                        self._catalog = None
                        return self.catalog
                    raise NeedsWarehouse(
                        f"catalog at {self.target.uri} needs a warehouse: pass --warehouse NAME", options
                    ) from exc
                raise classify(exc, f"cannot open catalog {name!r} ({self.target.catalog_type})") from exc
        return self._catalog

    @property
    def engine(self) -> str:
        """Which client will answer a row request: `duckdb` or `pyiceberg`."""
        return "pyiceberg" if self._duck() is None else "duckdb"

    @property
    def mode(self) -> str:
        """How DuckDB reaches the rows - the one thing that really varies by catalog type.

        - `attach`    DuckDB speaks this catalog itself: REST, Glue, S3 Tables.
        - `bridge`    PyIceberg resolves each table to its `metadata.json` and DuckDB scans
                      that file. This is what gives a Hive, SQL or in-memory catalog the
                      same rows, the same SQL and the same time travel as a REST one.
        - `pyiceberg` no DuckDB at all: no extension, or nothing it could open.
        """
        self._duck()
        return self._mode

    @property
    def attach_error(self) -> str | None:
        self._duck()
        return self._attach_error

    @property
    def sql_route(self) -> str:
        """How SQL is answered, which is not always how rows are read.

        - `attach`, `bridge`  DuckDB with its iceberg extension, as `mode` describes.
        - `arrow`             DuckDB without it: each table the SQL names is scanned by PyIceberg
                              and copied in. What Pyodide gets, where DuckDB runs but
                              extensions.duckdb.org publishes no iceberg extension for it.
        - `none`              no DuckDB module at all, so no SQL.
        """
        mode = self.mode
        if mode != "pyiceberg":
            return mode
        try:
            import duckdb  # noqa: F401
        except ImportError:
            return "none"
        return "arrow"

    @property
    def attach_refused(self) -> bool:
        """True when DuckDB tried to attach this catalog and was told no.

        Scanning metadata files is the expected route for a Hive or SQL catalog and a symptom
        for a REST one: the status bar names the second and not the first.
        """
        self._duck()
        return self._attach_refused

    def _duck(self, nested: bool = False) -> Any:
        """The DuckDB connection, attached or bridged. Worked out once.

        `nested` says the caller is about to name a table in a nested namespace, which an
        attached DuckDB can see only if it was attached to look for them.

        Attaching is tried first because a real catalog attachment is better than a pile of
        views: DuckDB then sees new tables and new snapshots without being told. When the
        catalog is one DuckDB has never heard of, that is not a reason to give up on DuckDB -
        it is a reason to hand it file paths instead, which is what `bridge` does.
        """
        with self._lock:
            if self._attached is not None and self._reattach:
                self._reattach = False
                if self._mode == "attach":
                    _log.info("route: reattaching DuckDB without asking for vended credentials")
                    self._drop_duck()
                    self._nested = self._nested or nested
                    return self._open_duck()
            if self._attached is not None:
                if nested and self._mode == "attach" and not self._nested:
                    # Attached flat, and now a nested table is wanted. Once per lake, so the
                    # cost is one reconnect and only on catalogs that actually nest.
                    _log.info("route: reattaching DuckDB to see nested namespaces")
                    self._drop_duck()
                    self._nested = True
                    return self._open_duck()
                return self._con if self._attached else None
            self._nested = self._nested or nested or self._known_to_nest()
            return self._open_duck()

    def _duck_for(self, ident: str) -> Any:
        """The connection for reading one table: asked for nesting only when it is nested."""
        return self._duck(nested=True) if is_nested(ident) else self._duck()

    def _known_to_nest(self) -> bool:
        """Whether a namespace walk already done found a nested namespace. Never does I/O.

        DuckDB lists nested namespaces only when attached with `SUPPORT_NESTED_NAMESPACES`,
        and that option makes it ask every namespace for children - extra requests a flat
        catalog does not need, against catalogs (Unity, Horizon) where nobody has watched what
        the question does. So it is turned on only where nesting has been seen.
        """
        return any("." in name for name in self._namespace_cache or ())

    def _drop_duck(self) -> None:
        """Forget the DuckDB connection, keeping the PyIceberg side. Called under the lock."""
        if self._con is not None:
            try:
                self._con.close()
            except Exception:  # noqa: BLE001 - an invalidated database may refuse to close; it is gone either way
                pass
            self._con = None
        self._attached = None
        self._mode = "pyiceberg"
        self._views_ready = False
        self._attach_error = None
        self._attach_refused = False
        self._reattach = False
        self._unreadable.clear()

    def _open_duck(self) -> Any:
        """Called under the lock, once per connection. Split out so `_duck` stays a cheap read."""
        self._attached = False
        # A connection opened now is opened on the current target, so a reattach asked for
        # before it existed - PyIceberg refusing vended credentials ahead of any row read - is
        # already done. Left set, it threw this connection away on the next call.
        self._reattach = False
        try:
            import duckdb

            con = trace.connection(duckdb.connect(":memory:"))
            con.execute("SET preserve_insertion_order = false")
            for extension in ("iceberg", "httpfs", "aws"):
                try:
                    con.execute(f"LOAD {extension}")
                except Exception:  # noqa: BLE001 - not installed yet; try the network once
                    con.execute(f"INSTALL {extension}")
                    con.execute(f"LOAD {extension}")
            self._create_secret(con)
        except Exception as exc:  # noqa: BLE001 - duckdb raises a dozen different types
            self._attach_error = f"{type(exc).__name__}: {_one_line(exc)}"
            _log.info("route: no DuckDB, so PyIceberg reads everything (%s)", self._attach_error)
            return None

        attach = self._attach_sql()
        if attach is not None:
            try:
                con.execute(attach)
                self._con, self._attached, self._mode = con, True, "attach"
                _log.info("route: DuckDB attached the catalog; it reads rows and runs SQL")
                return con
            except Exception as exc:  # noqa: BLE001
                # Kept apart from "this catalog type cannot be attached": a refusal is the
                # catalog saying no - usually to a login - and it is the one worth showing.
                self._attach_refused = True
                self._attach_error = scrub(
                    f"attach refused, {_refusal(exc)}; scanning metadata files instead"
                )
        else:
            self._attach_error = (
                f"DuckDB cannot attach a {self.target.catalog_type} catalog; scanning metadata files instead"
            )

        try:
            # An empty database under the same alias, so `icelens."ns"."table"` means the
            # same thing to the user whichever mode they are in.
            con.execute(f"ATTACH ':memory:' AS {ALIAS}")
        except Exception as exc:  # noqa: BLE001
            self._attach_error = f"{type(exc).__name__}: {_one_line(exc)}"
            return None
        self._con, self._attached, self._mode = con, True, "bridge"
        _log.info("route: %s", self._attach_error)
        return con

    def _attach_sql(self) -> str | None:
        """The ATTACH statement for this catalog, or None if DuckDB speaks no such dialect.

        Verified against duckdb 1.5.5, not assumed: `ENDPOINT_TYPE` accepts `glue` and
        `s3_tables` and refuses everything else by name, the Glue warehouse string is an
        account id, `catalog/catalog`, or `:` for the caller's own account, and S3 Tables
        wants the table bucket ARN.
        """
        props = self.target.properties
        warehouse = props.get("warehouse") or ""
        kind = self.target.catalog_type
        if ":s3tables:" in warehouse:
            return self._attach_stmt(warehouse, ["TYPE ICEBERG", "ENDPOINT_TYPE 'S3_TABLES'"])
        if kind == "glue":
            # A Glue "warehouse" is often an s3:// path, which is a location, not a catalog id.
            named = props.get("glue.id") or props.get("glue.catalog-id") or ""
            if not named and warehouse and not warehouse.startswith(("s3:", "s3a:", "file:", "/")):
                named = warehouse
            return self._attach_stmt(named or ":", ["TYPE ICEBERG", "ENDPOINT_TYPE 'GLUE'"])
        uri = props.get("uri", "")
        if kind == "rest" and uri.startswith(("http://", "https://")):
            options = ["TYPE ICEBERG", f"ENDPOINT {literal(uri)}", *rest_login(props)]
            if not asks_for_vended_credentials(props):
                options.append("ACCESS_DELEGATION_MODE 'none'")
            if self._nested:
                options.append("SUPPORT_NESTED_NAMESPACES true")
            return self._attach_stmt(warehouse, options)
        return None

    def _attach_stmt(self, name: str, options: list[str]) -> str:
        """The ATTACH, with `READ_ONLY` when this lake is one.

        DuckDB enforces this in the engine, which is the only enforcement worth having:
        measured, it refuses `CREATE`, `DELETE` and `EXPLAIN ANALYZE DELETE` alike on the
        attached database, while `SELECT`, `DESCRIBE` and `EXPLAIN SELECT` still work.
        """
        if self.read_only:
            options = [*options, "READ_ONLY"]
        return one_statement(f"ATTACH IF NOT EXISTS {literal(name)} AS {ALIAS} ({', '.join(options)})")

    def _create_secret(self, con: Any) -> None:
        """Hand DuckDB the object-store credentials the catalog was configured with.

        In bridge mode DuckDB opens the Parquet files itself, so it needs the same keys, the
        same endpoint and the same path style PyIceberg got - otherwise the rows come back as
        a 403 that reads like an Iceberg problem and is not one. One store per statement, and
        a store nobody configured gets none: DuckDB's own chain is better than a half-filled
        secret that shadows it.
        """
        for sql in (self._s3_secret_sql(), self._gcs_secret_sql()):
            if sql:
                con.execute(sql)

    def _s3_secret_sql(self) -> str | None:
        """The `CREATE SECRET` for an S3-compatible store, or None if none was configured."""
        return _s3_secret(self.target.properties, f"{ALIAS}_s3")

    def _gcs_secret_sql(self) -> str | None:
        """The same favour for a warehouse on Cloud Storage: BigLake, or any `gs://` catalog.

        Verified against duckdb 1.5.5 rather than assumed: the `gcs` secret type takes a
        `BEARER_TOKEN`, or an HMAC `KEY_ID`/`SECRET` pair, and refuses `TOKEN` by name with a
        binder error. A catalog that vends credentials returns an OAuth token, which is the
        bearer one; the HMAC pair only exists if somebody made one by hand.
        """
        props = self.target.properties
        token = props.get("gcs.oauth2.token", "")
        if not token and _is_google_catalog(props.get("uri", "")):
            # Google's REST endpoint authenticates with an ordinary Google OAuth access token,
            # and that same token is what Cloud Storage wants. Reusing it is what makes
            # `-p token=$(gcloud auth print-access-token)` reach the rows as well as the
            # metadata. Only for Google: on any other catalog the two are unrelated secrets.
            token = props.get("token") or props.get("header.Authorization", "").removeprefix("Bearer ")
        return _gcs_secret({**props, "gcs.oauth2.token": token} if token else props, f"{ALIAS}_gcs")

    def _scan_expr(self, ident: str, snapshot_id: int | None = None) -> str:
        """`iceberg_scan` over the metadata file PyIceberg resolved, optionally at a snapshot.

        This is the whole bridge. PyIceberg knows where a table's current `metadata.json`
        lives for *any* catalog it can open, and DuckDB's `iceberg_scan` reads a
        `metadata.json` wherever it lives - so DuckDB-speed rows and real time travel stop
        being a REST-catalog privilege. Verified against duckdb 1.5.5: `snapshot_from_id`
        selects the snapshot with its deletes applied.
        """
        table = self.table(ident)
        self._hand_over_credentials(ident, table)
        location = table.metadata_location
        at = f", snapshot_from_id => {int(snapshot_id)}" if snapshot_id is not None else ""
        return f"iceberg_scan({literal(location)}{at})"

    def _hand_over_credentials(self, ident: str, table: Any) -> None:
        """Give DuckDB the storage credentials the catalog vended for this one table.

        A vending catalog hands PyIceberg short-lived keys in each table's load response, and
        on the scanning route DuckDB opens the same files with none of them: measured against
        Polaris, a 403 from the store for a table PyIceberg had just read. The keys go into a
        secret scoped to the table's own directories (see `_table_scopes`), so DuckDB picks
        them for that table's files and for nothing else, and they are scrubbed like any
        configured credential. The secret is named after the table, so handing over another
        table's keys can never replace this one's.

        Nothing is made when the table carries no keys of its own - a catalog that does not
        vend - because the catalog-wide secret already says everything there is to say.
        """
        con = self._con
        raw = getattr(getattr(table, "io", None), "properties", None) or {}
        props = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}
        mine = self.target.properties
        if con is None or not any(props.get(k) and props.get(k) != mine.get(k) for k in _VENDED_KEYS):
            return
        remember_secrets(props)
        scopes = _table_scopes(table.location(), table.metadata_location)
        if scopes is None:
            _log.info(
                "route: not handing %s's vended keys to DuckDB: %s and %s are too broad a scope; "
                "the configured credentials stay", ident, table.location(), table.metadata_location,
            )
            return
        name = f"{ALIAS}_t_{hashlib.sha1(ident.encode()).hexdigest()[:16]}"
        for sql in (_s3_secret(props, name + "_s3", scopes), _gcs_secret(props, name + "_gcs", scopes)):
            if sql:
                con.execute(sql)

    def _register_views(self, con: Any, cap: int = 500) -> None:
        """Give every table a name in bridge mode, so the SQL pane reads the same either way.

        Done once, lazily, and only for SQL: the Data pane scans by path and needs none of
        this. A table that will not register is skipped rather than fatal - one unreadable
        table should not cost you the other four hundred.
        """
        if self._views_ready:
            return
        self._views_ready = True
        registered = 0
        for namespace in self.namespaces():
            try:
                idents = self.tables(namespace)
            except Exception:  # noqa: BLE001 - a namespace that will not list is not fatal
                continue
            # Nested namespaces become one schema name: DuckDB schemas do not nest.
            schema = quote(namespace)
            try:
                con.execute(one_statement(f"CREATE SCHEMA IF NOT EXISTS {ALIAS}.{schema}"))
            except Exception:  # noqa: BLE001
                continue
            for ident in idents:
                if registered >= cap:
                    self._attach_error = f"SQL sees the first {cap} tables of this catalog"
                    return
                name = ident.split(".")[-1]
                try:
                    con.execute(one_statement(
                        f"CREATE OR REPLACE VIEW {ALIAS}.{schema}.{quote(name)} AS "
                        f"SELECT * FROM {self._scan_expr(ident)}"
                    ))
                    registered += 1
                except Exception as exc:  # noqa: BLE001 - skip what cannot be read, keep the rest
                    # ...but remember why, or the SQL pane reports a table the tree is showing
                    # as one that "does not exist".
                    self._unreadable[ident] = _clip(str(exc), 100)
                    continue

    def close(self) -> None:
        with self._lock:
            self._drop_duck()
            self._drop_arrow()
            self._tables.clear()
            self._namespace_cache = None

    # -- structure -------------------------------------------------------------------
    def namespaces(self, depth: int = 4) -> list[str]:
        """Every namespace this catalog has, nested ones included.

        `list_namespaces()` returns one level. Catalogs that nest - REST catalogs generally,
        Nessie and Polaris in practice - keep the rest behind a parent argument, so a tool
        that calls it once shows a half-empty catalog and leaves the user blaming the server.
        Glue and Hive are flat and answer the first call only, which costs nothing.
        """
        with self._lock:
            # resolved once, and outside the walk: opening the catalog is what raises
            # NeedsWarehouse, and swallowing that here is how a catalog with many warehouses
            # and no default ends up drawn as an empty tree with nothing to click.
            catalog = self.catalog
            found: dict[str, None] = {}

            def walk(parent: tuple[str, ...], level: int) -> None:
                try:
                    children = catalog.list_namespaces(parent)
                except Exception as exc:  # noqa: BLE001 - a catalog that will not nest is not broken
                    if not parent:
                        # the root listing is different: if *that* fails the catalog told us
                        # something - no permission, no such warehouse - and an empty list
                        # would report it as an empty catalog.
                        raise classify(exc, "cannot list namespaces") from exc
                    return
                for child in children:
                    name = ".".join(child)
                    if not name or name in found:
                        continue
                    found[name] = None
                    if level < depth:
                        walk(tuple(child), level + 1)

            walk((), 1)
            # Remembered for `_known_to_nest`, never served from: the tree re-walks on purpose.
            self._namespace_cache = sorted(found)
            return list(self._namespace_cache)

    def tables(self, namespace: str) -> list[str]:
        with self._lock:
            catalog = self.catalog  # outside the try: NeedsWarehouse must reach the caller whole
            try:
                listed = catalog.list_tables(tuple(namespace.split(".")))
            except Exception as exc:  # noqa: BLE001 - `--list nope` was a NoSuchNamespaceError traceback
                raise classify(exc, f"cannot list the tables in {namespace}") from exc
            return sorted(".".join(ident) for ident in listed)

    def views(self, namespace: str) -> list[str]:
        """The views in a namespace, or none where the catalog keeps no views.

        Not every catalog can: PyIceberg's SQL catalog raises NotImplementedError, and a REST
        server without the view endpoints answers 404 or 400. Neither is worth a red pane - a
        catalog with no views and one that cannot hold them look the same in a tree.
        """
        with self._lock:
            catalog = self.catalog
            try:
                listed = catalog.list_views(tuple(namespace.split(".")))
            except Exception as exc:  # noqa: BLE001
                _log.info("views: none listed in %s: %s", namespace, scrub(_one_line(exc)))
                return []
            return sorted(".".join(ident) for ident in listed)

    def view(self, ident: str) -> Any:
        """A PyIceberg view: its metadata, which holds the SQL. Not cached: nothing reads it twice."""
        with self._lock:
            try:
                return self.catalog.load_view(ident)
            except Exception as exc:  # noqa: BLE001
                raise classify(exc, f"cannot load view {ident}") from exc

    def is_view(self, ident: str) -> bool:
        if "." not in ident:
            return False
        return ident in self.views(ident.rsplit(".", 1)[0])

    def table(self, ident: str, refresh: bool = False) -> Any:
        """A PyIceberg table, cached until something asks for it fresh."""
        from icelens import opaque

        opaque.install()  # a variant or geometry column must not stop the table opening
        with self._lock:
            if refresh or ident not in self._tables:
                try:
                    self._tables[ident] = self.catalog.load_table(ident)
                except Exception as exc:  # noqa: BLE001
                    if _unreadable_type(exc):
                        raise IcelensError(
                            f"cannot load {ident}: {scrub(_one_line(exc))} - icelens reads table metadata with "
                            f"PyIceberg {_pyiceberg_version()}, which cannot read this column type yet"
                        ) from exc
                    if not (refused_to_vend(exc) and asks_for_vended_credentials(self.target.properties)):
                        raise classify(exc, f"cannot load {ident}") from exc
                    self._stop_asking_for_credentials()
                    try:
                        self._tables[ident] = self.catalog.load_table(ident)
                    except Exception as again:  # noqa: BLE001
                        raise classify(again, f"cannot load {ident}") from again
            return self._tables[ident]

    def _stop_asking_for_credentials(self) -> None:
        """Reconnect both halves without asking for vended credentials. Called under the lock.

        Once, for the whole catalog: a catalog with no STS has none to give for any table, and
        the storage credentials the user configured are the ones to use from here on. The
        status bar says so, because "these rows were read with your keys, not the catalog's"
        is a fact about where the numbers came from.
        """
        self.target = without_vended_credentials(self.target)
        self._catalog = None
        self._tables.clear()
        # Not closed here: a read may be holding the connection right now. `_duck` reattaches
        # on its next call, and only an attached DuckDB needs to - one that scans files never
        # asked the catalog for anything.
        self._reattach = True
        self.delegation_note = "the catalog vends no credentials; using the configured ones"
        _log.info("route: %s", self.delegation_note)

    def format_version(self, ident: str) -> int:
        """The table's Iceberg format version, or 2 when it cannot be read.

        2 is the safe answer to guess: it is what almost every catalog out there holds, and
        the only thing this is used for is deciding whether to promise a v3-only column.
        """
        try:
            return int(self.table(ident).metadata.format_version)
        except Exception:  # noqa: BLE001 - a table that will not load is the caller's problem, not this check's
            return 2

    def forget(self, ident: str | None = None) -> None:
        with self._lock:
            if ident is None:
                self._tables.clear()
                self._namespace_cache = None
            else:
                self._tables.pop(ident, None)

    # -- rows ------------------------------------------------------------------------
    def rows(
        self,
        ident: str,
        limit: int = 200,
        snapshot_id: int | None = None,
        where: str | None = None,
        columns: list[str] | None = None,
        lineage: bool = False,
    ) -> tuple[pa.Table, str]:
        """`limit` rows of a table, optionally as of an old snapshot. Returns (rows, engine).

        The filter is raw SQL on purpose: this is an exploration tool, and anyone who can run
        `icelens` can already run DuckDB. It is spliced into a SELECT and nowhere else, and the
        assembled query is checked for being one statement like every other query icelens
        builds - `--where "false; DROP TABLE x"` parses as two, and is refused.

        `lineage` prepends the v3 row-lineage columns. They are absent from `SELECT *` because
        nothing stores them - the reader computes each row's id from the data file's
        `first_row_id` plus its position - so naming them is the entire feature. Not every
        route can: the fallback is the rows without the two columns, said out loud in the
        returned engine string, because silently dropping them would teach the wrong thing.
        """
        rows, engine = self._rows(ident, limit, snapshot_id, where, columns, lineage)
        _log.info("rows: %d of %s via %s", rows.num_rows, ident, engine)
        return rows, engine

    def _rows(
        self,
        ident: str,
        limit: int,
        snapshot_id: int | None,
        where: str | None,
        columns: list[str] | None,
        lineage: bool,
    ) -> tuple[pa.Table, str]:
        with self._lock:
            con = self._duck_for(ident)
            refused: IcelensError | None = None
            if con is not None:
                try:
                    return self._duck_rows(con, ident, limit, snapshot_id, where, columns, lineage)
                except IcelensError as exc:
                    if not _unreachable_by_duckdb(exc):
                        raise
                    refused = exc
                if self._vending_refused(refused) and (con := self._duck_for(ident)) is not None:
                    try:
                        return self._duck_rows(con, ident, limit, snapshot_id, where, columns, lineage)
                    except IcelensError as exc:
                        if not _unreachable_by_duckdb(exc):
                            raise
                        refused = exc
            try:
                return self._pyiceberg_rows(ident, limit, snapshot_id, where, columns, lineage, refused)
            except Exception:
                if refused is not None:
                    # Both routes failed. DuckDB's reason is the one about the storage, which
                    # is the one worth reading; PyIceberg's is usually the same thing again.
                    raise refused from None
                raise

    def _duck_rows(
        self,
        con: Any,
        ident: str,
        limit: int,
        snapshot_id: int | None,
        where: str | None,
        columns: list[str] | None,
        lineage: bool,
    ) -> tuple[pa.Table, str]:
        """The rows through DuckDB, attached or scanning. Called under the lock."""
        clause = f" WHERE {where}" if where else ""
        if self._mode == "attach":
            travel = f" AT (VERSION => {int(snapshot_id)})" if snapshot_id is not None else ""
            source = f"{qualified(ident)}{travel}"
        else:
            source = self._scan_expr(ident, snapshot_id)
        tail = f" FROM {source}{clause} LIMIT {row_limit(limit)}"
        if lineage and not columns and self.format_version(ident) < 3:
            # Measured against an attached REST catalog: DuckDB accepts `_row_id` on a
            # v2 table and answers with a column of NULLs rather than an error, so the
            # fallback below never fires in attach mode. A column of NULLs is worse
            # than no column - it reads as "this row has no id" rather than "this table
            # has no row ids" - and it is the opposite of what bridge mode does with
            # the same call, which is how it went unnoticed.
            return self._run(con, f"SELECT *{tail}", generated=True), "duckdb (no row lineage here)"
        star, said = self._readable(ident, None)
        if columns:
            projection, said = self._readable(ident, columns)
        elif lineage:
            projection = ", ".join(quote(c) for c in LINEAGE_COLUMNS) + f", {star}"
        else:
            projection = star
        try:
            return self._run(con, f"SELECT {projection}{tail}", generated=True), f"duckdb{said}"
        except IcelensError:
            if not lineage or columns:
                raise
        return self._run(con, f"SELECT {star}{tail}", generated=True), f"duckdb (no row lineage here){said}"

    def _readable(self, ident: str, columns: list[str] | None) -> tuple[str, str]:
        """A projection of `columns` (None: all of them) that casts a `variant` to JSON text,
        and what that changed - `*` and nothing, when there is no variant.

        DuckDB 1.5.5 reads a variant but cannot hand it to Arrow ("Unsupported Arrow type
        VARIANT", measured on Spark 4.1's and DuckDB's own), so a table with one could not show
        a single row. Cast to JSON text, a variant reads as what it holds: `"hello"` is a string,
        `42` a number. Only top-level columns: a variant nested in a struct still fails, in words.
        """
        from icelens.opaque import is_opaque

        plain = ", ".join(quote(c) for c in columns) if columns else "*"
        try:
            fields = self.table(ident).schema().fields
        except IcelensError:
            return plain, ""
        variant = {f.name for f in fields if is_opaque(f.field_type) and str(f.field_type).strip().lower() == "variant"}
        names = columns or [f.name for f in fields]
        if not variant.intersection(names):
            return plain, ""
        spelled = [f"CAST(CAST({quote(n)} AS JSON) AS VARCHAR) AS {quote(n)}" if n in variant else quote(n) for n in names]
        return ", ".join(spelled), " (variant shown as JSON text)"

    def _pyiceberg_rows(
        self,
        ident: str,
        limit: int,
        snapshot_id: int | None,
        where: str | None,
        columns: list[str] | None,
        lineage: bool,
        refused: IcelensError | None = None,
    ) -> tuple[pa.Table, str]:
        """The rows through PyIceberg: no DuckDB here, or DuckDB could not reach them.

        The second case is the one that matters on a catalog that vends credentials: PyIceberg
        holds the per-table keys the catalog handed it, and DuckDB on the scanning route may
        not. Measured against Polaris, DuckDB got a 403 for rows PyIceberg then read. The
        engine string says which happened, because a note that only said "pyiceberg" would
        hide that DuckDB was turned away, and why.
        """
        table = self.table(ident)
        try:
            scan = table.scan(
                limit=row_limit(limit),
                snapshot_id=snapshot_id,
                row_filter=where or "true",
                selected_fields=tuple(columns) if columns else ("*",),
            )
        except Exception as exc:  # noqa: BLE001 - pyparsing and PyIceberg each raise their own
            # The filter is parsed here, by PyIceberg's own grammar rather than DuckDB's. A
            # `WHERE` it cannot read used to escape as a ParseException and a traceback, where
            # the DuckDB route says the same thing in one line and exits 1.
            raise IcelensError(_unfilterable(where, exc)) from exc
        # PyIceberg 0.12 has no reserved-field constant for `_row_id` and no way to ask a
        # scan for it, so this route cannot serve row lineage at all.
        notes = ["no row lineage here"] if lineage else []
        if refused is not None:
            notes.append(f"duckdb could not read it: {_clip(str(refused), 120)}")
        try:
            rows = scan.to_arrow()
        except Exception as exc:  # noqa: BLE001 - binding the filter, and reading the files, raise anything
            # Binding happens here, so a filter naming a column the table does not have fails
            # here and not above; so does a store that refuses the read. Both are one line.
            if where and _names_a_field(exc):
                raise IcelensError(_unfilterable(where, exc)) from exc
            raise classify(exc, f"cannot read {ident}") from exc
        return rows, "pyiceberg" + (f" ({'; '.join(notes)})" if notes else "")

    def query(self, sql: str) -> pa.Table:
        """Run SQL against the catalog, by the route `sql_route` names. Raises IcelensError when none can.

        A read-only lake refuses anything that is not a read - which is what `--sql` opens,
        and what the app, whose SQL pane is deliberately a way to drive DuckDB, does not.
        """
        if self.read_only or self.sql_is_guarded:
            only_reads(sql)
        with self._lock:
            con = self._duck()
            if con is None:
                return self._arrow_query(sql)
            if self._mode == "bridge":
                self._register_views(con)
            try:
                return self._run(con, sql)
            except IcelensError as exc:
                if self._vending_refused(exc):
                    return self._run(self._duck(), sql)
                if self._nests_after_all(exc):
                    return self._run(self._duck(nested=True), sql)
                if self._unreadable:
                    named = ", ".join(f"{k} ({v})" for k, v in list(self._unreadable.items())[:3])
                    more = f" and {len(self._unreadable) - 3} more" if len(self._unreadable) > 3 else ""
                    raise IcelensError(
                        f"{exc}\n{len(self._unreadable)} tables could not be read for SQL: {named}{more}"
                    ) from exc
                raise

    def _arrow_query(self, sql: str) -> pa.Table:
        """SQL with no iceberg extension: DuckDB over PyIceberg's scans. Called under the lock.

        Each table the SQL names is read whole by PyIceberg and copied into a plain DuckDB under
        the name the other routes give it, so `icelens."demo"."orders"` means the same thing
        here. Whole, because which rows a query needs is DuckDB's to work out and it cannot ask
        PyIceberg; the copy is kept until the table's snapshot moves, so the second query costs
        a metadata load rather than a scan.
        """
        con = self._arrow()
        offenders = _writes_in(sql)
        if offenders:
            # Refused rather than run: a write would land in the copy, report success, and
            # change nothing in the catalog.
            raise IcelensError(
                f"refusing to run {', '.join(sorted(offenders))}: SQL here runs over copies of the "
                "tables PyIceberg scanned, because DuckDB has no iceberg extension, so a write "
                "would change nothing in the catalog"
            )
        for ident in self._named_tables(con, sql):
            self._arrow_scan(con, ident)
        return self._run(con, sql)

    def _arrow(self) -> Any:
        """The `arrow` route's connection, opened once. Called under the lock.

        Separate from `_con` so that `_duck` still answers None: `engine`, `mode` and the Data
        pane keep reporting the PyIceberg route they are on.
        """
        if self._arrow_con is not None:
            return self._arrow_con
        try:
            import duckdb
        except ImportError:
            raise IcelensError(
                f"SQL needs DuckDB, which could not be started here: {self._attach_error or 'unknown reason'}"
            ) from None
        # This DuckDB only ever holds copies of what PyIceberg scanned, so it is shut off from the
        # file system and the network, and locked so SQL cannot turn that back on: without it,
        # `read_text('/etc/hosts')` in the SQL pane would read the machine's files.
        sealed: dict[str, Any] = {"enable_external_access": False, "lock_configuration": True}
        try:
            con = trace.connection(duckdb.connect(":memory:", config=sealed))
        except Exception as exc:  # noqa: BLE001 - duckdb raises a dozen different types
            raise IcelensError(f"SQL needs DuckDB, which could not be started here: {_one_line(exc)}") from exc
        # `USE` makes `demo.orders` mean `icelens.demo.orders`, as an attached catalog's
        # schemas would be reached if it were the default database.
        self._run(con, f"ATTACH ':memory:' AS {ALIAS}", generated=True)
        self._run(con, f"USE {ALIAS}", generated=True)
        self._arrow_con = con
        _log.info(
            "route: SQL runs in DuckDB over PyIceberg's scans, without the iceberg extension (%s)",
            self._attach_error,
        )
        return con

    def _drop_arrow(self) -> None:
        """Forget the `arrow` route's connection and its copies. Called under the lock."""
        if self._arrow_con is not None:
            try:
                self._arrow_con.close()
            except Exception:  # noqa: BLE001 - an invalidated database may refuse to close
                pass
            self._arrow_con = None
        self._arrow_scans.clear()

    def _named_tables(self, con: Any, sql: str) -> list[str]:
        """The catalog tables `sql` names, as `ns.table`, by DuckDB's own parser.

        Only names with a schema count: `icelens.ns.t` and `ns.t`. A bare name is a CTE as often
        as a table, and guessing which namespace it meant is a scan of the wrong table.
        """
        try:
            import duckdb

            statements = duckdb.extract_statements(sql)
        except Exception:  # noqa: BLE001 - SQL that will not parse fails on `execute`, in DuckDB's words
            return []
        found: dict[str, None] = {}
        for statement in statements:
            text = getattr(statement, "query", "") or ""
            explained = _EXPLAINING.match(text)
            if explained is not None:
                text = explained.group("tail")
            for catalog, schema, table in _references(con, text):
                if schema and table and catalog.lower() in ("", ALIAS):
                    found[f"{schema}.{table}"] = None
        return list(found)

    def _arrow_scan(self, con: Any, ident: str) -> None:
        """Copy one table into the `arrow` route's DuckDB, unless its snapshot is already there."""
        try:
            table = self.table(ident, refresh=True)
        except IcelensError as exc:
            if not _no_such(exc.__cause__):
                raise
            # A typo, or `main.t`: DuckDB's own catalog error says so. Given the schema when
            # the namespace is real, it says "no table nope" rather than "no schema demo".
            namespace = ident.rsplit(".", 1)[0]
            try:
                real = self.catalog.namespace_exists(tuple(namespace.split(".")))
            except Exception:  # noqa: BLE001 - only the wording of an error depends on it
                real = False
            if real:
                self._run(con, f"CREATE SCHEMA IF NOT EXISTS {ALIAS}.{quote(namespace)}", generated=True)
            # Dropped since it was copied, or a copy under the same name in other letters:
            # DuckDB matches names whatever their case and PyIceberg does not, so a copy of
            # `demo.orders` would go on answering `DEMO.ORDERS` from an old snapshot.
            for copied in [k for k in self._arrow_scans if k.lower() == ident.lower()]:
                del self._arrow_scans[copied]
                self._run(con, f"DROP TABLE IF EXISTS {qualified(copied)}", generated=True)
            return
        snapshot = table.metadata.current_snapshot_id
        if ident in self._arrow_scans and self._arrow_scans[ident] == snapshot:
            return
        # Whole tables are copied, so a big one is refused before it is read rather than after
        # it has filled the memory. The browser's demo is a few hundred rows; this is for a
        # desktop whose DuckDB has no iceberg extension, pointed at a real catalog.
        current = table.metadata.snapshot_by_id(snapshot) if snapshot is not None else None
        summary = current.summary if current is not None else None
        try:
            total = int(summary["total-records"]) if summary is not None else None
        except (KeyError, TypeError, ValueError):
            total = None
        if total is not None and total > ARROW_ROW_LIMIT:
            raise IcelensError(
                f"{ident} has {total:,} rows, more than {ARROW_ROW_LIMIT:,}: SQL here copies each table "
                "it names whole, because DuckDB has no iceberg extension. Install the iceberg "
                "extension (python3 scripts/setup.py) to query it in place."
            )
        try:
            rows = table.scan().to_arrow()
        except Exception as exc:  # noqa: BLE001 - reading the files raises whatever the store does
            raise classify(exc, f"cannot read {ident} for SQL") from exc
        namespace = ident.rsplit(".", 1)[0]
        self._run(con, f"CREATE SCHEMA IF NOT EXISTS {ALIAS}.{quote(namespace)}", generated=True)
        con.register(_SCANNED, rows)
        try:
            self._run(
                con,
                f"CREATE OR REPLACE TABLE {qualified(ident)} AS SELECT * FROM temp.main.{quote(_SCANNED)}",
                generated=True,
            )
        finally:
            con.unregister(_SCANNED)
        self._arrow_scans[ident] = snapshot
        _log.info("route: PyIceberg scanned %s at snapshot %s for SQL (%d rows)", ident, snapshot, rows.num_rows)

    def _vending_refused(self, exc: IcelensError) -> bool:
        """Whether DuckDB was refused vended credentials; if so, stop asking. Under the lock.

        `Lake.table` catches the same refusal from PyIceberg, but the SQL pane and an attached
        read reach the catalog through DuckDB alone and never pass through it.
        """
        if not refused_to_vend(exc):
            return False
        if asks_for_vended_credentials(self.target.properties):
            self._stop_asking_for_credentials()
            return True
        # Already stopped - by PyIceberg, partway through this same read - and DuckDB is still
        # on the attach that asked. The next `_duck` call replaces it.
        return self._reattach

    def _nests_after_all(self, exc: IcelensError) -> bool:
        """Whether a failed query was looking for a nested namespace DuckDB was not shown.

        `--sql` attaches without walking the catalog first, so an attach that did not know the
        catalog nests learns it here - one walk, on the first catalog error, and only then. A
        walk already made is the answer: re-walking on every misspelt table name cost one list
        call per namespace, each time.
        """
        try:
            import duckdb
        except ImportError:
            return False
        if self._mode != "attach" or self._nested or not isinstance(exc.__cause__, duckdb.CatalogException):
            return False
        if self._namespace_cache is None:
            try:
                self.namespaces()
            except IcelensError:
                return False
        return self._known_to_nest()

    # -- writing, sandbox only -------------------------------------------------------
    def write(self, sql: str) -> list[Written]:
        """Commit every statement in `sql` against a sandbox, and say what each one did.

        The split is the point. DuckDB parses the statement and runs the query - its whole
        vocabulary, `read_csv_auto` and window functions and joins against another Iceberg
        table included - and writes the Parquet. PyIceberg turns that Parquet into a snapshot.
        Neither half is asked to do the other's job, which is why this works against a local
        catalog DuckDB cannot attach at all.

        One record per snapshot a statement made, which is not always one: a DELETE that drops
        some files whole and rewrites others commits twice, and so does DuckDB's MERGE. A
        statement that committed nothing has no record. When a statement fails, the ones
        before it have committed and stay committed, so the error carries their records.
        """
        if not self.is_sandbox:
            raise IcelensError("icelens writes only to a sandbox; start one with --sandbox")
        written: list[Written] = []
        with self._lock:
            if self.is_rest_sandbox:
                self._ensure_sandbox_namespace()
                jobs, commit = route_attached(sql, self.sandbox_namespace), self._commit_attached
            elif self.is_served_sandbox:
                jobs, commit = route_attached(sql, None), self._commit_attached
            else:
                jobs, commit = route(sql), self._commit
            done = 0
            for job in jobs:
                if job.kind == "read":
                    continue
                try:
                    written.extend(commit(job))
                except IcelensError as exc:
                    if _fatal(exc.__cause__) and self._con is not None:
                        self._drop_duck()  # reached without `_run`, through `_peek` or the views
                    raise _partly(exc, written, done) from exc.__cause__
                except Exception as exc:  # noqa: BLE001 - PyIceberg, pyparsing and DuckDB each raise their own
                    # A second CREATE of the same table, a WHERE PyIceberg's parser rejects, a
                    # value DuckDB cannot cast: all the statement's fault, all fixable, and none
                    # of them an IcelensError - so `--write` used to end in a traceback.
                    why = self._after_fatal(self._con, exc) if _fatal(exc) else scrub(_one_line(exc))
                    raise _partly(IcelensError(f"{job.kind} on {job.table} failed: {why}"), written, done) from exc
                done += 1
        return written

    def _ensure_sandbox_namespace(self) -> None:
        """Make the sandbox's namespace with PyIceberg, before DuckDB ever attaches.

        Order matters and cost a while to find: a DuckDB connection caches the catalog it
        attached to, so a namespace created through DuckDB afterwards may be invisible to
        the connection that created it. PyIceberg talks to the catalog directly, and the
        attach that follows sees a namespace that was already there.
        """
        if self._namespace_ready:
            return
        try:
            self.catalog.create_namespace(self.sandbox_namespace)
        except Exception:  # noqa: BLE001 - it already existing is the common case
            pass
        self._namespace_ready = True

    def _ensure_namespace(self, namespace: str) -> None:
        """Make a namespace, and its parents, if they are not there yet."""
        levels = namespace.split(".")
        for depth in range(1, len(levels) + 1):
            try:
                self.catalog.create_namespace(tuple(levels[:depth]))
            except Exception:  # noqa: BLE001 - it already existing is the common case
                pass

    def empty_sandbox_namespace(self) -> list[str]:
        """Drop every table in the sandbox's namespace, and nothing anywhere else.

        The offline reset deletes a directory after checking the directory is a sandbox.
        This one cannot delete anything at all: it lists one namespace and drops what is in
        it, so a mistyped sandbox name empties an empty namespace rather than a real one.
        """
        if not self.is_rest_sandbox:
            raise IcelensError("this sandbox is a directory; reset_sandbox empties it")
        with self._lock:
            namespace = self.sandbox_namespace
            # Opened outside the try: a catalog that is down, or wants a warehouse, used to be
            # swallowed here along with "no such namespace", and the CLI then reported
            # "emptied sandbox_x, 0 tables" about a catalog it had never reached.
            catalog = self.catalog
            try:
                names = list(catalog.list_tables(namespace))
            except Exception as exc:  # noqa: BLE001
                if "nosuch" in type(exc).__name__.lower():
                    return []  # never made, which is the common case: nothing to empty
                raise classify(exc, f"cannot list the tables in {namespace}") from exc
            dropped = []
            for identifier in names:
                name = ".".join(identifier)
                self.catalog.drop_table(identifier)
                self._tables.pop(name, None)
                dropped.append(name)
            self.forget()
            return dropped

    def _commit_attached(self, job: Write) -> list[Written]:
        """Hand the statement to DuckDB whole and let it commit through the REST catalog.

        There is no routing table on this side and no `add_files`: DuckDB owns the write,
        which is why `UPDATE` and `MERGE` work here and not offline, and why the result is
        format-version 3 rather than 2.
        """
        sql = job.payload
        if job.kind == "create" and self.is_served_sandbox:
            # A v3 sandbox makes namespaces as it goes, as the offline one does. Through
            # PyIceberg, and before DuckDB looks: measured, DuckDB sees it without reattaching.
            self._ensure_namespace(job.table.rsplit(".", 1)[0])
        if job.kind == "create":
            match = _ATTACH_SHAPES["CREATE"].match(sql)
            if match is not None:
                sql = with_format_version(sql, match.end("table"))
        since = self._mark(job.table)
        con = self._writer()
        before = self._live_rows(con, job.table)
        known = {sid for sid, _ in self._duck_snapshots(con, job.table)}
        self._run(con, sql)
        if job.kind == "drop":
            self._tables.pop(job.table, None)
            if since[1] is None:
                return []  # `DROP TABLE IF EXISTS` of a table that was not there
            return [Written(job.kind, job.table, "drop", 0, 0, 0, 0, 0, _ATTACHED_ENGINE, job.sql)]
        after = self._live_rows(con, job.table)
        try:
            table = self.table(job.table, refresh=True)
        except IcelensError as exc:
            # DuckDB has committed by now; only the read-back failed. A table with a column
            # PyIceberg cannot parse (`variant`, an unquoted `geometry(…)`) used to end the write
            # in an error, so a CREATE that had happened was reported as one that had not.
            if exc.__cause__ is None or not _unreadable_type(exc.__cause__):
                raise
            return self._report_from_duckdb(con, job, known, (before, after), scrub(_one_line(exc.__cause__)))
        if job.kind == "create" and since[1] is None and table.metadata.format_version != 3:
            # DuckDB accepted `FORMAT_VERSION 3`, `VERSION 3` and an option that does not
            # exist when writing Iceberg to a directory, and wrote v2 every time without a
            # word. So the version is checked against the table rather than assumed from
            # the clause that asked for it.
            raise IcelensError(
                f"asked for format-version 3 and got v{table.metadata.format_version} on "
                f"{job.table}. This catalog will not write v3, so deletion vectors and row "
                f"lineage are not available here."
            )
        return self._report(job, table, _ATTACHED_ENGINE, live=(before, after), since=since)

    def _commit(self, job: Write) -> list[Written]:
        """One classified statement, committed, then read back off the snapshots it made."""
        if job.kind == "delete":
            table = self.table(job.table, refresh=True)
            since = (table.metadata.current_snapshot_id, table.metadata_location)
            with warnings.catch_warnings():
                # PyIceberg's "did not match any records" is a UserWarning, printed over the
                # ledger; a statement that changed nothing already says so by having no record.
                warnings.filterwarnings("ignore", message="Delete operation did not match any records")
                table.delete(job.payload)
            engine = "pyiceberg delete"
        else:
            con = self._writer()
            since = (None, None)
            if job.kind == "create":
                namespace = job.table.rsplit(".", 1)[0]
                try:
                    self.catalog.create_namespace(namespace)
                except Exception:  # noqa: BLE001 - it already existing is the common case
                    pass
                # The schema comes from the query run with no rows, so the table exists -
                # and owns a directory to write into - before a byte of data is produced.
                empty = self._peek(con, job.payload).arrow()
                self.catalog.create_table(job.table, schema=empty.schema)
            table = self.table(job.table, refresh=True)
            if job.kind != "create":
                since = (table.metadata.current_snapshot_id, table.metadata_location)
            path = self._duck_parquet(con, job.payload, self._shape(con, table), _local(table.location()) / "data")
            table.add_files([_file_uri(path)])
            engine = "duckdb parquet + pyiceberg add_files"
            if self.table(job.table, refresh=True).current_snapshot() is None:
                raise IcelensError(f"{job.kind} on {job.table} produced no snapshot")

        table = self.table(job.table, refresh=True)
        return self._report(job, table, engine, since=since)

    def _mark(self, ident: str) -> tuple[int | None, str | None]:
        """Where a table stands before a statement: its snapshot and its metadata file.

        `(None, None)` for a table that is not there yet. The two together tell a statement
        that committed nothing - `INSERT … WHERE false`, a `CREATE TABLE IF NOT EXISTS` that
        found one - from one that changed only metadata, like `ALTER TABLE … ADD COLUMN`.
        """
        try:
            table = self.table(ident, refresh=True)
        except IcelensError:
            return None, None
        return table.metadata.current_snapshot_id, table.metadata_location

    @staticmethod
    def _duck_snapshots(con: Any, ident: str) -> list[tuple[int, str]]:
        """(snapshot id, operation) for each snapshot, oldest first, as DuckDB reads them.
        Empty for a table that is not there yet."""
        try:
            rows = con.execute(
                f"SELECT snapshot_id, operation FROM iceberg_snapshots({qualified(ident)}) ORDER BY sequence_number"
            ).fetchall()
        except Exception:  # noqa: BLE001 - before a CREATE there is nothing to list
            return []
        return [(int(sid), str(operation or "")) for sid, operation in rows]

    def _report_from_duckdb(
        self, con: Any, job: Write, known: set[int], live: tuple[int, int], reason: str
    ) -> list[Written]:
        """What a commit did, from DuckDB alone, for a table PyIceberg cannot read back.

        Rows are counted, as `_report` counts them under deletion vectors; the per-snapshot
        split and the file count are not knowable without the manifests, so the rows go on
        the last snapshot and files are 0. The engine says why, so the ledger is not mistaken
        for the full report.
        """
        made = [(sid, op) for sid, op in self._duck_snapshots(con, job.table) if sid not in known]
        if not made:
            return []
        before, after = live
        engine = f"{_ATTACHED_ENGINE} (read back by DuckDB: {reason})"
        last = len(made) - 1
        return [
            Written(
                kind=job.kind,
                table=job.table,
                operation=operation or "unknown",
                snapshot_id=sid,
                added=max(0, after - before) if index == last else 0,
                deleted=max(0, before - after) if index == last else 0,
                records=after if index == last else before,
                files=0,
                engine=engine,
                sql=job.sql,
            )
            for index, (sid, operation) in enumerate(made)
        ]

    @staticmethod
    def _live_rows(con: Any, ident: str) -> int:
        """How many rows the table has right now. 0 for a table that is not there yet."""
        try:
            return int(con.execute(f"SELECT count(*) FROM {qualified(ident)}").fetchone()[0])
        except Exception:  # noqa: BLE001 - before a CREATE there is nothing to count
            return 0

    def _report(
        self,
        job: Write,
        table: Any,
        engine: str,
        live: tuple[int, int] | None = None,
        since: tuple[int | None, str | None] | None = None,
    ) -> list[Written]:
        """What the commit did, one record per snapshot, read back off the table.

        `since` is `_mark` from before the statement; None reports the current snapshot alone.
        Reading only the current snapshot lost the first of two - a DELETE that drops some
        files and rewrites others commits a `delete` then an `overwrite`, and its rows did not
        add up - and reported the previous statement's snapshot again for one that committed
        nothing. So the new snapshots are walked back to `since`, and no snapshot is no record.

        A statement that changes only metadata - `ALTER TABLE … ADD COLUMN` - makes no new
        snapshot, and says so rather than failing: it is a real thing to do to a table and a
        good thing to watch the Schema pane answer.
        """
        current = table.current_snapshot()
        if since is None:
            made = [current] if current is not None else []
            changed = True
        else:
            made = _snapshots_since(table.metadata, since[0])
            changed = table.metadata_location != since[1]
        if not made:
            if not changed:
                return []
            records, files = self._counts(table)
            if live is not None:
                records = live[1]
            snapshot_id = current.snapshot_id if current is not None else 0
            return [Written(job.kind, job.table, "metadata", snapshot_id, 0, 0, records, files, engine, job.sql)]

        added = [int(s.summary.get("added-records", 0) or 0) for s in made]
        deleted = [int(s.summary.get("deleted-records", 0) or 0) for s in made]
        records = [int(s.summary.get("total-records", 0) or 0) for s in made]
        operations = [str(s.summary.operation).replace("Operation.", "").lower() for s in made]
        if live is not None:
            # Merge-on-read makes the summary's own counters unusable as a row count, and
            # measurably so: three deletes of ten rows each reported `added-position-deletes`
            # of 10, then 20, then 30, because a deletion vector is rewritten whole and
            # counts every position ever deleted from its data file. `total-records` sat at
            # 100 throughout. So rows are counted rather than read off the summary, and the
            # deletions are derived from what the count actually did: a table that gained
            # `added` rows and grew by less than that lost the difference.
            before, after = live
            deleted = _share(max(0, sum(added) - (after - before)), operations, deleted)
            running = before
            for index in range(len(made)):
                running += added[index] - deleted[index]
                records[index] = running
            records[-1] = after
        return [
            Written(
                kind=job.kind,
                table=job.table,
                operation=operations[index],
                snapshot_id=snapshot.snapshot_id,
                added=added[index],
                deleted=deleted[index],
                records=records[index],
                files=int(snapshot.summary.get("total-data-files", 0) or 0),
                engine=engine,
                sql=job.sql,
            )
            for index, snapshot in enumerate(made)
        ]

    @staticmethod
    def _counts(table: Any) -> tuple[int, int]:
        """Records and files as the table currently claims them; 0 before the first commit."""
        snapshot = table.current_snapshot()
        if snapshot is None:
            return 0, 0
        summary = snapshot.summary
        return int(summary.get("total-records", 0) or 0), int(summary.get("total-data-files", 0) or 0)

    def _writer(self) -> Any:
        """The DuckDB connection writes run through, with the bridge views already in place."""
        con = self._duck()
        if con is None:
            raise IcelensError(f"writing needs DuckDB: {self._attach_error or 'unknown reason'}")
        if self._mode == "bridge":
            self._register_views(con)
        return con

    @staticmethod
    def _peek(con: Any, query: str) -> Any:
        """Run the query with no rows, for its shape. The user's SQL is the user's to fix."""
        try:
            return con.execute(f"SELECT * FROM ({query}) LIMIT 0")
        except Exception as exc:  # noqa: BLE001
            raise IcelensError(f"that query will not run: {_one_line(exc)}") from exc

    @staticmethod
    def _shape(con: Any, table: Any) -> list[tuple[str, str]]:
        """The table's columns as DuckDB spells them: `[("amount", "DECIMAL(21,1)"), …]`.

        Asked of DuckDB rather than derived here, by registering the table's own schema with
        no rows in it. A hand-written Iceberg-to-DuckDB type map would be one more place for
        `decimal(21,1)` to come out as `DOUBLE`, and the whole point of the cast below is that
        the Parquet matches the table exactly.
        """
        from pyiceberg.io.pyarrow import schema_to_pyarrow

        name = "_icelens_shape"
        con.register(name, pa.Table.from_pylist([], schema=schema_to_pyarrow(table.schema())))
        try:
            return [(row[0], row[1]) for row in con.execute(f"DESCRIBE SELECT * FROM {name}").fetchall()]
        finally:
            con.unregister(name)

    def _duck_parquet(self, con: Any, query: str, shape: list[tuple[str, str]], home: Path) -> Path:
        """Run a query in DuckDB and leave the result as a Parquet file the table can adopt.

        The file lands in the table's own data directory and stays there: `add_files` records
        the path it is given, so this is a data file of the table from the moment it commits,
        not a temporary that something may clean up underneath it.

        `shape` maps the query's output onto the table's fields **by position**, which is what
        `INSERT INTO t SELECT …` has always meant, and casts each one to the type the table
        declares. `add_files` adopts the file as written, so both halves matter: without the
        rename it refuses a query whose author did not happen to alias `i * 1.5` to `amount`,
        and without the cast it refuses `9.99` written as a `DECIMAL(3,2)` into a
        `DECIMAL(21,1)` column.
        """
        import uuid

        query = self._project(con, query, shape)
        home.mkdir(parents=True, exist_ok=True)
        path = home / f"icelens-{uuid.uuid4().hex}.parquet"
        con.execute(one_statement(f"COPY ({query}) TO {literal(str(path))} (FORMAT PARQUET)"))
        return path

    def _project(self, con: Any, query: str, shape: list[tuple[str, str]]) -> str:
        """Cast and rename a query's output onto the table's columns, positionally.

        Renaming through a positional table alias rather than by the names DuckDB inferred:
        a query is free to produce two columns called `id`, and matching those by name would
        pick one of them twice. A cast that cannot be made is DuckDB's error to raise, and it
        names the column and both types, which is more use than anything this could say.
        """
        described = self._peek(con, query).description or []
        if len(described) != len(shape):
            names = ", ".join(name for name, _ in shape)
            raise IcelensError(f"the table has {len(shape)} columns ({names}) and the query returns {len(described)}")
        holders = [f"c{i}" for i in range(len(shape))]
        picked = ", ".join(
            f"CAST({quote(holder)} AS {kind}) AS {quote(name)}"
            for holder, (name, kind) in zip(holders, shape, strict=True)
        )
        return f"SELECT {picked} FROM ({query}) AS _icelens_q({', '.join(quote(h) for h in holders)})"

    def rebind(self, target: Target) -> None:
        """Point at a different catalog - a warehouse picked in the app, or ctrl+o.

        Everything decided about the old target is decided again, read-only first: it was
        worked out once, in `__init__`, so a lake opened on a sandbox - `--demo`, the welcome
        screen - stayed writable after ctrl+o took it to a real catalog, and the SQL pane
        could `DROP TABLE` there.
        """
        with self._lock:
            self.close()
            self.target = target
            self.read_only = self._asked_read_only and not target.sandbox
            remember_secrets(target.properties)
            self._catalog = None
            self._nested = False
            self._namespace_ready = False
            self.delegation_note = ""

    def warehouses(self) -> list[str]:
        """What this server offers, for the picker. Empty when it will not say."""
        return probe_warehouses(self.target.uri, properties=self.target.properties)

    def _run(self, con: Any, sql: str, generated: bool = False) -> pa.Table:
        """Run one query. `generated` means icelens built it, so it must be a single statement.

        The SQL pane is deliberately exempt: a person typing into it is driving DuckDB on
        purpose. A query assembled out of a warehouse name and a file path is not.
        """
        import duckdb

        if generated:
            one_statement(sql)
        try:
            return con.execute(sql).to_arrow_table()
        except duckdb.Error as exc:
            if _fatal(exc):
                raise IcelensError(self._after_fatal(con, exc)) from exc
            raise IcelensError(scrub(str(exc).strip())) from exc

    def _after_fatal(self, con: Any, exc: BaseException) -> str:
        """Drop a connection DuckDB has given up on, and say so. Called under the lock.

        After an internal error DuckDB invalidates the whole database, and every later
        statement on it fails with "database has been invalidated" - measured, after an
        `UPDATE … WHERE false` the iceberg extension could not plan. The next call opens a
        fresh connection instead. The stack trace DuckDB appends is cut: it is for its authors.
        """
        if con is self._con:
            self._drop_duck()
        elif con is self._arrow_con:
            self._drop_arrow()
        return f"{_clip(str(exc), 300)}\nDuckDB cannot go on after that error, so icelens closed it; the next request opens a fresh one."


#: The keys a vending catalog puts in a table's load response. A table whose FileIO carries one
#: of these that the connection itself was not given has credentials of its own.
_VENDED_KEYS = ("s3.access-key-id", "s3.secret-access-key", "s3.session-token", "gcs.oauth2.token")


def _scoped(options: list[str], scope: str | list[str] | None) -> list[str]:
    if isinstance(scope, list) and len(scope) == 1:
        scope = scope[0]
    if isinstance(scope, list):
        return [*options, f"SCOPE [{', '.join(literal(s) for s in scope)}]"] if scope else options
    return [*options, f"SCOPE {literal(scope)}"] if scope else options


def _s3_secret(props: dict[str, str], name: str, scope: str | list[str] | None = None) -> str | None:
    """A `CREATE SECRET` for an S3-compatible store, or None if `props` configure none.

    `scope` limits it to prefixes - a table's own, for keys the catalog vended for that table
    alone. DuckDB picks the longest matching scope, so a table's own secret wins over the
    catalog-wide one for that table's files and is never used for anybody else's.
    """
    key = props.get("s3.access-key-id") or props.get("s3.access-key")
    secret = props.get("s3.secret-access-key") or props.get("s3.secret-key")
    endpoint = props.get("s3.endpoint", "")
    region = props.get("s3.region") or props.get("client.region")
    if not (key or endpoint or region):
        return None  # nothing configured: let DuckDB use its own credential chain
    options = ["TYPE S3"]
    if key and secret:
        options += [f"KEY_ID {literal(key)}", f"SECRET {literal(secret)}"]
    if props.get("s3.session-token"):
        options.append(f"SESSION_TOKEN {literal(props['s3.session-token'])}")
    if region:
        options.append(f"REGION {literal(region)}")
    if endpoint:
        options += [f"ENDPOINT {literal(endpoint.split('://', 1)[-1].rstrip('/'))}", "URL_STYLE 'path'"]
        if endpoint.startswith("http://"):
            options.append("USE_SSL false")
    return one_statement(f"CREATE OR REPLACE SECRET {quote(name)} ({', '.join(_scoped(options, scope))})")


def _gcs_secret(props: dict[str, str], name: str, scope: str | list[str] | None = None) -> str | None:
    """A `CREATE SECRET` for Cloud Storage - a bearer token or an HMAC pair - or None.

    Verified against duckdb 1.5.5 rather than assumed: the `gcs` secret type takes a
    `BEARER_TOKEN`, or an HMAC `KEY_ID`/`SECRET` pair, and refuses `TOKEN` by name with a
    binder error. A catalog that vends credentials returns an OAuth token, which is the
    bearer one; the HMAC pair only exists if somebody made one by hand.
    """
    token = props.get("gcs.oauth2.token", "")
    key = props.get("gcs.access-key-id") or props.get("gcs.access-key")
    secret = props.get("gcs.secret-access-key") or props.get("gcs.secret-key")
    if not (token or (key and secret)):
        return None  # nothing configured: DuckDB's own chain still finds ADC if there is any
    options = ["TYPE GCS"]
    if token:
        options.append(f"BEARER_TOKEN {literal(token)}")
    elif key and secret:
        options += [f"KEY_ID {literal(key)}", f"SECRET {literal(secret)}"]
    return one_statement(f"CREATE OR REPLACE SECRET {quote(name)} ({', '.join(_scoped(options, scope))})")


def _table_scopes(location: str, metadata_location: str) -> list[str] | None:
    """The prefixes one table's own secret covers: its root, and its metadata's directory.

    Two scopes, not their common prefix. Databricks keeps metadata outside the table root, so
    the root alone would not cover it - but the common prefix of the two was as wide as the
    catalog cared to make it: `s3://lake/sales/t` with metadata at `s3://elsewhere/m.json` gave
    `s3://`, and metadata kept beside the root gave the shared parent, which every table laid
    out that way shared - one scope, so one secret name, and each handover replaced the last
    until table A's files were read with table B's keys.

    Each ends in `/`, so a scope stops at a path boundary: `…/core/items` would otherwise also
    match `…/core/items2`, somebody else's table. None when either is shorter than
    `scheme://bucket/something/`: a secret that wide would serve every read in the bucket, so
    the configured credentials are kept instead.
    """
    folders = [location.rstrip("/") + "/"] if location else []
    if metadata_location:
        folders.append(metadata_location.rsplit("/", 1)[0] + "/")
    if not folders or not all(_narrow_scope(folder) for folder in folders):
        return None
    unique = list(dict.fromkeys(folders))
    return [f for f in unique if not any(other != f and f.startswith(other) for other in unique)]


def _narrow_scope(prefix: str) -> bool:
    """Whether a prefix names something inside a bucket, not the bucket or the whole store."""
    scheme, sep, rest = prefix.partition("://")
    bucket, _, path = rest.partition("/")
    return bool(scheme and sep and bucket and any(path.split("/")))


def _unreachable_by_duckdb(exc: BaseException) -> bool:
    """Whether DuckDB failed to *reach* a table, as opposed to rejecting the question.

    Decided on DuckDB's own exception class, which `_run` keeps as the cause: I/O and HTTP
    errors (a 403 from the store, a 400 from the catalog), a catalog error (a table the
    attached catalog does not show) and a permission error are all things another route
    might get past. A binder or parser error is about the SQL - usually a `WHERE` somebody
    typed - and handing that to PyIceberg would only replace a good message with a worse one.

    A column type DuckDB cannot read is the table's doing, not the SQL's, so it goes to PyIceberg
    too. Measured with DuckDB 1.5.5: a `timestamptz_ns` column, which Trino 484 writes, fails
    every query on the table with a bare `duckdb.Error` "Unrecognized primitive type" - even one
    that does not select it - and PyIceberg 0.12 reads it, all nine digits.
    """
    try:
        import duckdb
    except ImportError:
        return False
    cause = exc.__cause__
    return isinstance(
        cause, (duckdb.IOException, duckdb.CatalogException, duckdb.PermissionException, duckdb.NotImplementedException)
    ) or (isinstance(cause, duckdb.Error) and "Unrecognized primitive type" in str(cause))


def _snapshots_since(metadata: Any, before: int | None) -> list[Any]:
    """The snapshots committed on top of `before`, oldest first; empty when none were.

    Walked from the current snapshot back through its parents, so a snapshot on another branch
    is never counted. `before` not among the ancestors means the history was replaced rather
    than added to, and then only the current snapshot is this statement's to report.
    """
    made: list[Any] = []
    snapshot_id = metadata.current_snapshot_id
    while snapshot_id is not None and snapshot_id != before:
        snapshot = metadata.snapshot_by_id(snapshot_id)
        if snapshot is None or any(s.snapshot_id == snapshot_id for s in made):
            break
        made.append(snapshot)
        snapshot_id = snapshot.parent_snapshot_id
    if before is not None and snapshot_id != before:
        return made[:1]
    return made[::-1]


def _share(total: int, operations: list[str], claimed: list[int]) -> list[int]:
    """Counted deletions, spread over the snapshots one statement made.

    An `append` deletes nothing. DuckDB's MERGE makes an `overwrite` and an `append`, so in
    practice one snapshot takes the lot; where several could, each takes what its own summary
    claims, in order, and the last takes what is left, so the shares always add up to `total`.
    """
    shares = [0] * len(operations)
    takers = [i for i, op in enumerate(operations) if op != "append"] or [len(operations) - 1]
    left = total
    for index in takers[:-1]:
        shares[index] = min(claimed[index], left)
        left -= shares[index]
    shares[takers[-1]] = left
    return shares


def _partly(exc: IcelensError, written: list[Written], done: int) -> IcelensError:
    """`exc`, carrying what the statements before it committed, and saying they did."""
    if written:
        plural = done != 1
        exc.args = (
            f"{exc}\n{done} earlier statement{'s' if plural else ''} committed before this one failed, "
            f"and {'they stay' if plural else 'it stays'} committed.",
        )
        exc.written = tuple(written)
    return exc


def _fatal(exc: BaseException | None) -> bool:
    """Whether DuckDB has invalidated its database: an internal or a fatal error."""
    try:
        import duckdb
    except ImportError:
        return False
    return isinstance(exc, (duckdb.InternalException, duckdb.FatalException))


def _unfilterable(where: str | None, exc: BaseException) -> str:
    """Why PyIceberg would not filter on `where`, in a form a person can act on."""
    return (
        f"PyIceberg cannot filter on {where!r}: {scrub(_one_line(exc))}. Without DuckDB the filter "
        "is PyIceberg's expression language - comparisons, and/or/not, in, is null, starts with - "
        "not SQL."
    )


def _names_a_field(exc: BaseException) -> bool:
    """Whether PyIceberg failed to bind a filter to the schema: a column it does not have."""
    text = str(exc).lower()
    return "could not find field" in text or "cannot find field" in text


def _clip(text: str, width: int) -> str:
    """One line of at most `width` characters, credentials scrubbed."""
    line = scrub(text.strip().splitlines()[0] if text.strip() else "")
    return line if len(line) <= width else line[: width - 1] + "…"


def _needs_warehouse(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "warehouse" in text and any(word in text for word in ("no ", "not ", "specify", "missing", "required"))

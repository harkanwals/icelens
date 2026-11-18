"""`--trace FILE`: what icelens asked of the catalog, the store and DuckDB, and how long it took.

A bug report about a catalog nobody else can reach is only as good as what it says happened,
and the status bar says the outcome, not the conversation. This writes the conversation down:

- every HTTP request PyIceberg makes, with its status and time;
- every statement icelens runs in DuckDB, with its time, or its error;
- every decision about the route to the rows: attached, scanning, or PyIceberg, and why;
- the warehouse probe's requests, and what icelens's own REST catalog was asked.

Nothing here is written unless a trace was asked for, and what icelens itself sends is not
written in a form that leaks it: every line goes through `scrub()`, and before that the
credential-shaped parts of a statement or a URL are masked - every value of a `CREATE SECRET`
but its type, scope, endpoint and key id; the literal after `TOKEN`, `SECRET` and their kin;
a connection string's password; a URL's user and password and its signature parameters - so
a secret icelens was never told about still does not reach the file. SQL a person types is
traced as typed apart from those, which is why the README says to read a trace before posting
it. The file is created readable by its owner only, and an existing file is only appended to
if it is a regular file this user owns.

DuckDB's own HTTP requests, made inside its extensions, are not here: DuckDB does not hand
them to Python. `CALL enable_logging('HTTP')` in the SQL pane shows them.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: The one logger icelens writes to; modules log under it as `icelens.<module>`.
log = logging.getLogger("icelens")

#: The keywords whose string literal is a credential, in ATTACH and CREATE SECRET options.
_SENSITIVE = (
    "SECRET",
    "TOKEN",
    "CLIENT_SECRET",
    "SESSION_TOKEN",
    "BEARER_TOKEN",
    "ACCOUNT_KEY",
    "CONNECTION_STRING",
    "PASSWORD",
)
#: A DuckDB string: single-quoted with '' escapes, or dollar-quoted ($$...$$, $tag$...$tag$).
_STRING = r"(?:'(?:[^']|'')*'|\$(?P<tag>[A-Za-z_]*)\$.*?\$(?P=tag)\$)"
_LITERAL_AFTER = re.compile(r"\b(" + "|".join(_SENSITIVE) + r")(\s+)" + _STRING, re.IGNORECASE | re.DOTALL)
#: The options of a `CREATE SECRET` whose value is worth seeing; every other value is masked.
_SECRET_SAFE = ("TYPE", "PROVIDER", "SCOPE", "REGION", "ENDPOINT", "URL_STYLE", "USE_SSL", "KEY_ID", "ACCOUNT_NAME", "CHAIN")
_CREATE_SECRET = re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:(?:PERSISTENT|TEMPORARY)\s+)?SECRET\b", re.IGNORECASE)
_ANY_STRING = re.compile(r"(\b[A-Za-z_]+\s+)?" + _STRING, re.DOTALL)
#: A password in a connection string (`ATTACH 'dbname=x password=y'`), and a URL's signature.
_KEY_VALUE = re.compile(r"\b(password|pwd|passwd)=[^\s;'&]+", re.IGNORECASE)
_SIGNED_QUERY = re.compile(
    r"([?&](?:X-Amz-Signature|X-Amz-Security-Token|X-Amz-Credential|X-Goog-Signature|X-Goog-Credential|sig|signature|token|access_token)=)[^&#\s']+",
    re.IGNORECASE,
)

_handler: logging.Handler | None = None
_unwatch: Callable[[], None] | None = None
#: the logger levels a trace raised, put back when it stops
_levels: dict[str, int] = {}


def redact(sql: str) -> str:
    """`sql` with every credential-bearing value replaced, on one line."""
    if _CREATE_SECRET.match(sql):
        sql = _ANY_STRING.sub(_mask_unless_safe, sql)
    else:
        sql = _LITERAL_AFTER.sub(lambda m: f"{m.group(1)}{m.group(2)}'***'", sql)
    sql = _SIGNED_QUERY.sub(r"\1***", _KEY_VALUE.sub(r"\1=***", sql))
    return " ".join(sql.split())


def _mask_unless_safe(match: re.Match[str]) -> str:
    keyword = (match.group(1) or "").strip().upper()
    if keyword in _SECRET_SAFE:
        return match.group(0)
    return f"{match.group(1) or ''}'***'"


def safe_url(url: str) -> str:
    """A URL fit for the trace: no user or password, and no signature in its query."""
    import urllib.parse

    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return "<unparseable url>"
    if "@" in parts.netloc:
        parts = parts._replace(netloc=parts.netloc.rsplit("@", 1)[1])
    return _SIGNED_QUERY.sub(r"\1***", urllib.parse.urlunsplit(parts))


class _Scrubbing(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from icelens.catalog import scrub
        from icelens.format import safe

        record.exc_info = None  # a traceback is a page of somebody else's internals; the message is enough
        return safe(scrub(super().format(record)))


def active() -> bool:
    return _handler is not None


def start(path: Path) -> None:
    """Begin writing the trace to `path`, appending. Raises OSError if it cannot be written."""
    global _handler, _unwatch
    stop()
    # O_NOFOLLOW: a link planted at a shared trace path must not redirect lines (which carry
    # catalog-controlled text) into a file of the user's. A file that already exists keeps its
    # old mode under O_CREAT, so the mode is set explicitly too.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path} is not a regular file")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise OSError(f"{path} belongs to another user")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    stream = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_Scrubbing("%(asctime)s.%(msecs)03d %(name)-16s %(message)s", "%H:%M:%S"))
    for name, level in (("icelens", logging.DEBUG), ("pyiceberg", logging.INFO)):
        logger = logging.getLogger(name)
        _levels[name] = logger.level
        logger.setLevel(level)
        logger.addHandler(handler)
    _handler = handler
    _unwatch = _watch_requests()
    log.info("trace started: %s", _versions())


def stop() -> None:
    global _handler, _unwatch
    if _unwatch is not None:
        _unwatch()
        _unwatch = None
    if _handler is None:
        return
    for name in ("icelens", "pyiceberg"):
        logger = logging.getLogger(name)
        logger.removeHandler(_handler)
        logger.setLevel(_levels.pop(name, logging.NOTSET))
    _handler.close()
    _handler = None


def _versions() -> str:
    from importlib.metadata import PackageNotFoundError, version

    parts = [f"python {sys.version.split()[0]}"]
    for package in ("icelens", "duckdb", "pyiceberg", "pyarrow", "textual"):
        try:
            parts.append(f"{package} {version(package)}")
        except PackageNotFoundError:
            pass
    return ", ".join(parts)


# -- DuckDB ------------------------------------------------------------------------------------


class _TracedConnection:
    """A DuckDB connection that logs each `execute`, its time and its outcome.

    Only `execute` is wrapped, because it is the only call icelens makes with SQL in it;
    everything else passes straight through. What `execute` returns is DuckDB's own object,
    so the `.fetchall()` chained on it is untouched.
    """

    __slots__ = ("_con",)

    def __init__(self, con: Any) -> None:
        self._con = con

    def execute(self, sql: str, *args: Any, **kwargs: Any) -> Any:
        log = logging.getLogger("icelens.duckdb")
        started = time.perf_counter()
        try:
            result = self._con.execute(sql, *args, **kwargs)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            reason = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            log.info("%7.1f ms  failed  %s  -> %s: %s", elapsed, redact(sql), type(exc).__name__, reason)
            raise
        log.info("%7.1f ms  %s", (time.perf_counter() - started) * 1000, redact(sql))
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._con, name)


def connection(con: Any) -> Any:
    """`con`, logged statement by statement when a trace is on; `con` itself otherwise."""
    return _TracedConnection(con) if active() else con


# -- PyIceberg's HTTP --------------------------------------------------------------------------


def _watch_requests() -> Callable[[], None]:
    """Log every request made through `requests`, which is every request PyIceberg makes.

    A session hook would be tidier, but PyIceberg's REST catalog fetches `/v1/config` and does
    the OAuth exchange inside its constructor, before anything outside it could add one - and
    a failing login is the trace people most need. So `Session.send` itself is wrapped while a
    trace is on, and put back when it stops.
    """
    try:
        import requests
    except ImportError:
        return lambda: None
    original = requests.Session.send
    http = logging.getLogger("icelens.http")

    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            response = original(self, request, **kwargs)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            http.info("%7.1f ms  %s %s -> %s: %s", elapsed, request.method, safe_url(request.url), type(exc).__name__, exc)
            raise
        elapsed = (time.perf_counter() - started) * 1000
        http.info("%7.1f ms  %s %s -> %s", elapsed, request.method, safe_url(request.url), response.status_code)
        return response

    requests.Session.send = send  # type: ignore[method-assign]
    return lambda: setattr(requests.Session, "send", original)

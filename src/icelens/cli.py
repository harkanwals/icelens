"""`icelens`: browse an Iceberg catalog in the terminal, or print one pane of it and exit.

The headless half is not an afterthought. Every pane the UI shows is available as
`icelens --pane <name> <table>`, which means the same code answers "let me look" and "put that in
a diff, a ticket, a test". It is also how the project tests itself against a real catalog.

`--json` is the half of that meant for something that is not a person. It emits one object -
never a bare list, never a line-delimited stream - carrying the pane's rows with their types
intact, the note that would otherwise have gone to stderr, and `schema_version`, so a caller
can tell a contract change from a catalog change. Failures are JSON too, on stderr, with the
exception class named: a program should not have to read English to find out it needs a
warehouse. The rendered output is unaffected; `--csv` and the default table are what they were.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, NoReturn

from icelens import demo, model, terms, trace
from icelens.catalog import (
    IcelensError,
    Lake,
    NeedsWarehouse,
    NoCatalog,
    discover,
    list_profiles,
    list_sandboxes,
    literal,
    make_sandbox_home,
    only_reads,
    quote,
    redact_uri,
    reset_sandbox,
    rest_sandbox_target,
    sandbox_target,
    scrub,
    stays_in_catalog,
    without_own_keys,
)
from icelens.format import csv_bytes, grid, safe
from icelens.model import Pane, View

USAGE = """icelens - learn Apache Iceberg, and inspect your production Iceberg tables

  icelens                                   open the catalog found in ./.env or ~/.pyiceberg.yaml
  icelens --demo                            no catalog yet? a local one, with a table to look at
  icelens --uri http://localhost:8181/catalog --warehouse demo
  icelens demo.orders                       open straight onto a table
  icelens --list                            namespaces and table counts
  icelens --list demo                       the tables in one namespace
  icelens --pane snapshots demo.orders      print a pane and exit
  icelens --pane layout demo.orders --snapshot <snapshot-id>
  icelens --sql 'SELECT count(*) FROM icelens."demo"."orders"'
  icelens --catalogs                        every catalog named in ~/.pyiceberg.yaml
  icelens --catalog prod_glue               open one of them
  icelens --explain                         every Iceberg term the app can explain
  icelens --explain "manifest list"         one of them, in full
"""

PANE_NAMES = tuple(model.PANES)

#: Bumped when the shape of a `--json` object changes in a way that could break a reader:
#: a key removed or renamed, a value's type changed. New keys do not bump it.
SCHEMA_VERSION = 1


def _port(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a port number: {text!r}") from None
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"a port is 1 to 65535, not {value}")
    return value


def _whole(text: str) -> int:
    """A `--limit` argparse rejects rather than quietly clamps.

    `LIMIT -1` is "no limit" in DuckDB. The catalog clamps anyway, for values arriving from a
    config file, but a number typed on the command line deserves to be argued with.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"cannot read {value} rows")
    return value


class UsageError(Exception):
    """The command line asks for something that cannot be done. Exit code 2, as argparse has it."""


class _Parser(argparse.ArgumentParser):
    """argparse, except that a usage error is JSON when the command line asked for JSON.

    argparse prints its complaint and exits before `main` has looked at a single flag, so a
    program that passed `--json` got English for exactly one class of mistake - `--limit -5`,
    a pane that does not exist, `--pane` with no table - and JSON for every other.
    """

    json_errors = False

    def error(self, message: str) -> NoReturn:
        if self.json_errors:
            _emit_error(UsageError(message))
            raise SystemExit(2)
        super().error(message)


def _wants_json(argv: list[str]) -> bool:
    """Whether `--json` is on the command line, spelled any way argparse would accept it.

    Read off argv rather than parsed, because this is consulted while argparse is failing. No
    other flag starts `--j`, so every unambiguous abbreviation argparse allows is one of these.
    """
    for arg in argv:
        if arg == "--":
            return False
        if arg.split("=", 1)[0] in ("--j", "--js", "--jso", "--json"):
            return True
    return False


def build_parser() -> _Parser:
    parser = _Parser(
        prog="icelens",
        description=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("table", nargs="?", help="table to open, as <namespace>.<table>")

    connection = parser.add_argument_group("connection")
    connection.add_argument("--uri", help="Iceberg REST catalog URI")
    connection.add_argument("--warehouse", help="warehouse to open (REST catalogs that host several)")
    connection.add_argument("--catalog", dest="profile", help="named catalog from ~/.pyiceberg.yaml")
    connection.add_argument(
        "-p",
        "--property",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="any PyIceberg catalog property, repeatable (s3.endpoint=…, token=…, type=glue)",
    )
    connection.add_argument("--env-file", type=Path, help="read connection settings from this .env (default: the nearest .env, searching upwards)")
    connection.add_argument("--no-env", action="store_true", help="ignore .env entirely")

    sandbox = parser.add_argument_group("sandbox")
    sandbox.add_argument(
        "--sandbox",
        nargs="?",
        const="default",
        metavar="NAME",
        help="open a local scratch catalog you can write to, instead of connecting to one. Named, and kept between runs.",
    )
    sandbox.add_argument(
        "--rest",
        action="store_true",
        help="put the sandbox in a namespace of a running catalog, so DuckDB writes format-version 3 (deletion vectors, row lineage). Takes the usual connection settings.",
    )
    sandbox.add_argument(
        "--demo",
        action="store_true",
        help="put a demo table in the sandbox - five commits of history, so every pane has something to show. Alone, it opens the sandbox called demo.",
    )
    sandbox.add_argument(
        "--v3",
        action="store_true",
        help="make the sandbox format-version 3: icelens serves it through its own REST catalog on 127.0.0.1 and DuckDB writes it - deletion vectors, row lineage, UPDATE and MERGE. The sandbox remembers.",
    )
    sandbox.add_argument("--sandbox-reset", action="store_true", help="empty the sandbox before opening it")
    sandbox.add_argument("--sandbox-dir", type=Path, help="where sandboxes live (default ~/.local/share/icelens/sandboxes)")
    sandbox.add_argument("--write", help="run write SQL against the sandbox and print what each statement committed")
    sandbox.add_argument(
        "--serve",
        action="store_true",
        help="serve the sandbox's catalog over Iceberg REST on 127.0.0.1 for other engines (PyIceberg, DuckDB, "
        "another icelens) until ctrl+c, and print how to connect. ICELENS_SERVE_TOKEN fixes the token.",
    )
    sandbox.add_argument("--port", type=_port, help="the port --serve listens on (default: any free one)")

    display = parser.add_argument_group("display")
    display.add_argument("--limit", type=_whole, default=200, help="rows to read in the Data pane (default 200)")
    display.add_argument("--width", type=int, default=44, help="widest cell before truncation (default 44)")

    headless = parser.add_argument_group("headless")
    headless.add_argument("--list", nargs="?", const="", metavar="NAMESPACE", help="list namespaces, or one namespace")
    headless.add_argument("--pane", choices=PANE_NAMES, help="print one pane for TABLE and exit")
    headless.add_argument("--sql", help="run read-only SQL against the attached catalog and exit")
    headless.add_argument(
        "--allow-writes",
        action="store_true",
        help="attach the catalog read-write, so --sql can change it",
    )
    headless.add_argument("--snapshot", type=int, help="snapshot id for --pane (time travel)")
    headless.add_argument("--where", help="row filter for the data pane")
    headless.add_argument("--csv", action="store_true", help="print CSV instead of a table")
    headless.add_argument("--json", action="store_true", help="print one JSON object: typed rows, the note, and errors a program can branch on")
    headless.add_argument("--warehouses", action="store_true", help="list the warehouses this catalog offers")
    headless.add_argument("--catalogs", action="store_true", help="list the catalogs named in ~/.pyiceberg.yaml")
    headless.add_argument(
        "--explain",
        nargs="?",
        const="",
        metavar="TERM",
        help="explain one Iceberg term, or list them all. The same corpus the guide rail uses.",
    )
    parser.add_argument(
        "--trace",
        type=Path,
        metavar="FILE",
        help="append what icelens asks the catalog, the store and DuckDB, with timings, to FILE; "
        "secrets scrubbed. Also ICELENS_TRACE=FILE",
    )
    parser.add_argument(
        "--skill",
        action="store_true",
        help="print the agent skill (SKILL.md) that teaches a coding agent to drive icelens, and exit. "
        "Install it with: mkdir -p ~/.claude/skills/icelens && icelens --skill > ~/.claude/skills/icelens/SKILL.md",
    )
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    return parser


#: Module-level so `_finite` does not rebuild it per cell on a million-row payload.
_INF = float("inf")


def _jsonable(value: Any) -> Any:
    """The last resort for a value json cannot encode; bytes are hex, everything else is text."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return str(value)


def _finite(value: Any) -> Any:
    """`value` with every non-finite float turned into its name.

    JSON has no NaN and no infinity. Python writes them as the bare words `NaN`, `Infinity`
    and `-Infinity`, which Python itself reads back and almost nothing else does: node,
    Go's encoding/json and serde_json all reject the document outright, so one NaN in one
    cell loses the caller the whole answer. The names are strings instead, which is the same
    thing this tool already does to a decimal and a date, and `columns` still says the column
    is a double - so a reader that cares can tell `"NaN"` from a string column's "NaN".
    """
    if isinstance(value, float):
        if value != value:
            return "NaN"
        if value == _INF:
            return "Infinity"
        if value == -_INF:
            return "-Infinity"
        return value
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def _emit(kind: str, rows: list[dict[str, Any]], note: str = "", **extra: Any) -> int:
    """One JSON object on stdout: what was asked for, what came back, and how much of it."""
    payload: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "kind": kind}
    payload.update({k: v for k, v in extra.items() if v is not None})
    if note:
        payload["note"] = safe(scrub(note))
    payload["count"] = len(rows)
    payload["rows"] = [_finite(row) for row in rows]
    # allow_nan=False is the backstop, not the mechanism: if a non-finite value ever reaches
    # here by a route `_finite` does not walk, this raises instead of printing a document no
    # strict parser will read.
    print(json.dumps(payload, default=_jsonable, allow_nan=False))
    return 0


def _emit_error(exc: BaseException, **extra: Any) -> None:
    """A failure a caller can branch on, on stderr, leaving stdout empty."""
    error: dict[str, Any] = {"kind": type(exc).__name__, "message": safe(scrub(str(exc)))}
    error.update({k: v for k, v in extra.items() if v is not None})
    print(json.dumps({"schema_version": SCHEMA_VERSION, "error": error}, default=_jsonable), file=sys.stderr)


def _print_explain(needle: str, as_json: bool = False) -> int:
    """The guide rail, headless. Same corpus, so a term cannot be right in one and stale in the other."""
    keys = {id(term): key for key, term in terms.TERMS.items()}

    def record(term: terms.Term, full: bool) -> dict[str, Any]:
        entry: dict[str, Any] = {"key": keys.get(id(term), ""), "name": term.name, "gloss": term.gloss}
        if full:
            entry["body"] = term.body
            entry["where"] = term.where
            entry["see"] = [terms.TERMS[k].name for k in term.see if k in terms.TERMS]
        return entry

    if not needle:
        ordered = sorted(terms.TERMS.values(), key=lambda t: t.name.lower())
        if as_json:
            # Glosses only, as in the printed listing: a caller discovering the vocabulary
            # wants the index, and `--explain <term> --json` is one call away for the rest.
            return _emit("explain", [record(t, full=False) for t in ordered])
        width = max(len(term.name) for term in terms.TERMS.values())
        for term in ordered:
            print(f"{term.name:<{width}}  {term.gloss}")
        print(f"\n{len(terms.TERMS)} terms. icelens --explain <term> for one in full.", file=sys.stderr)
        return 0
    found = terms.search(needle)
    if as_json:
        # No cap and no truncation notice: a program can read all of them, and silently
        # dropping matches is how a caller concludes a term does not exist.
        _emit("explain", [record(t, full=True) for t in found], query=needle)
        return 0 if found else 1
    if not found:
        print(f"icelens: nothing about {needle!r}. `icelens --explain` lists every term.", file=sys.stderr)
        return 1
    for term in found[:5]:
        print(term.name)
        print("  " + term.gloss)
        if term.body:
            print()
            for line in _wrap(term.body, 92):
                print("  " + line)
        if term.where:
            print(f"\n  where: {term.where}")
        if term.see:
            names = ", ".join(terms.TERMS[k].name for k in term.see if k in terms.TERMS)
            print(f"  next:  {names}")
        print()
    if len(found) > 5:
        print(f"({len(found) - 5} more matches)", file=sys.stderr)
    return 0


def _wrap(text: str, width: int) -> list[str]:
    import textwrap

    return textwrap.wrap(text, width)


def _note(text: str) -> None:
    """Anything that came from a catalog, on its way to a terminal.

    Notes and error messages carry table names, partition values and server text. Printing them
    raw hands a remote catalog the ability to move the cursor, repaint the screen or reverse the
    reading order of the line - so control codes are replaced and any known credential scrubbed
    before the bytes leave.
    """
    print(safe(scrub(str(text))), file=sys.stderr)


def properties(pairs: list[str]) -> dict[str, str]:
    """`-p KEY=VALUE`, as a dict - less any `icelens.` key, which marks a catalog icelens made."""
    out: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            # IcelensError, not SystemExit: this is raised inside main's try, so `--json`
            # reports it the same way it reports every other failure. A program should not
            # have to parse English for one class of mistake and JSON for the rest.
            raise IcelensError(f"--property expects KEY=VALUE, got {pair!r}")
        out[key.strip()] = value.strip()
    return without_own_keys(out)


def main(argv: list[str] | None = None) -> int:
    try:
        code = _main(argv)
        sys.stdout.flush()  # a reader that left early is found here, not in the flush at exit
        return code
    except BrokenPipeError:
        # `icelens --pane data t | head -1`: the reader has all it wanted and left. Python's
        # advice: point stdout at devnull, or the flush at exit raises a second time and
        # prints a traceback about a pipe nobody is reading. 141 is what a shell reports for
        # a process SIGPIPE stopped, which is what a C tool in the same place would exit with.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141
    finally:
        trace.stop()


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    parser.json_errors = _wants_json(sys.argv[1:] if argv is None else argv)
    args = parser.parse_args(argv)

    trace_path = args.trace or (Path(os.environ["ICELENS_TRACE"]) if os.environ.get("ICELENS_TRACE") else None)
    if trace_path is not None:
        try:
            trace.start(trace_path.expanduser())
        except OSError as exc:
            parser.error(f"--trace cannot write {trace_path}: {exc.strerror or exc}")

    if args.version:
        from importlib.metadata import version

        print(version("icelens"))
        return 0

    if args.skill:
        # Shipped inside the package, so the skill an agent reads matches the flags it calls.
        from importlib.resources import files

        sys.stdout.write((files("icelens") / "skills" / "icelens" / "SKILL.md").read_text(encoding="utf-8"))
        return 0

    if args.explain is not None:
        return _print_explain(args.explain, as_json=args.json)

    if args.catalogs:
        return _print_catalogs(as_json=args.json)

    # Read-only is the default everywhere, the app included, because that is what the tool
    # promises. `--allow-writes` is the one way past it, and a sandbox - this tool's own
    # scratch catalog, holding nothing anybody can lose - is the one place it is not needed.
    lake: Lake | None = None
    serving: Any = None
    try:
        if args.env_file is not None and not args.env_file.is_file():
            # A file the caller named and that is not there is a mistake worth saying out
            # loud. Silently falling through to the environment produces a "URI missing"
            # three layers down, about the one thing they thought they had supplied.
            raise IcelensError(f"no such --env-file: {args.env_file}")
        if args.env_file is not None and args.no_env:
            # --no-env used to win in silence, so the file the caller named was never read and
            # the failure arrived as "cannot open catalog 'default': URI missing".
            raise IcelensError("--no-env ignores every .env and --env-file names one to read; pass one or the other")
        if args.demo and args.rest:
            raise IcelensError("--demo writes its table with PyIceberg into an offline sandbox; --rest sandboxes are written by DuckDB, with --write")
        if args.v3 and args.rest:
            raise IcelensError("--v3 serves a sandbox from this machine; --rest puts one in a catalog you run, which writes v3 already")
        if args.port is not None and not args.serve:
            raise IcelensError("--port is where --serve listens; pass --serve too")
        if args.serve and args.rest:
            raise IcelensError("--rest puts the sandbox in a catalog you already run, which serves it; --serve is for a sandbox icelens keeps")
        if args.serve and _headless(args):
            raise IcelensError("--serve keeps running until ctrl+c; it does not combine with --list, --pane, --sql, --write or --warehouses")
        if args.demo and not args.sandbox:
            args.sandbox = "demo-v3" if args.v3 else "demo"
        if args.serve and not args.sandbox:
            raise IcelensError("--serve shares a sandbox: say which, with --sandbox NAME (or --demo)")
        if args.v3 and not args.sandbox:
            raise IcelensError("--v3 makes a sandbox format-version 3: say which, with --sandbox NAME (or --demo)")
        if args.sandbox and args.rest:
            # The v3 sandbox: somebody's running catalog, one namespace of it, DuckDB doing
            # the writing. The connection flags mean here what they mean everywhere else.
            if args.sandbox_dir:
                raise IcelensError("--sandbox-dir is for an offline sandbox; --rest uses the catalog you connect to")
            base = discover(
                uri=args.uri,
                warehouse=args.warehouse,
                profile=args.profile,
                properties=properties(args.property),
                env_file=args.env_file,
                search_dotenv=not args.no_env,
            )
            if base.catalog_type == "unknown":
                # Left to itself this surfaces four layers down as "cannot open catalog
                # 'icelens' (unknown): URI missing", which names neither flag and reads like a
                # bug in a tool the reader was told needs no catalog. --sandbox says "no
                # server"; --rest quietly means the opposite, so --rest has to say so.
                raise IcelensError(
                    "--rest puts the sandbox inside a catalog you are running, so it needs the usual connection "
                    "settings: --uri and --warehouse, a .env in this directory, or --catalog PROFILE from "
                    "~/.pyiceberg.yaml. Plain --sandbox needs none of them - it makes its own catalog in a "
                    "directory, at format-version 2." + (f" icelens {base.ignored}." if base.ignored else "")
                )
            target = rest_sandbox_target(args.sandbox, base)
        elif args.sandbox:
            # An offline sandbox is a catalog of this tool's own making, so there is nothing
            # for the connection flags to do. Ignoring them silently would look like a
            # connection that failed over to something local, the wrong impression exactly.
            if args.uri or args.warehouse or args.profile or args.property or args.env_file is not None:
                raise IcelensError("--sandbox makes its own catalog; it takes no connection settings. Use --rest to put it in one you run.")
            if args.sandbox_reset:
                reset_sandbox(args.sandbox, args.sandbox_dir)
            if args.serve:
                # Started before the sandbox is opened, so a v3 sandbox - which serves itself -
                # uses this server, on the asked-for port and token, rather than one of its own.
                serving = _start_serving(args)
            target = sandbox_target(args.sandbox, args.sandbox_dir, version=3 if args.v3 else None)
        else:
            if args.sandbox_reset or args.sandbox_dir or args.write or args.rest:
                raise IcelensError("--sandbox-reset, --sandbox-dir, --rest and --write need --sandbox")
            target = discover(
                uri=args.uri,
                warehouse=args.warehouse,
                profile=args.profile,
                properties=properties(args.property),
                env_file=args.env_file,
                search_dotenv=not args.no_env,
            )
            if target.unconfigured and args.profile:
                # Asked for by name, so the mistake is the name - not that nothing is set up.
                known = ", ".join(sorted(list_profiles())) or "none"
                raise IcelensError(f"no catalog named {args.profile!r} in ~/.pyiceberg.yaml (it names: {known})")
            if target.unconfigured and (_headless(args) or not _interactive()):
                # In a terminal the app asks what to open instead (`_welcome`); anywhere
                # else there is nobody to ask, so the answer is said rather than offered.
                raise NoCatalog(target.ignored)
            if target.ignored and _headless(args) and not args.json:
                # Something else was found and is being opened, so the refusal is context: said
                # once, on stderr, rather than left for "why is it not using my .env".
                _note(f"icelens: {target.ignored}")
        lake = Lake(target, read_only=not args.allow_writes)
        trace.log.info("catalog: %s", target.label if target.origin == target.label else f"{target.label}, {target.origin}")
        if args.demo:
            seeded = _seed(lake)
            if not args.json:
                print(f"icelens: {seeded.line()}", file=sys.stderr)
            if not (args.table or _headless(args)):
                args.table = seeded.table
        if args.sandbox_reset and lake.is_rest_sandbox:
            # This one needs the catalog open, so it happens here rather than beside the
            # offline reset above. It drops tables in one namespace and can reach no other.
            dropped = lake.empty_sandbox_namespace()
            # stderr, because a reset is context and stdout belongs to whatever comes next -
            # and not at all under --json, where stderr is for the one error object.
            if not args.json:
                print(f"icelens: emptied {safe(lake.sandbox_namespace)}, {len(dropped)} tables", file=sys.stderr)
        if args.serve:
            return _serve(lake, serving, args)
        if args.write:
            return _print_written(lake, args.write, args)
        if args.warehouses:
            return _print_warehouses(lake, args)
        if args.list is not None:
            return _print_list(lake, args.list, args)
        if args.sql:
            if not args.allow_writes:
                # Here and not only in `Lake.query`, which lets a sandbox's SQL through: `--sql`
                # is the read-only half everywhere, and in a sandbox `COPY … TO '/tmp/x.csv'`
                # still wrote the file. `--write` is how a sandbox is written to.
                try:
                    only_reads(args.sql)
                except IcelensError as exc:
                    if lake.is_sandbox:
                        raise IcelensError(f"{exc} In a sandbox, --write commits a statement and says what it did.") from exc
                    raise
                stays_in_catalog(args.sql, lake.sql_finds)
            data = lake.query(args.sql)
            return _print_pane(Pane(data), args, "sql", sql=args.sql)
        if args.pane:
            if not args.table:
                parser.error("--pane needs a table: icelens --pane snapshots demo.orders")
            view = View(lake, args.table, snapshot_id=args.snapshot, limit=args.limit, where=args.where)
            try:
                pane = model.PANES[args.pane](view)
            except IcelensError:
                # Asked only once the table has failed, so a table costs no extra catalog call.
                if not lake.is_view(args.table):
                    raise
                if args.pane != "overview":
                    raise IcelensError(f"{args.table} is a view: it has a definition, not {args.pane} - use --pane overview") from None
                return _print_pane(model.view_definition(lake, args.table), args, "view", view=args.table)
            return _print_pane(
                pane, args, args.pane, table=args.table, snapshot=view.effective_snapshot_id, where=args.where
            )
        return _run_app(lake, args)
    except NeedsWarehouse as exc:
        trace.log.info("stopped: %s", exc)
        if args.json:
            _emit_error(exc, warehouses=list(exc.options) or None)
            return 1
        _note(f"icelens: {exc}")
        if exc.options:
            print("\nwarehouses this catalog offers:", file=sys.stderr)
            for name in exc.options:
                _note(f"  {name}")
        return 1
    except IcelensError as exc:
        trace.log.info("stopped: %s", exc)
        # A write that failed partway has commits behind it that stand, and they are said
        # before the failure is, in the shape a whole write would have said them.
        if args.json:
            _emit_error(exc, written=[dict(vars(item)) for item in exc.written] or None)
            return 1
        for item in exc.written:
            print(safe(item.line()))
        _note(f"icelens: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if lake is not None:
            lake.close()


#: The shortest `ICELENS_SERVE_TOKEN` taken. The token is all that stands between the sandbox and
#: every other process on this machine, and the one icelens makes for itself is 43 characters.
MIN_SERVE_TOKEN = 16


def _start_serving(args: argparse.Namespace) -> Any:
    from icelens import rest

    token = os.environ.get("ICELENS_SERVE_TOKEN") or None
    if token is not None and len(token) < MIN_SERVE_TOKEN:
        raise IcelensError(
            f"ICELENS_SERVE_TOKEN is {len(token)} characters; a token anyone on this machine could guess "
            f"opens the sandbox to them, so it takes {MIN_SERVE_TOKEN} or more. Unset it for a random one."
        )
    try:
        return rest.serve(make_sandbox_home(args.sandbox, args.sandbox_dir), port=args.port or 0, token=token)
    except OSError as exc:
        raise IcelensError(f"cannot listen on 127.0.0.1:{args.port or 0}: {exc.strerror or exc}") from exc


def _seed(lake: Lake) -> demo.Seeded:
    """The demo table, written; a failure said in one line rather than as PyIceberg's traceback."""
    try:
        return demo.seed(lake)
    except IcelensError:
        raise
    except Exception as exc:  # noqa: BLE001 - PyIceberg, pyarrow and the filesystem each raise their own
        reason = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise IcelensError(f"cannot write the demo table into sandbox {lake.target.sandbox!r}: {reason}") from exc


def connection_details(name: str, running: Any, format_version: int) -> dict[str, Any]:
    """How another engine reaches a served sandbox: the settings, and each client's spelling of them."""
    from icelens import rest

    alias = name.replace("-", "_").replace(".", "_")
    return {
        "sandbox": name,
        "format_version": format_version,
        "uri": running.uri,
        "warehouse": rest.PREFIX,
        "token": running.token,
        "warehouse_path": str(running.home / "warehouse"),
        "pyiceberg": {"type": "rest", "uri": running.uri, "warehouse": rest.PREFIX, "token": running.token},
        "duckdb": (
            f"ATTACH {literal(rest.PREFIX)} AS {quote(alias)} (TYPE ICEBERG, ENDPOINT {literal(running.uri)}, "
            f"TOKEN {literal(running.token)}, ACCESS_DELEGATION_MODE 'none')"
        ),
    }


def _serve(lake: Lake, running: Any, args: argparse.Namespace) -> int:
    """Print how to connect, then keep the catalog up until ctrl+c.

    The token is printed on purpose - it is the one thing a client needs - and on stdout only.
    The server listens on 127.0.0.1 and nowhere else, and the data files are paths on this
    machine, so the clients it can serve are the ones running here.
    """
    import signal

    name = lake.target.sandbox
    details = connection_details(name, running, 3 if lake.is_served_sandbox else 2)

    def interrupt(*_: Any) -> None:
        raise KeyboardInterrupt

    # Before the address is printed, not after: whoever reads it may send SIGTERM at once, and
    # SIGTERM's default action kills the process without stopping the server. Measured: one
    # start in twelve died that way when the handler was installed after printing.
    # It raises rather than setting an Event the wait polls: a handler runs on the main thread,
    # which may be inside that Event's own lock at the moment, and `set()` then waits on it for
    # ever. Measured: one SIGTERM in 250 left the server running and deaf to the next.
    previous = signal.signal(signal.SIGTERM, interrupt)
    try:
        # The printing is inside the `try` with the wait: a script that reads the address and
        # signals at once can land its ctrl+c before the wait has begun, and that is a stop like
        # any other - not exit 130 with the server never told to stop.
        try:
            if args.json:
                _emit("serve", [details], note="serving until interrupted")
            else:
                print(_serving_text(details), flush=True)
            sys.stdout.flush()
            _wait_for_interrupt()
        except KeyboardInterrupt:
            pass
    finally:
        signal.signal(signal.SIGTERM, previous)
    running.stop()
    if not args.json:
        print(f"icelens: stopped serving sandbox {safe(name)!r}", file=sys.stderr)
    return 0


def _serving_text(d: dict[str, Any]) -> str:
    key = json.dumps(d["sandbox"])
    return f"""icelens: serving sandbox {d["sandbox"]!r} (format-version {d["format_version"]}) on this machine only - ctrl+c to stop

  uri        {d["uri"]}
  warehouse  {d["warehouse"]}
  token      {d["token"]}

PyIceberg, and icelens --catalog {d["sandbox"]}, from ~/.pyiceberg.yaml:

catalog:
  {key}:
    type: rest
    uri: {d["uri"]}
    warehouse: {d["warehouse"]}
    token: {d["token"]}

DuckDB:

  INSTALL iceberg; LOAD iceberg;
  {d["duckdb"]};

Anyone on this machine with the token can read and write the sandbox. Its data files are under
{d["warehouse_path"]}, so a client has to run here to read them.{_v3_note(d)}"""


def _v3_note(d: dict[str, Any]) -> str:
    """How a client gets a v3 table here, said before one of them gets a v2 table instead.

    The sandbox is v3 but the catalog does not impose it: a table gets the version its client
    asks for. Measured: DuckDB 1.5.5 asks for format-version 2 explicitly unless told otherwise,
    and PyIceberg 0.12, which asks for nothing, cannot append to a v3 table - so a catalog
    default of 3 would only have made PyIceberg's tables unwritable.
    """
    if d["format_version"] < 3:
        return ""
    return """

A table is v3 only if its client asks for it (icelens --write does):
  DuckDB   CREATE TABLE … WITH ("format-version" = 3)    - without it, DuckDB asks for v2
  Spark    CREATE TABLE … TBLPROPERTIES ('format-version'='3')"""


def _wait_for_interrupt() -> None:
    """Block until ctrl+c or SIGTERM; both arrive as KeyboardInterrupt, which the caller takes as "stop".

    In slices, because a wait with no timeout ignores ctrl+c on Windows.
    """
    import time

    while True:
        time.sleep(0.5)


def _interactive() -> bool:
    """Whether there is a person at a terminal to show the app to, or to ask a question of."""
    return sys.stdout.isatty()


def _run_app(lake: Lake, args: argparse.Namespace) -> int:
    if not _interactive():
        refusal = "not a terminal - use --list, --pane or --sql for non-interactive output"
        if args.json:
            _emit_error(UsageError(refusal))
        else:
            print(f"icelens: {refusal}", file=sys.stderr)
        return 2
    from icelens.app import IcelensApp

    if lake.target.unconfigured:
        return _welcome(args)
    IcelensApp(lake, ident=args.table, limit=args.limit, width=args.width).run()
    return 0


def _headless(args: argparse.Namespace) -> bool:
    """Whether this run prints something and exits, rather than opening the app."""
    return bool(args.write or args.warehouses or args.list is not None or args.sql or args.pane)


def _welcome(args: argparse.Namespace) -> int:
    """Nothing is configured, and there is a person here: ask them what to open.

    The answer is opened in place - a sandbox made and, for a demo, filled - so the
    first run ends in the app looking at a table, not in a list of flags to go and read about.
    """
    from icelens.app import IcelensApp
    from icelens.welcome import CONNECT, Welcome, duckdb_writes_iceberg, fresh_name

    sandboxes = list_sandboxes(args.sandbox_dir)
    choice = Welcome(sandboxes, duckdb_writes_iceberg()).run() or ""
    if choice == "connect":
        print(CONNECT)
        return 0
    if not choice:
        return 0
    name = {"demo": "demo", "demo-v3": "demo-v3", "empty": fresh_name(sandboxes)}.get(choice) or choice.removeprefix("sandbox:")
    version = 3 if choice == "demo-v3" else None
    local = Lake(sandbox_target(name, args.sandbox_dir, version=version), read_only=not args.allow_writes)
    try:
        # Made now rather than when the tree first asks, so a sandbox chosen is a sandbox that
        # exists - and is offered as one to open next time, even if it was never written to.
        local.catalog  # noqa: B018 - opening the catalog is what creates it
        ident = args.table
        if choice in ("demo", "demo-v3"):
            ident = ident or _seed(local).table
        IcelensApp(local, ident=ident, limit=args.limit, width=args.width).run()
    finally:
        local.close()
    return 0


def _print_catalogs(as_json: bool = False) -> int:
    """The catalogs this machine already knows about, whatever their type.

    Printed before connecting to anything, because the usual reason to ask is that you do not
    yet know which one you want.
    """
    profiles = list_profiles()
    if as_json:
        # redact_uri here too: a credential in a config file is still a credential in a pipe.
        rows = [
            {
                "catalog": name,
                "type": props.get("type") or ("rest" if props.get("uri", "").startswith("http") else None),
                "uri": redact_uri(props.get("uri") or "") or None,
                "warehouse": props.get("warehouse"),
            }
            for name, props in sorted(profiles.items())
        ]
        _emit("catalogs", rows)
        return 0 if profiles else 1
    if not profiles:
        print("icelens: no catalogs in ~/.pyiceberg.yaml", file=sys.stderr)
        return 1
    width = max(len(name) for name in profiles)
    for name, props in sorted(profiles.items()):
        kind = props.get("type") or ("rest" if props.get("uri", "").startswith("http") else "")
        # redact_uri, because `--catalogs` is the command people paste into an issue.
        where = redact_uri(props.get("uri") or "") or props.get("warehouse") or ""
        print(safe(f"{name:<{width}}  {kind:<10}  {where}"))
    return 0


def _print_warehouses(lake: Lake, args: argparse.Namespace) -> int:
    names = lake.warehouses()
    if args.json:
        _emit("warehouses", [{"warehouse": name} for name in names])
        return 0 if names else 1
    if not names:
        print("icelens: this catalog does not list its warehouses", file=sys.stderr)
        return 1
    print("\n".join(safe(name) for name in names))
    return 0


def _print_written(lake: Lake, sql: str, args: argparse.Namespace) -> int:
    """Run write SQL against the sandbox and report the snapshot each statement produced.

    One record per commit rather than one per statement: a `SELECT` mixed in with the writes
    commits nothing and so has nothing to report, and one DELETE can produce a snapshot that
    says it *added* rows, which is the whole lesson and is not a thing to round off.
    """
    written = lake.write(sql)
    if args.json:
        return _emit("written", [dict(vars(item)) for item in written])
    for item in written:
        print(safe(item.line()))
    return 0


def _print_list(lake: Lake, namespace: str, args: argparse.Namespace) -> int:
    if namespace:
        return _print_pane(model.namespace_overview(lake, namespace), args, "tables", namespace=namespace)
    return _print_pane(model.catalog_overview(lake), args, "namespaces")


def _print_pane(pane: Pane, args: argparse.Namespace, kind: str, **extra: Any) -> int:
    """One pane, in whichever of the three shapes was asked for.

    The note is context, not data, so it goes to stderr for a person and into the object for
    a program - never into stdout beside the rows, where it would corrupt a CSV or a pipe.
    """
    if getattr(args, "json", False):
        # A pane with no separate facts is one whose arrow table is already typed, so its
        # column types are worth publishing: JSON has no date and no decimal, and a reader
        # that gets "73.34" should not have to guess whether the column is a string.
        if pane.facts is None:
            extra["columns"] = [{"name": f.name, "type": str(f.type)} for f in pane.data.schema]
        return _emit(kind, pane.records(), pane.note, **extra)
    if getattr(args, "csv", False):
        data = csv_bytes(pane.data)
        if sys.stdout.isatty():
            # Exact bytes are the CSV contract for a file or a pipe. A terminal is a reader, and
            # a value out of someone's Parquet can hold a sequence that writes the clipboard.
            text = data.decode("utf-8", errors="replace")
            data = "\n".join(safe(line) for line in text.split("\n")).encode()
        sys.stdout.buffer.write(data)
        return 0
    if pane.note:
        _note(pane.note)
    _rich_print(pane.data, getattr(args, "width", 44))
    return 0


def _rich_print(data, width: int) -> None:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    # Piped output has no width of its own, and rich's 80-column default silently drops
    # columns off the right - exactly the columns a metadata pane exists to show. Give a
    # redirected stream room, and let the pager or COLUMNS decide otherwise.
    piped_width = int(os.environ.get("COLUMNS") or 0) or 220
    console = Console(width=None if sys.stdout.isatty() else piped_width)
    columns, rows = grid(data, width)
    table = Table(box=None, pad_edge=False, header_style="bold")
    for name in columns:
        # Crop rather than wrap: a metadata listing that reflows into paragraphs is
        # unreadable, and --csv / --json are there when the whole value is wanted.
        # escape: a column name is a field name from someone else's schema, and rich reads
        # square brackets in a header as markup.
        table.add_column(escape(safe(name)) if name.strip() else " ", overflow="ellipsis", no_wrap=True)
    for row in rows:
        table.add_row(*row)
    console.print(table)
    if data.num_rows == 0:
        console.print("[dim](no rows)[/dim]")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

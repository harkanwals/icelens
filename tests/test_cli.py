"""The headless surface: the half of icelens that works in a pipe."""

from __future__ import annotations

import json

import pytest

from icelens import terms
from icelens.catalog import IcelensError, Lake, only_reads
from icelens.cli import PANE_NAMES, _finite, main


def argv(lake: Lake, *rest: str) -> list[str]:
    """Point the CLI at the test catalog without going near a .env or a config file."""
    out = ["--no-env"]
    for key, value in lake.target.properties.items():
        out += ["-p", f"{key}={value}"]
    return [*out, *rest]


def test_list_prints_namespaces(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--list")) == 0
    out = capsys.readouterr()
    assert "demo" in out.out
    assert "1 namespaces, 2 tables" in out.err  # the note goes to stderr, so pipes stay clean


def test_list_one_namespace(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--list", "demo")) == 0
    out = capsys.readouterr().out
    assert "events" in out and "empty" in out


def test_pane_prints_a_table(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "snapshots", "demo.events")) == 0
    captured = capsys.readouterr()
    assert captured.out.count("append") == 2
    assert "3 snapshots" in captured.err


def test_pane_at_a_snapshot(lake: Lake, capsys) -> None:
    main(argv(lake, "--pane", "snapshots", "demo.events", "--json"))
    first = json.loads(capsys.readouterr().out)["rows"][0]
    assert main(argv(lake, "--pane", "data", "demo.events", "--snapshot", str(first["snapshot id"]), "--json")) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 50  # the first snapshot, before the second append and the delete


def test_csv_is_untruncated(lake: Lake, capfd) -> None:
    assert main(argv(lake, "--pane", "files", "demo.events", "--csv")) == 0
    out = capfd.readouterr().out
    header, *body = out.strip().splitlines()
    assert "path" in header
    assert body and "…" not in out  # the grid truncates for display; CSV never does


def test_a_missing_table_is_an_error_not_a_traceback(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "overview", "demo.nope")) == 1
    assert capsys.readouterr().err.startswith("icelens: ")


def test_pane_without_a_table_is_a_usage_error(lake: Lake) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv(lake, "--pane", "overview"))
    assert raised.value.code == 2


@pytest.mark.duckdb_extensions
def test_sql_works_without_a_rest_catalog(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--sql", 'SELECT count(*) AS n FROM icelens."demo"."events"')) == 0
    assert "55" in capsys.readouterr().out


def test_the_ui_refuses_a_pipe(lake: Lake, capsys) -> None:
    assert main(argv(lake)) == 2
    assert "not a terminal" in capsys.readouterr().err


def test_version(capsys) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip()[0].isdigit()


def test_property_without_a_value_is_rejected(lake: Lake, capsys) -> None:
    assert main(["--no-env", "-p", "nonsense"]) == 1
    assert "KEY=VALUE" in capsys.readouterr().err


def test_a_rejected_property_is_json_when_json_was_asked_for(lake: Lake, capsys) -> None:
    # It is raised before the catalog is even opened, which is exactly when a caller has the
    # least context - so it has to arrive in the shape the caller asked for.
    assert main(["--no-env", "--json", "-p", "nonsense"]) == 1
    caught = capsys.readouterr()
    assert caught.out == ""
    assert json.loads(caught.err)["error"]["kind"] == "IcelensError"


def test_an_env_file_that_is_not_there_says_so(tmp_path, capsys) -> None:
    missing = tmp_path / "nope.env"
    assert main(["--json", "--env-file", str(missing), "--list"]) == 1
    assert "no such --env-file" in json.loads(capsys.readouterr().err)["error"]["message"]


def test_a_negative_limit_is_refused_at_the_door(lake: Lake) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["--no-env", "--limit", "-5", "--pane", "data", "demo.events"])
    assert caught.value.code == 2


# -- every pane, headless ---------------------------------------------------------------------


@pytest.mark.parametrize("pane", PANE_NAMES)
@pytest.mark.parametrize("table", ["demo.events", "demo.empty"])
def test_every_pane_prints_for_every_table(lake: Lake, capsys, pane: str, table: str) -> None:
    """The headless half has the same job as the UI: a snapshotless table is a state, not a crash."""
    assert main(argv(lake, "--pane", pane, table)) == 0
    out = capsys.readouterr()
    assert "Traceback" not in out.err
    assert out.out or "no snapshots" in out.err or "unpartitioned" in out.err


@pytest.mark.parametrize("shape", ["--csv", "--json"])
def test_a_pane_survives_both_machine_formats(lake: Lake, capsys, shape: str) -> None:
    assert main(argv(lake, "--pane", "snapshots", shape, "demo.events")) == 0
    out = capsys.readouterr().out
    assert out.strip()
    if shape == "--json":
        assert json.loads(out)["rows"]


def test_a_snapshot_that_is_not_there_is_an_error_a_person_can_read(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "overview", "--snapshot", "1", "demo.events")) == 1
    err = capsys.readouterr().err
    assert "not in demo.events" in err and "Traceback" not in err


@pytest.mark.duckdb_extensions
def test_a_where_clause_that_is_not_sql_is_an_error_not_a_traceback(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "--where", "id >>> banana", "demo.events")) == 1
    assert "Traceback" not in capsys.readouterr().err


def test_where_narrows_the_data_pane(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "--where", "id > 55", "--csv", "demo.events")) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 6  # a header and the five surviving rows


# -- the guide, headless ------------------------------------------------------------------------


def test_explain_needs_no_catalog_at_all(capsys) -> None:
    """`--explain` is dispatched before any connection: the vocabulary is not the catalog's."""
    assert main(["--explain", "manifest list"]) == 0
    out = capsys.readouterr().out
    assert "Manifest list" in out
    assert "where:" in out and "next:" in out


def test_explain_lists_the_whole_corpus(capsys) -> None:
    assert main(["--explain"]) == 0
    out = capsys.readouterr()
    assert len(out.out.strip().splitlines()) == len(terms.TERMS)
    assert f"{len(terms.TERMS)} terms" in out.err


def test_explain_matches_on_the_gloss_as_well_as_the_name(capsys) -> None:
    assert main(["--explain", "deletion"]) == 0
    assert "Deletion vector" in capsys.readouterr().out


def test_explain_a_word_that_is_not_iceberg(capsys) -> None:
    assert main(["--explain", "kubernetes"]) == 1
    err = capsys.readouterr().err
    assert "nothing about 'kubernetes'" in err
    assert "--explain" in err  # and it says how to find out what there is


def test_explain_is_case_insensitive_and_takes_the_key_or_the_name(capsys) -> None:
    for spelling in ("manifest-list", "Manifest list", "MANIFEST LIST"):
        assert main(["--explain", spelling]) == 0
        assert "Manifest list" in capsys.readouterr().out


# -- the machine-readable half ------------------------------------------------------------------


def one(capsys) -> dict:
    """The single JSON object `--json` prints, parsed."""
    return json.loads(capsys.readouterr().out)


def test_json_is_one_object_not_a_stream(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "snapshots", "demo.events", "--json")) == 0
    body = one(capsys)
    assert body["schema_version"] == 1
    assert body["kind"] == "snapshots"
    assert body["table"] == "demo.events"
    assert body["count"] == len(body["rows"]) == 3


def test_json_carries_the_note_a_person_would_have_read(lake: Lake, capsys) -> None:
    """The note says when a pane stopped early. Dropping it from --json hides a truncation."""
    assert main(argv(lake, "--pane", "snapshots", "demo.events", "--json")) == 0
    assert "3 snapshots" in one(capsys)["note"]


def test_counts_are_numbers_not_formatted_text(lake: Lake, capsys) -> None:
    main(argv(lake, "--pane", "snapshots", "demo.events", "--json"))
    row = one(capsys)["rows"][0]
    assert row["+rows"] == 50 and isinstance(row["+rows"], int)
    assert isinstance(row["snapshot id"], int)
    assert isinstance(row["committed at"], int)  # epoch milliseconds, not "2026-09-18 03:59:20"
    assert row["parent"] is None  # the first snapshot has no parent; "" is not the same fact


def test_sizes_are_bytes_not_prose(lake: Lake, capsys) -> None:
    main(argv(lake, "--pane", "files", "demo.events", "--json"))
    row = one(capsys)["rows"][0]
    assert isinstance(row["size"], int) and row["size"] > 0
    assert isinstance(row["rows"], int)


def test_overview_splits_the_sentences_it_shows_a_person(lake: Lake, capsys) -> None:
    """The pane shows "1 (current id 0)"; a caller gets the two numbers."""
    main(argv(lake, "--pane", "overview", "demo.events", "--json"))
    body = one(capsys)
    assert body["count"] == 1
    facts = body["rows"][0]
    assert isinstance(facts["schemas"], int)
    assert isinstance(facts["current_schema_id"], int)
    assert isinstance(facts["total_records"], int)
    assert isinstance(facts["last_updated_ms"], int)
    assert isinstance(facts["partition_spec"], list)
    assert isinstance(facts["properties"], dict)


def test_layout_splits_the_summary_line_it_draws(lake: Lake, capsys) -> None:
    main(argv(lake, "--pane", "layout", "demo.events", "--json"))
    rows = one(capsys)["rows"]
    root = rows[0]
    assert root["kind"] == "table-metadata"
    assert isinstance(root["snapshots"], int) and isinstance(root["schemas"], int)
    files = [r for r in rows if r.get("kind") == "file"]
    assert files and all(isinstance(r["size_bytes"], int) for r in files)
    assert all("depth" in r for r in rows)


def test_schema_says_required_as_a_boolean(lake: Lake, capsys) -> None:
    main(argv(lake, "--pane", "schema", "demo.events", "--json"))
    row = one(capsys)["rows"][0]
    assert isinstance(row["required"], bool) and isinstance(row["identifier"], bool)
    assert isinstance(row["id"], int)
    assert "\u2514" not in row["name"]  # the tree prefix belongs to the display column only


@pytest.mark.parametrize("pane", PANE_NAMES)
def test_every_pane_emits_one_parseable_object(lake: Lake, capsys, pane: str) -> None:
    assert main(argv(lake, "--pane", pane, "demo.events", "--json")) == 0
    body = one(capsys)
    assert body["schema_version"] == 1 and isinstance(body["rows"], list)


def test_an_empty_table_still_emits_an_object(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "files", "demo.empty", "--json")) == 0
    body = one(capsys)
    assert body["rows"] == [] and body["count"] == 0


def test_list_honours_json(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--list", "--json")) == 0
    body = one(capsys)
    assert body["kind"] == "namespaces"
    assert body["rows"][0]["namespace"] == "demo"
    assert body["rows"][0]["tables"] == 2


def test_list_of_one_namespace_keeps_its_numbers(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--list", "demo", "--json")) == 0
    rows = {r["table"]: r for r in one(capsys)["rows"]}
    assert isinstance(rows["events"]["rows"], int)
    assert isinstance(rows["events"]["updated"], int)


@pytest.mark.duckdb_extensions
def test_sql_comes_back_in_the_same_envelope(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--sql", 'SELECT count(*) AS n FROM icelens."demo"."events"', "--json")) == 0
    body = one(capsys)
    assert body["kind"] == "sql" and body["rows"] == [{"n": 55}]


def test_an_error_is_json_when_json_was_asked_for(lake: Lake, capsys) -> None:
    """A program should not have to read English to find out what went wrong."""
    assert main(argv(lake, "--pane", "overview", "demo.nope", "--json")) == 1
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout stays empty, so a pipe is not fed half an answer
    error = json.loads(captured.err)["error"]
    assert error["kind"] and "demo.nope" in error["message"]


@pytest.mark.duckdb_extensions
def test_a_bad_where_clause_is_json_too(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "--where", "id >>> banana", "demo.events", "--json")) == 1
    assert json.loads(capsys.readouterr().err)["error"]["message"]


def test_explain_lists_the_whole_vocabulary(capsys) -> None:
    assert main(["--no-env", "--explain", "--json"]) == 0
    body = one(capsys)
    assert body["kind"] == "explain"
    assert body["count"] == len(terms.TERMS)
    assert all(row["key"] and row["name"] and row["gloss"] for row in body["rows"])
    assert "body" not in body["rows"][0]  # the index is glosses; the full text is one call away


def test_explain_one_term_in_full(capsys) -> None:
    assert main(["--no-env", "--explain", "manifest list", "--json"]) == 0
    body = one(capsys)
    assert body["query"] == "manifest list"
    term = body["rows"][0]
    assert term["body"] and isinstance(term["see"], list)


def test_explain_a_term_that_is_not_there_says_so_without_guessing(capsys) -> None:
    assert main(["--no-env", "--explain", "flux capacitor", "--json"]) == 1
    assert one(capsys)["rows"] == []


# -- the read-only guard ------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT 1; SELECT 2",
        "EXPLAIN SELECT 1",
        'DESCRIBE icelens."demo"."events"',
        "SUMMARIZE SELECT 1",
        "WITH a AS (SELECT 1 AS n) SELECT * FROM a",
    ],
)
def test_read_only_lets_reads_through(sql: str) -> None:
    assert only_reads(sql) == sql


@pytest.mark.parametrize(
    ("sql", "named"),
    [
        ("DROP TABLE x", "DROP"),
        ("SELECT 1; DROP TABLE x", "DROP"),
        ("CREATE TABLE t AS SELECT 1", "CREATE"),
        ("INSERT INTO t VALUES (1)", "INSERT"),
        ("UPDATE t SET a = 1", "UPDATE"),
        ("DELETE FROM t", "DELETE"),
        ("ALTER TABLE t ADD COLUMN c INT", "ALTER"),
        ("COPY (SELECT 1) TO '/tmp/icelens-should-not-write.csv'", "COPY"),
        ("CALL foo()", "CALL"),
    ],
)
def test_read_only_refuses_anything_that_changes_something(sql: str, named: str) -> None:
    with pytest.raises(IcelensError) as caught:
        only_reads(sql)
    assert named in str(caught.value)
    assert "--allow-writes" in str(caught.value)


def test_unparseable_sql_is_duckdbs_to_refuse_not_the_guards(tmp_path) -> None:
    """The guard passes it through; `execute` rejects it with a better message."""
    assert only_reads("this is not sql at all (((") == "this is not sql at all ((("


def test_sql_refuses_to_write_by_default(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--sql", "CREATE TABLE zz AS SELECT 1", "--json")) == 1
    out = capsys.readouterr()
    assert out.out == "", "a refusal must not put anything on stdout"
    assert json.loads(out.err)["error"]["message"].startswith("refusing to run CREATE")


@pytest.mark.duckdb_extensions
def test_allow_writes_means_it(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--sql", "CREATE OR REPLACE TEMP TABLE zz AS SELECT 1 AS n", "--allow-writes")) == 0


@pytest.mark.duckdb_extensions
def test_the_app_pane_is_not_guarded(lake: Lake) -> None:
    """The guard is `--sql`'s. A person typing into the SQL pane is driving DuckDB on purpose."""
    assert lake.query("CREATE OR REPLACE TEMP TABLE zz2 AS SELECT 1 AS n") is not None


# -- what a typed pane publishes about its columns ----------------------------------------


def test_a_typed_pane_names_its_column_types(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "demo.events", "--json")) == 0
    body = one(capsys)
    types = {c["name"]: c["type"] for c in body["columns"]}
    assert types["id"] == "int64"
    assert types["measured_at"].startswith("timestamp")


def test_a_packed_pane_does_not_claim_column_types(lake: Lake, capsys) -> None:
    """Its arrow table is all strings by construction - publishing that would be a lie."""
    assert main(argv(lake, "--pane", "snapshots", "demo.events", "--json")) == 0
    assert "columns" not in one(capsys)


def test_a_snapshot_the_table_does_not_have_names_the_table(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "demo.events", "--snapshot", "999", "--json")) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "999" in message and "demo.events" in message


# -- EXPLAIN ANALYZE: the word that ran the statement -----------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "EXPLAIN ANALYZE DELETE FROM t",
        "EXPLAIN ANALYZE CREATE TABLE t AS SELECT 1",
        "EXPLAIN ANALYZE COPY (SELECT 1) TO '/tmp/icelens-should-not-write.csv'",
        "explain analyze delete from t",
        "ExPlAiN\taNaLyZe DELETE FROM t",
        "EXPLAIN/*x*/ANALYZE DELETE FROM t",
        "EXPLAIN -- comment\nANALYZE DELETE FROM t",
        "/*lead*/ EXPLAIN ANALYZE DELETE FROM t",
        "SELECT 1; EXPLAIN ANALYZE DELETE FROM t",
    ],
)
def test_explain_analyze_is_not_a_read(sql: str) -> None:
    """DuckDB types all of these as EXPLAIN, and ANALYZE runs what follows.

    Measured against duckdb 1.5.5: `EXPLAIN ANALYZE DELETE FROM t` empties `t`, and every
    spelling above executes. The guard re-parses the tail, so the ones with a comment in the
    middle fail to parse and are refused rather than guessed at.
    """
    with pytest.raises(IcelensError) as caught:
        only_reads(sql)
    assert "EXPLAIN ANALYZE" in str(caught.value)


@pytest.mark.parametrize("sql", ["EXPLAIN SELECT 1", 'EXPLAIN DESCRIBE icelens."demo"."events"'])
def test_explaining_a_read_is_still_a_read(sql: str) -> None:
    assert only_reads(sql) == sql


# -- read-only is the lake's, not the call's --------------------------------------------------


def test_a_read_only_lake_refuses_a_write(lake: Lake) -> None:
    read_only = Lake(lake.target, read_only=True)
    try:
        with pytest.raises(IcelensError):
            read_only.query("CREATE TABLE zz3 AS SELECT 1")
    finally:
        read_only.close()


def test_a_read_only_lake_attaches_read_only(lake: Lake) -> None:
    """The guard above is the message; this is the enforcement.

    DuckDB refuses `CREATE`, `DELETE` and `EXPLAIN ANALYZE DELETE` alike on a database attached
    this way, which is the only reason the string check can afford to be a string check.
    """
    options = ["TYPE ICEBERG", "ENDPOINT 'http://localhost:8181/catalog'"]
    assert "READ_ONLY" in Lake(lake.target, read_only=True)._attach_stmt("w", options)
    assert "READ_ONLY" not in Lake(lake.target)._attach_stmt("w", options)


@pytest.mark.duckdb_extensions
def test_where_cannot_smuggle_a_second_statement(lake: Lake, capsys) -> None:
    """`--where` is a fragment. A fragment that parses as two statements is not one.

    Before this, `--where 'false; SELECT 1'` returned the columns of `SELECT 1` - which means
    `--where 'false; DROP TABLE x'` would have dropped it, no `--sql` and no `--allow-writes`
    anywhere in the command.
    """
    assert main(argv(lake, "--pane", "data", "demo.events", "--where", "false; SELECT 1", "--json")) == 1
    message = json.loads(capsys.readouterr().err)["error"]["message"]
    assert "more than one statement" in message


def test_an_ordinary_where_still_works(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--pane", "data", "demo.events", "--where", "id < 10", "--json")) == 0
    assert one(capsys)["count"] == 4  # ids 6..9; the fixture deletes id < 6


# -- JSON that a parser other than Python's will read -----------------------------------------


@pytest.mark.duckdb_extensions
def test_non_finite_floats_are_named_not_bare(lake: Lake, capsys) -> None:
    """`NaN`, `Infinity` and `-Infinity` are not JSON: node, Go and serde all reject them."""
    sql = "SELECT 'nan'::DOUBLE AS n, 'inf'::DOUBLE AS i, (-'inf'::DOUBLE) AS ni"
    assert main(argv(lake, "--sql", sql, "--json")) == 0
    raw = capsys.readouterr().out

    def strict(constant: str) -> None:
        raise AssertionError(f"bare {constant} is not JSON")

    body = json.loads(raw, parse_constant=strict)
    assert body["rows"] == [{"n": "NaN", "i": "Infinity", "ni": "-Infinity"}]
    # and the types are still published, so a reader can tell these from a string column
    assert {c["type"] for c in body["columns"]} == {"double"}


@pytest.mark.duckdb_extensions
def test_finite_floats_are_left_as_numbers(lake: Lake, capsys) -> None:
    assert main(argv(lake, "--sql", "SELECT 1.5::DOUBLE AS f", "--json")) == 0
    assert one(capsys)["rows"] == [{"f": 1.5}]


def test_nested_non_finite_floats_are_named_too() -> None:
    inf = float("inf")
    assert _finite({"a": [float("nan"), {"b": -inf}], "c": (inf, 2.0)}) == {
        "a": ["NaN", {"b": "-Infinity"}],
        "c": ["Infinity", 2.0],
    }


# -- a limit that means what it says -----------------------------------------------------------


def test_limit_zero_reads_no_rows_and_says_so(lake: Lake, capsys) -> None:
    """`LIMIT 0` is a real question - what are the columns? - and used to be answered with a row."""
    assert main(argv(lake, "--pane", "data", "demo.events", "--limit", "0", "--json")) == 0
    body = one(capsys)
    assert body["count"] == 0
    assert "limit 0" in body["note"]
    assert body["columns"], "the columns are the whole point of asking for no rows"


# -- a reader that leaves early -------------------------------------------------------------------


@pytest.mark.duckdb_extensions
def test_a_reader_that_stops_reading_gets_no_traceback(tmp_path) -> None:
    """`icelens … | head -c 100`: the pipe closes under a large write, and the next write - here
    `print`'s newline - raised BrokenPipeError, as a traceback. 141 is what a shell reports for
    a process SIGPIPE stopped, as it would for a C tool in the same place."""
    import os
    import subprocess
    import sys

    process = subprocess.Popen(
        [
            sys.executable, "-c", "import sys; from icelens.cli import main; sys.exit(main())",
            "--sandbox", "pipe", "--sandbox-dir", str(tmp_path / "sandboxes"),
            "--sql", "SELECT range AS n, repeat('x', 40) AS pad FROM range(20000)", "--json",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=tmp_path,
        env={**os.environ, "PYICEBERG_HOME": str(tmp_path)},
    )
    assert process.stdout is not None and process.stderr is not None
    assert process.stdout.read(100).startswith(b'{"schema_version": 1')
    process.stdout.close()
    err = process.stderr.read().decode()
    assert process.wait(timeout=120) == 141, err
    assert "Traceback" not in err and "BrokenPipe" not in err, err

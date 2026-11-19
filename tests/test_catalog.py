from __future__ import annotations

import os
from pathlib import Path

import pytest

from icelens import catalog
from icelens.catalog import (
    CatalogAuthError,
    CatalogPermissionDenied,
    CatalogUnreachable,
    IcelensError,
    Lake,
    NeedsWarehouse,
    Target,
    classify,
    discover,
    list_profiles,
    profile_target,
    qualified,
    quote,
)


def test_dotenv_is_read_by_suffix_not_by_prefix(repo_env: Path) -> None:
    target = discover(env_file=repo_env)
    assert target.properties["uri"] == "http://catalog.example:8181/catalog"  # trailing slash gone
    assert target.properties["warehouse"] == "prod"
    assert target.properties["s3.endpoint"] == "http://minio:9000"
    assert target.properties["s3.access-key-id"] == "key"
    assert target.properties["type"] == "rest"
    assert target.properties["header.X-Iceberg-Access-Delegation"] == "vended-credentials"
    assert "UNRELATED" not in target.properties


def test_the_command_line_wins(repo_env: Path) -> None:
    target = discover(uri="http://other:8181/catalog", warehouse="staging", env_file=repo_env)
    assert target.properties["uri"] == "http://other:8181/catalog"
    assert target.properties["warehouse"] == "staging"
    assert "command line" in target.origin


def test_secrets_are_not_displayed(repo_env: Path) -> None:
    redacted = discover(env_file=repo_env).redacted()
    assert redacted["s3.secret-access-key"] == "********"
    assert redacted["s3.access-key-id"] == "key"


def test_no_settings_anywhere_falls_back_to_the_pyiceberg_profile(tmp_path: Path) -> None:
    target = discover(env_file=tmp_path / "absent.env")
    assert target.profile == "default"


@pytest.fixture()
def profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `~/.pyiceberg.yaml` with a default catalog and a named one, and nothing else configured."""
    for key in [k for k in os.environ if k.startswith("ICELENS_") and not k.startswith("ICELENS_TEST")]:
        monkeypatch.delenv(key)
    (tmp_path / ".pyiceberg.yaml").write_text(
        "catalog:\n"
        "  default:\n    uri: https://default.example/catalog\n    warehouse: main\n"
        "  lab:\n    uri: https://lab.example/catalog\n    warehouse: lab\n"
    )
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_a_profile_asked_for_by_name_wins_over_a_dotenv_nearby(profiles: Path, repo_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--catalog lab` in a directory with a `.env` connected to the `.env`'s catalog, the profile
    merged in underneath it. Asked for by name is more explicit than found by walking."""
    assert repo_env.parent == profiles
    target = discover(profile="lab")
    assert (target.properties["uri"], target.properties["warehouse"]) == ("https://lab.example/catalog", "lab")
    assert "s3.endpoint" not in target.properties, "nothing of the .env's"
    # what is explicit still goes on top of the profile
    monkeypatch.setenv("ICELENS_WAREHOUSE", "staging")
    target = discover(profile="lab", uri="https://other.example/c", properties={"scope": "x"})
    assert (target.properties["uri"], target.properties["warehouse"], target.properties["scope"]) == (
        "https://other.example/c", "staging", "x",
    )
    # and a .env named on purpose is read, over the profile, as it always was
    assert discover(profile="lab", env_file=repo_env).properties["uri"] == "http://catalog.example:8181/catalog"


def test_the_default_profile_fills_in_whenever_nothing_says_which_catalog(profiles: Path) -> None:
    """It was used only when nothing at all was found, so a `.env` holding just a region - or
    `-p token=…` - hid the catalog the user had configured, and there was nothing to open."""
    env = profiles / ".env"
    env.write_text("AWS_REGION=eu-west-1\n")
    env.chmod(0o600)
    target = discover()
    assert (target.properties["uri"], target.properties["warehouse"], target.properties["s3.region"]) == (
        "https://default.example/catalog", "main", "eu-west-1",
    )
    assert target.profile == "default" and not target.unconfigured
    target = discover(properties={"token": "t0ken-for-default"}, search_dotenv=False)
    assert (target.properties["uri"], target.properties["token"]) == ("https://default.example/catalog", "t0ken-for-default")
    # something that does say which catalog keeps the default out
    named = discover(uri="https://mine.example/c", search_dotenv=False)
    assert ("warehouse" not in named.properties, named.profile) == (True, None)
    typed = discover(properties={"type": "glue"}, search_dotenv=False)
    assert "uri" not in typed.properties


def test_identifiers_are_quoted_not_interpolated() -> None:
    assert qualified("crm.accounts") == 'icelens."crm"."accounts"'
    assert qualified("a.b.c") == 'icelens."a.b"."c"'  # one schema to DuckDB, dots and all
    # escaped, not rejected: Iceberg allows almost any name, so a whitelist would refuse
    # legal tables, while a doubled quote is unambiguous to DuckDB whatever is inside it
    assert quote('sneaky"; DROP TABLE x; --') == '"sneaky""; DROP TABLE x; --"'
    assert qualified('demo.odd"name') == 'icelens."demo"."odd""name"'
    for impossible in ("", "nul\x00byte"):
        with pytest.raises(IcelensError):
            quote(impossible)
    with pytest.raises(IcelensError):
        qualified("no_namespace")


def test_probe_warehouses_never_raises_on_a_dead_host() -> None:
    assert catalog.probe_warehouses("http://127.0.0.1:1/catalog", timeout=0.2) == []


def test_catalog_type_is_inferred_from_the_address(tmp_path: Path) -> None:
    """Every dialect PyIceberg speaks has to be recognisable before anything is opened."""
    cases = {
        "rest": {"uri": "https://catalog.example/api/catalog"},
        "sql": {"uri": "postgresql://host/db"},
        "hive": {"uri": "thrift://metastore:9083"},
        "glue": {"type": "glue", "warehouse": "s3://bucket/wh"},
        "unknown": {},
    }
    for expected, props in cases.items():
        assert Target(properties=props).catalog_type == expected, props


def test_an_s3_tables_arn_attaches_as_s3_tables(tmp_path: Path) -> None:
    lake = Lake(Target(properties={"type": "glue", "warehouse": "arn:aws:s3tables:eu-west-1:1234:bucket/b"}))
    assert "ENDPOINT_TYPE 'S3_TABLES'" in (lake._attach_sql() or "")


def test_glue_attaches_by_catalog_id_not_by_its_s3_location() -> None:
    """A Glue warehouse is usually an s3:// path, which DuckDB will not accept as a catalog."""
    lake = Lake(Target(properties={"type": "glue", "warehouse": "s3://bucket/warehouse"}))
    sql = lake._attach_sql() or ""
    assert "ENDPOINT_TYPE 'GLUE'" in sql
    assert "s3://" not in sql


def test_profiles_come_from_the_pyiceberg_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".pyiceberg.yaml").write_text(
        "catalog:\n"
        "  prod:\n"
        "    type: rest\n"
        "    uri: https://catalog.example/catalog\n"
        "  lab:\n"
        "    type: sql\n"
        "    uri: sqlite:///lab.db\n"
    )
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))
    profiles = list_profiles()
    assert set(profiles) == {"prod", "lab"}
    assert profile_target("lab").catalog_type == "sql"
    # a profile's properties reach the Target, not just load_catalog, so DuckDB can be routed
    assert discover(profile="prod", search_dotenv=False).properties["uri"] == "https://catalog.example/catalog"


def test_errors_are_classified_into_things_a_user_can_act_on() -> None:
    assert isinstance(classify(RuntimeError("401 Unauthorized"), "x"), CatalogAuthError)
    assert isinstance(classify(RuntimeError("AccessDenied: no"), "x"), CatalogPermissionDenied)
    assert isinstance(classify(OSError("Connection refused"), "x"), CatalogUnreachable)
    assert type(classify(ValueError("no such table"), "x")) is IcelensError


def test_a_metadata_parse_error_says_why_rather_than_how_many() -> None:
    # The error PyIceberg 0.12 raises on a column type it cannot parse, made by its own parser:
    # its first line is "1 validation error for TableResponse", which says nothing. `file` is
    # v4's new type; variant, which used to fail this way, is now kept by icelens.opaque.
    from pydantic import ValidationError
    from pyiceberg.table.metadata import TableMetadataUtil

    field = {"id": 1, "name": "f", "required": False, "type": "file"}
    metadata = {
        "format-version": 3, "table-uuid": "9c12d441-03fe-4693-9a96-a0705ddf69c1", "location": "file:///t",
        "last-sequence-number": 0, "last-updated-ms": 0, "last-column-id": 1, "current-schema-id": 0,
        "schemas": [{"type": "struct", "schema-id": 0, "fields": [field]}], "default-spec-id": 0,
        "partition-specs": [{"spec-id": 0, "fields": []}], "last-partition-id": 999, "next-row-id": 0,
    }
    with pytest.raises(ValidationError) as raised:
        TableMetadataUtil.parse_obj(metadata)
    assert str(raised.value).startswith("1 validation error")
    assert str(classify(raised.value, "cannot load t")) == "cannot load t: Unsupported field type: 'file'"
    assert catalog._unreadable_type(raised.value)
    assert not catalog._unreadable_type(ValueError("Table does not exist: t"))


@pytest.mark.parametrize(
    ("message", "kind"),
    [
        # the way each dialect actually says it: PyIceberg over REST, botocore over Glue, OAuth
        ("RESTError 401: Unauthorized", CatalogAuthError),
        ("status=401, body={}", CatalogAuthError),
        ("An error occurred (ExpiredTokenException) when calling the GetDatabases operation", CatalogAuthError),
        ('{"error": "token_expired"}', CatalogAuthError),
        ("The provided token has expired.", CatalogAuthError),
        ("An error occurred (AccessDeniedException) when calling the GetTable operation", CatalogPermissionDenied),
        ("User: arn:aws:iam::1:user/x is not authorized to perform: glue:GetTable", CatalogPermissionDenied),
        ("HTTP 403 Forbidden", CatalogPermissionDenied),
        ("HTTPConnectionPool(host='x', port=8181): Read timed out. (read timeout=5)", CatalogUnreachable),
        ("Max retries exceeded with url: /v1/config", CatalogUnreachable),
        ("[Errno -2] Name or service not known", CatalogUnreachable),
    ],
)
def test_each_dialect_is_still_classified_the_way_it_says_it(message: str, kind: type) -> None:
    assert type(classify(RuntimeError(message), "cannot open catalog")) is kind


@pytest.mark.parametrize(
    "message",
    [
        "500: commit failed for demo.orders_2401",  # `401`, after a digit
        "500: commit failed for raw.expired_sessions",  # `expired`, running on into an underscore
        "500: nothing to compact in billing.timeout_audit",
        "500: ops.forbidden_2024 has no current snapshot",
        "500: rewrite of x.unauthorized_logins was cancelled",
    ],
)
def test_a_word_inside_a_name_is_not_the_reason_even_when_the_type_says_nothing(message: str) -> None:
    """The exception's type settles a missing table; this is the case it does not, a server
    error that quotes a table name. Matched as a bare substring, every one of these was a login,
    a grant or a network problem - the wrong fix, named with confidence."""
    assert type(classify(RuntimeError(message), "cannot commit")) is IcelensError


@pytest.mark.parametrize("name", ["demo.expired_sessions", "demo.orders_2401", "demo.session_timeout", "demo.expired", "demo.forbidden", "demo.403"])
def test_a_table_that_is_not_there_is_not_a_login_problem(lake: Lake, name: str) -> None:
    """The reason is in the exception's type, and a table's name is not a reason.

    PyIceberg says "Table does not exist: demo.orders_2401", and the keyword scan used to find
    `401` in it and tell the user to log in again - or, for `session_timeout`, that the catalog
    was unreachable while it was answering.
    """
    with pytest.raises(IcelensError) as caught:
        lake.table(name)
    assert type(caught.value) is IcelensError
    assert "does not exist" in str(caught.value)
    assert "not authenticated" not in str(caught.value)


def test_a_catalog_that_needs_a_warehouse_does_not_look_like_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failure that started this: many warehouses on the server, an empty tree on screen.

    `namespaces()` walks nested namespaces and forgives a catalog that will not nest. Opening
    the catalog happens on the first of those calls, so the forgiveness used to swallow
    NeedsWarehouse too - and every caller above it was waiting for exactly that exception.
    """
    lake = Lake(Target(properties={"type": "rest", "uri": "http://catalog.example/catalog"}))

    def needs_one(self: Lake) -> None:
        raise NeedsWarehouse("needs a warehouse", ["wh-a", "wh-b"])

    monkeypatch.setattr(type(lake), "catalog", property(needs_one))
    with pytest.raises(NeedsWarehouse) as caught:
        lake.namespaces()
    assert caught.value.options == ["wh-a", "wh-b"]


def test_a_root_listing_that_fails_is_an_error_and_a_nested_one_is_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Flat:
        def list_namespaces(self, parent: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
            if parent:
                raise RuntimeError("this catalog does not nest")
            return [("core",), ("raw",)]

    class Shut:
        def list_namespaces(self, parent: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
            raise RuntimeError("403 Forbidden")

    lake = Lake(Target(properties={"type": "rest", "uri": "http://catalog.example/catalog"}))
    monkeypatch.setattr(type(lake), "catalog", property(lambda self: Flat()))
    assert lake.namespaces() == ["core", "raw"]  # flat catalogs answer once; that is not a failure

    monkeypatch.setattr(type(lake), "catalog", property(lambda self: Shut()))
    with pytest.raises(CatalogPermissionDenied):
        lake.namespaces()


def test_a_gcs_catalog_gets_a_bearer_secret_not_an_hmac_one() -> None:
    """duckdb 1.5.5 takes BEARER_TOKEN or KEY_ID/SECRET for `TYPE GCS`, and refuses TOKEN."""
    lake = Lake(Target(properties={"type": "rest", "uri": "https://x.example/c", "gcs.oauth2.token": "ya29.abc"}))
    sql = lake._gcs_secret_sql() or ""
    assert "TYPE GCS" in sql
    assert "BEARER_TOKEN 'ya29.abc'" in sql
    assert "TOKEN 'ya29.abc'" not in sql.replace("BEARER_TOKEN", "")


def test_an_hmac_pair_is_used_when_there_is_no_oauth_token() -> None:
    lake = Lake(Target(properties={"gcs.access-key-id": "GOOG1E", "gcs.secret-access-key": "shh"}))
    sql = lake._gcs_secret_sql() or ""
    assert "KEY_ID 'GOOG1E'" in sql and "SECRET 'shh'" in sql


def test_google_reuses_the_catalog_token_for_cloud_storage() -> None:
    """One `gcloud` token authenticates the catalog *and* the bucket, so asking twice is noise."""
    props = {"type": "rest", "uri": "https://biglake.googleapis.com/iceberg/v1/restcatalog", "token": "ya29.abc"}
    assert "BEARER_TOKEN 'ya29.abc'" in (Lake(Target(properties=props))._gcs_secret_sql() or "")


def test_a_catalog_token_is_never_handed_to_a_non_google_store() -> None:
    """The token for Lakekeeper is not a Cloud Storage credential; sending it there leaks it."""
    props = {"type": "rest", "uri": "http://localhost:8181/catalog", "token": "secret-lakekeeper-token"}
    assert Lake(Target(properties=props))._gcs_secret_sql() is None


def test_googleapis_must_be_the_host_and_not_merely_in_the_uri() -> None:
    for uri in ("https://evil.example/?next=googleapis.com", "https://googleapis.com.evil.example/c"):
        assert not catalog._is_google_catalog(uri), uri
    for uri in ("https://biglake.googleapis.com/iceberg/v1/restcatalog", "https://GOOGLEAPIS.COM/x"):
        assert catalog._is_google_catalog(uri), uri


def test_no_gcs_configuration_means_no_secret_at_all() -> None:
    """A half-filled secret shadows DuckDB's own credential chain; absence lets ADC work."""
    assert Lake(Target(properties={"type": "rest", "uri": "https://x.example/c"}))._gcs_secret_sql() is None
    assert Lake(Target(properties={"gcs.access-key-id": "GOOG1E"}))._gcs_secret_sql() is None


def test_a_catalog_token_in_a_dotenv_is_used_and_no_other_token_is(tmp_path: Path) -> None:
    """The docs say a token can live in a .env only you can read; it used to be dropped silently,
    and the catalog then answered 401 with no hint why. Only a CATALOG_ name counts: any other
    token in the file would be handed to the catalog server."""
    env = tmp_path / ".env"
    env.write_text(
        "APP_CATALOG_URI=http://catalog.example:8181/catalog\n"
        "APP_CATALOG_TOKEN=tok-for-the-catalog-1234\n"
        "APP_CATALOG_CREDENTIAL=client:secret-5678\n"
        "GITHUB_TOKEN=ghp-not-for-the-catalog\n"
        "SLACK_CREDENTIAL=not-for-the-catalog-either\n"
    )
    env.chmod(0o600)
    target = discover(env_file=env)
    assert target.properties["token"] == "tok-for-the-catalog-1234"
    assert target.properties["credential"] == "client:secret-5678"
    assert "ghp-not-for-the-catalog" not in str(target.properties)
    assert "not-for-the-catalog-either" not in str(target.properties)
    shown = str(target.redacted())
    assert "tok-for-the-catalog-1234" not in shown and "secret-5678" not in shown

"""Polaris: the catalogs that log in with OAuth and hand out storage credentials per table.

Every test here is a defect that was real on 2026-10-15, against Polaris 1.8.0 over RustFS:
the metadata panes worked and the rows did not, because neither the login nor the vended
credentials reached DuckDB. `docs/FINDINGS.md` has the whole list. Skips unless a Polaris
answers - see `tests/polaris.py` for how to point it at one.

The connections below are configured the way a person configures them - client credentials
and a scope, and no store keys - because that is where it broke. A test that handed DuckDB a
token, or the store's keys, would pass against the old code too.
"""

from __future__ import annotations

import uuid

import pyarrow as pa
import pytest
from live import unavailable
from polaris import Polaris

from icelens.catalog import IcelensError, Lake, NeedsWarehouse, Target, probe_warehouses


@pytest.fixture(scope="session")
def polaris() -> Polaris:
    server = Polaris.from_env()
    if server is None:
        unavailable("no Polaris configured: set ICELENS_TEST_POLARIS_URI (see tests/polaris.py)")
    if (down := server.unreachable()) is not None:
        unavailable(down)
    if (missing := server.unusable()) is not None:
        unavailable(missing)
    return server


@pytest.fixture(scope="session")
def vending(polaris: Polaris) -> str:
    """A catalog that vends credentials, holding a v3 table and a nested-namespace table.

    The v3 table is written by DuckDB through its own OAuth attach - the only writer here
    that makes v3 - and its history is said in order: 100 rows, 10 deleted with a deletion
    vector, one updated. The nested table is written by PyIceberg, because DuckDB cannot
    name a nested namespace in DDL.
    """
    import duckdb

    name = f"icelens_test_{uuid.uuid4().hex[:8]}"
    polaris.create(name, vending=True)
    try:
        catalog = polaris.pyiceberg_catalog(name)
        catalog.create_namespace("core")
        catalog.create_namespace("core.nested")
        deep = catalog.create_table("core.nested.deep", schema=pa.schema([("x", pa.int64())]))
        deep.append(pa.table({"x": [1, 2, 3]}))

        client_id, _, secret = polaris.credential.partition(":")
        con = duckdb.connect()
        for extension in ("iceberg", "httpfs", "aws"):
            con.execute(f"LOAD {extension}")
        con.execute(
            f"ATTACH '{name}' AS p (TYPE ICEBERG, ENDPOINT '{polaris.uri}', CLIENT_ID '{client_id}', "
            f"CLIENT_SECRET '{secret}', OAUTH2_SCOPE 'PRINCIPAL_ROLE:ALL')"
        )
        for statement in (
            'CREATE TABLE p.core.items WITH ("format-version" = 3) AS '
            "SELECT i AS id, 'n' || i AS name FROM range(1, 101) t(i)",
            "DELETE FROM p.core.items WHERE id % 10 = 0",
            "UPDATE p.core.items SET name = 'changed' WHERE id = 4",
        ):
            con.execute(statement)
        con.close()
        yield name
    finally:
        try:
            polaris.drop(name)
        except Exception as exc:  # noqa: BLE001 - a leaked catalog is a warning, not a red suite
            print(f"\nwarning: could not drop Polaris catalog {name!r}: {exc}")


@pytest.fixture(scope="session")
def stingy(polaris: Polaris) -> str:
    """A catalog with no STS, so it vends nothing - and refuses every load that asks."""
    name = f"icelens_nosts_{uuid.uuid4().hex[:8]}"
    polaris.create(name, vending=False)
    try:
        catalog = polaris.pyiceberg_catalog(name, vending=False)
        catalog.create_namespace("core")
        table = catalog.create_table("core.t", schema=pa.schema([("x", pa.int64())]))
        table.append(pa.table({"x": [1, 2, 3, 4]}))
        yield name
    finally:
        try:
            polaris.drop(name)
        except Exception as exc:  # noqa: BLE001
            print(f"\nwarning: could not drop Polaris catalog {name!r}: {exc}")


def _lake(polaris: Polaris, warehouse: str, **extra: str) -> Lake:
    return Lake(polaris.target(warehouse, **extra), read_only=True)


# -- the login ------------------------------------------------------------------------


def test_client_credentials_attach_duckdb(polaris: Polaris, vending: str) -> None:
    """The defect at the root of the rest: DuckDB used to attach with no login and get a 401."""
    lake = _lake(polaris, vending)
    try:
        rows, engine = lake.rows("core.items", limit=1000)
        assert (lake.mode, engine, rows.num_rows) == ("attach", "duckdb", 90)
        assert not lake.attach_refused
    finally:
        lake.close()


def test_v3_row_lineage_and_time_travel_arrive_through_the_login(polaris: Polaris, vending: str) -> None:
    lake = _lake(polaris, vending)
    try:
        rows, engine = lake.rows("core.items", limit=5, lineage=True)
        assert rows.column_names[:2] == ["_row_id", "_last_updated_sequence_number"]
        assert engine == "duckdb"
        first = lake.table("core.items").metadata.snapshots[0].snapshot_id
        assert lake.rows("core.items", limit=1000, snapshot_id=first)[0].num_rows == 100
    finally:
        lake.close()


def test_sql_reads_and_still_refuses_to_write(polaris: Polaris, vending: str) -> None:
    lake = _lake(polaris, vending)
    try:
        assert lake.query('SELECT count(*) AS n FROM icelens."core"."items"').to_pylist() == [{"n": 90}]
        with pytest.raises(IcelensError, match="refusing to run DELETE"):
            lake.query('DELETE FROM icelens."core"."items"')
    finally:
        lake.close()


def test_a_wrong_secret_is_never_printed(polaris: Polaris, vending: str) -> None:
    """The secret half of `id:secret` now reaches DuckDB on its own, so it must scrub on its own."""
    client_id = polaris.credential.partition(":")[0]
    lake = _lake(polaris, vending, credential=f"{client_id}:not-the-secret-4817")
    try:
        with pytest.raises(IcelensError) as caught:
            lake.namespaces()
        assert "not-the-secret-4817" not in str(caught.value)
    finally:
        lake.close()


def test_a_refused_attach_is_named_and_the_rows_still_come(polaris: Polaris, vending: str) -> None:
    """When the catalog turns DuckDB away, the reason is shown - it used to be swallowed."""
    lake = _lake(polaris, vending)
    good = lake._attach_sql
    lake._attach_sql = lambda: good().replace("OAUTH2_SCOPE 'PRINCIPAL_ROLE:ALL'", "OAUTH2_SCOPE 'NOT_A_SCOPE'")
    try:
        assert lake.mode == "bridge"
        assert lake.attach_refused
        assert "attach refused" in (lake.attach_error or "")
        assert lake.rows("core.items", limit=1000)[0].num_rows == 90
    finally:
        lake.close()


# -- namespaces -----------------------------------------------------------------------


def test_nested_namespaces_reach_duckdb(polaris: Polaris, vending: str) -> None:
    """`icelens."core"."nested"."deep"` used to be a parser error; it is one schema to DuckDB."""
    lake = _lake(polaris, vending)
    try:
        rows, engine = lake.rows("core.nested.deep")
        assert (rows.num_rows, engine) == (3, "duckdb")
        assert lake.query('SELECT count(*) AS n FROM icelens."core.nested"."deep"').to_pylist() == [{"n": 3}]
    finally:
        lake.close()


def test_sql_finds_a_nested_namespace_without_being_told(polaris: Polaris, vending: str) -> None:
    """`--sql` attaches before anything has walked the catalog; the first miss teaches it."""
    lake = _lake(polaris, vending)
    try:
        assert lake.query('SELECT count(*) AS n FROM icelens."core.nested"."deep"').to_pylist() == [{"n": 3}]
    finally:
        lake.close()


def test_a_flat_catalog_is_attached_flat(polaris: Polaris, vending: str) -> None:
    """Nesting costs DuckDB a request per namespace, so it is asked for only where it is seen."""
    lake = _lake(polaris, vending)
    try:
        lake.rows("core.items", limit=1)
        assert "SUPPORT_NESTED_NAMESPACES" not in (lake._attach_sql() or "")
        lake.rows("core.nested.deep", limit=1)
        assert "SUPPORT_NESTED_NAMESPACES" in (lake._attach_sql() or "")
    finally:
        lake.close()


# -- storage credentials --------------------------------------------------------------


def test_the_scanning_route_gets_each_tables_vended_credentials(polaris: Polaris, vending: str) -> None:
    """A catalog DuckDB cannot attach still has its rows read by DuckDB, with the table's keys."""
    lake = _lake(polaris, vending)
    lake._attach_sql = lambda: None
    try:
        rows, engine = lake.rows("core.items", limit=1000)
        assert (lake.mode, engine, rows.num_rows) == ("bridge", "duckdb", 90)
        scopes = [row[0] for row in lake._con.execute("SELECT scope FROM duckdb_secrets()").fetchall()]
        assert any(scope and scope[0].endswith("/core/items/") for scope in scopes)
        assert lake.query('SELECT count(*) AS n FROM icelens."core"."items"').to_pylist() == [{"n": 90}]
    finally:
        lake.close()


def test_pyiceberg_reads_what_duckdb_cannot_and_says_so(polaris: Polaris, vending: str) -> None:
    """The fallback the Data pane used to lack: a 403 for DuckDB is not the end of the rows."""
    lake = _lake(polaris, vending)
    lake._attach_sql = lambda: None
    lake._hand_over_credentials = lambda ident, table: None
    try:
        rows, engine = lake.rows("core.items", limit=1000)
        assert rows.num_rows == 90
        assert engine.startswith("pyiceberg (duckdb could not read it:")
        with pytest.raises(IcelensError, match=r"could not be read for SQL: .*core\.items"):
            lake.query('SELECT count(*) FROM icelens."core"."items"')
    finally:
        lake.close()


def test_a_catalog_that_vends_nothing_still_opens(polaris: Polaris, stingy: str) -> None:
    """Asking for vended credentials made this catalog refuse every table. Now it asks once."""
    lake = _lake(polaris, stingy, **polaris.store_keys())
    try:
        assert lake.table("core.t").metadata.format_version in (2, 3)
        assert "vends no credentials" in lake.delegation_note
        rows, engine = lake.rows("core.t")
        assert (rows.num_rows, engine) == (4, "duckdb")
    finally:
        lake.close()


def test_sql_alone_recovers_from_a_catalog_that_vends_nothing(polaris: Polaris, stingy: str) -> None:
    """SQL reaches the catalog through DuckDB only, so DuckDB's refusal has to be caught too."""
    lake = _lake(polaris, stingy, **polaris.store_keys())
    try:
        assert lake.query('SELECT count(*) AS n FROM icelens."core"."t"').to_pylist() == [{"n": 4}]
        assert lake.delegation_note
    finally:
        lake.close()


# -- warehouses -----------------------------------------------------------------------


def test_the_warehouse_picker_lists_polaris_catalogs(polaris: Polaris, vending: str, stingy: str) -> None:
    """It used to know only Lakekeeper's API, and offered nothing here."""
    options = probe_warehouses(polaris.uri, properties=polaris.properties(""))
    assert {vending, stingy} <= set(options)


def test_a_missing_warehouse_offers_the_catalogs(polaris: Polaris, vending: str, stingy: str) -> None:
    props = {k: v for k, v in polaris.properties("").items() if k != "warehouse"}
    lake = Lake(Target(properties=props), read_only=True)
    try:
        with pytest.raises(NeedsWarehouse) as caught:
            lake.namespaces()
        assert {vending, stingy} <= set(caught.value.options)
    finally:
        lake.close()

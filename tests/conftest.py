"""A catalog of our own, so the tests need no server and no network.

PyIceberg's SQLite catalog over a temporary directory writes real Iceberg metadata - real
manifests, real snapshots, real delete files - which is exactly what the panes read. It is also
the second engine path: DuckDB cannot attach a SQLite catalog, so these tests exercise the
PyIceberg fallback that a REST-catalog run never touches.

The first engine path - `attach`, where DuckDB speaks the catalog itself - cannot be reached
this way at all, so it lives in `tests/live.py` and the `keeper` / `rest_lake` fixtures below.
Those skip unless a REST catalog is configured and answering. Read `tests/live.py` for how to
point them at one.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import live
import pyarrow as pa
import pytest

# DuckDB renders a timestamptz in the session's zone, which it takes from the process's. CI runs
# in UTC and the expected values say so; on a machine in any other zone they read its offset and fail.
# Set before anything opens a DuckDB connection, which is why it is here and not in a fixture.
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):  # not on Windows
    time.tzset()

from icelens.app import IcelensApp
from icelens.catalog import Lake, Target, rest_sandbox_target


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--snapshot-update",
        action="store_true",
        help="rewrite tests/snapshots/ from the screens as they are now (see tests/snapshots.py)",
    )


# The DuckDB extensions: skip what needs them where they will not load. See `tests/offline.py`.
def pytest_configure(config: pytest.Config) -> None:
    from offline import configure

    configure()


def pytest_runtest_setup(item: pytest.Item) -> None:
    from offline import check

    check(item)


@pytest.fixture(autouse=True)
def _secrets_of_its_own() -> None:
    """What `remember_secrets` learns in a test is forgotten when the test ends.

    The set is module state, and `scrub` masks every string in it. `repo_env` holds
    `ACME_S3_SECRET_KEY=secret`, so once one test had read it every later `scrub` blanked the
    word "secret" - and an assertion that a secret was scrubbed passed whether or not it had
    been remembered. Session fixtures are set up before this runs, so their secrets stay.
    """
    from icelens import catalog

    saved = set(catalog._secrets)
    yield
    catalog._secrets = saved


def _rows(ids: range, day: str) -> pa.Table:
    return pa.table(
        {
            "id": pa.array(list(ids), pa.int64()),
            "name": pa.array([f"row {i}" for i in ids], pa.string()),
            "ingest_date": pa.array([day] * len(ids), pa.string()),
            "measured_at": pa.array(
                [dt.datetime(2026, 3, 2, 12, 0, 0) for _ in ids], pa.timestamp("us")
            ),
        }
    )


@pytest.fixture(scope="session")
def lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    warehouse = tmp_path_factory.mktemp("warehouse")
    target = Target(
        properties={
            "type": "sql",
            "uri": f"sqlite:///{warehouse / 'catalog.db'}",
            "warehouse": warehouse.as_uri(),
        },
        origin="tests",
    )
    lake = Lake(target)
    catalog = lake.catalog
    catalog.create_namespace("demo")

    table = catalog.create_table("demo.events", schema=_rows(range(0), "x").schema)
    table.append(_rows(range(1, 51), "2026-03-02"))          # snapshot 1: 50 rows
    table.append(_rows(range(51, 61), "2026-03-03"))         # snapshot 2: 10 more
    table.delete("id < 6")                                   # snapshot 3: retires 5 rows

    catalog.create_table("demo.empty", schema=_rows(range(0), "x").schema)
    yield lake
    lake.close()


@pytest.fixture(scope="session")
def v3_lake(tmp_path_factory: pytest.TempPathFactory) -> Lake:
    """A format-version 3 table, written by hand because nothing here can write one.

    PyIceberg 0.12 refuses outright - `NotImplementedError: Writing V3 is not yet supported` -
    and DuckDB writes v3 only through a REST catalog, which these tests deliberately do not
    need. So the metadata is produced the honest way: let PyIceberg write a real v2 table,
    then rewrite the one document that decides the version, adding the two v3 fields a reader
    looks for (`next-row-id`, and `first-row-id` per snapshot). Everything below it - the
    manifests, the manifest lists, the data files - is untouched and real.
    """
    warehouse = tmp_path_factory.mktemp("v3")
    target = Target(
        properties={
            "type": "sql",
            "uri": f"sqlite:///{warehouse / 'catalog.db'}",
            "warehouse": warehouse.as_uri(),
        },
        origin="tests",
    )
    lake = Lake(target)
    catalog = lake.catalog
    catalog.create_namespace("demo")
    table = catalog.create_table("demo.staged", schema=_rows(range(0), "x").schema)
    table.append(_rows(range(1, 51), "2026-03-02"))
    table.append(_rows(range(51, 61), "2026-03-03"))

    document = json.loads(Path(catalog.load_table("demo.staged").metadata_location[7:]).read_text())
    document["format-version"] = 3
    document["next-row-id"] = 60
    assigned = 0
    for snapshot in sorted(document["snapshots"], key=lambda s: s["sequence-number"]):
        snapshot["first-row-id"] = assigned
        assigned += int(snapshot["summary"].get("added-records", 0))
    document["table-uuid"] = str(uuid.uuid4())
    location = warehouse / "demo.db" / "events"
    document["location"] = location.as_uri()
    written = location / "metadata" / "00000-hand-written.metadata.json"
    written.parent.mkdir(parents=True, exist_ok=True)
    written.write_text(json.dumps(document))
    catalog.register_table("demo.events", written.as_uri())
    yield lake
    lake.close()


@pytest.fixture()
def repo_env(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(
        "# a project's own names, not icelens's\n"
        "ACME_CATALOG_URI=http://catalog.example:8181/catalog/\n"
        "ACME_WAREHOUSE=prod\n"
        "ACME_S3_ENDPOINT=http://minio:9000\n"
        "ACME_S3_ACCESS_KEY=key\n"
        "ACME_S3_SECRET_KEY=secret\n"
        "UNRELATED=1\n"
    )
    return path


@pytest.fixture()
def app(lake: Lake) -> IcelensApp:
    """The app under test: a small limit and a narrow width, so grids stay assertable."""
    return IcelensApp(lake, limit=10, width=30)


# -- the live REST catalog: attach mode, or a skip ------------------------------------
#
# Three fixtures rather than one, because the three things that can go wrong want different
# words: nothing configured, nothing answering, nothing to write with.


@pytest.fixture(scope="session")
def keeper() -> live.Keeper:
    """The live catalog's admin API, or a skip that says exactly what is missing."""
    props = live.settings()
    if not props.get("uri"):
        live.unavailable("no live catalog configured: set ICELENS_TEST_ENV_FILE or ICELENS_TEST_CATALOG_URI")
    keeper = live.Keeper.from_settings(props)
    if (down := keeper.unreachable()) is not None:
        live.unavailable(f"{down} - start one with `make live-up`")
    if (missing := keeper.unusable()) is not None:
        live.unavailable(missing)
    return keeper


@pytest.fixture(scope="session")
def live_warehouse(keeper: live.Keeper) -> str:
    """A warehouse of this run's own, deleted when the session ends.

    The name carries a random suffix so two runs - two checkouts, a rerun after a crash - never
    fight over one storage prefix. Teardown tolerates a warehouse that is already gone, and
    never `deactivate`s: see `live.Keeper.drop`.
    """
    name = f"icelens-test-{uuid.uuid4().hex[:8]}"
    keeper.create(name)
    try:
        yield name
    finally:
        try:
            keeper.drop(name)
        except Exception as exc:  # noqa: BLE001 - a leaked warehouse is worth a warning, not a red suite
            print(f"\nwarning: could not drop test warehouse {name!r}: {exc}")


@dataclass(frozen=True)
class Attachable:
    """A REST catalog DuckDB can attach: the live one when it is configured, else icelens's own.

    The attach-mode tests used to skip on every machine without a Lakekeeper running, which
    left the route real runs take covered by nothing in CI. icelens now serves its v3
    sandboxes through a REST catalog of its own, so that catalog stands in when no live one
    is configured - and when one *is* configured but not answering, the tests skip rather than
    quietly testing something else.
    """

    kind: str
    uri: str
    warehouse: str
    properties: dict[str, str]

    def target(self) -> Target:
        return Target(properties=dict(self.properties), origin=f"{self.kind} tests")

    def cli_args(self) -> list[str]:
        """The command-line flags that reach the same catalog, credentials included."""
        args = ["--no-env", "--uri", self.uri, "--warehouse", self.warehouse]
        for key, value in self.properties.items():
            if key not in ("uri", "warehouse", "type"):
                args += ["-p", f"{key}={value}"]
        return args


@pytest.fixture(scope="session")
def attachable(tmp_path_factory: pytest.TempPathFactory) -> Attachable:
    props = live.settings()
    if props.get("uri"):
        keeper = live.Keeper.from_settings(props)
        if (down := keeper.unreachable()) is not None:
            live.unavailable(f"{down} - start one, or unset it to test against icelens's own catalog")
        if (missing := keeper.unusable()) is not None:
            live.unavailable(missing)
        name = f"icelens-test-{uuid.uuid4().hex[:8]}"
        keeper.create(name)
        try:
            yield Attachable("lakekeeper", keeper.uri, name, keeper.properties(name))
        finally:
            try:
                keeper.drop(name)
            except Exception as exc:  # noqa: BLE001 - a leaked warehouse is worth a warning, not a red suite
                print(f"\nwarning: could not drop test warehouse {name!r}: {exc}")
        return
    from icelens import rest

    running = rest.serve(tmp_path_factory.mktemp("rest-catalog"))
    try:
        yield Attachable(
            "icelens-rest",
            running.uri,
            rest.PREFIX,
            {
                "type": "rest",
                "uri": running.uri,
                "warehouse": rest.PREFIX,
                "token": running.token,
                "header.X-Iceberg-Access-Delegation": "",
            },
        )
    finally:
        running.stop()


@pytest.fixture(scope="session")
def rest_lake(attachable: Attachable) -> Lake:
    """A writable lake on the live catalog, with the same tables the hermetic `lake` has.

    Written entirely by DuckDB through the attached catalog, because that is the only writer
    here that can produce a v3 table - and because every statement is one Iceberg snapshot, so
    the three-snapshot history below is built by saying what happened, in order.

    The namespace is created with PyIceberg *before* DuckDB opens, deliberately: a DuckDB
    connection caches the catalog it attached to, and a namespace that appears afterwards may
    not be visible to it.
    """
    lake = Lake(attachable.target())
    lake.catalog.create_namespace("live")
    made = (
        # snapshot 1: 50 rows
        "CREATE TABLE icelens.live.events AS "
        "SELECT i AS id, 'row ' || i AS name, '2026-03-02' AS ingest_date, "
        "TIMESTAMP '2026-03-02 12:00:00' AS measured_at FROM range(1, 51) t(i)",
        # snapshot 2: 10 more
        "INSERT INTO icelens.live.events "
        "SELECT i, 'row ' || i, '2026-03-03', TIMESTAMP '2026-03-02 12:00:00' FROM range(51, 61) t(i)",
        # snapshot 3: retires 5, so the head is 55 rows and the first snapshot is still 50
        "DELETE FROM icelens.live.events WHERE id < 6",
        # v3, which is the whole point of having a REST catalog in the tests: row lineage is a
        # v3 feature and DuckDB writes v3 only through an attached catalog.
        'CREATE TABLE icelens.live.lineage WITH ("format-version" = 3) AS '
        "SELECT i AS id, 'row ' || i AS name FROM range(1, 11) t(i)",
    )
    for statement in made:
        lake.query(statement)
    try:
        yield lake
    finally:
        lake.close()


@pytest.fixture(scope="session")
def read_only_lake(attachable: Attachable, rest_lake: Lake) -> Lake:
    """The same warehouse, opened the way every headless route opens it.

    It depends on `rest_lake` so the tables exist before this connection attaches, and so this
    one is torn down first.
    """
    lake = Lake(attachable.target(), read_only=True)
    try:
        yield lake
    finally:
        lake.close()


@pytest.fixture(scope="session")
def rest_sandbox(attachable: Attachable) -> Lake:
    """A sandbox in a namespace of the live catalog: the only route to a v3 write.

    Its own warehouse, so nothing it does can be confused with the tables `rest_lake` makes,
    and `read_only=True` on purpose - a sandbox has to attach writable regardless, and the
    tests below check that the SQL pane is still held shut.
    """
    lake = Lake(rest_sandbox_target("t", attachable.target()), read_only=True)
    try:
        yield lake
    finally:
        lake.close()

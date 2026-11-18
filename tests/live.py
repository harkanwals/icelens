"""A real REST catalog, when one is running, so attach mode is tested and not just assumed.

Everything else in this suite runs against a SQLite catalog, which DuckDB cannot attach. That
leaves `attach` mode - the mode a real run spends all of its time in - covered by nothing: the
ATTACH itself, `READ_ONLY` as the engine enforces it rather than as a substring of a statement,
`AT (VERSION => ...)` time travel, and a v3 table written the only way anything here can write
one. Every defect those paths have had was found by hand, which is a way of saying it was found
late.

So: when a REST catalog answers, these tests build a throwaway warehouse of their own, use it,
and delete it. When nothing answers they skip, and `pytest` on a machine with no server still
passes - which is the whole reason the hermetic fixtures stay.

`make test-live` starts a Lakekeeper (and a Polaris, for `polaris.py`) in Docker and points the
suite at it; see `tests/compose/stack.yml`. Point the suite at a catalog of your own one of two ways:

    ICELENS_TEST_ENV_FILE=path/to/your/.env uv run pytest
    ICELENS_TEST_CATALOG_URI=http://localhost:8181/catalog \
      ICELENS_TEST_S3_ENDPOINT=http://localhost:9000 \
      ICELENS_TEST_S3_BUCKET=warehouse ICELENS_TEST_S3_REGION=local-01 \
      ICELENS_TEST_S3_ACCESS_KEY=... ICELENS_TEST_S3_SECRET_KEY=... uv run pytest

The first is the usual one, and reads the project's own variable names through the same code
`discover` uses - so a broken `.env` reader fails a test here too. The second is for CI, where
there is no project next door.

The management API that makes and unmakes the warehouse is **Lakekeeper's**. That is a coupling
and a deliberate one: there is no line in the Iceberg REST spec for creating a warehouse, so a
test that wants its own has to know one server's admin API or borrow somebody's warehouse - and
borrowing means writing tables into a catalog that belongs to something else.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

import pytest

from icelens.catalog import Target, _from_dotenv, read_dotenv

#: Lakekeeper's default project. Everything here lives in it.
PROJECT_ID = "00000000-0000-0000-0000-000000000000"

#: Purging a table queues file deletions, and a warehouse will not go while they are outstanding.
DROP_ATTEMPTS = 10
DROP_PAUSE = 1.0


def unavailable(reason: str) -> NoReturn:
    """Skip a test that needs a live catalog - unless this run promised one.

    CI's live job sets ICELENS_REQUIRE_LIVE_CATALOGS=1. Without it, a stack that failed to start
    would turn every live test into a skip and the job green, which is the one outcome that job
    exists to rule out.
    """
    if os.environ.get("ICELENS_REQUIRE_LIVE_CATALOGS") == "1":
        pytest.fail(f"ICELENS_REQUIRE_LIVE_CATALOGS=1, but {reason}", pytrace=False)
    pytest.skip(reason)


class LiveError(RuntimeError):
    """The live catalog said no. Carries the server's own words, which are the useful ones."""


def _bucket(values: dict[str, str]) -> str:
    for key, value in values.items():
        if key.upper().endswith("S3_BUCKET") and value:
            return value
    return ""


def settings() -> dict[str, str]:
    """Catalog and object-store settings for the live tests, or `{}` when none are configured.

    The `warehouse` a project names is deliberately dropped: these tests make their own and
    delete it afterwards, and a fixture that writes into the warehouse someone is using is a
    fixture that eventually deletes it.
    """
    env_file = os.environ.get("ICELENS_TEST_ENV_FILE")
    if env_file:
        raw = read_dotenv(Path(env_file).expanduser())
        props = _from_dotenv(raw)
    else:
        raw = {
            key[len("ICELENS_TEST_") :]: value
            for key, value in os.environ.items()
            if key.startswith("ICELENS_TEST_")
        }
        props = _from_dotenv(raw)
    props.pop("warehouse", None)
    if bucket := _bucket(raw):
        props["s3.bucket"] = bucket
    return props


@dataclass(frozen=True)
class Keeper:
    """Lakekeeper's management API - enough of it to make and unmake one warehouse."""

    uri: str
    bucket: str = ""
    endpoint: str = ""
    region: str = "local-01"
    access_key: str = ""
    secret_key: str = ""

    @classmethod
    def from_settings(cls, props: dict[str, str]) -> Keeper:
        return cls(
            uri=props.get("uri", ""),
            bucket=props.get("s3.bucket", ""),
            endpoint=props.get("s3.endpoint", ""),
            region=props.get("s3.region", "local-01"),
            access_key=props.get("s3.access-key-id", ""),
            secret_key=props.get("s3.secret-access-key", ""),
        )

    @property
    def management_uri(self) -> str:
        """Lakekeeper's admin API sits *beside* the Iceberg REST API, not under it."""
        base = self.uri[: -len("/catalog")] if self.uri.endswith("/catalog") else self.uri
        return f"{base}/management/v1"

    def unreachable(self) -> str | None:
        """None when the catalog answers; otherwise the reason, phrased as a skip message."""
        try:
            self._request("GET", "/info")
        except Exception as exc:  # noqa: BLE001 - any failure at all means "no live catalog here"
            return f"no Iceberg catalog at {self.uri} ({type(exc).__name__}: {exc})"
        return None

    def unusable(self) -> str | None:
        """None when a warehouse can be created; otherwise what is missing."""
        if not self.bucket:
            return "live catalog configured without an object-store bucket (…_S3_BUCKET)"
        if not (self.access_key and self.secret_key):
            return "live catalog configured without object-store keys (…_S3_ACCESS_KEY / …_S3_SECRET_KEY)"
        return None

    # -- warehouses ------------------------------------------------------------------
    def warehouse_id(self, name: str) -> str | None:
        for warehouse in self._request("GET", "/warehouse").get("warehouses", []):
            if warehouse.get("name") == name:
                return warehouse.get("id")
        return None

    def create(self, name: str) -> None:
        """Register a warehouse with its own prefix in the bucket, so nothing else can collide."""
        self._request(
            "POST",
            "/warehouse",
            {
                "warehouse-name": name,
                "project-id": PROJECT_ID,
                "storage-profile": {
                    "type": "s3",
                    "bucket": self.bucket,
                    "key-prefix": name,
                    "endpoint": self.endpoint,
                    # STS is not optional: without it the catalog vends no credentials and
                    # DuckDB writes anonymously, which the object store rejects.
                    "sts-endpoint": self.endpoint,
                    "sts-enabled": True,
                    "region": self.region,
                    "path-style-access": True,
                    "flavor": "s3-compat",
                },
                "storage-credential": {
                    "type": "s3",
                    "credential-type": "access-key",
                    "access-key-id": self.access_key,
                    "secret-access-key": self.secret_key,
                },
            },
        )

    def drop(self, name: str) -> bool:
        """Purge every table, drop every namespace, then delete the warehouse. True if there was one.

        Never `deactivate` first: a deactivated warehouse disappears from the listing but keeps
        its storage prefix, so the name can neither be used nor re-registered and the next
        create fails with `CreateWarehouseStorageProfileOverlap`.
        """
        import time

        warehouse_id = self.warehouse_id(name)
        if warehouse_id is None:
            return False
        catalog = self.pyiceberg_catalog(name)
        for (namespace,) in catalog.list_namespaces():
            for ident in catalog.list_tables(namespace):
                catalog.purge_table(ident)
            catalog.drop_namespace(namespace)
        for attempt in range(DROP_ATTEMPTS):
            try:
                self._request("DELETE", f"/warehouse/{warehouse_id}")
                return True
            except LiveError as exc:
                if "UnfinishedTasks" not in str(exc) and "NotEmpty" not in str(exc):
                    raise
                if attempt == DROP_ATTEMPTS - 1:
                    raise
                time.sleep(DROP_PAUSE)
        raise AssertionError("unreachable")

    # -- what the tests connect with -------------------------------------------------
    def properties(self, warehouse: str) -> dict[str, str]:
        """The catalog properties icelens would be given for this warehouse.

        Keys included on purpose: bridge mode opens the Parquet files itself, so a test that
        forces that mode needs the same credentials PyIceberg got.
        """
        props = {
            "type": "rest",
            "uri": self.uri,
            "warehouse": warehouse,
            "header.X-Iceberg-Access-Delegation": "vended-credentials",
        }
        for key, value in (
            ("s3.endpoint", self.endpoint),
            ("s3.region", self.region),
            ("s3.access-key-id", self.access_key),
            ("s3.secret-access-key", self.secret_key),
        ):
            if value:
                props[key] = value
        if self.endpoint:
            props["s3.path-style-access"] = "true"
        return props

    def target(self, warehouse: str) -> Target:
        return Target(properties=self.properties(warehouse), origin="live tests")

    def pyiceberg_catalog(self, warehouse: str) -> Any:
        from pyiceberg.catalog.rest import RestCatalog

        return RestCatalog("icelens-live", **self.properties(warehouse))

    # -- plumbing --------------------------------------------------------------------
    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # S310 is safe here: the URI comes from this machine's own configuration, not from a table.
        request = urllib.request.Request(  # noqa: S310
            f"{self.management_uri}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Lakekeeper puts the reason in the body; the status line alone says nothing useful.
            raise LiveError(f"{method} {path}: {exc.code} {exc.read().decode(errors='replace')}") from exc
        except urllib.error.URLError as exc:
            raise LiveError(f"{method} {path}: {exc.reason}") from exc
        return json.loads(raw) if raw else {}

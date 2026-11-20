"""Apache Polaris, when one is running, so the catalogs that log in with OAuth are tested too.

`live.py` covers Lakekeeper, which authenticates with nothing or a bearer token. Polaris is the
other shape - OAuth client credentials, per-table vended credentials, nested namespaces - and
it is the shape Snowflake's catalogs share, because they are Polaris. Every defect
`tests/test_polaris.py` covers was found against one; these tests keep them found.

`make test-live` starts one (`tests/compose/stack.yml`, with RustFS as the store) and points
the suite at it through `tests/compose/live.env`. To use a Polaris of your own, with an S3 store
behind it:

    ICELENS_TEST_POLARIS_URI=http://127.0.0.1:8181/api/catalog \\
    ICELENS_TEST_POLARIS_S3_ENDPOINT=http://127.0.0.1:9000 \\
    ICELENS_TEST_POLARIS_S3_BUCKET=lake \\
    ICELENS_TEST_POLARIS_S3_ACCESS_KEY=… ICELENS_TEST_POLARIS_S3_SECRET_KEY=… \\
      uv run pytest tests/test_polaris.py

The credential defaults to the stack's bootstrap `root:s3cr3t`; `ICELENS_TEST_POLARIS_CREDENTIAL`
overrides it.

Creating a catalog is not in the Iceberg REST spec, so this speaks Polaris's management API,
as `live.py` speaks Lakekeeper's. Each run makes catalogs with a random suffix, uses them and
deletes them, and touches nothing else on the server.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from icelens.catalog import Target, _rest_defaults

SCOPE = "PRINCIPAL_ROLE:ALL"


class PolarisError(RuntimeError):
    """Polaris said no. Carries the server's own words."""


@dataclass(frozen=True)
class Polaris:
    """Enough of Polaris's management API to make and unmake a catalog."""

    uri: str
    credential: str = "root:s3cr3t"
    s3_endpoint: str = ""
    s3_internal_endpoint: str = ""
    bucket: str = ""
    region: str = "us-east-1"
    access_key: str = ""
    secret_key: str = ""

    @classmethod
    def from_env(cls) -> Polaris | None:
        env = os.environ
        uri = env.get("ICELENS_TEST_POLARIS_URI", "").rstrip("/")
        if not uri:
            return None
        endpoint = env.get("ICELENS_TEST_POLARIS_S3_ENDPOINT", "")
        return cls(
            uri=uri,
            credential=env.get("ICELENS_TEST_POLARIS_CREDENTIAL", "root:s3cr3t"),
            s3_endpoint=endpoint,
            # Where Polaris itself reaches the store, when that differs from where the tests
            # do: inside a compose network the store is `rustfs:9000`, from the host it is not.
            s3_internal_endpoint=env.get("ICELENS_TEST_POLARIS_S3_INTERNAL_ENDPOINT", endpoint),
            bucket=env.get("ICELENS_TEST_POLARIS_S3_BUCKET", ""),
            region=env.get("ICELENS_TEST_POLARIS_S3_REGION", "us-east-1"),
            access_key=env.get("ICELENS_TEST_POLARIS_S3_ACCESS_KEY", ""),
            secret_key=env.get("ICELENS_TEST_POLARIS_S3_SECRET_KEY", ""),
        )

    @property
    def management_uri(self) -> str:
        """Polaris's admin API sits beside the catalog API: `…/api/catalog`, `…/api/management`."""
        base = self.uri[: -len("/catalog")] if self.uri.endswith("/catalog") else self.uri
        return f"{base}/management/v1"

    def unreachable(self) -> str | None:
        try:
            self.token()
        except Exception as exc:  # noqa: BLE001 - any failure at all means "no Polaris here"
            return f"no Polaris at {self.uri} ({type(exc).__name__}: {exc})"
        return None

    def unusable(self) -> str | None:
        if not (self.s3_endpoint and self.bucket):
            return "Polaris configured without a store (ICELENS_TEST_POLARIS_S3_ENDPOINT / _BUCKET)"
        if not (self.access_key and self.secret_key):
            return "Polaris configured without store keys (ICELENS_TEST_POLARIS_S3_ACCESS_KEY / _SECRET_KEY)"
        return None

    # -- catalogs --------------------------------------------------------------------
    def create(self, name: str, vending: bool = True) -> None:
        """A catalog on its own prefix of the bucket, able to vend credentials or not.

        `vending=False` is `stsUnavailable`: the usual way to run Polaris over a store with
        no STS, and the one that refuses every table load that asks for credentials.
        """
        storage: dict[str, Any] = {
            "storageType": "S3",
            "allowedLocations": [f"s3://{self.bucket}/{name}"],
            "endpoint": self.s3_endpoint,
            "endpointInternal": self.s3_internal_endpoint,
            "stsEndpoint": self.s3_internal_endpoint,
            "pathStyleAccess": True,
            "region": self.region,
            # RustFS refuses to parse the KMS statement Polaris 1.8 puts in its STS session
            # policy, and every table load after a create then fails with a 500.
            "kmsUnavailable": True,
        }
        if vending:
            storage["roleArn"] = "arn:aws:iam::123456789012:role/icelens-tests"
        else:
            storage["stsUnavailable"] = True
        self._request(
            "POST",
            "/catalogs",
            {
                "catalog": {
                    "name": name,
                    "type": "INTERNAL",
                    "properties": {
                        "default-base-location": f"s3://{self.bucket}/{name}",
                        # Polaris refuses to purge a table otherwise, so `drop` could empty
                        # nothing and every run left its catalogs behind.
                        "polaris.config.drop-with-purge.enabled": "true",
                    },
                    "storageConfigInfo": storage,
                }
            },
        )
        # A new catalog's admin role can make namespaces and tables but not read their data,
        # and a table load that asks for vended credentials is refused without it.
        self._request(
            "PUT",
            f"/catalogs/{name}/catalog-roles/catalog_admin/grants",
            {"grant": {"type": "catalog", "privilege": "CATALOG_MANAGE_CONTENT"}},
        )

    def drop(self, name: str) -> None:
        """Purge every table, drop every namespace deepest first, then the catalog."""
        catalog = self.pyiceberg_catalog(name, vending=False)

        def walk(parent: tuple[str, ...]) -> list[tuple[str, ...]]:
            found: list[tuple[str, ...]] = []
            for child in catalog.list_namespaces(parent):
                found.extend(walk(tuple(child)))
                found.append(tuple(child))
            return found

        for namespace in walk(()):
            for ident in catalog.list_tables(namespace):
                catalog.purge_table(ident)
            catalog.drop_namespace(namespace)
        self._request("DELETE", f"/catalogs/{name}")

    # -- what the tests connect with -------------------------------------------------
    def properties(self, warehouse: str, **extra: str) -> dict[str, str]:
        """The connection a person would configure: OAuth, and nothing about the store.

        No store keys on purpose. A vending catalog hands them out per table, and the defects
        this file exists for are exactly the ones where that hand-out did not reach DuckDB.
        Passed through the same defaults `discover` applies, so a test connects exactly the way
        `icelens --uri … -p credential=…` does.
        """
        props = {"type": "rest", "uri": self.uri, "warehouse": warehouse, "credential": self.credential, "scope": SCOPE, **extra}
        return dict(_rest_defaults(Target(props)).properties)

    def store_keys(self) -> dict[str, str]:
        """Static keys for the store, for a catalog that vends none."""
        return {
            "s3.endpoint": self.s3_endpoint,
            "s3.access-key-id": self.access_key,
            "s3.secret-access-key": self.secret_key,
            "s3.region": self.region,
            "s3.path-style-access": "true",
        }

    def target(self, warehouse: str, **extra: str) -> Target:
        return Target(properties=self.properties(warehouse, **extra), origin="polaris tests")

    def pyiceberg_catalog(self, warehouse: str, vending: bool = True) -> Any:
        from pyiceberg.catalog.rest import RestCatalog

        props = self.properties(warehouse)
        if not vending:
            props = {**props, **self.store_keys(), "header.X-Iceberg-Access-Delegation": ""}
        return RestCatalog("icelens-polaris", **props)

    # -- plumbing --------------------------------------------------------------------
    def token(self) -> str:
        client_id, _, secret = self.credential.partition(":")
        form = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret, "scope": SCOPE}
        ).encode()
        request = urllib.request.Request(f"{self.uri}/v1/oauth/tokens", data=form, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - our own config
            return json.loads(response.read())["access_token"]

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - the URI is this machine's own configuration
            f"{self.management_uri}{path}",
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise PolarisError(f"{method} {path}: {exc.code} {exc.read().decode(errors='replace')}") from exc
        return json.loads(raw) if raw else {}

"""The DuckDB extensions, and what the suite does on a machine that cannot load them.

Rows and SQL go through DuckDB's `iceberg`, `httpfs` and `aws` extensions, which DuckDB
downloads from `extensions.duckdb.org` the first time they are asked for. So a suite that is
otherwise hermetic has one quiet network dependency, and on a machine that cannot reach that
host the tests that read rows used to *fail* - which reads as a broken tool rather than a
missing download.

A test that needs the extensions carries `@pytest.mark.duckdb_extensions`. Where they will not
load, those tests skip and say why, and everything else still runs. Two environment variables
change that, both meant for CI:

    ICELENS_REQUIRE_DUCKDB_EXTENSIONS=1        a skip becomes a failure, so a runner that lost
                                               the extensions cannot quietly lose the coverage
    ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS=1   run the whole suite as a machine with no
                                               extensions would - which is how a test that
                                               needs them and is not marked gets caught

The second is also how `tests/test_offline.py` reaches the `pyiceberg` route on purpose: not
by unplugging the network, but by pointing DuckDB at extension directories with nothing in them.
"""

from __future__ import annotations

import functools
import os
import tempfile
from pathlib import Path
from typing import Any

import duckdb
import pytest

from icelens.catalog import Lake, Target

REQUIRE = "ICELENS_REQUIRE_DUCKDB_EXTENSIONS"
WITHOUT = "ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS"

#: the real `duckdb.connect`, kept so a withholding wrapper never wraps another one
_CONNECT = duckdb.connect


def withheld(root: Path) -> Any:
    """A `duckdb.connect` that behaves as if the machine had no extensions and no network.

    Not a mock of the failure: DuckDB is pointed at an empty extension directory and at an
    empty *local* extension repository, so `LOAD iceberg` fails because the file is not there
    and `INSTALL iceberg` fails because the repository has nothing to give - DuckDB's own two
    errors, raised by DuckDB, in a millisecond and without a packet leaving the machine.
    Everything that needs no extension - Parquet, `extract_statements`, plain SQL - still works,
    which is exactly the machine the `pyiceberg` route exists for.
    """
    (root / "extensions").mkdir(parents=True, exist_ok=True)
    (root / "repository").mkdir(parents=True, exist_ok=True)
    nothing = {
        "extension_directory": str(root / "extensions"),
        "custom_extension_repository": str(root / "repository"),
        "autoinstall_known_extensions": False,
        "autoload_known_extensions": False,
    }

    def connect(database: str = ":memory:", read_only: bool = False, config: dict | None = None, **kwargs: Any) -> Any:
        return _CONNECT(database, read_only=read_only, config={**(config or {}), **nothing}, **kwargs)

    return connect


def configure() -> None:
    """Called once from `pytest_configure`: withhold the extensions for the whole run, if asked."""
    if os.environ.get(WITHOUT) == "1":
        duckdb.connect = withheld(Path(tempfile.mkdtemp(prefix="icelens-no-extensions-")))


@functools.cache
def missing() -> str | None:
    """None when the extensions load; otherwise DuckDB's reason, for the skip message.

    Asked of `Lake` itself rather than of DuckDB directly, so the answer is the product's: a
    local catalog reaches the `bridge` route exactly when every extension it loads did load -
    including the one-off `INSTALL` it attempts when one is absent, which is the only network
    this suite can cause, and only on a machine that has never run it before.
    """
    probe = Lake(Target(properties={"type": "sql"}, origin="extension probe"))
    try:
        return None if probe.mode == "bridge" else (probe.attach_error or "unknown reason")
    finally:
        probe.close()


def check(item: pytest.Item) -> None:
    """Called from `pytest_runtest_setup`: skip, or fail, a marked test the extensions cannot serve."""
    if item.get_closest_marker("duckdb_extensions") is None:
        return
    reason = missing()
    if reason is None:
        return
    if os.environ.get(REQUIRE) == "1":
        pytest.fail(f"{REQUIRE}=1, and the DuckDB extensions will not load: {reason}", pytrace=False)
    pytest.skip(f"needs the DuckDB extensions, which will not load here: {reason}")

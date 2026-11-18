"""Opening a catalog: what it is called on screen, where its settings come from, and the warehouse.

A REST catalog that hosts several warehouses is the case `test_driving.py` fakes with a monkeypatch;
here a real one answers from 127.0.0.1 (see `tests/stub.py`), so the path from "this server wants a
warehouse" to "there is only one, so it is open" runs through PyIceberg and the probe for real.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from stub import StubCatalog

from icelens import catalog
from icelens.catalog import IcelensError, Lake, NeedsWarehouse, Target, _local, list_profiles, profile_target, scrub

# -- what a catalog is called on screen ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("properties", "profile", "label"),
    [
        ({"uri": "https://lake.example/catalog", "warehouse": "wh"}, None, "wh @ https://lake.example/catalog"),
        ({"uri": "https://lake.example/catalog"}, None, "https://lake.example/catalog"),
        ({"warehouse": "s3://bucket/wh"}, None, "s3://bucket/wh"),
        ({}, "prod", "prod"),
        ({}, None, "catalog"),
    ],
)
def test_the_label_is_the_most_specific_thing_there_is(properties: dict, profile: str | None, label: str) -> None:
    assert Target(properties, profile=profile).label == label


def test_with_warehouse_keeps_everything_else(tmp_path: Path) -> None:
    base = Target({"uri": "https://lake.example/catalog", "token": "tok-1234567"}, profile="p", origin=".env")
    moved = base.with_warehouse("other")
    assert moved.properties == {"uri": "https://lake.example/catalog", "token": "tok-1234567", "warehouse": "other"}
    assert (moved.profile, moved.origin) == ("p", ".env")
    assert base.warehouse == ""  # a Target is a value: the original is untouched
    assert moved.with_warehouse("x", origin="picked").origin == "picked"


# -- ~/.pyiceberg.yaml ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "document",
    [
        "catalog: [not, a, mapping]\n",
        "catalog:\n  lab: just a string\n",
        "{{{ this is not yaml\n",
        "catalog:\n  lab:\n    uri: https://x\n    listed: [1, 2]\n",  # PyIceberg reads it with strictyaml: no flow style

        "",
    ],
)
def test_a_config_file_that_is_not_one_is_no_catalogs(tmp_path: Path, monkeypatch, document: str) -> None:
    """A broken ~/.pyiceberg.yaml must not stop the tool starting: it is somebody else's file too."""
    (tmp_path / ".pyiceberg.yaml").write_text(document)
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))
    assert list_profiles() == {}
    with pytest.raises(IcelensError, match="no catalog named 'lab' in ~/.pyiceberg.yaml"):
        profile_target("lab")


def test_a_profile_keeps_its_strings_and_drops_what_is_not_one(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / ".pyiceberg.yaml").write_text(
        "catalog:\n"
        "  lab:\n"
        "    uri: https://lab.example/catalog\n"
        "    token: tok-from-a-profile-123\n"
        "    nested:\n"
        "      ignored: true\n"
        "    listed:\n"
        "      - 1\n"
        "      - 2\n"
    )
    monkeypatch.setenv("PYICEBERG_HOME", str(tmp_path))
    assert list_profiles() == {"lab": {"uri": "https://lab.example/catalog", "token": "tok-from-a-profile-123"}}
    target = profile_target("lab")
    assert (target.profile, target.origin, target.catalog_type) == ("lab", "~/.pyiceberg.yaml", "rest")
    # a REST profile gets the conveniences a REST catalog wants, and its token is now a secret
    assert target.properties["header.X-Iceberg-Access-Delegation"] == "vended-credentials"
    assert "tok-from-a-profile-123" not in scrub("401 with tok-from-a-profile-123")


# -- the namespace walk ---------------------------------------------------------------------------------


@pytest.fixture()
def deep(tmp_path: Path) -> Lake:
    lake = Lake(Target(properties={"type": "sql", "uri": f"sqlite:///{tmp_path / 'catalog.db'}", "warehouse": tmp_path.as_uri()}))
    for level in range(1, 6):
        lake.catalog.create_namespace(tuple("abcde"[:level]))
    yield lake
    lake.close()


def test_nested_namespaces_are_walked_four_levels_deep(deep: Lake) -> None:
    """`list_namespaces()` answers one level; the tree wants all of them, within reason."""
    assert deep.catalog.list_namespaces() == [("a",)]
    assert deep.namespaces() == ["a", "a.b", "a.b.c", "a.b.c.d"]
    assert deep.namespaces(depth=2) == ["a", "a.b"]


def test_a_catalog_that_repeats_itself_is_listed_once(deep: Lake, monkeypatch) -> None:
    """Some servers answer a child listing with the parent in it; the walk must not loop or repeat."""
    real = deep.catalog

    class Echoing:
        def list_namespaces(self, parent: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
            return [("a",), *real.list_namespaces(parent)] if parent else real.list_namespaces()

    monkeypatch.setattr(type(deep), "catalog", property(lambda self: Echoing()))
    assert deep.namespaces() == ["a", "a.b", "a.b.c", "a.b.c.d"]


# -- the table cache ------------------------------------------------------------------------------------


def test_the_table_cache_forgets_one_table_or_all_of_them(lake: Lake) -> None:
    fresh = Lake(lake.target)
    try:
        events, empty = fresh.table("demo.events"), fresh.table("demo.empty")
        assert fresh.table("demo.events") is events
        fresh.forget("demo.events")
        assert fresh.table("demo.events") is not events
        assert fresh.table("demo.empty") is empty
        fresh.forget()
        assert fresh.table("demo.empty") is not empty
        assert fresh.format_version("demo.events") == 2
        assert fresh.format_version("demo.nope") == 2  # the safe guess, for a table that will not load
    finally:
        fresh.close()


def test_closing_a_lake_puts_it_back_as_it_started(lake: Lake) -> None:
    fresh = Lake(lake.target)
    fresh.table("demo.events")
    fresh.close()
    assert (fresh._con, fresh._attached, fresh._mode, fresh._tables) == (None, None, "pyiceberg", {})
    assert fresh.table("demo.events").name() == ("demo", "events")  # and opens again on demand
    fresh.close()


def test_a_sandbox_writes_only_to_local_files() -> None:
    with pytest.raises(IcelensError, match="a sandbox writes to local files, and this table lives at s3://b/t"):
        _local("s3://b/t")


# -- the warehouse, from a server that answers ----------------------------------------------------------


def test_a_catalog_with_one_warehouse_opens_it_without_asking() -> None:
    with StubCatalog({"only": ["demo", "raw"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri}))
        try:
            assert lake.namespaces() == ["demo", "raw"]
            assert (lake.target.warehouse, lake.target.origin) == ("only", "probed from the catalog")
            assert lake.tables("demo") == []
        finally:
            lake.close()


def test_a_catalog_with_several_asks_and_offers_them_all() -> None:
    with StubCatalog({"wh-a": ["demo"], "wh-b": ["raw"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri}))
        with pytest.raises(NeedsWarehouse) as caught:
            lake.namespaces()
        assert caught.value.options == ["wh-a", "wh-b"]
        assert lake.warehouses() == ["wh-a", "wh-b"]
        lake.rebind(lake.target.with_warehouse("wh-b", origin="picked"))
        assert lake.namespaces() == ["raw"]
        assert lake.target.origin == "picked"
        lake.close()


def test_a_catalog_that_will_not_say_which_warehouses_it_has_still_says_it_needs_one() -> None:
    with StubCatalog({"hidden": ["demo"]}, listed=False) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri}))
        with pytest.raises(NeedsWarehouse, match="needs a warehouse: pass --warehouse NAME") as caught:
            lake.namespaces()
        assert caught.value.options == []
        assert lake.warehouses() == []


def test_a_warehouse_that_is_not_there_is_an_error_naming_it() -> None:
    with StubCatalog({"wh-a": ["demo"]}) as stub:
        lake = Lake(Target(properties={"type": "rest", "uri": stub.uri, "warehouse": "nope"}))
        with pytest.raises(IcelensError, match="nope"):
            lake.namespaces()


def test_rebinding_remembers_the_new_targets_secrets(lake: Lake) -> None:
    fresh = Lake(lake.target)
    fresh.rebind(Target({**lake.target.properties, "token": "tok-rebound-98765"}))
    assert "tok-rebound-98765" not in scrub("failed with tok-rebound-98765")
    assert fresh.namespaces() == ["demo"]
    fresh.close()
    assert "tok-rebound-98765" in catalog._secrets

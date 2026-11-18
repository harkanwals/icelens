"""Column types PyIceberg 0.12 cannot parse, kept as written: see `icelens.opaque`.

No catalog and no DuckDB: the parse and the round trip are PyIceberg's own models.
"""

from __future__ import annotations

import json

import pytest

from icelens import opaque


@pytest.fixture(autouse=True)
def installed() -> None:
    opaque.install()


#: each the way a writer spells it: Spark 4.1, DuckDB, and the spec's own examples
FIELDS = [
    {"id": 1, "name": "v", "required": False, "type": "variant"},
    {"id": 2, "name": "g", "required": False, "type": "geometry(OGC:CRS84)"},
    {"id": 3, "name": "d", "required": False, "type": "geometry(ogc:crs84)"},
    {"id": 4, "name": "l", "required": False, "type": {"type": "list", "element-id": 5, "element": "variant", "element-required": False}},
    {"id": 6, "name": "m", "required": False, "type": {
        "type": "map", "key-id": 7, "key": "string", "value-id": 8, "value-required": False,
        "value": {"type": "list", "element-id": 9, "element": "geography(OGC:CRS84, spherical)", "element-required": False},
    }},
    {"id": 10, "name": "s", "required": False, "type": {"type": "struct", "fields": [{"id": 11, "name": "inner", "required": False, "type": "variant"}]}},
]


def test_every_spelling_and_nesting_parses_and_serialises_back_exactly() -> None:
    # Exactly, because a schema icelens commits must stay in the writer's spelling: PyIceberg's
    # own `geometry('…')` is not the spec's, and another engine may not read it.
    from pyiceberg.schema import Schema

    schema = Schema.model_validate({"type": "struct", "schema-id": 0, "fields": FIELDS})
    assert [str(f.field_type) for f in schema.fields][:3] == ["variant", "geometry(OGC:CRS84)", "geometry(ogc:crs84)"]
    assert all(opaque.is_opaque(schema.find_field(name).field_type) for name in ("v", "g", "d", "s.inner"))
    back = json.loads(schema.model_dump_json(by_alias=True, exclude_none=True))["fields"]
    assert json.dumps([f["type"] for f in back], sort_keys=True) == json.dumps([f["type"] for f in FIELDS], sort_keys=True)


def test_a_type_pyiceberg_parses_is_left_to_it_and_nonsense_still_fails() -> None:
    from pyiceberg.schema import Schema
    from pyiceberg.types import GeometryType

    quoted = Schema.model_validate({"type": "struct", "schema-id": 0, "fields": [
        {"id": 1, "name": "g", "required": False, "type": "geometry('EPSG:4326')"}]})
    assert isinstance(quoted.fields[0].field_type, GeometryType)
    with pytest.raises(Exception, match="bogus"):
        Schema.model_validate({"type": "struct", "schema-id": 0, "fields": [{"id": 1, "name": "x", "required": False, "type": "bogus"}]})


def test_install_is_idempotent() -> None:
    from pyiceberg import types

    parse = types.IcebergType.handle_primitive_type
    opaque.install()
    assert types.IcebergType.handle_primitive_type is parse

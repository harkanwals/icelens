"""Column types PyIceberg cannot parse yet, kept as the text the table's writer used.

PyIceberg 0.12 refuses a whole table for one column it cannot parse, and two v3 types are
written in forms it refuses: `variant` (it has no such type: iceberg-python#1819), and
`geometry(OGC:CRS84)` / `geography(…)` unquoted, which is the spec's form and the one Spark 4.1
and DuckDB write, where PyIceberg accepts only `geometry('…')` (iceberg-python#4079). Measured
2026-11-13: every table with such a column failed to open in every pane.

`install()` teaches PyIceberg to keep such a column as an `OpaqueType` instead: the type's text,
exactly as written, which is also what it serialises back to. That last part is the safety
property. A table icelens commits to - a tag, a sandbox write - must not have its schema
rewritten into PyIceberg's spelling, which the other engines may not read. Nothing else about
the column is claimed: its rows come from DuckDB, which reads both types, and a reader that
needs to understand the values (a PyIceberg scan) refuses it in words.

Remove this module once PyIceberg parses both forms.
"""

from __future__ import annotations

import re
from typing import Any

#: the types kept opaque: only those, so a type that is really malformed still fails loudly
_KEPT = re.compile(r"(?i)\s*(variant|geometry\b.*|geography\b.*)\s*$")

_installed = False
_opaque: type | None = None


def install() -> None:
    """Make PyIceberg keep `variant`, `geometry(…)` and `geography(…)` columns it cannot parse.

    Idempotent, and cheap after the first call. It patches the one place PyIceberg turns a type
    string into a type - `IcebergType.handle_primitive_type`, which `NestedField` calls for
    every field, list element and map value - and only for strings it would otherwise refuse.
    Called from `Lake`, not at import: importing PyIceberg costs startup time, which the browser
    build in particular does not have to spare.
    """
    global _installed, _opaque
    if _installed:
        return
    from pydantic import Field, model_serializer, model_validator
    from pyiceberg import types

    class OpaqueType(types.PrimitiveType):
        """A column type PyIceberg cannot parse, held as the text its writer used."""

        root: str = Field()

        def __init__(self, text: str) -> None:
            super().__init__(root=text)

        # Its own, so that building one never re-enters the parser this module patches.
        @model_validator(mode="wrap")
        @classmethod
        def handle_primitive_type(cls, v: Any, handler: Any) -> Any:
            return handler(v)

        @model_serializer
        def ser_model(self) -> str:
            return self.root

        def __repr__(self) -> str:
            return f"OpaqueType({self.root!r})"

        def minimum_format_version(self) -> int:
            return 3

    parse: Any = types.IcebergType.handle_primitive_type

    def tolerant(v: Any, handler: Any) -> Any:
        try:
            return parse(v, handler)
        except Exception:
            if isinstance(v, str) and _KEPT.match(v):
                return OpaqueType(v)
            raise

    types.IcebergType.handle_primitive_type = staticmethod(tolerant)  # type: ignore[method-assign]

    # A list's element and a map's key and value are validated by pydantic's compiled schema,
    # which calls the parser it was built with, not the patched one. But PyIceberg builds both
    # with an explicit `ListType(**v)` / `MapType(**v)`, so their `__init__` - looked up when
    # called - can hand the compiled schema a type it no longer has to parse.
    def keeping(init: Any, *keys: str) -> Any:
        def patched(self: Any, *args: Any, **data: Any) -> None:
            for key in keys:
                if isinstance(data.get(key), str) and _KEPT.match(data[key]):
                    data[key] = tolerant(data[key], None)
            init(self, *args, **data)

        return patched

    types.ListType.__init__ = keeping(types.ListType.__init__, "element")  # type: ignore[method-assign]
    types.MapType.__init__ = keeping(types.MapType.__init__, "key", "value")  # type: ignore[method-assign]
    _opaque = OpaqueType
    _installed = True


def is_opaque(field_type: Any) -> bool:
    """Whether a field's type is one `install()` kept as text."""
    return _opaque is not None and isinstance(field_type, _opaque)

"""Compiled modules for the browser's demo, made once when the site is built instead of in every tab.

Pyodide's packages and its standard library ship Python source and no bytecode, so each module
the demo imports is compiled in the visitor's tab. Measured under Pyodide 314 in Node: compiling
the ~700 modules imported before the first frame is 1.9 s, loading the same code already compiled
0.05 s. Compressed with xz (4.4 MB, 0.3 s to unpack in Pyodide) rather than zlib (6.3 MB,
0.06 s): the bytes cost more than the CPU on any link slower than about 60 Mbps. `pack` runs in Pyodide when the site is built (`tests/pyodide/bytecode.mjs`) and keeps
the code of every module imported so far; `install` runs in the page's worker before anything
else is imported.

The code is served by a finder at the front of `sys.meta_path` that asks the usual finders for
each module's spec and swaps only its loader, so a module keeps its file, its package path and its
resources, and a module the bundle does not have, or found at another path than the one the bundle
was made from, is imported from source as before. Only the path is compared, not the file's bytes:
the site names the bundle, the wheel and the Pyodide version together (`wheels/latest.json`), so
a bundle never meets files other than the ones it was compiled from. Standard library only:
importing this compiles nothing.
"""

from __future__ import annotations

import importlib.util
import lzma
import marshal
import sys
from collections.abc import Iterable
from typing import Any

_FORMAT = 1


def pack(names: Iterable[str] | None = None) -> bytes:
    """The code of every module imported so far (or of `names`) that came from Python source."""
    modules: dict[str, tuple[str, bytes]] = {}
    wanted = list(sys.modules) if names is None else list(names)
    for name in wanted:
        module = sys.modules.get(name)
        spec = getattr(module, "__spec__", None)
        # `name != spec.name` skips aliases such as `os.path`, which is `posixpath`.
        if spec is None or spec.name != name or not (spec.origin or "").endswith(".py"):
            continue
        try:
            source = spec.loader.get_source(name)
        except Exception:  # noqa: BLE001 - a module with no source to read is simply not packed
            continue
        if source is None:
            continue
        code = compile(source, spec.origin, "exec", dont_inherit=True)
        modules[name] = (spec.origin, marshal.dumps(code))
    bundle = {"format": _FORMAT, "magic": importlib.util.MAGIC_NUMBER, "modules": modules}
    return lzma.compress(marshal.dumps(bundle), preset=9 | lzma.PRESET_EXTREME)


class _Loader:
    """The module's own loader, except that its code comes from the bundle."""

    def __init__(self, loader: Any, code: bytes) -> None:
        self._loader = loader
        self._code = code

    def create_module(self, spec: Any) -> Any:
        return self._loader.create_module(spec)

    def exec_module(self, module: Any) -> None:
        exec(marshal.loads(self._code), module.__dict__)  # noqa: S102 - code compiled from this module's own file

    def __getattr__(self, name: str) -> Any:
        # get_source for tracebacks, get_resource_reader for importlib.resources, and the rest.
        return getattr(self._loader, name)


class _Finder:
    def __init__(self, modules: dict[str, tuple[str, bytes]]) -> None:
        self.modules = modules
        self.served = 0

    def find_spec(self, name: str, path: Any = None, target: Any = None) -> Any:
        entry = self.modules.get(name)
        if entry is None:
            return None
        for finder in sys.meta_path:
            find = getattr(finder, "find_spec", None)
            if finder is self or find is None:
                continue
            spec = find(name, path, target)
            if spec is not None:
                break
        else:
            return None
        origin, code = entry
        if spec.origin != origin or spec.loader is None:
            return spec
        del self.modules[name]  # served once; a reload compiles from source
        spec.loader = _Loader(spec.loader, code)
        self.served += 1
        return spec


def install(path: str) -> int:
    """Serve imports from the bundle at `path`. Returns how many modules it holds; 0 if none fit.

    A bundle made by another Python (its magic number differs) holds code this one cannot run,
    and is ignored whole rather than half-used.
    """
    try:
        with open(path, "rb") as f:
            bundle = marshal.loads(lzma.decompress(f.read()))
    except (OSError, ValueError, EOFError, TypeError, lzma.LZMAError):
        return 0
    if not isinstance(bundle, dict) or bundle.get("format") != _FORMAT or bundle.get("magic") != importlib.util.MAGIC_NUMBER:
        return 0
    modules = {name: entry for name, entry in bundle["modules"].items() if name not in sys.modules}
    sys.meta_path.insert(0, _Finder(modules))
    return len(modules)

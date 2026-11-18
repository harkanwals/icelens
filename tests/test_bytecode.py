"""The compiled-module bundle the browser's demo imports from (icelens.bytecode).

Run here on CPython with a package made in a temp directory; tests/pyodide checks the real bundle
is used under Pyodide.
"""

from __future__ import annotations

import importlib
import lzma
import marshal
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from icelens import bytecode


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "src"
    pkg = root / "bcpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("WHERE = 'source'\n")
    (pkg / "mod.py").write_text("def answer():\n    return 42\n")
    (pkg / "other.py").write_text("NAME = 'other'\n")
    (pkg / "data.txt").write_text("a resource\n")
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    before = list(sys.meta_path)
    yield pkg
    sys.meta_path[:] = before
    for name in [n for n in sys.modules if n == "bcpkg" or n.startswith("bcpkg.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def _forget() -> None:
    for name in [n for n in sys.modules if n == "bcpkg" or n.startswith("bcpkg.")]:
        del sys.modules[name]
    importlib.invalidate_caches()


def _finder() -> object:
    return next(f for f in sys.meta_path if isinstance(f, bytecode._Finder))


def _bundle(tmp_path: Path, names: list[str]) -> Path:
    for name in names:
        importlib.import_module(name)
    path = tmp_path / "bundle.xz"
    path.write_bytes(bytecode.pack(names))
    _forget()
    return path


def test_modules_come_from_the_bundle_and_keep_their_files(package: Path, tmp_path: Path) -> None:
    path = _bundle(tmp_path, ["bcpkg", "bcpkg.mod"])
    # The source changes after packing; a module served from the bundle runs the packed code.
    (package / "__init__.py").write_text("WHERE = 'edited'\n")
    assert bytecode.install(str(path)) == 2
    import bcpkg
    import bcpkg.mod

    assert bcpkg.WHERE == "source" and bcpkg.mod.answer() == 42
    assert _finder().served == 2  # type: ignore[attr-defined]
    assert bcpkg.__file__ == str(package / "__init__.py")
    # Resources and source are still the package's own: the loader is wrapped, not replaced.
    assert (importlib.resources.files("bcpkg") / "data.txt").read_text() == "a resource\n"
    assert "edited" in (bcpkg.__loader__.get_source("bcpkg") or "")


def test_a_module_the_bundle_lacks_is_compiled_from_source(package: Path, tmp_path: Path) -> None:
    path = _bundle(tmp_path, ["bcpkg"])
    bytecode.install(str(path))
    import bcpkg.other

    assert bcpkg.other.NAME == "other" and _finder().served == 1  # type: ignore[attr-defined]


def test_a_module_found_elsewhere_than_where_it_was_packed_is_compiled_from_source(
    package: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _bundle(tmp_path, ["bcpkg"])
    moved = tmp_path / "moved" / "bcpkg"
    moved.mkdir(parents=True)
    (moved / "__init__.py").write_text("WHERE = 'moved'\n")
    monkeypatch.syspath_prepend(str(moved.parent))
    importlib.invalidate_caches()
    bytecode.install(str(path))
    import bcpkg

    assert bcpkg.WHERE == "moved" and _finder().served == 0  # type: ignore[attr-defined]


def test_a_bundle_from_another_python_is_ignored_whole(package: Path, tmp_path: Path) -> None:
    path = _bundle(tmp_path, ["bcpkg"])
    bundle = marshal.loads(lzma.decompress(path.read_bytes()))
    bundle["magic"] = b"\0\0\r\n"
    path.write_bytes(lzma.compress(marshal.dumps(bundle)))
    assert bytecode.install(str(path)) == 0
    assert not any(isinstance(f, bytecode._Finder) for f in sys.meta_path)


@pytest.mark.parametrize("content", [b"", b"not xz at all", lzma.compress(b"not marshal")])
def test_an_unreadable_bundle_is_ignored(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "bundle.xz"
    path.write_bytes(content)
    assert bytecode.install(str(path)) == 0
    assert bytecode.install(str(tmp_path / "missing.xz")) == 0


def test_importing_it_imports_no_package_but_itself() -> None:
    # The worker installs it before anything else; a third-party import here would be compiled
    # from source before the bundle could serve it.
    import subprocess

    code = "import sys; before = set(sys.modules); import icelens.bytecode; print(sorted(set(sys.modules) - before))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    added = {name.split(".")[0] for name in eval(out)}  # noqa: S307 - our own subprocess's repr of names
    assert added - set(sys.stdlib_module_names) == {"icelens"}, added

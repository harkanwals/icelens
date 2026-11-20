"""What `pip install icelens` actually gets: build the distributions, install one, run it.

Every other test imports icelens from the checkout, where every file is present whether or not
the package says so. A dependency missing from pyproject.toml, or `icelens.tcss` left out of the
wheel, passes all of them and fails on the first machine that installs the thing - which is how
`sql-sqlite` went missing once already. So this builds the sdist and the wheel the way a release
would (`uv build`, which builds the wheel *from* the sdist, so a file the sdist drops is caught
too), looks inside both, installs the wheel into a venv of its own, and drives the installed
`icelens` from outside: `--version`, `--explain`, a sandbox write, and the app itself starting
far enough to have loaded its stylesheet.

The venv is pinned to the lockfile's versions, so a failure here is this package's fault rather
than a release somebody else made this morning. It needs `uv`, a package index or a warm uv
cache, and half a minute, so it runs only when asked:

    ICELENS_TEST_PACKAGING=1 uv run pytest tests/test_packaging.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import version
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULES = ("__init__", "app", "catalog", "cli", "demo", "format", "model", "terms", "welcome")

pytestmark = [
    pytest.mark.packaging,
    pytest.mark.slow,
    pytest.mark.skipif(os.environ.get("ICELENS_TEST_PACKAGING") != "1", reason="builds and installs the package: set ICELENS_TEST_PACKAGING=1"),
]


def _uv(*args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    uv = shutil.which("uv")
    if uv is None:
        pytest.fail("ICELENS_TEST_PACKAGING=1 needs uv on PATH")
    done = subprocess.run([uv, *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, f"uv {' '.join(args)}\n{done.stdout}\n{done.stderr}"
    return done


@pytest.fixture(scope="module")
def dist(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("dist")
    _uv("build", "--out-dir", str(out))
    return out


@pytest.fixture(scope="module")
def wheel(dist: Path) -> Path:
    (found,) = dist.glob("icelens-*.whl")
    return found


@pytest.fixture(scope="module")
def sdist(dist: Path) -> Path:
    (found,) = dist.glob("icelens-*.tar.gz")
    return found


@pytest.fixture(scope="module")
def installed(tmp_path_factory: pytest.TempPathFactory, wheel: Path) -> Path:
    """A venv holding the wheel and its dependencies at the lockfile's versions, and nothing else."""
    home = tmp_path_factory.mktemp("venv")
    pins = home / "constraints.txt"
    exported = _uv("export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes", "--format", "requirements-txt")
    pins.write_text(exported.stdout)
    venv = home / "venv"
    _uv("venv", str(venv), "--python", sys.executable)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _uv("pip", "install", "--python", str(python), "--constraint", str(pins), str(wheel))
    return venv


def icelens(venv: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """The installed command, run from a directory that is not the checkout, so nothing leaks in."""
    command = venv / ("Scripts/icelens.exe" if os.name == "nt" else "bin/icelens")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ICELENS_", "PYTHON", "VIRTUAL_ENV"))}
    env["PYICEBERG_HOME"] = str(cwd)
    return subprocess.run([str(command), *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300)


# -- what is inside --------------------------------------------------------------------------------


def test_the_wheel_carries_every_module_and_the_stylesheet(wheel: Path) -> None:
    names = set(zipfile.ZipFile(wheel).namelist())
    for module in MODULES:
        assert f"icelens/{module}.py" in names, module
    assert "icelens/icelens.tcss" in names, "the app cannot draw without its stylesheet"
    assert "icelens/skills/icelens/SKILL.md" in names, "icelens --skill prints the skill from inside the package"
    assert not [n for n in names if n.startswith(("tests/", "docs/")) or "__pycache__" in n or n.endswith(".pyc")]


def test_the_wheel_says_what_it_needs(wheel: Path) -> None:
    archive = zipfile.ZipFile(wheel)
    (metadata,) = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
    text = archive.read(metadata).decode()
    assert f"Version: {version('icelens')}" in text
    assert "Requires-Python: >=3.10" in text
    requires = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("Requires-Dist:")]
    runtime = [r for r in requires if "extra ==" not in r]
    for needed in ("textual", "pyiceberg[pyarrow,sql-sqlite]", "duckdb", "pyarrow"):
        assert any(r.replace(" ", "").startswith(needed) for r in runtime), f"{needed} is not a runtime dependency: {runtime}"
    # An installer that ignores uv.lock takes the newest DuckDB the range allows; 2.0 broke
    # every sandbox CREATE, so the range must stop short of a major nobody has run the suite on.
    (duckdb,) = [r for r in runtime if r.replace(" ", "").startswith("duckdb")]
    assert "<2" in duckdb.replace(" ", ""), duckdb
    assert "Operating System :: OS Independent" not in text, "local catalogs do not work on Windows yet"
    (entry_points,) = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
    assert "icelens = icelens.cli:main" in archive.read(entry_points).decode()


def test_the_sdist_can_rebuild_the_wheel(sdist: Path) -> None:
    """`uv build` already built the wheel from it; this says what it relied on being there."""
    names = {name.split("/", 1)[1] for name in tarfile.open(sdist).getnames() if "/" in name}
    for needed in ("pyproject.toml", "README.md", "src/icelens/icelens.tcss", *(f"src/icelens/{m}.py" for m in MODULES)):
        assert needed in names, needed
    stray = [n for n in names if n.startswith((".venv", ".hypothesis", ".pytest_cache", ".coverage", "dist/")) or "__pycache__" in n]
    assert not stray, stray


def test_the_sdist_collects_its_own_tests(sdist: Path, tmp_path: Path) -> None:
    """Someone packaging icelens runs `pytest` in the unpacked sdist; a test that imports a file
    the sdist leaves out stops collection there, and then nothing runs at all."""
    with tarfile.open(sdist) as archive:
        archive.extractall(tmp_path, filter="data") if sys.version_info >= (3, 12) else archive.extractall(tmp_path)  # noqa: S202
    (tree,) = tmp_path.glob("icelens-*")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("ICELENS_", "PYTHON"))}
    env["PYTHONPATH"] = str(tree / "src")
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=tree, env=env, capture_output=True, text=True, timeout=300,
    )
    assert done.returncode == 0 and "error" not in done.stdout.lower().splitlines()[-1], done.stdout[-2000:] + done.stderr[-2000:]


# -- what it does once installed ------------------------------------------------------------------


def test_the_installed_command_knows_its_version(installed: Path, tmp_path: Path) -> None:
    done = icelens(installed, "--version", cwd=tmp_path)
    assert (done.returncode, done.stdout.strip()) == (0, version("icelens")), done.stderr


def test_the_installed_command_explains_without_a_catalog(installed: Path, tmp_path: Path) -> None:
    done = icelens(installed, "--explain", "manifest", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "Manifest" in done.stdout


def test_the_installed_command_writes_a_sandbox_and_reads_it_back(installed: Path, tmp_path: Path) -> None:
    """The one path that needs every dependency at once: SQLAlchemy, PyIceberg, DuckDB, PyArrow."""
    common = ["--sandbox", "t", "--sandbox-dir", str(tmp_path / "sandboxes")]
    done = icelens(installed, *common, "--write", "CREATE TABLE demo.s AS SELECT 1 AS x", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert "append on demo.s" in done.stdout
    done = icelens(installed, *common, "--pane", "data", "demo.s", "--json", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["rows"] == [{"x": 1}]


def test_the_installed_command_says_what_to_do_with_no_catalog(installed: Path, tmp_path: Path) -> None:
    """The first thing a new install meets: nothing configured, and a way to start anyway."""
    done = icelens(installed, "--list", cwd=tmp_path)
    assert done.returncode == 1 and "icelens --demo" in done.stderr


def test_the_installed_demo_makes_a_table_to_look_at(installed: Path, tmp_path: Path) -> None:
    """`--demo` writes with PyIceberg alone, so this holds on a machine with no DuckDB extension."""
    common = ["--demo", "--sandbox-dir", str(tmp_path / "sandboxes")]
    done = icelens(installed, *common, "--pane", "snapshots", "demo.orders", "--json", cwd=tmp_path)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["count"] == 5


def test_the_installed_app_starts_and_loads_its_stylesheet(installed: Path, tmp_path: Path) -> None:
    """Textual reads `icelens.tcss` when the app mounts, so a wheel without it fails here, not at import."""
    python = installed / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = (
        "import asyncio, sys\n"
        "from icelens.app import IcelensApp\n"
        "from icelens.catalog import Lake, sandbox_target\n"
        "async def main():\n"
        "    app = IcelensApp(Lake(sandbox_target('t', root=sys.argv[1])))\n"
        "    async with app.run_test() as pilot:\n"
        "        await pilot.pause()\n"
        "        assert app.stylesheet.rules, 'no stylesheet loaded'\n"
        "        print('started', app.query_one('#status').__class__.__name__)\n"
        "asyncio.run(main())\n"
    )
    done = subprocess.run([str(python), "-c", script, str(tmp_path / "sandboxes")], cwd=tmp_path, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    assert "started Static" in done.stdout

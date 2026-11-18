#!/usr/bin/env python3
"""Make a checkout of icelens ready to develop and test, in one command, for people and agents.

    python3 scripts/setup.py                         # uv, the dev environment, DuckDB's extensions
    python3 scripts/setup.py --extensions-from-pypi  # ...where extensions.duckdb.org is unreachable
    python3 scripts/setup.py --check                 # change nothing; say what is there and what is not

It is idempotent and uses only the standard library, so it runs before anything is installed.
Every agent's own setup hook calls this script rather than repeating its steps (see AGENTS.md),
so there is one place where "ready" is defined.

DuckDB downloads its extensions from extensions.duckdb.org the first time anything asks for
them, and some sandboxes cannot reach that host. `--extensions-from-pypi` then fetches the same
binaries from the `duckdb-extension-<name>` packages on PyPI. They are a third party's
repackaging, so nothing is trusted on that account: the download is checked against PyPI's
digest, and DuckDB checks the extension's signature - against DuckDB's own core key only, not
the community one - before it will install or load it. An extension that fails is removed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import site
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: What rows, SQL and sandbox writes need, dependencies first: the iceberg extension reads
#: manifests with `avro` and installs it as it loads, which is exactly the fetch that fails offline.
EXTENSIONS = ("avro", "httpfs", "aws", "iceberg")

#: DuckDB's own platform names, as `PRAGMA platform` says them, to the wheel tag that carries them.
WHEEL_TAGS = {
    "linux_amd64": "manylinux2014_x86_64",
    "linux_arm64": "manylinux2014_aarch64",
    "osx_amd64": "macosx_11_0_x86_64",
    "osx_arm64": "macosx_11_0_arm64",
    "windows_amd64": "win_amd64",
}

PYPI = "https://pypi.org/pypi"
TIMEOUT = 60


def say(label: str, text: str) -> None:
    print(f"  {label:<11} {text}", flush=True)


# -- uv and the environment --------------------------------------------------------------------


#: Where uv goes when this Python will not take a `pip --user` install (PEP 668, as on Ubuntu 24.04).
UV_VENV = Path.home() / ".local" / "share" / "icelens" / "uv"


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    scripts = "Scripts" if os.name == "nt" else "bin"
    exe = "uv.exe" if os.name == "nt" else "uv"
    for base in (
        Path(site.getuserbase()) / scripts,
        Path.home() / ".local" / "bin",
        Path.home() / ".cargo" / "bin",
        UV_VENV / scripts,
    ):
        if (base / exe).is_file():
            return str(base / exe)
    return None


def ensure_uv(check: bool) -> str | None:
    uv = find_uv()
    if uv or check:
        return uv
    # pip is on nearly every Python; the alternative, a curl-to-shell installer, is not something
    # a setup script should run on somebody's behalf.
    if _quietly([sys.executable, "-m", "pip", "install", "--user", "--quiet", "uv"]):
        # An externally managed Python refuses --user; a venv of uv's own is still allowed.
        pip = UV_VENV / ("Scripts" if os.name == "nt" else "bin") / "pip"
        if _quietly([sys.executable, "-m", "venv", str(UV_VENV)]) or _quietly([str(pip), "install", "--quiet", "uv"]):
            return None
    return find_uv()


def _quietly(command: list[str]) -> int:
    return subprocess.run(command, capture_output=True, check=False).returncode


def uv_version(uv: str) -> str:
    out = subprocess.run([uv, "--version"], capture_output=True, text=True, check=False).stdout
    return out.split()[1] if len(out.split()) > 1 else out.strip()


def sync(uv: str, locked: bool) -> bool:
    command = [uv, "sync", "--extra", "dev"]
    if locked:
        command.append("--locked")
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        say("environment", "uv sync failed:")
        print(result.stderr.strip(), file=sys.stderr)
        return False
    say("environment", f".venv synced ({' '.join(command[1:])})")
    return True


def docker_state() -> str:
    if not shutil.which("docker"):
        return "not installed: the live tests (Lakekeeper, Polaris) will skip"
    try:
        running = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False).returncode == 0
    except subprocess.TimeoutExpired:
        running = False
    if running:
        return "running: `make live-up` starts Lakekeeper and Polaris for the live tests"
    return "installed, daemon not running: the live tests will skip until it is"


# -- DuckDB's extensions, run inside the project's environment ---------------------------------


def extension_report(from_pypi: bool, check: bool) -> int:
    """Install and load each extension; print one line; return how many will not load."""
    import duckdb

    say("duckdb", duckdb.__version__)
    missing: dict[str, str] = {}
    for name in EXTENSIONS:
        reason = _load(duckdb, name)
        if reason and not check:
            reason = _install(duckdb, name) or _load(duckdb, name)
            if reason and from_pypi:
                reason = _install_from_pypi(duckdb, name)
        if reason:
            missing[name] = reason
    loaded = [name for name in EXTENSIONS if name not in missing]
    say("extensions", ", ".join(loaded) + " load" if loaded else "none load")
    for name, reason in missing.items():
        say("", f"{name} does not: {reason}")
    if missing:
        hint = "" if from_pypi or check else "; try --extensions-from-pypi"
        say("", f"tests marked duckdb_extensions will skip{hint}")
    return len(missing)


def _connect(duckdb, core_only: bool = False):
    # A fresh connection with nothing installed behind its back: explicit LOAD only, and the
    # default refusal of unsigned extensions left as it is. `core_only` also refuses binaries
    # signed with the community key, which DuckDB otherwise accepts: a third party's wheel
    # named `httpfs` has no business carrying anything DuckDB itself did not build.
    con = duckdb.connect(config={"allow_community_extensions": False} if core_only else {})
    con.execute("SET autoinstall_known_extensions = false")
    return con


def _load(duckdb, name: str, core_only: bool = False) -> str | None:
    con = _connect(duckdb, core_only)
    try:
        con.execute(f"LOAD {name}")
    except duckdb.Error as exc:
        return str(exc).splitlines()[0]
    finally:
        con.close()
    return None


def _install(duckdb, name: str) -> str | None:
    con = _connect(duckdb)
    try:
        con.execute(f"INSTALL {name}")
    except duckdb.Error as exc:
        return str(exc).splitlines()[0]
    finally:
        con.close()
    return None


def _install_from_pypi(duckdb, name: str) -> str | None:
    con = _connect(duckdb)
    try:
        platform = con.execute("PRAGMA platform").fetchone()[0]
    finally:
        con.close()
    tag = WHEEL_TAGS.get(platform)
    if tag is None:
        return f"no PyPI wheel for DuckDB platform {platform}"
    version = duckdb.__version__
    package = f"duckdb-extension-{name}"
    try:
        with urllib.request.urlopen(f"{PYPI}/{package}/{version}/json", timeout=TIMEOUT) as response:
            release = json.load(response)
    except OSError as exc:
        return f"{package} {version} not on PyPI ({exc})"
    wheels = [f for f in release.get("urls", []) if f.get("filename", "").endswith(f"-{tag}.whl")]
    if not wheels:
        return f"{package} {version} has no {tag} wheel"
    wheel = wheels[0]
    with tempfile.TemporaryDirectory(prefix="icelens-ext-") as scratch:
        path = Path(scratch) / wheel["filename"]
        try:
            with urllib.request.urlopen(wheel["url"], timeout=TIMEOUT) as response, path.open("wb") as out:
                shutil.copyfileobj(response, out)
        except OSError as exc:
            return f"download of {wheel['filename']} failed ({exc})"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != wheel.get("digests", {}).get("sha256"):
            return f"{wheel['filename']} does not match PyPI's sha256; not installed"
        member = f"duckdb_extension_{name}/extensions/v{version}/{name}.duckdb_extension"
        try:
            with zipfile.ZipFile(path) as archive:
                binary = Path(archive.extract(member, scratch))
        except (KeyError, zipfile.BadZipFile) as exc:
            return f"{wheel['filename']} has no {member} ({exc})"
        con = _connect(duckdb, core_only=True)
        try:
            literal = "'" + str(binary).replace("'", "''") + "'"
            con.execute(f"FORCE INSTALL {literal}")
            installed = con.execute(
                "SELECT install_path FROM duckdb_extensions() WHERE extension_name = ?", [name]
            ).fetchone()
        except duckdb.Error as exc:
            return f"FORCE INSTALL from {wheel['filename']} failed: {str(exc).splitlines()[0]}"
        finally:
            con.close()
    reason = _load(duckdb, name, core_only=True)
    if reason and installed and installed[0]:
        # Whatever DuckDB will not load stays out of its extension directory.
        for leftover in (Path(installed[0]), Path(installed[0] + ".info")):
            leftover.unlink(missing_ok=True)
        return f"the PyPI binary did not load and was removed: {reason}"
    return reason


# -- the command -------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="change nothing; report what is ready")
    parser.add_argument(
        "--extensions-from-pypi",
        action="store_true",
        default=os.environ.get("ICELENS_SETUP_EXTENSIONS_FROM_PYPI") == "1",
        help="when extensions.duckdb.org is unreachable, fetch DuckDB's extensions from PyPI",
    )
    parser.add_argument(
        "--no-extensions",
        action="store_true",
        default=os.environ.get("ICELENS_SETUP_NO_EXTENSIONS") == "1",
        help="leave DuckDB's extensions alone",
    )
    parser.add_argument("--locked", action="store_true", help="fail if uv.lock is out of date, as CI does")
    parser.add_argument("--extensions-only", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.extensions_only:
        # The second half, re-run under the project's own interpreter, where duckdb is.
        return 1 if extension_report(args.extensions_from_pypi, args.check) else 0

    print("icelens setup" + (" (check only)" if args.check else ""), flush=True)
    uv = ensure_uv(args.check)
    if uv is None:
        say("uv", "missing: see https://docs.astral.sh/uv/getting-started/installation/")
        return 1
    say("uv", f"{uv_version(uv)} ({uv})")

    if args.check:
        ready = (ROOT / ".venv").is_dir()
        say("environment", ".venv present" if ready else ".venv missing: run python3 scripts/setup.py")
    elif not sync(uv, args.locked):
        return 1
    else:
        ready = True

    if ready and not args.no_extensions:
        command = [uv, "run", "--no-sync", "python", str(Path(__file__).resolve()), "--extensions-only"]
        if args.extensions_from_pypi:
            command.append("--extensions-from-pypi")
        if args.check:
            command.append("--check")
        # Missing extensions are reported, not fatal: the tests that need them skip.
        subprocess.run(command, cwd=ROOT, check=False)

    say("docker", docker_state())
    print("next: uv run pytest -q   (or: make check)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

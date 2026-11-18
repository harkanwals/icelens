"""Driving the CLI the way a script does: argv in; an exit code, stdout and stderr out.

Shared by the JSON contract and the argument matrix, which both care about the three things a
caller can observe and nothing else. `run` catches the SystemExit argparse raises, so a usage
error is an exit code like any other rather than an exception the test has to expect.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from icelens.catalog import Lake
from icelens.cli import main

#: the variables that steer the test run itself, and so survive `clean_environment`
_OURS = (
    "ICELENS_REQUIRE_DUCKDB_EXTENSIONS",
    "ICELENS_TEST_WITHOUT_DUCKDB_EXTENSIONS",
    "ICELENS_REQUIRE_LIVE_CATALOGS",
    "ICELENS_HYPOTHESIS_EXAMPLES",
)


def argv(lake: Lake, *rest: str) -> list[str]:
    """Point the CLI at a test catalog without going near a .env or a config file."""
    out = ["--no-env"]
    for key, value in lake.target.properties.items():
        out += ["-p", f"{key}={value}"]
    return [*out, *rest]


def _refuse(constant: str) -> None:
    raise AssertionError(f"bare {constant} is not JSON: node, Go and serde all reject the document")


def strict(text: str) -> Any:
    """JSON as a strict parser reads it: `NaN`, `Infinity` and `-Infinity` are errors, not numbers."""
    return json.loads(text, parse_constant=_refuse)


@dataclass(frozen=True)
class Ran:
    code: int
    out: str
    err: str

    @property
    def body(self) -> dict[str, Any]:
        """The one object on stdout: one line, newline-terminated, strictly parsed."""
        assert self.out.endswith("\n") and self.out.count("\n") == 1, f"not one line of JSON: {self.out[:200]!r}"
        return strict(self.out)

    @property
    def error(self) -> dict[str, Any]:
        """The error object on stderr, with stdout left empty so a pipe is never fed half an answer."""
        assert self.out == "", f"an error must leave stdout empty, got {self.out[:200]!r}"
        assert self.err.endswith("\n") and self.err.count("\n") == 1, f"not one line of JSON: {self.err[:200]!r}"
        return strict(self.err)


def run(capsys: pytest.CaptureFixture[str], args: list[str]) -> Ran:
    try:
        code = main(list(args))
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return Ran(code, captured.out, captured.err)


def clean_environment(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """No `ICELENS_*` settings, no ~/.pyiceberg.yaml and no .env: only what the test passes.

    `ICELENS_*` is read even under `--no-env`, so a developer with the live suite's variables
    exported would otherwise run these against a catalog they did not name.
    """
    for key in [k for k in os.environ if k.startswith("ICELENS_") and k not in _OURS]:
        monkeypatch.delenv(key)
    monkeypatch.setenv("PYICEBERG_HOME", str(home))
    # PyIceberg reads the first .pyiceberg.yaml it finds in PYICEBERG_HOME, then ~, then the
    # working directory - so an empty PYICEBERG_HOME still reaches the developer's own file, and
    # "no catalogs" tests failed on any machine that has one. A file with no catalogs stops the
    # search here; HOME stays put, because DuckDB keeps its extensions under it. A test that
    # wants catalogs overwrites this file.
    config = home / ".pyiceberg.yaml"
    if not config.exists():
        config.write_text("isolated: by tests/headless.py\n")
    monkeypatch.chdir(home)

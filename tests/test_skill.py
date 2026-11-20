"""The agent skill: `icelens --skill` prints a SKILL.md that teaches a coding agent the headless flags.

It ships inside the package so the instructions an agent reads are the ones for the version it
calls. What has to hold is that every command it shows is one this icelens accepts, and that
the demo commands an agent would start with actually answer.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import pytest
from headless import clean_environment, run

from icelens.cli import build_parser
from icelens.model import PANES

SKILL = Path(__file__).resolve().parents[1] / "src" / "icelens" / "skills" / "icelens" / "SKILL.md"


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    clean_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def commands() -> list[list[str]]:
    """Every `icelens ...` line inside the skill's code blocks, as argv, comments dropped."""
    text = SKILL.read_text(encoding="utf-8")
    found = []
    for block in re.findall(r"```bash\n(.*?)```", text, re.S):
        for line in block.splitlines():
            if line.startswith("icelens "):
                found.append(shlex.split(line, comments=True)[1:])
    return found


def test_skill_prints_the_file_with_the_frontmatter_agents_read(capsys) -> None:
    ran = run(capsys, ["--skill"])
    assert ran.code == 0 and ran.out == SKILL.read_text(encoding="utf-8")
    front = re.match(r"---\nname: (.+)\ndescription: (.+)\n---\n", ran.out)
    assert front, "SKILL.md must open with name and description frontmatter"
    name, description = front.groups()
    # The Agent Skills format: a lowercase, hyphenated name, and a description that says when to use it.
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name) and len(name) <= 64
    assert name == SKILL.parent.name == "icelens"  # the spec: the folder is named after the skill
    assert 0 < len(description) <= 1024 and "Use when" in description


def test_every_command_in_the_skill_is_one_this_icelens_accepts() -> None:
    parser = build_parser()
    shown = commands()
    assert len(shown) >= 15, "the skill's examples were not found"
    for argv in shown:
        args = parser.parse_args([a if a != "..." else "SELECT 1" for a in argv])
        if args.pane:
            assert args.pane in PANES


def test_the_panes_the_skill_names_are_the_panes_there_are() -> None:
    text = SKILL.read_text(encoding="utf-8")
    for pane in PANES:
        assert f"`{pane}`" in text or f"--pane {pane}" in text, f"the skill never mentions the {pane} pane"


@pytest.mark.duckdb_extensions
def test_the_demo_commands_an_agent_starts_with_answer_in_json(capsys, tmp_path: Path) -> None:
    demo = ["--demo", "--sandbox-dir", str(tmp_path / "sandboxes")]
    listed = run(capsys, [*demo, "--list", "--json"])
    assert listed.code == 0 and json.loads(listed.out)["kind"] == "namespaces"
    snapshots = json.loads(run(capsys, [*demo, "--pane", "snapshots", "demo.orders", "--json"]).out)
    assert snapshots["count"] == 5
    sql = 'SELECT region, count(*) AS n FROM icelens."demo"."orders" GROUP BY 1'
    answered = json.loads(run(capsys, [*demo, "--sql", sql, "--json"]).out)
    assert answered["kind"] == "sql" and answered["count"] > 0
    refused = run(capsys, [*demo, "--sql", "DELETE FROM icelens.demo.orders", "--json"])
    assert refused.code == 1 and refused.out == "" and "refusing" in json.loads(refused.err)["error"]["message"]

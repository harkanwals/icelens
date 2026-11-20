"""The home page's walk: the real app on the demo table, and labels on what each scene shows."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "screenshot.py").is_file():
    pytest.skip("the website's scripts are not in this source tree", allow_module_level=True)
sys.path.insert(0, str(ROOT / "scripts"))

import screenshot  # noqa: E402


async def test_every_scene_labels_what_it_shows(tmp_path: Path) -> None:
    shots = await screenshot.capture(120, 36, tmp_path)
    assert [shot["name"] for shot in shots] == [scene["name"] for scene in screenshot.SCENES]
    for shot, scene in zip(shots, screenshot.SCENES, strict=True):
        lines = shot["dark"]["text"].splitlines()
        assert len(lines) == 36 and "<pre>" in shot["light"]["html"]
        # One session, so both themes show the same ids and the labels land in the same cells.
        assert shot["light"]["text"] == shot["dark"]["text"] and shot["light"]["places"] == shot["dark"]["places"]
        assert len(shot["dark"]["places"]) == len(shot["notes"]) == len(scene["callouts"])
        for (needle, side, _), (row, col) in zip(scene["callouts"], shot["dark"]["places"], strict=True):
            # On the row that shows the text, beside it, and on the screen.
            assert row > 0 and needle in lines[row] and 0 <= col < 120
            at = lines[row].index(needle)
            assert (col < at) if side in ("before", "row") else (col > at + len(needle))
    # The walk is time travel: by the third scene the rows are read as of an older commit.
    assert "as of snapshot" in shots[2]["dark"]["text"] and "DELETED" in shots[3]["dark"]["text"]


def test_a_label_whose_text_is_gone_fails_the_capture() -> None:
    # A screenshot that drifted from its labels would point readers at the wrong thing.
    with pytest.raises(RuntimeError, match="no longer shows 'no such text'"):
        screenshot.places(["title", "a line on screen"], (("no such text", "after", "x"),))


def test_a_row_label_sits_at_the_start_of_its_row_inside_the_pane() -> None:
    lines = ["title", "│ catalog ││ 3  │  overwrite  123"]
    [(row, col)] = screenshot.places(lines, (("overwrite", "row", "x"),))
    assert (row, col) == (1, lines[1].index("3") - 1.2)


def test_the_command_prints_the_walk_and_publishes_no_local_path(capsys: Any, monkeypatch: Any, tmp_path: Path) -> None:
    monkeypatch.setattr(screenshot, "HOME", tmp_path / "shot")
    monkeypatch.setitem(sys.modules, "duckdb", sys.modules.get("duckdb"))  # restored after
    assert screenshot.main(["120", "36"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["cols"], got["rows"]) == (120, 36) and len(got["scenes"]) == len(screenshot.SCENES)
    first = got["scenes"][0]
    assert "#0b141c" in first["dark"]["html"] and "#f6f9fb" in first["light"]["html"]
    assert "read through pyiceberg" in first["dark"]["text"]  # the same picture with or without DuckDB
    assert not (tmp_path / "shot").exists()

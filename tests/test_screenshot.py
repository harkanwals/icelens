"""The home page's screenshot: the real app on the demo table, and labels on what it shows."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "screenshot.py").is_file():
    pytest.skip("the website's scripts are not in this source tree", allow_module_level=True)
sys.path.insert(0, str(ROOT / "scripts"))

import screenshot  # noqa: E402


async def test_the_screenshot_labels_what_it_shows(tmp_path: Path) -> None:
    shot = await screenshot.capture(120, 36, tmp_path, dark=True)
    lines = shot["text"].splitlines()
    assert len(lines) == 36 and "demo.orders" in shot["html"] and "<pre>" in shot["html"]
    assert len(shot["places"]) == len(shot["notes"]) == len(screenshot.CALLOUTS)
    for (needle, side, _), (row, col) in zip(screenshot.CALLOUTS, shot["places"], strict=True):
        # On the row that shows the text, beside it, and on the screen.
        assert row > 0 and needle in lines[row] and 0 <= col < 120
        at = lines[row].index(needle)
        assert (col < at) if side == "before" else (col > at + len(needle))


def test_a_label_whose_text_is_gone_fails_the_capture() -> None:
    # A screenshot that drifted from its labels would point readers at the wrong thing.
    with pytest.raises(RuntimeError, match="no longer shows 'no such text'"):
        screenshot.places(["title", "a line on screen"], (("no such text", "after", "x"),))


def test_the_command_prints_both_themes_and_publishes_no_local_path(capsys: Any, monkeypatch: Any, tmp_path: Path) -> None:
    import json

    monkeypatch.setattr(screenshot, "HOME", tmp_path / "shot")
    monkeypatch.setitem(sys.modules, "duckdb", sys.modules.get("duckdb"))  # restored after
    assert screenshot.main(["120", "36"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["cols"], got["rows"]) == (120, 36) and "#0b141c" in got["dark"]["html"] and "#f6f9fb" in got["light"]["html"]
    assert "read through pyiceberg" in got["dark"]["text"]  # the same picture with or without DuckDB
    assert not (tmp_path / "shot").exists()

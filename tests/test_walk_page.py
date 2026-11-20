"""The home page's script, against scripts/walk.py's rules: the same keys and clicks, the same screens.

tests/test_walk.py checks the rules against the real app; this checks the page keeps them. The
page's script is a second copy of `step`, `click`, `hovered` and `compose`, in JavaScript, and a
copy drifts: this drives the built page in headless Chrome and compares every cell. It needs
Chrome and Node 22 or newer (for its WebSocket), and skips without them.
"""

from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "walk.py").is_file():
    pytest.skip("the website's scripts are not in this source tree", allow_module_level=True)
sys.path.insert(0, str(ROOT / "scripts"))

import build_site  # noqa: E402
import test_walk as tw  # noqa: E402

CHROMES = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "google-chrome", "google-chrome-stable",
           "chromium", "chromium-browser")


def chrome() -> str | None:
    if os.environ.get("CHROME"):
        return os.environ["CHROME"]
    return next((found for name in CHROMES if (found := shutil.which(name) or (name if Path(name).is_file() else None))), None)


def node() -> str | None:
    found = shutil.which("node")
    if not found:
        return None
    version = subprocess.run([found, "--version"], capture_output=True, text=True).stdout.strip().lstrip("v")
    return found if version and int(version.split(".")[0]) >= 22 else None


@pytest.fixture(scope="module")
def site(tmp_path_factory: pytest.TempPathFactory, walk_graph: dict[str, Any]):
    if not chrome() or not node():
        pytest.skip("needs Chrome and Node 22 or newer")
    out = tmp_path_factory.mktemp("site") / "_site"
    build_site.build(out, walk_graph)
    handler = functools.partial(QuietHandler, directory=str(out))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    finally:
        server.shutdown()


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def on_page(url: str, actions: list[list[Any]], tmp_path: Path) -> dict[str, Any]:
    (tmp_path / "actions.json").write_text(json.dumps(actions))
    subprocess.run([str(node()), str(ROOT / "tests" / "walk_page.mjs"), str(chrome()), url, str(tmp_path / "actions.json"),
                    str(tmp_path / "out.json"), str(tmp_path / "profile")], check=True, timeout=180)
    return json.loads((tmp_path / "out.json").read_text())


def test_the_page_draws_what_the_rules_draw(site: str, walk_graph: dict[str, Any], tmp_path: Path) -> None:
    graph = walk_graph
    keys: list[Any] = []
    for step in (
        lambda k: tw.on_cell(graph, k, 3, 1), lambda k: "ArrowDown",
        lambda k: tw.on_cell(graph, k, 4, 1),        # the cursor's own cell
        lambda k: tw.on_tab(graph, k, "snapshots"), lambda k: "ArrowRight", lambda k: "ArrowLeft",
        lambda k: tw.on_cell(graph, k, 2, 0), lambda k: tw.on_cell(graph, k, 2, 0),
        lambda k: tw.on_footer(graph, k, "u"),
        lambda k: tw.on_tab(graph, k, "overview"),
        lambda k: "Tab",                             # into a grid whose state is the walk's first, 0
        lambda k: tw.on_tab(graph, k, "data"), lambda k: tw.on_cell(graph, k, 5, 3),
        lambda k: tw.on_cell(graph, k, 9, -1), lambda k: "ArrowLeft",
        lambda k: "4", lambda k: "ArrowDown", lambda k: "Enter", lambda k: ",", lambda k: ",",
        lambda k: tw.on_tab(graph, k, "commit"), lambda k: ".",
        lambda k: "?", lambda k: "ArrowDown", lambda k: "PageDown", lambda k: "Escape",
        lambda k: "u", lambda k: "3", *[lambda k: "ArrowDown"] * 12,
    ):
        keys.append(step(keys))
    actions: list[list[Any]] = [["click", k[1], k[2]] if isinstance(k, tuple) else ["key", k] for k in keys]
    actions.insert(12, ["theme", "light"])  # the page redraws in the other theme where it stands
    got = on_page(site, actions, tmp_path)
    assert got["errors"] == []
    want = tw.replay(graph, keys, toasts=True)
    light = graph | graph["light"]
    done = 0
    for n, shot in enumerate(got["shots"]):
        if n and actions[n - 1][0] != "theme":
            done += 1
        screens = graph if shot["theme"] == "dark" else light
        styles = [*screens["styles"], ""]  # text the page writes without a span has no style
        page = tw.styled([[[t, s if s >= 0 else len(styles) - 1] for t, s in row if t] for row in shot["rows"]], styles)
        assert shot["theme"] == ("light" if n > 12 else "dark")
        try:
            tw.same([want[done]], [page], [])
        except AssertionError as e:
            raise AssertionError(f"after {actions[max(0, n - 3):n]} (action {n}): {e}") from None

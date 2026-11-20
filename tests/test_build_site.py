"""The website build: the Markdown renderer, and the pages it writes.

The renderer is ours rather than a dependency, so these hold it to the constructs the docs use
and to the one rule that matters on a public page: text from a Markdown file is never markup.
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "scripts" / "build_site.py").is_file():
    # The sdist ships the package and its tests, not the website; a plain `pytest` there must
    # still collect everything else.
    pytest.skip("the website's build script is not in this source tree", allow_module_level=True)
sys.path.insert(0, str(ROOT / "scripts"))

import build_site  # noqa: E402
from build_site import DOCS, REPO, render  # noqa: E402


def md(text: str, source: str = "README.md") -> str:
    return render(source, text)


def test_a_table_keeps_its_columns_and_pipes_inside_code():
    out = md("| key | pane |\n|---|:-:|\n| `a | b` | **Overview** |\n| 2 | Schema |\n")
    assert "<thead><tr><th>key</th>" in out
    assert '<th style="text-align:center">pane</th>' in out
    assert "<td><code>a | b</code></td>" in out
    assert "<strong>Overview</strong>" in out
    assert out.count("<tr>") == 3


def test_nested_lists_nest():
    out = md("- one\n  - two\n    1. three\n- four\n")
    assert re.sub(r"\s+", "", out) == (
        "<ul><li>one<ul><li>two<ol><li>three</li></ol></li></ul></li><li>four</li></ul>"
    )


def test_a_list_item_continues_onto_its_next_lines():
    out = md("- **Bold.** first line\n  second line\n- next\n")
    assert "<li><strong>Bold.</strong> first line\nsecond line</li>" in out


def test_an_ordered_list_keeps_its_start_and_loose_items_get_paragraphs():
    out = md("3. a\n\n4. b\n")
    assert '<ol start="3">' in out
    assert "<li><p>a</p></li>" in out


def test_a_code_fence_is_escaped_and_not_parsed():
    out = md("```html\n<script>alert('x')</script>\n**not bold** [not](a link)\n```\n")
    assert "<script" not in out
    assert "&lt;script&gt;alert(&#x27;x&#x27;)&lt;/script&gt;" in out
    assert "<strong>" not in out and "<a " not in out
    assert 'class="language-html"' in out


def test_raw_html_in_text_is_text():
    out = md('A <script>alert(1)</script> and <img src=x onerror="y"> and `<b>`.\n')
    assert "<script" not in out and "<img" not in out
    assert "&lt;script&gt;" in out and "<code>&lt;b&gt;</code>" in out


def test_links_between_docs_go_to_the_pages():
    out = md("[P](docs/PLATFORMS.md) and [S](docs/PLATFORMS.md#status) and [A](AGENTS.md)")
    assert '<a href="platforms.html">P</a>' in out
    assert '<a href="platforms.html#status">S</a>' in out
    assert f'<a href="{REPO}/blob/main/AGENTS.md">A</a>' in out


def test_links_resolve_relative_to_the_doc_they_are_in():
    out = md("[r](../README.md) [u](GUIDE.md#3) [t](../tests/live.py)", source="docs/PLATFORMS.md")
    assert '<a href="start.html">r</a>' in out
    assert '<a href="index.html#3">u</a>' in out
    assert f'<a href="{REPO}/blob/main/tests/live.py">t</a>' in out


def test_only_web_links_survive():
    out = md('[x](javascript:alert(1)) [y](data:text/html,hi) [z](https://example.com/"a")')
    assert "javascript" not in out.split("x")[0] and 'href="javascript' not in out
    assert 'href="data' not in out
    assert "x" in out and "y" in out
    assert 'href="https://example.com/&quot;a&quot;"' in out


def test_inline_code_and_emphasis_inside_link_text():
    out = md("[**PyIceberg's own CLI**](https://py.iceberg.apache.org/cli/) (`pyiceberg list`)")
    assert "<a href=\"https://py.iceberg.apache.org/cli/\"><strong>PyIceberg's own CLI</strong></a>" in out
    assert "<code>pyiceberg list</code>" in out


def test_underscores_inside_words_are_not_emphasis():
    out = md("snake_case_name and _this_ and *that*")
    assert "snake_case_name" in out
    assert "<em>this</em>" in out and "<em>that</em>" in out


def test_headings_get_githubs_anchors_and_duplicates_are_numbered():
    out = md("## Status\n\n### 1. OAuth ✅\n\n## Status\n")
    assert '<h2 id="status">Status</h2>' in out
    assert '<h3 id="1-oauth-">' in out
    assert '<h2 id="status-1">' in out


def test_blockquote_and_rule_and_escapes():
    out = md("> quoted *note*\n> more\n\n---\n\n\\#11 is literal\n")
    assert "<blockquote>\n<p>quoted <em>note</em>\nmore</p>\n</blockquote>" in out
    assert "<hr>" in out
    assert "<p>#11 is literal</p>" in out


class Tags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags: list[str] = []
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.hrefs += [v or "" for k, v in attrs if k == "href"]


@pytest.mark.parametrize("source", list(DOCS))
def test_every_doc_renders_to_safe_html(source):
    text = (ROOT / source).read_text(encoding="utf-8")
    out = render(source, text)
    assert "<script" not in out.lower()
    assert "\x00" not in out
    parser = Tags()
    parser.feed(out)
    allowed = {
        "h1", "h2", "h3", "h4", "h5", "h6", "p", "a", "code", "pre", "em", "strong", "ul", "ol", "li",
        "blockquote", "hr", "div", "table", "thead", "tbody", "tr", "th", "td",
    }
    assert set(parser.tags) <= allowed, set(parser.tags) - allowed
    for href in parser.hrefs:
        assert href.startswith(("https://", "http://", "#", "mailto:")) or re.fullmatch(r"[a-z]+\.html(#.*)?", href), href
    # Every fence opened in the source became one <pre>.
    assert out.count("<pre>") == len(re.findall(r"^```", text, re.M)) // 2


def test_the_build_writes_the_pages(tmp_path):
    out = tmp_path / "_site"
    made = build_site.build(out)
    assert (out / "index.html").is_file() and (out / "style.css").is_file()
    assert (out / ".nojekyll").is_file()
    for page, _ in DOCS.values():
        html = (out / "docs" / page).read_text(encoding="utf-8")
        assert re.search(r'<link rel="stylesheet" href="\.\./style\.css\?v=[0-9a-f]{12}">', html)
        assert '<header class="site">' in html
        assert f"docs/{page}" in made
    # The hand-written page gets the same header and footer as the docs, filled in at build.
    home = (out / "index.html").read_text(encoding="utf-8")
    assert "<!--#" not in home and '<header class="site">' in home and '<footer class="site">' in home
    assert "aria-current" not in home
    # The home page explains the app with a walk through labelled screens of it, one label per
    # note in every scene, and runs no app: the way to try it is on your own machine.
    scenes = re.findall(r'<div class="scene" id="scene-([\w-]+)".*?</ol>', home, re.S)
    assert len(scenes) == home.count('role="tab" id=') == 4
    for scene in re.findall(r'<div class="scene" .*?</ol>', home, re.S):
        pins = re.findall(r'class="pin"[^>]*data-n="(\d+)"', scene)
        notes = re.findall(r'<li data-n="(\d+)"', scene)
        assert pins == notes and pins == [str(n) for n in range(1, len(pins) + 1)]
    assert "uvx icelens --demo" in home
    assert not any((out / name).exists() for name in ("try.html", "demo.js", "worker.js", "wheels"))
    assert not re.search(r"<script[^>]+src=", home) and "try.html" not in home
    # Every reference to the stylesheet names this build, so a browser holding an earlier one
    # does not pair it with this build's pages.
    for path in [*out.glob("*.html"), *out.glob("docs/*.html")]:
        text = path.read_text(encoding="utf-8")
        for name in build_site.STAMPED:
            assert not re.search(rf"""["'](\.\.?/)?{re.escape(name)}["']""", text), (path.name, name)
    # A stylesheet from another origin runs with this one's authority, so each carries the hash of
    # the bytes it is meant to be.
    for path in [*out.glob("*.html"), *out.glob("docs/*.html")]:
        html = path.read_text(encoding="utf-8")
        for tag in re.findall(r"<script[^>]+src=\"https?://[^>]*>|<link[^>]+rel=\"stylesheet\"[^>]+href=\"https?://[^>]*>", html):
            assert re.search(r'integrity="sha384-[A-Za-z0-9+/]{64}"', tag) and 'crossorigin="anonymous"' in tag, (path.name, tag)
    # A rebuild replaces a previous build; anything else in the way is refused.
    build_site.build(out)
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.txt").write_text("mine")
    with pytest.raises(build_site.BuildError, match="not a previous build"):
        build_site.build(other)
    assert (other / "keep.txt").read_text() == "mine"


def test_the_build_refuses_the_checkout_itself(capsys):
    assert build_site.main(["--out", str(ROOT)]) == 1
    err = capsys.readouterr().err.strip()
    assert err.startswith("build_site: refusing to build into") and "\n" not in err


def test_every_page_is_a_link_preview_at_the_address_it_is_served_from(tmp_path, monkeypatch):
    # Networks read only absolute URLs, so a site served somewhere new must say so everywhere.
    monkeypatch.setenv("ICELENS_SITE_URL", "https://icelens.example/")
    out = tmp_path / "_site"
    build_site.build(out)
    card = (out / build_site.CARD).read_bytes()
    assert card[:8] == b"\x89PNG\r\n\x1a\n"
    assert (int.from_bytes(card[16:20], "big"), int.from_bytes(card[20:24], "big")) == (1200, 630)
    for path in ["index.html", *(f"docs/{page}" for page, _ in DOCS.values())]:
        html = (out / path).read_text(encoding="utf-8")
        head = html[: html.index("</head>")]
        url = "https://icelens.example/" + ("" if path == "index.html" else path)
        assert f'<link rel="canonical" href="{url}">' in head and f'<meta property="og:url" content="{url}">' in head, path
        assert '<meta property="og:image" content="https://icelens.example/og.png">' in head
        assert '<meta name="twitter:card" content="summary_large_image">' in head
        for tag in ("og:title", "og:description", "twitter:title", "twitter:description"):
            content = re.search(rf'<meta (?:property|name)="{tag}" content="([^"]+)">', head)
            assert content and content.group(1).strip(), (path, tag)
        assert re.search(r'<meta name="description" content="[^"]{40,}">', head), path
        assert "harkanwals.github.io" not in head

#!/usr/bin/env python3
"""Build the website GitHub Pages serves: `python3 scripts/build_site.py [--out _site]`.

The site is hand-written HTML in `site/`, plus the repository's Markdown rendered into pages and
the icelens wheel the in-browser demo installs. Standard library only, so the Pages workflow needs
nothing but Python and uv, and the Markdown renderer is the small one below rather than a
dependency: it handles what README.md and docs/ actually use, and escapes everything, so a
`<script>` written in a doc arrives as text.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/harkanwals/icelens"
BRANCH = "main"

# Repository path -> (page under docs/, its name in the docs list). Links between these are
# rewritten to the pages; a link to any other repository file goes to it on GitHub.
DOCS = {
    "docs/GUIDE.md": ("index.html", "Guide"),
    "README.md": ("start.html", "Start"),
    "docs/PLATFORMS.md": ("platforms.html", "Platforms"),
}


class BuildError(Exception):
    """A build that cannot finish; main() prints it as one line and exits 1."""


# --- Markdown ------------------------------------------------------------------------------------

FENCE = re.compile(r"^(```+|~~~+)\s*([\w+-]*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
MARKER = re.compile(r"^(\s*)([-*+]|\d{1,9}[.)])(\s+|$)")
TABLE_DIVIDER = re.compile(r"^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
LINK = re.compile(r"\[((?:[^\[\]]|\[[^\[\]]*\])+)\]\(\s*<?([^()\s<>]*(?:\([^()\s]*\)[^()\s<>]*)*)>?\s*\)")
SAFE_SCHEME = re.compile(r"^(https?:|mailto:)", re.I)
ANY_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
HOLD = re.compile(r"\x00(\d+)\x00")
CURRENT = ' aria-current="page"'


def slug(text: str) -> str:
    """GitHub's anchor for a heading, so `[Status](#status)` in a doc still lands."""
    text = re.sub(r"<[^>]*>", "", text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


class Markdown:
    """One document's renderer. `link` maps a link target as written to the href it becomes."""

    def __init__(self, link=lambda url: url):
        self.link = link
        self.held: list[str] = []
        self.ids: dict[str, int] = {}

    def render(self, text: str) -> str:
        # NUL is the placeholder marker below; a source that carries one gets it stripped first.
        lines = text.replace("\x00", "").replace("\r\n", "\n").replace("\t", "    ").split("\n")
        return self.blocks(lines, tight=False)

    # Blocks.

    def blocks(self, lines: list[str], tight: bool) -> str:
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            fence = FENCE.match(line) if line.startswith(("```", "~~~")) else None
            if fence:
                i = self.fence(lines, i, fence, out)
            elif m := HEADING.match(line):
                level, text = len(m.group(1)), m.group(2)
                inner = self.inline(text)
                anchor = self.anchor(slug(text))
                out.append(f'<h{level} id="{anchor}">{inner}</h{level}>')
                i += 1
            elif RULE.match(line):
                out.append("<hr>")
                i += 1
            elif line.lstrip().startswith(">"):
                quoted = []
                while i < len(lines) and lines[i].lstrip().startswith(">"):
                    quoted.append(re.sub(r"^\s*>\s?", "", lines[i]))
                    i += 1
                out.append(f"<blockquote>\n{self.blocks(quoted, tight=False)}\n</blockquote>")
            elif "|" in line and i + 1 < len(lines) and "|" in lines[i + 1] and TABLE_DIVIDER.match(lines[i + 1]):
                i = self.table(lines, i, out)
            elif MARKER.match(line):
                i = self.listing(lines, i, out)
            else:
                para = [line]
                i += 1
                while i < len(lines) and lines[i].strip() and not self.starts_block(lines, i):
                    para.append(lines[i])
                    i += 1
                text = self.inline("\n".join(s.strip() for s in para))
                out.append(text if tight else f"<p>{text}</p>")
        return "\n".join(out)

    def starts_block(self, lines: list[str], i: int) -> bool:
        line = lines[i]
        return bool(
            line.startswith(("```", "~~~")) or HEADING.match(line) or RULE.match(line)
            or line.lstrip().startswith(">") or MARKER.match(line)
        )

    def anchor(self, base: str) -> str:
        base = base or "section"
        n = self.ids.get(base, 0)
        self.ids[base] = n + 1
        return base if n == 0 else f"{base}-{n}"

    def fence(self, lines: list[str], i: int, m: re.Match, out: list[str]) -> int:
        mark, lang = m.group(1), m.group(2)
        body = []
        i += 1
        while i < len(lines) and not lines[i].strip().startswith(mark):
            body.append(lines[i])
            i += 1
        cls = f' class="language-{html.escape(lang)}"' if lang else ""
        code = html.escape("\n".join(body))
        out.append(f"<pre><code{cls}>{code}</code></pre>")
        return i + 1  # past the closing fence; an unclosed one runs to the end, as GitHub's does

    def table(self, lines: list[str], i: int, out: list[str]) -> int:
        head = self.cells(lines[i])
        aligns = []
        for cell in self.cells(lines[i + 1]):
            left, right = cell.startswith(":"), cell.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left" if left else "")
        i += 2
        rows = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            rows.append(self.cells(lines[i]))
            i += 1

        def row(cells: list[str], tag: str) -> str:
            tds = []
            for n in range(len(head)):
                align = aligns[n] if n < len(aligns) and aligns[n] else ""
                style = f' style="text-align:{align}"' if align else ""
                tds.append(f"<{tag}{style}>{self.inline(cells[n] if n < len(cells) else '')}</{tag}>")
            return "<tr>" + "".join(tds) + "</tr>"

        body = "\n".join(row(r, "td") for r in rows)
        out.append(
            f'<div class="table-scroll"><table>\n<thead>{row(head, "th")}</thead>\n'
            f"<tbody>\n{body}\n</tbody>\n</table></div>"
        )
        return i

    @staticmethod
    def cells(line: str) -> list[str]:
        line = line.strip()
        if line.startswith("|"):
            line = line[1:]
        if line.endswith("|") and not line.endswith("\\|"):
            line = line[:-1]
        # A pipe inside a code span stays in its cell: the docs write `a | b` in tables unescaped.
        cells, cell, tick = [], "", ""
        for part in re.split(r"(`+|\\\||\|)", line):
            if part.startswith("`"):
                tick = "" if part == tick else tick or part
            if part == "|" and not tick:
                cells.append(cell)
                cell = ""
            else:
                cell += "|" if part == "\\|" else part
        cells.append(cell)
        return [c.strip() for c in cells]

    def listing(self, lines: list[str], i: int, out: list[str]) -> int:
        first = MARKER.match(lines[i])
        assert first
        indent = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        items: list[list[str]] = []
        loose = False
        while i < len(lines):
            m = MARKER.match(lines[i])
            if not m or len(m.group(1)) != indent or m.group(2)[0].isdigit() != ordered:
                break
            content = indent + len(m.group(2)) + max(1, min(len(m.group(3)), 4))
            item = [lines[i][content:] if len(lines[i]) > content else lines[i][m.end():]]
            i += 1
            while i < len(lines):
                line = lines[i]
                if not line.strip():
                    # A blank line ends the list unless what follows is indented into this item
                    # or is the next item; either way the list is loose from here.
                    nxt = next((n for n in range(i, len(lines)) if lines[n].strip()), None)
                    if nxt is None:
                        i = len(lines)
                        break
                    lead = len(lines[nxt]) - len(lines[nxt].lstrip())
                    nm = MARKER.match(lines[nxt])
                    if lead >= content:
                        loose = True
                        item.extend([""] * (nxt - i))
                        i = nxt
                        continue
                    if nm and len(nm.group(1)) == indent and nm.group(2)[0].isdigit() == ordered:
                        loose = True
                    i = nxt
                    break
                lead = len(line) - len(line.lstrip())
                if lead >= content:
                    item.append(line[content:])
                elif MARKER.match(line) and lead > indent:
                    item.append(line[lead:])  # a sublist indented less than this item's text
                elif MARKER.match(line) or self.starts_block(lines, i):
                    break
                else:
                    item.append(line.strip())  # a lazy continuation of the item's paragraph
                i += 1
            items.append(item)
        tag = "ol" if ordered else "ul"
        start = int(first.group(2)[:-1]) if ordered else 1
        attr = f' start="{start}"' if ordered and start != 1 else ""
        lis = "\n".join(f"<li>{self.blocks(item, tight=not loose)}</li>" for item in items)
        out.append(f"<{tag}{attr}>\n{lis}\n</{tag}>")
        return i

    # Inline.

    def hold(self, rendered: str) -> str:
        self.held.append(rendered)
        return f"\x00{len(self.held) - 1}\x00"

    def inline(self, text: str) -> str:
        # Code spans first, so nothing inside one is a link or emphasis.
        text = re.sub(r"(`+)(.+?)(?<!`)\1(?!`)", lambda m: self.hold(self.code(m.group(2))), text, flags=re.S)
        text = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|<>~])", lambda m: self.hold(html.escape(m.group(1))), text)
        text = LINK.sub(lambda m: self.hold(self.anchor_tag(m.group(1), m.group(2))), text)
        text = re.sub(
            r"<(https?://[^\s<>]+)>",
            lambda m: self.hold(f'<a href="{html.escape(m.group(1))}">{html.escape(m.group(1))}</a>'),
            text,
        )
        text = html.escape(text, quote=False)
        text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text, flags=re.S)
        text = re.sub(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])", r"<em>\1</em>", text, flags=re.S)
        text = re.sub(r"(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])", r"<em>\1</em>", text, flags=re.S)
        # Placeholders can nest (code inside a link's text), so restore until none remain.
        while HOLD.search(text):
            text = HOLD.sub(lambda m: self.held[int(m.group(1))], text)
        return text

    @staticmethod
    def code(span: str) -> str:
        span = span.replace("\n", " ")
        return f"<code>{html.escape(span.strip() or span)}</code>"

    def anchor_tag(self, label: str, url: str) -> str:
        inner = self.inline(label)
        href = self.link(url)
        if href is None:
            return inner  # a scheme that is not a web link stays as its text
        return f'<a href="{html.escape(href)}">{inner}</a>'


def resolve(source: str, url: str) -> str | None:
    """Where a link in the repository file `source` should point on the site.

    A link between the rendered docs goes to the rendered page; any other relative link goes to
    the file on GitHub, since the site carries no copy of it. Only web schemes survive.
    """
    if url.startswith("#"):
        return url
    if SAFE_SCHEME.match(url):
        return url
    if ANY_SCHEME.match(url):
        return None
    path, _, fragment = url.partition("#")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(source), path)) if path else source
    if target.startswith("../"):
        return None
    suffix = f"#{fragment}" if fragment else ""
    if target in DOCS:
        return DOCS[target][0] + suffix
    return f"{REPO}/blob/{BRANCH}/{target}{suffix}"


def render(source: str, text: str) -> str:
    return Markdown(lambda url: resolve(source, url)).render(text)


# --- Pages ---------------------------------------------------------------------------------------


# The mark: an iceberg's tip above a waterline, and the larger part below it that icelens shows.
MARK = (
    '<svg class="mark" viewBox="0 0 32 32" aria-hidden="true">'
    '<path class="tip" d="M16 4 21.5 13h-11z"/><path class="line" d="M3 15.5h26"/>'
    '<path class="below" d="M8.5 18h15l-3 9.5h-9z"/></svg>'
)


# Light or dark: the system's choice until the visitor picks one with the header's button, and
# theirs after, on every page. In <head>, before the body exists, so a page never paints the
# other theme first. localStorage can throw (private windows, blocked storage); the
# theme then simply is not remembered.
THEME_SCRIPT = """<script>
(() => {
  const root = document.documentElement;
  try { const saved = localStorage.getItem("icelens-theme"); if (saved) root.dataset.theme = saved; } catch (e) {}
  const dark = () => root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  window.icelensTheme = { dark };
  const label = (button) => button.setAttribute("aria-label", dark() ? "Switch to light mode" : "Switch to dark mode");
  addEventListener("DOMContentLoaded", () => {
    const button = document.querySelector(".theme-toggle");
    if (!button) return;
    label(button);
    button.addEventListener("click", () => {
      root.dataset.theme = dark() ? "light" : "dark";
      try { localStorage.setItem("icelens-theme", root.dataset.theme); } catch (e) {}
      label(button);
      dispatchEvent(new CustomEvent("icelens-theme", { detail: { dark: dark() } }));
    });
  });
})();
</script>"""

TOGGLE = (
    '<button class="theme-toggle" type="button" aria-label="Switch between light and dark mode">'
    '<svg class="moon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/></svg>'
    '<svg class="sun" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/>'
    '<path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>'
    "</button>"
)


def header(prefix: str, current: str = "") -> str:
    """The site's header. `prefix` is the path back to the site root ("" or "../")."""
    links = [
        ("docs/index.html", "Docs"),
        ("docs/platforms.html", "Platforms"),
    ]
    items = []
    for href, label in links:
        items.append(f'      <li><a href="{prefix}{href}"{CURRENT if href == current else ""}>{label}</a></li>')
    items.append(f'      <li><a href="{REPO}">GitHub</a></li>')
    items.append(f"      <li>{TOGGLE}</li>")
    items.append(f'      <li><a class="pill" href="{prefix}try.html"{CURRENT if current == "try.html" else ""}>Try it</a></li>')
    return (
        '<header class="site">\n'
        '  <nav class="site-nav" aria-label="Site">\n'
        f'    <a class="brand" href="{prefix}index.html">{MARK}icelens</a>\n'
        "    <ul>\n" + "\n".join(items) + "\n    </ul>\n"
        "  </nav>\n"
        "</header>"
    )


def footer(prefix: str) -> str:
    """The site's footer, on every page."""
    def links(pairs: list[tuple[str, str]]) -> str:
        return "\n".join(f'        <li><a href="{href}">{label}</a></li>' for href, label in pairs)

    use = [(f"{prefix}try.html", "Try it in your browser"), (f"{prefix}index.html#install", "Install"),
           (f"{prefix}docs/index.html", "The guide"), (REPO, "Source on GitHub")]
    docs = [(f"{prefix}docs/start.html", "Start"), (f"{prefix}docs/platforms.html", "Platforms")]
    built = [("https://iceberg.apache.org", "Apache Iceberg"), ("https://py.iceberg.apache.org", "PyIceberg"),
             ("https://duckdb.org", "DuckDB"), ("https://textual.textualize.io", "Textual"),
             ("https://pyodide.org", "Pyodide")]
    return f"""<footer class="site">
  <div class="cols">
    <div>
      <a class="brand" href="{prefix}index.html">{MARK}icelens</a>
      <p>A terminal browser for Apache Iceberg catalogs: snapshots, manifests, files and rows, as of any snapshot you pick.</p>
    </div>
    <div>
      <h2>Use</h2>
      <ul>
{links(use)}
      </ul>
    </div>
    <div>
      <h2>Docs</h2>
      <ul>
{links(docs)}
      </ul>
    </div>
    <div>
      <h2>Built on</h2>
      <ul>
{links(built)}
      </ul>
    </div>
  </div>
  <p class="legal">Apache Iceberg and Iceberg are trademarks of the Apache Software Foundation. icelens is an
  independent project, not affiliated with or endorsed by the ASF.</p>
</footer>"""


# --- The live demo, on the home page and full screen on try.html ---------------------------------

# In <head>, before anything else is fetched: the worker starts loading Python the moment it
# exists. Not on a phone or a connection that says it is slow or metered, where 29 MB nobody
# asked for is a cost: those get the preview and a button. demo.js replays whatever the worker
# said before it was listening.
DEMO_HEAD = """<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css" integrity="sha384-8Xk9wy/gzEDUKrXtrmCFa2bBuK3BpjpDuL/p0SeKQX19Khl/M+lHOgD/CyYf7efP" crossorigin="anonymous">
<link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
<link rel="preconnect" href="https://files.pythonhosted.org" crossorigin>
<link rel="preload" href="first-frame.json" as="fetch" crossorigin>
<script>
  (() => {
    const link = navigator.connection || {};
    const slow = link.saveData || /(^|-)2g$|^3g$/.test(link.effectiveType || "");
    const auto = !slow && matchMedia("(min-width: 760px)").matches;
    window.icelens = { early: [], auto, worker: null };
    if (!auto) return;
    try {
      window.icelens.worker = new Worker("worker.js", { type: "module" });
      window.icelens.worker.onmessage = (event) => window.icelens.early.push(event);
      // A worker that fails to load says so before demo.js is listening: kept like a message.
      window.icelens.worker.onerror = () =>
        window.icelens.early.push({ data: { type: "error", message: "the worker could not start" } });
    } catch (err) {
      window.icelens.worker = null;
    }
  })();
</script>"""

# Pinned by hash as well as version: a CDN that served other bytes would otherwise run them on
# this origin. Each hash is of the file in the npm tarball, which is what jsDelivr serves.
DEMO_SCRIPTS = """<script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js" integrity="sha384-M169f14mRZOXm3hD/v2Ti0ThIT/RnAQagXA9nlE15yHAtrW19gdePJh/HaTzUOe/" crossorigin="anonymous"></script>
<script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js" integrity="sha384-iF+jqbuti4XlB64clWgFWYEscb+UnSRv3VgVikGYZu+otNFnSHr7y7NcKfBnGizn" crossorigin="anonymous"></script>
<script src="demo.js"></script>"""

# The loading card: an iceberg riding a moving waterline, the four stages the worker reports,
# and what is happening in words. It sits over the dimmed preview, and lets clicks through.
BOOT_MARK = (
    '<svg class="boot-mark" viewBox="0 0 120 80" aria-hidden="true">'
    '<g class="berg"><path class="tip" d="M60 8 74 32H46z"/>'
    '<path class="below" d="M40 40h40l-8 30H48z"/></g>'
    '<path class="wave" d="M-60 36q15-6 30 0t30 0 30 0 30 0 30 0 30 0 30 0 30 0"/>'
    "</svg>"
)


def demo(full_screen_link: bool) -> str:
    """The demo's window: terminal, loading card, and the notes under it."""
    link = '<a class="bar-link" href="try.html">Full screen &nearr;</a>' if full_screen_link else ""
    steps = "".join(f"<li>{name}</li>" for name in ("Python", "Packages", "Demo table", "icelens"))
    return f"""<div class="window live demo-window">
    <div class="bar"><i></i><i></i><i></i><span class="title">icelens — sandbox 'demo'</span>{link}</div>
    <div class="stage">
      <div id="terminal" class="terminal"></div>
      <div id="boot" class="boot">
        <div class="boot-card">
          {BOOT_MARK}
          <p id="label" class="boot-label" role="status">Getting ready</p>
          <progress id="bar" max="4" value="0" aria-label="Loading the demo"></progress>
          <ol id="steps" class="boot-steps" aria-hidden="true">{steps}</ol>
          <button id="launch" type="button" class="button" hidden>Start the demo</button>
          <p class="boot-fine">The real icelens, running in your browser through Pyodide. About 29&nbsp;MB
          the first time, cached after. Keys you type while it loads are kept.</p>
        </div>
      </div>
    </div>
  </div>
  <p id="sql-note" class="status" role="status" hidden>The SQL pane's engine (DuckDB) is still loading; everything else is ready.</p>
  <p id="status" class="status" role="alert" hidden></p>"""


def fill(page: str, text: str) -> str:
    """A hand-written page from site/, with the shared header and footer in their placeholders.

    `<!--#header-->` and `<!--#footer-->`; `<!--#header try.html-->` marks that link current.
    """
    def head(match: re.Match[str]) -> str:
        return header("", (match.group(1) or "").strip())

    text = re.sub(r"<!--#header( [\w./-]+)?-->", head, text)
    text = text.replace("</head>", THEME_SCRIPT + "\n</head>", 1)
    text = text.replace("<!--#demo-head-->", DEMO_HEAD).replace("<!--#demo-scripts-->", DEMO_SCRIPTS)
    text = text.replace("<!--#demo-->", demo(full_screen_link=page != "try.html"))
    return text.replace("<!--#footer-->", footer(""))


def doc_page(source: str, text: str) -> str:
    page, _ = DOCS[source]
    first = re.search(r"^#\s+(.+)$", text, re.M)
    # The README's own title is the project's name, which every page title already carries.
    heading = first.group(1).strip() if first and source != "README.md" else DOCS[source][1]
    title = html.escape(re.sub(r"[`*]", "", heading))
    nav = "\n".join(
        f'    <li><a href="{p}"{CURRENT if p == page else ""}>{html.escape(n)}</a></li>' for p, n in DOCS.values()
    )
    current = "docs/platforms.html" if page == "platforms.html" else "docs/index.html"
    body = render(source, text)
    edit = f"{REPO}/blob/{BRANCH}/{source}"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · icelens</title>
<link rel="stylesheet" href="../style.css">
{THEME_SCRIPT}
</head>
<body>
{header("../", current)}
<main class="doc">
  <nav class="doc-nav" aria-label="Documentation">
  <ul>
{nav}
  </ul>
  </nav>
  <article class="prose">
{body}
  </article>
  <p class="source">Rendered from <a href="{edit}">{html.escape(source)}</a>.</p>
</main>
{footer("../")}
</body>
</html>
"""


# --- Build ---------------------------------------------------------------------------------------


def prepare(out: Path) -> None:
    """Empty `out`, refusing anything that is not a previous build: `--out .` must not wipe a checkout."""
    out = out.resolve()
    if out == ROOT or out in ROOT.parents or out == ROOT / "site":
        raise BuildError(f"refusing to build into {out}")
    if out.exists():
        if not out.is_dir():
            raise BuildError(f"{out} exists and is not a directory")
        if any(out.iterdir()) and not (out / ".nojekyll").exists():
            raise BuildError(f"{out} is not empty and is not a previous build; remove it or pick another --out")
        shutil.rmtree(out)
    out.mkdir(parents=True)


#: What Textual needs that Pyodide's own lockfile lacks, and the module each installs. Rich,
#: Pygments, platformdirs and typing-extensions are in Pyodide's lockfile and come from there.
TEXTUAL_WHEELS = {
    "textual": "textual",
    "markdown-it-py": "markdown_it",
    "mdit-py-plugins": "mdit_py_plugins",
    "mdurl": "mdurl",
}


def version_of(wheel_name: str) -> str:
    return wheel_name.split("-")[1]


def lock_entry(name: str, version: str, file_name: str, sha256: str, imports: list[str], depends: list[str]) -> dict:
    """One package in Pyodide's lockfile format, so the page loads it with everything else."""
    return {
        "name": name, "version": version, "file_name": file_name, "sha256": sha256,
        "install_dir": "site", "package_type": "package", "imports": imports, "depends": depends,
        "unvendored_tests": False, "tool": {},
    }


def textual_entries() -> list[dict]:
    """Textual and the three wheels it needs that Pyodide lacks, at the versions uv.lock pins.

    Listed for Pyodide's own loader rather than left to micropip, which asks PyPI about each
    package in turn before it downloads anything: here they download beside the rest, in one go.
    The URLs and hashes are uv.lock's, so the page runs the Textual the suite was run with.
    """
    try:
        import tomllib  # pyright: ignore[reportMissingImports] - 3.11+, checked against 3.10
    except ImportError as e:  # Python 3.10
        raise BuildError("building the site needs Python 3.11 or newer, for tomllib") from e
    locked = {p["name"]: p for p in tomllib.loads((ROOT / "uv.lock").read_text())["package"]}
    entries = []
    for name, module in TEXTUAL_WHEELS.items():
        package = locked.get(name)
        wheel = next((w for w in (package or {}).get("wheels", []) if w["url"].endswith("-none-any.whl")), None)
        if package is None or wheel is None:
            raise BuildError(f"uv.lock has no pure-Python wheel for {name}, which the in-browser demo needs")
        depends = [d["name"] for d in package.get("dependencies", []) if "marker" not in d]
        entries.append(lock_entry(
            name, package["version"], wheel["url"], wheel["hash"].removeprefix("sha256:"), [module], depends,
        ))
    return entries


def build_wheel(out: Path) -> str:
    uv = shutil.which("uv")
    if uv is None:
        raise BuildError("uv is not on PATH; it builds the wheel (python3 scripts/setup.py installs it)")
    wheels = out / "wheels"
    try:
        run = subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(wheels)],
            cwd=ROOT, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as e:
        raise BuildError("uv build took more than ten minutes") from e
    if run.returncode != 0:
        tail = (run.stderr or run.stdout).strip().splitlines()
        raise BuildError(f"uv build failed: {tail[-1] if tail else f'exit {run.returncode}'}")
    found = sorted(wheels.glob("icelens-*.whl"))
    if len(found) != 1:
        raise BuildError(f"expected one icelens wheel in {wheels}, found {len(found)}")
    # The version stays 0.1.0 across many deploys, so a browser holding the last wheel would keep
    # it. A directory named for the wheel's contents gives every different build its own URL;
    # latest.json, fetched uncached, says which one is current.
    digest = hashlib.sha256(found[0].read_bytes()).hexdigest()[:16]
    (wheels / digest).mkdir()
    found[0].rename(wheels / digest / found[0].name)
    name = f"{digest}/{found[0].name}"
    wheel = wheels / name
    icelens = lock_entry(
        "icelens", version_of(wheel.name), name, hashlib.sha256(wheel.read_bytes()).hexdigest(),
        ["icelens"], ["pyarrow", "pyiceberg", "textual"],
    )
    latest = {"wheel": name, "packages": [*textual_entries(), icelens]}
    (wheels / "latest.json").write_text(json.dumps(latest, indent=1) + "\n")
    # uv drops a .gitignore of "*" into its out-dir; harmless on Pages, but not part of the site.
    (wheels / ".gitignore").unlink(missing_ok=True)
    return name


def capture_first_frame(out: Path) -> str:
    """The demo's first screen, which try.html shows dimmed while Python loads (icelens.browser)."""
    uv = shutil.which("uv")
    if uv is None:
        raise BuildError("uv is not on PATH; it runs the app to capture the demo's first frame")
    try:
        run = subprocess.run(
            [uv, "run", "--quiet", "python", "-m", "icelens.browser", "first-frame", "120", "36"],
            cwd=ROOT, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired as e:
        raise BuildError("capturing the demo's first frame took more than five minutes") from e
    if run.returncode != 0:
        tail = (run.stderr or run.stdout).strip().splitlines()
        raise BuildError(f"capturing the first frame failed: {tail[-1] if tail else f'exit {run.returncode}'}")
    frame = json.loads(run.stdout)
    if "demo.orders" not in frame.get("ansi", ""):
        raise BuildError("the captured first frame does not show the demo table")
    # The frame shows the demo table's location and is published as it is.
    shown = json.dumps(frame)
    leaked = [m for m in ("/Users/", "/home/", "/var/folders/", "\\\\Users\\\\") if m in shown]
    if leaked:
        raise BuildError(f"the captured first frame shows a local path ({leaked[0]}); it would be published")
    (out / "first-frame.json").write_text(json.dumps(frame) + "\n")
    return "first-frame.json"


# The files a page or the worker loads by a fixed name. A browser may keep any of them from an
# earlier build (GitHub Pages says ten minutes; a server that says nothing leaves it to the
# browser's guess), and a new demo.js talking to an old worker.js is a demo that half works. So
# every reference to them carries the build's stamp, and a new build is a new URL for all of them.
STAMPED = ("style.css", "demo.js", "worker.js", "loader.mjs", "first-frame.json")


def stamp(out: Path) -> str:
    """Add `?v=<build>` to every reference to a STAMPED file, in the pages and the scripts."""
    digest = hashlib.sha256()
    for path in sorted(p for p in out.rglob("*") if p.is_file()):
        digest.update(path.relative_to(out).as_posix().encode())
        digest.update(path.read_bytes())
    version = digest.hexdigest()[:12]
    names = "|".join(re.escape(name) for name in STAMPED)
    # Only a quoted reference: "style.css", "../style.css", "./loader.mjs".
    pattern = re.compile(rf"""(["'])((?:\.\.?/)?(?:{names}))\1""")
    for path in [*out.glob("*.html"), *out.glob("docs/*.html"), out / "demo.js", out / "worker.js"]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            path.write_text(pattern.sub(lambda m: f"{m[1]}{m[2]}?v={version}{m[1]}", text), encoding="utf-8")
    return version


def build(out: Path, wheel: bool = True) -> list[str]:
    site = ROOT / "site"
    if not (site / "index.html").is_file():
        raise BuildError(f"{site / 'index.html'} is missing")
    prepare(out)
    shutil.copytree(site, out, dirs_exist_ok=True)
    for page in out.glob("*.html"):
        page.write_text(fill(page.name, page.read_text(encoding="utf-8")), encoding="utf-8")
    (out / "docs").mkdir(exist_ok=True)
    made = []
    for source, (page, _) in DOCS.items():
        path = ROOT / source
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            raise BuildError(f"cannot read {source}: {e.strerror}") from e
        (out / "docs" / page).write_text(doc_page(source, text), encoding="utf-8")
        made.append(f"docs/{page}")
    if wheel:
        made.append(f"wheels/{build_wheel(out)}")
        made.append("wheels/latest.json")
        made.append(capture_first_frame(out))
    stamp(out)
    # Pages runs Jekyll over the upload otherwise, which would drop files and directories whose
    # names start with an underscore.
    (out / ".nojekyll").write_text("")
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the icelens website into a directory GitHub Pages can serve.")
    parser.add_argument("--out", default="_site", help="output directory, replaced on each build (default _site)")
    parser.add_argument("--no-wheel", action="store_true", help="skip building the wheel the in-browser demo installs")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    try:
        made = build(out, wheel=not args.no_wheel)
    except BuildError as e:
        print(f"build_site: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"build_site: {e.filename or ''}: {e.strerror}", file=sys.stderr)
        return 1
    for name in made:
        print(f"  {name}")
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

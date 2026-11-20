#!/usr/bin/env python3
"""Build the website GitHub Pages serves: `python3 scripts/build_site.py [--out _site]`.

The site is hand-written HTML in `site/`, the repository's Markdown rendered into pages, and the
home page's walk-through of the app (`scripts/walk.py`). Standard library only, so the Pages
workflow needs nothing but Python and uv, and the Markdown renderer is the small one below rather than a
dependency: it handles what README.md and docs/ actually use, and escapes everything, so a
`<script>` written in a doc arrives as text.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

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

# What a search result or a shared link says about each doc page.
DESCRIPTIONS = {
    "docs/GUIDE.md": "Every pane, key and flag in icelens, the terminal app for learning Apache Iceberg and inspecting production Iceberg tables.",
    "README.md": "Install icelens, open the demo table or your own catalog, and walk a table's snapshots, manifests, files and rows.",
    "docs/PLATFORMS.md": "Which Iceberg catalogs icelens has been tested with, how to connect to each, and what to do when something fails.",
}

#: Where the site is served when nothing says otherwise. A shared link's preview needs absolute
#: URLs, so the deploy that serves the site somewhere else sets ICELENS_SITE_URL to that address.
DEFAULT_SITE = "https://harkanwals.github.io/icelens"

#: The picture a shared link shows: 1200 by 630, the size every network's large card expects.
CARD = "og.png"


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
    items.append(f'      <li><a class="pill" href="{prefix}index.html#install">Install</a></li>')
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

    use = [(f"{prefix}index.html#install", "Install"), (f"{prefix}docs/index.html", "The guide"),
           (REPO, "Source on GitHub")]
    docs = [(f"{prefix}docs/start.html", "Start"), (f"{prefix}docs/platforms.html", "Platforms")]
    built = [("https://iceberg.apache.org", "Apache Iceberg"), ("https://py.iceberg.apache.org", "PyIceberg"),
             ("https://duckdb.org", "DuckDB"), ("https://textual.textualize.io", "Textual")]
    return f"""<footer class="site">
  <div class="cols">
    <div>
      <a class="brand" href="{prefix}index.html">{MARK}icelens</a>
      <p>Learn Apache Iceberg, and inspect your production Iceberg tables: snapshots, manifests, files and rows, as of any commit.</p>
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


# --- The home page's walk-through ---------------------------------------------------------------

# The terminal on the home page: screens the real app drew at build time (scripts/walk.py), and a
# table of which key leads where. Keys and clicks move through it; a tour presses the keys itself
# until the visitor takes over. Nothing moves for reduced motion, off screen or in a hidden tab.
WALK_SCRIPT = """<script>
(() => {
  // The rules are scripts/walk.py's - step, click, hover_at, hovered, compose - one for one.
  const fig = document.querySelector(".shot[data-walk]");
  if (!fig) return;
  const term = fig.querySelector(".shot-screen"), pres = { dark: term.querySelector("pre.t-dark"), light: term.querySelector("pre.t-light") };
  const pins = term.querySelector(".pins"), hint = fig.querySelector(".walk-hint");
  const pressed = fig.querySelector(".pressed"), notes = fig.querySelector(".callouts");
  const free = fig.querySelector("template.free").innerHTML;
  const tabs = [...fig.querySelectorAll('[role="tab"]')], toggle = fig.querySelector(".tour-toggle");
  const DWELL = 9000, STEP = 700;
  const NAMES = { ArrowUp: "\\u2191", ArrowDown: "\\u2193", ArrowLeft: "\\u2190", ArrowRight: "\\u2192", Escape: "Esc", " ": "Space" };
  // Keys the app answers that the page does not follow: it says so instead of doing nothing.
  const ELSEWHERE = new Set(["0", "q", "r", "x", "c", "w", "g", "l", "e", "/", "[", "]", "Escape", "PageUp", "PageDown", "Home", "End"]);
  let W = null, at = -1, help = null, shown = [], hover = null, mouse = null;
  let chapter = 0, inView = false, timer = 0, run = 0, hintTimer = 0, touring = !matchMedia("(prefers-reduced-motion: reduce)").matches;
  fig.classList.add("live");
  let loading = null;
  const load = () => loading ??= fetch(fig.dataset.walk).then((r) => r.json()).then((w) => { W = w; if (at < 0) at = W.start; });
  const theme = () => {
    const t = document.documentElement.dataset.theme;
    return t === "light" || t === "dark" ? t : matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const screens = () => (theme() === "light" ? W.light : W);
  const esc = (t) => t.replace(/[&<>]/g, (c) => (c === "&" ? "&amp;" : c === "<" ? "&lt;" : "&gt;"));
  const width = (runs) => runs.reduce((n, [t]) => n + t.length, 0);
  const cut = (runs, a0, b0) => {
    const out = []; let x = 0;
    for (const [t, s] of runs) {
      const a = Math.max(a0, x), b = Math.min(b0, x + t.length);
      if (a < b) out.push([t.slice(a - x, b - x), s]);
      x += t.length;
    }
    return out;
  };
  const group = (s) => W.groups[W.group[s]];
  const step = (s, k) => {
    const m = W.moves.indexOf(k);
    if (m >= 0) {
      const to = W.move[s][m], n = k === "Enter" && to >= 0 ? W.enter_note[s] : -1;
      return [to < 0 ? s : to, n < 0 ? null : n];
    }
    const g = group(s), to = g.next[W.fresh[s]][k], n = g.toast[k];
    return [to === undefined ? s : to, n === undefined ? null : n];
  };
  const hoverAt = (s, y, x) => {
    const lay = W.layouts[W.layout[s]];
    if (!lay.pane) return null;
    const [y0, r0, n, cols] = lay.cells;
    for (const [x0, x1, c] of cols) {
      if (x0 <= x && x < x1) {
        if (y === lay.header) return [-1, c];
        if (y0 <= y && y < y0 + n) return [r0 + y - y0, c];
      }
    }
    return null;
  };
  const hovered = (before, after, h, k) => {
    if (!h || (W.moves.includes(k) && W.cursor[before])) return null;
    const a = group(before).name, b = group(after).name;
    if (a === b) return h;
    if (!a.includes("/tabs") && !b.includes("/tabs") && a.split("/")[1] === b.split("/")[1]) return [0, 0];
    return null;
  };
  const click = (s, y, x) => {
    const lay = W.layouts[W.layout[s]], g = group(s), strip = g.name.includes("/tabs");
    const [fy, keys] = lay.footer;
    if (y === fy) {
      for (const [x0, x1, name] of keys) if (x0 <= x && x < x1) return W.footer_keys[name] ? ["key", W.footer_keys[name]] : ["unfollowed"];
      return ["none"];
    }
    const [ty, tx0, tx1, names] = lay.tabs;
    if (y === ty && tx0 <= x && x < tx1) {
      const pane = g.name.split("/")[1];
      for (const [x0, x1, name] of names) {
        if (x0 <= x && x < x1 && name !== pane) {
          const to = g.tabs[name] ?? -1;
          return to >= 0 ? ["state", to, null] : ["unfollowed"];
        }
      }
      if (strip) return ["none"];
      return s === g.home ? ["state", g.tabs[pane], null] : ["unfollowed"];
    }
    if (lay.pane) {
      const [top, bottom, left, right] = lay.pane;
      if (top <= y && y < bottom && left <= x && x < right) {
        const [y0, , n, cols] = lay.cells, here = strip ? g.home : s;
        for (let k = 0; k < cols.length; k++) {
          const [x0, x1] = cols[k];
          if (y0 <= y && y < y0 + n && x0 <= x && x < x1) {
            const to = lay.targets[(y - y0) * cols.length + k];
            const same = to === here || (strip && to >= 0 && W.cursor[to][0] === W.cursor[here][0] && W.cursor[to][1] === W.cursor[here][1]);
            if (same) return ["state", ...step(here, "Enter")];
            return to >= 0 ? ["state", to, null] : ["unfollowed"];
          }
        }
        if (y === lay.header) return here !== s ? ["state", here, null] : ["none"];
        return ["unfollowed"];
      }
    }
    for (const [y0, y1, x0, x1] of lay.others) if (y0 <= y && y < y1 && x0 <= x && x < x1) return ["unfollowed"];
    return ["none"];
  };
  const compose = (S) => {
    let rows = S.states[at].map((i) => S.lines[i]);
    const lay = W.layouts[W.layout[at]], cursor = W.cursor[at];
    const paste = (y, left, inner) => {
      if (y >= 0 && y < rows.length) rows[y] = [...cut(rows[y], 0, left), ...inner, ...cut(rows[y], left + width(inner), W.cols)];
    };
    const restyle = (y, x0, x1, map) => {
      rows[y] = [...cut(rows[y], 0, x0), ...cut(rows[y], x0, x1).map(([t, s]) => [t, map[s]]), ...cut(rows[y], x1, W.cols)];
    };
    if (hover && lay.pane && !(cursor && hover[0] === cursor[0] && hover[1] === cursor[1])) {
      const [r, c] = hover, [y0, r0, n, cols] = lay.cells;
      const y = r < 0 ? lay.header : y0 + r - r0, span = cols.find((col) => col[2] === c);
      if (span && (r < 0 || (r0 <= r && r < r0 + n))) restyle(y, span[0], span[1], S.hovered[r < 0 ? 1 : 0]);
    }
    if (mouse && help === null) {
      const [fy, keys] = lay.footer, [ty, , , names] = lay.tabs, pane = group(at).name.split("/")[1];
      for (const [y, spans, which] of [[fy, keys, 2], [ty, names.filter((t) => t[2] !== pane), 3]]) {
        const span = spans.find(([x0, x1]) => y === mouse[1] && x0 <= mouse[0] && mouse[0] < x1);
        if (span) restyle(y, span[0], span[1], S.hovered[which]);
      }
    }
    if (help !== null) {
      rows = rows.map((row) => row.map(([t, s]) => [t, S.tint[s]]));
      for (const [y, x, inner] of S.help.frames[help]) paste(y, x, inner);
    }
    let y = null;
    for (const n of [...shown].reverse()) {
      const t = S.toasts[n], top = (y ?? t.bottom) - t.rows.length + 1;
      t.rows.forEach((inner, k) => paste(top + k, t.left, inner));
      y = top - 2;
    }
    return rows;
  };
  const html = new WeakMap();
  const render = () => {
    if (!W || at < 0) return;
    const t = theme(), S = screens(), prefix = t === "light" ? "l" : "d";
    const pre = pres[t];
    pre.innerHTML = compose(S).map((row) => {
      let h = html.get(row);
      if (h === undefined) {
        h = row.map(([text, s]) => (S.styles[s] ? `<span class="${prefix}${s}">${esc(text)}</span>` : esc(text))).join("");
        html.set(row, h);
      }
      return h;
    }).join("\\n");
  };
  const say = (text) => {
    hint.innerHTML = text; hint.hidden = false;
    clearTimeout(hintTimer); hintTimer = setTimeout(() => { hint.hidden = true; }, 6000);
  };
  const toast = (n) => {
    shown.push(n);
    setTimeout(() => { const k = shown.indexOf(n); if (k >= 0) { shown.splice(k, 1); render(); } }, W.toasts[n].timeout * 1000);
  };
  const show = (k) => {
    const kbd = document.createElement("kbd");
    kbd.textContent = NAMES[k] || k;
    pressed.append(kbd);
    while (pressed.children.length > 5) pressed.firstElementChild.remove();
  };
  // One key, as the app takes it. The tour presses its keys through here too.
  const press = (k, user) => {
    show(k);
    if (help !== null) {
      if (k === "Escape" || k === "?" || k === "q") help = null;
      else if (W.help.next[k]) help = W.help.next[k][help];
      return render();
    }
    if (k === "?") { help = 0; hover = mouse = null; return render(); }
    const before = at, [to, note] = step(at, k);
    if (user && to === at && note === null) {
      if (W.beyond[at].includes(k)) say("The page has the first rows of this grid; the app goes on down. <code>uvx icelens --demo</code>");
      else if (ELSEWHERE.has(k) || (k === "Tab" && !("Tab" in group(at).next[W.fresh[at]]))) say(`<kbd>${esc(NAMES[k] || k)}</kbd> works in the app itself, which this page copies but does not run: <code>uvx icelens --demo</code>`);
    }
    at = to;
    if (note !== null) toast(note);
    hover = hovered(before, at, hover, k);
    render();
  };
  const label = (c) => {
    pins.innerHTML = c.places.map(([row, col], k) =>
      `<span class="pin" style="--row:${row};--col:${col};--i:${k}" data-n="${k + 1}"><b>${k + 1}</b></span>`).join("");
    notes.innerHTML = c.notes.map((n, k) => `<li data-n="${k + 1}">${n}</li>`).join("");
    notes.classList.remove("free");
  };
  const unlabel = () => { pins.innerHTML = ""; notes.innerHTML = free; notes.classList.add("free"); };
  const running = () => touring && inView && !document.hidden;
  const select = (i) => tabs.forEach((tab, k) => { tab.setAttribute("aria-selected", String(k === i)); tab.tabIndex = k === i || (i < 0 && k === 0) ? 0 : -1; });
  const schedule = () => {
    clearTimeout(timer);
    fig.classList.toggle("playing", running());
    toggle.textContent = touring ? "Pause" : "Play the tour";
    toggle.setAttribute("aria-pressed", String(touring));
    const bar = tabs[chapter].querySelector(".progress");
    bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
    if (running()) timer = setTimeout(() => go(chapter + 1, true), DWELL);
  };
  // A chapter: its keys pressed one at a time while the tour plays, or straight to its screen.
  const go = async (i, animate) => {
    const mine = ++run;
    chapter = (i + tabs.length) % tabs.length;
    select(chapter); clearTimeout(timer); fig.classList.remove("playing");
    pins.innerHTML = ""; help = null; hover = mouse = null;
    await load();
    const c = W.tour[chapter];
    if (chapter === 0 || !animate) { shown = []; at = c.state; render(); }
    else {
      at = W.tour[chapter - 1].state;
      for (const k of c.keys) {
        await new Promise((r) => setTimeout(r, STEP));
        if (mine !== run) return;
        press(k, false);
      }
    }
    if (mine !== run) return;
    at = c.state; render(); label(c); schedule();
  };
  const takeOver = () => {
    fig.classList.add("used");
    if (!touring && !pins.innerHTML) return;
    touring = false; run++; select(-1); unlabel(); schedule();
  };
  const KEYS = new Set([...ELSEWHERE, "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Enter", ",", ".", "u", "?", " ", "Tab",
    "1", "2", "3", "4", "5", "6", "7", "8", "9"]);
  term.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || !KEYS.has(e.key)) return;
    // Tab is the page's, to leave the terminal, except where the app's Tab goes into a grid.
    if (e.key === "Tab" && (e.shiftKey || !W || help !== null || !("Tab" in group(at).next[W.fresh[at]]))) return;
    e.preventDefault();
    takeOver();
    load().then(() => { if (e.key !== " ") press(e.key, true); });
  });
  // Where the mouse is, in the screen's character cells.
  const cell = (e) => {
    const box = pres[theme()].getBoundingClientRect();
    return [Math.floor(((e.clientX - box.left) / box.width) * W.cols), Math.floor(((e.clientY - box.top) / box.height) * W.rows)];
  };
  term.addEventListener("pointermove", (e) => {
    if (!W || e.pointerType !== "mouse" || help !== null) return;
    const [x, y] = cell(e);
    mouse = [x, y]; hover = hoverAt(at, y, x);
    render();
  });
  term.addEventListener("pointerleave", () => { if (W && (mouse || hover)) { mouse = hover = null; render(); } });
  term.addEventListener("pointerdown", async (e) => {
    if (e.button !== 0) return;
    term.focus({ preventScroll: true });
    takeOver();
    await load();
    const [x, y] = cell(e);
    if (x < 0 || y < 0 || x >= W.cols || y >= W.rows || help !== null) return;
    // A toast takes a click to dismiss it, as Textual's do.
    let bottom = null;
    for (const n of [...shown].reverse()) {
      const t = screens().toasts[n], top = (bottom ?? t.bottom) - t.rows.length + 1;
      if (top <= y && y <= (bottom ?? t.bottom) && t.left <= x) { shown.splice(shown.indexOf(n), 1); return render(); }
      bottom = top - 2;
    }
    if (e.pointerType === "mouse") { mouse = [x, y]; hover = hoverAt(at, y, x); }
    const done = click(at, y, x), before = at;
    show("click");
    if (done[0] === "key") {
      if (done[1] === "?") { help = 0; hover = mouse = null; return render(); }
      const [to, note] = step(at, done[1]);
      at = to; if (note !== null) toast(note);
    } else if (done[0] === "state") {
      at = done[1]; if (done[2] !== null) toast(done[2]);
    } else if (done[0] === "unfollowed") {
      say("In the app, that click moves the focus there. This page follows the grids, the pane names and the footer\\u2019s keys: <code>uvx icelens --demo</code> has the rest.");
    }
    hover = hovered(before, at, hover, null);
    if (e.pointerType !== "mouse") hover = mouse = null;
    render();
  });
  fig.querySelectorAll(".keypad button").forEach((b) => b.addEventListener("click", () => {
    takeOver(); load().then(() => press(b.dataset.key, true));
  }));
  tabs.forEach((tab, k) => tab.addEventListener("click", () => { touring = false; go(k, false); }));
  fig.querySelector('[role="tablist"]').addEventListener("keydown", (e) => {
    const d = { ArrowRight: 1, ArrowLeft: -1 }[e.key];
    if (d) { e.preventDefault(); touring = false; go(chapter + d, false); tabs[chapter].focus(); }
  });
  toggle.addEventListener("click", () => { touring = !touring; if (touring) go(chapter + 1, true); else { run++; schedule(); } });
  fig.addEventListener("pointerover", (e) => {
    const on = e.target.closest("[data-n]");
    fig.querySelectorAll("[data-n]").forEach((el) => el.classList.toggle("on", !!on && el.dataset.n === on.dataset.n));
  });
  new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting;
    if (inView) load();
    if (inView && touring && !fig.classList.contains("playing") && pins.innerHTML) schedule();
    else if (!inView) { clearTimeout(timer); fig.classList.remove("playing"); }
  }, { threshold: 0.4 }).observe(fig);
  document.addEventListener("visibilitychange", () => { if (pins.innerHTML) schedule(); });
  // The page's theme is the terminal's: redraw in the other one's colours when it changes.
  new MutationObserver(render).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", render);
  select(0);
})();
</script>"""


def walk_figure(walk: dict[str, Any]) -> str:
    """The home page's terminal: the tour's first chapter drawn in, in both themes, ready
    before any script runs.

    `walk` is `scripts/walk.py`'s JSON; the script loads the rest of it as walk.json. Labels sit
    at the row and column where the text they point at was found, in the screen's own character
    cells, so they stay on it at any width.
    """
    first = walk["tour"][0]

    def screen(screens: dict[str, Any], prefix: str) -> str:
        def line(i: int) -> str:
            return "".join(
                f'<span class="{prefix}{style}">{html.escape(text, quote=False)}</span>' if screens["styles"][style]
                else html.escape(text, quote=False)
                for text, style in screens["lines"][i]
            )
        return "\n".join(line(i) for i in screens["states"][first["state"]])

    styles = "".join(f".walk .{prefix}{n}{{{css}}}" for prefix, screens in (("d", walk), ("l", walk["light"]))
                     for n, css in enumerate(screens["styles"]) if css)
    tabs = "".join(
        f'<button type="button" role="tab" aria-selected="false"><span class="n">{n}</span>'
        f'{html.escape(chapter["title"])}<i class="progress" aria-hidden="true"></i></button>'
        for n, chapter in enumerate(walk["tour"], 1)
    )
    pins = "".join(
        f'<span class="pin" style="--row:{row};--col:{col};--i:{k}" data-n="{k + 1}"><b>{k + 1}</b></span>'
        for k, (row, col) in enumerate(first["places"])
    )
    notes = "".join(f'<li data-n="{k}">{note}</li>' for k, note in enumerate(first["notes"], 1))
    keys = [("ArrowLeft", "&larr;"), ("ArrowUp", "&uarr;"), ("ArrowDown", "&darr;"), ("ArrowRight", "&rarr;"),
            ("Enter", "Enter"), (",", ","), (".", "."), ("u", "u"), ("?", "?"), ("Escape", "Esc")]
    pad = "".join(f'<button type="button" data-key="{html.escape(k)}">{label}</button>' for k, label in keys)
    return f"""<figure class="shot" data-walk="walk.json">
      <div class="scenes"><div role="tablist" aria-label="A tour of icelens">{tabs}</div>
        <button type="button" class="tour-toggle" aria-pressed="true">Pause</button></div>
      <div class="window">
        <div class="bar"><i></i><i></i><i></i><span class="title">icelens --demo</span><span class="try-chip" aria-hidden="true">Click the screen, then use the keys</span><span class="pressed" aria-hidden="true"></span></div>
        <div class="shot-scroll"><div class="shot-screen walk" tabindex="0" role="application"
          aria-label="icelens on a demo table. Arrow keys move the cursor, Enter pins a snapshot, 1 to 9 switch panes, comma and full stop step through commits, u returns to now, question mark lists the keys.">
          <style>{styles}</style><pre class="t-dark" aria-hidden="true">{screen(walk, "d")}</pre><pre class="t-light" aria-hidden="true">{screen(walk["light"], "l")}</pre>
          <div class="pins" aria-hidden="true">{pins}</div>
        </div></div>
      </div>
      <p class="walk-hint" role="status" hidden></p>
      <div class="keypad" aria-label="Keys">{pad}</div>
      <ol class="callouts">{notes}</ol>
      <template class="free"><li class="free-note">Your turn. The arrow keys move the cursor and the guide follows it;
        <kbd>Enter</kbd> on a snapshot, or a click on the cell under the cursor, pins it; <kbd>1</kbd>&ndash;<kbd>9</kbd>, or a click on a
        pane&rsquo;s name, switch panes; <kbd>,</kbd> and <kbd>.</kbd> step through commits; <kbd>u</kbd> comes back to now;
        <kbd>?</kbd> lists every key. Every screen is one the app drew; typing a query or a filter needs the app itself:
        <code>uvx icelens --demo</code>.</li></template>
    </figure>
    {WALK_SCRIPT}"""


def fill(page: str, text: str, walk: dict[str, Any] | None = None) -> str:
    """A hand-written page from site/, with the shared header and footer in their placeholders.

    `<!--#header-->` and `<!--#footer-->`; `<!--#header docs/index.html-->` marks that link current.
    """
    def head(match: re.Match[str]) -> str:
        return header("", (match.group(1) or "").strip())

    text = re.sub(r"<!--#header( [\w./-]+)?-->", head, text)
    title = re.search(r"<title>(.*?)</title>", text, re.S)
    description = re.search(r'<meta name="description" content="([^"]*)">', text)
    if title is None or description is None:
        raise BuildError(f"{page} needs a <title> and a description for its link preview")
    text = text.replace(
        "</head>", social(page, html.unescape(title.group(1)), html.unescape(description.group(1))) + "\n</head>", 1,
    )
    text = text.replace("</head>", THEME_SCRIPT + "\n</head>", 1)
    if "<!--#walk-->" in text:
        if walk is None:
            raise BuildError(f"{page} has the walk-through and none was captured")
        text = text.replace("<!--#walk-->", walk_figure(walk))
    return text.replace("<!--#footer-->", footer(""))


def site_url() -> str:
    return (os.environ.get("ICELENS_SITE_URL") or DEFAULT_SITE).rstrip("/")


def social(path: str, title: str, description: str) -> str:
    """The tags that make a shared link a card: canonical address, Open Graph, and X's own.

    `title` and `description` are plain text; `path` is the page's place under the site root.
    """
    site = site_url()
    url = f"{site}/" if path == "index.html" else f"{site}/{path}"
    title, description = html.escape(title), html.escape(description)
    tags = [
        f'<link rel="canonical" href="{url}">',
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="icelens">',
        f'<meta property="og:title" content="{title}">',
        f'<meta property="og:description" content="{description}">',
        f'<meta property="og:url" content="{url}">',
        f'<meta property="og:image" content="{site}/{CARD}">',
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="icelens in a terminal, showing a table\'s snapshots">',
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:title" content="{title}">',
        f'<meta name="twitter:description" content="{description}">',
        f'<meta name="twitter:image" content="{site}/{CARD}">',
    ]
    return "\n".join(tags)


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
<meta name="description" content="{html.escape(DESCRIPTIONS[source])}">
{social(f"docs/{page}", f"{html.unescape(title)} · icelens", DESCRIPTIONS[source])}
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


def capture_walk() -> dict[str, Any]:
    """The home page's walk-through of the demo (scripts/walk.py): every screen, and the keys between."""
    uv = shutil.which("uv")
    if uv is None:
        raise BuildError("uv is not on PATH; it runs the app to capture the home page's walk-through")
    try:
        run = subprocess.run(
            [uv, "run", "--quiet", "python", "scripts/walk.py", "120", "36"],
            # Seven minutes on a laptop, for both themes at once; a CI runner may take twice that.
            cwd=ROOT, capture_output=True, text=True, timeout=1800,
        )
    except subprocess.TimeoutExpired as e:
        raise BuildError("capturing the walk-through took more than half an hour") from e
    if run.returncode != 0:
        tail = (run.stderr or run.stdout).strip().splitlines()
        raise BuildError(f"capturing the walk-through failed: {tail[-1] if tail else f'exit {run.returncode}'}")
    return json.loads(run.stdout)


def publishable(walk: dict[str, Any]) -> str:
    """The walk as the page loads it, refused if a screen shows this machine's own paths."""
    shown = json.dumps(walk, ensure_ascii=False, separators=(",", ":"))
    leaked = [m for m in ("/Users/", "/home/", "/var/folders/", "\\\\Users\\\\") if m in shown]
    if leaked:
        raise BuildError(f"the walk-through shows a local path ({leaked[0]}); it would be published")
    return shown


# The files a page loads by a fixed name. A browser may keep one from an earlier build (GitHub
# Pages says ten minutes; a server that says nothing leaves it to the browser's guess), so every
# reference to them carries the build's stamp, and a new build is a new URL.
STAMPED = ("style.css", "walk.json")


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
    for path in [*out.glob("*.html"), *out.glob("docs/*.html")]:
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            path.write_text(pattern.sub(lambda m: f"{m[1]}{m[2]}?v={version}{m[1]}", text), encoding="utf-8")
    return version


def build(out: Path, walk: dict[str, Any] | None = None) -> list[str]:
    site = ROOT / "site"
    if not (site / "index.html").is_file():
        raise BuildError(f"{site / 'index.html'} is missing")
    prepare(out)
    shutil.copytree(site, out, dirs_exist_ok=True)
    walk = capture_walk() if walk is None else walk  # the tests bring a smaller one
    (out / "walk.json").write_text(publishable(walk), encoding="utf-8")
    for page in out.glob("*.html"):
        page.write_text(fill(page.name, page.read_text(encoding="utf-8"), walk), encoding="utf-8")
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
    stamp(out)
    # Pages runs Jekyll over the upload otherwise, which would drop files and directories whose
    # names start with an underscore.
    (out / ".nojekyll").write_text("")
    return made


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the icelens website into a directory GitHub Pages can serve.")
    parser.add_argument("--out", default="_site", help="output directory, replaced on each build (default _site)")
    args = parser.parse_args(argv)
    out = Path(args.out)
    if not out.is_absolute():
        out = ROOT / out
    try:
        made = build(out)
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

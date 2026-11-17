"""Message formatting: a small Markdown subset rendered to fixed, escaped HTML.

Callers never supply HTML. The renderer is deterministic, so the owner approves
exactly what will be composed, and the adapter can verify the result.
"""
from __future__ import annotations

import html
import re

MAX_TEXT = 8000
MAX_TITLE = 200
MAX_ROWS = 20
MAX_COLS = 7
MAX_CELL = 300

_INLINE = re.compile(r"""
  (?P<esc>\\(?P<esc_t>[\\`*_~\[\]()#>.-]))
 |(?P<code>`(?P<code_t>[^`\n]+)`)
 |(?P<link>\[(?P<link_t>[^\]\n]+)\]\((?P<href>(?:https?://|mailto:)[^\s()<>"]+)\))
 |(?P<bold>\*\*(?=\S)(?P<bold_t>.+?)(?<=\S)\*\*(?!\*))
 |(?P<strike>~~(?=\S)(?P<strike_t>.+?)(?<=\S)~~)
 |(?P<em>(?<![\w*])\*(?=[^\s*])(?P<em_t>.+?)(?<=[^\s*])\*(?![\w*]))
 |(?P<em2>(?<!\w)_(?=[^\s_])(?P<em2_t>.+?)(?<=[^\s_])_(?!\w))
""", re.X | re.S)

_TAGS = (("code", "code"), ("strike", "s"), ("em", "em"), ("bold", "strong"))

# Name -> pattern that must match the composed draft. Teams and the editor use
# different tags for the same mark, so each pattern lists the known variants.
_REQUIRE = {
    "bold": r"<(strong|b)[\s>]",
    "italic": r"<(em|i)[\s>]",
    "strikethrough": r"<(s|strike|del)[\s>]",
    "code": r"<(code|pre)[\s>]",
    "link": r"<a\s",
    "bullet list": r"<ul[\s>]",
    "numbered list": r"<ol[\s>]",
    "quote": r"<blockquote[\s>]",
    "heading": r"<h[1-6][\s>]",
    "code block": r"<pre[\s>]",
    "table": r"<table[\s>]",
}


def squash(text: str) -> str:
    """Comparison form: whitespace and zero-width characters carry no meaning."""
    return re.sub(r"[\s​﻿]+", "", text)


def inline(text: str, marks: frozenset = frozenset(), href: str | None = None) -> list[dict]:
    """Split text into runs: {"t": text, "m": sorted marks, "href": url or None}."""
    runs: list[dict] = []

    def add(piece: str, extra: frozenset = frozenset(), link: str | None = href) -> None:
        if piece:
            runs.append({"t": piece, "m": sorted(marks | extra), "href": link})

    position = 0
    for match in _INLINE.finditer(text):
        add(text[position:match.start()])
        position = match.end()
        kind = match.lastgroup
        if kind == "esc":
            add(match["esc_t"])
        elif kind == "code":
            add(match["code_t"], frozenset({"code"}))
        elif kind == "link":
            runs.extend(inline(match["link_t"], marks, match["href"]))
        else:
            mark = "em" if kind == "em2" else kind
            runs.extend(inline(match[kind + "_t"], marks | {mark}, href))
    add(text[position:])
    return runs


def blocks(text: str) -> list[dict]:
    """Parse the Markdown subset into blocks. Unsupported syntax stays literal text."""
    out: list[dict] = []
    paragraph: list[str] = []
    lines = text.split("\n")

    def flush() -> None:
        if paragraph:
            out.append({"type": "p", "runs": inline("\n".join(paragraph))})
            paragraph.clear()

    def take(pattern: str, index: int) -> tuple[list[str], int]:
        items = []
        while index < len(lines) and (found := re.fullmatch(pattern, lines[index])):
            items.append(found[1])
            index += 1
        return items, index

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            flush()
            end = next((j for j in range(i + 1, len(lines)) if lines[j].startswith("```")), None)
            body = "\n".join(lines[i + 1:end]) if end else ""
            if not body.strip():
                raise ValueError("Code block must be closed with ``` and must not be empty")
            out.append({"type": "pre", "runs": [{"t": body, "m": [], "href": None}]})
            i = end + 1
            continue
        heading = re.fullmatch(r"(#{1,3}) +(\S.*)", line)
        if heading:
            flush()
            out.append({"type": f"h{len(heading[1])}", "runs": inline(heading[2])})
            i += 1
            continue
        for kind, pattern in (("ul", r"[-*] +(\S.*)"), ("ol", r"\d{1,3}[.)] +(\S.*)")):
            items, after = take(pattern, i)
            if items:
                flush()
                out.append({"type": kind, "items": [inline(x) for x in items]})
                i = after
                break
        else:
            quoted, after = take(r"> ?(.*)", i)
            if quoted and "".join(quoted).strip():
                flush()
                out.append({"type": "quote", "runs": inline("\n".join(quoted).strip())})
                i = after
            elif not line.strip():
                flush()
                i += 1
            else:
                paragraph.append(line)
                i += 1
    flush()
    return out


def _runs_html(runs: list[dict]) -> str:
    parts = []
    for run in runs:
        piece = html.escape(run["t"], quote=False).replace("\n", "<br>")
        for mark, tag in _TAGS:
            if mark in run["m"]:
                piece = f"<{tag}>{piece}</{tag}>"
        if run["href"]:
            piece = f'<a href="{html.escape(run["href"], quote=True)}">{piece}</a>'
        parts.append(piece)
    return "".join(parts)


def _runs_text(runs: list[dict]) -> str:
    return "".join(run["t"] for run in runs)


def render(message: dict) -> dict:
    """Return what the adapter composes and what must be true before sending.

    plain: visible text expected in the draft and in the delivered message.
    html: fixed HTML for the paste path, or "" when plain typing is enough.
    type_text: text typed on the plain path.
    require: [name, regex] pairs that the composed draft must match.
    """
    text = message["text"]
    title = message.get("title", "")
    table = message.get("table", [])
    # Stored jobs from before the format field existed were plain text.
    if message.get("format", "plain") == "markdown":
        parsed = blocks(text)
    else:
        parsed = [{"type": "p", "runs": [{"t": part, "m": [], "href": None}]}
                  for part in re.split(r"\n{2,}", text) if part.strip()]
    if not parsed:
        raise ValueError("Message has no visible text")

    need: set[str] = set()
    body: list[str] = []
    visible: list[str] = [title] if title else []
    if title:
        need.add("bold")
        body.append(f"<p><strong>{html.escape(title, quote=False)}</strong></p>")
    for block in parsed:
        kind = block["type"]
        runs = [r for item in block["items"] for r in item] if "items" in block else block["runs"]
        for run in runs:
            need.update({"bold": "bold", "em": "italic", "strike": "strikethrough",
                         "code": "code"}[m] for m in run["m"])
            if run["href"]:
                need.add("link")
        if kind in ("ul", "ol"):
            need.add("bullet list" if kind == "ul" else "numbered list")
            body.append(f"<{kind}>" + "".join(f"<li>{_runs_html(x)}</li>" for x in block["items"]) + f"</{kind}>")
            visible.extend(_runs_text(x) for x in block["items"])
            continue
        if kind == "quote":
            need.add("quote")
            body.append(f"<blockquote><p>{_runs_html(runs)}</p></blockquote>")
        elif kind == "pre":
            need.add("code block")
            body.append(f"<pre><code>{html.escape(runs[0]['t'], quote=False)}</code></pre>")
        elif kind == "p":
            body.append(f"<p>{_runs_html(runs)}</p>")
        else:
            need.add("heading")
            body.append(f"<{kind}>{_runs_html(runs)}</{kind}>")
        visible.append(_runs_text(runs))
    if table:
        need.add("table")
        header = message.get("table_header", False)
        if header:
            need.add("bold")
        rows = []
        for index, row in enumerate(table):
            cells = (html.escape(cell, quote=False) for cell in row)
            wrap = "<td><strong>{}</strong></td>" if header and index == 0 else "<td>{}</td>"
            rows.append("<tr>" + "".join(wrap.format(cell) for cell in cells) + "</tr>")
        body.append("<table><tbody>" + "".join(rows) + "</tbody></table>")
        visible.extend(cell for row in table for cell in row)
    rich = bool(need)
    return {
        "plain": "\n".join(visible),
        "html": "".join(body) if rich else "",
        "type_text": "\n".join(visible),
        "require": [[name, _REQUIRE[name]] for name in sorted(need)],
    }


def format_problems(rendered: dict, raw_html: str) -> list[str]:
    """Names of requested formats missing from persisted or composed HTML."""
    return [name for name, pattern in rendered["require"] if not re.search(pattern, raw_html, re.I)]

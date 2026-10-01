"""Match markdown link fragments against heading anchors for check-doc-links.sh (#3048).

stdin holds NUL-terminated (source, target, link) triples; target is the existing ``.md``
file the link points at. Anchors are GitHub heading slugs (github-slugger) of ATX headings
outside fences and front matter, plus ``<a id|name>`` values. Exit 0 when every fragment
matches, 3 after printing ``broken anchor: <source> -> <link>`` for each miss, 2 when the
input or a target cannot be read. Python itself exits 1 on an uncaught exception, so the
caller treats every status but 0 and 3 as a failed check.
"""

from __future__ import annotations

import html
import os
import re
import sys
import unicodedata
from urllib.parse import unquote

_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING_HASHES = re.compile(r"(?:^|[ \t]+)#+$")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_CODE_SPAN = re.compile(r"(`+)(.+?)\1")
_TAG = re.compile(r"<[^>]*>")
_EXPLICIT = re.compile(r"<a\s[^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
_MISS = 3


def _kept(char: str) -> bool:
    category = unicodedata.category(char)
    return char in " -" or category[0] in "LM" or category in ("Nd", "Nl", "Pc")


def _rendered(heading: str) -> str:
    text = _LINK.sub(r"\1", heading)
    parts: list[str] = []
    end = 0
    for span in _CODE_SPAN.finditer(text):
        parts += [html.unescape(_TAG.sub("", text[end : span.start()])), span[2]]
        end = span.end()
    parts.append(html.unescape(_TAG.sub("", text[end:])))
    return "".join(parts)


def slug(heading: str) -> str:
    text = _rendered(heading).strip().lower()
    return "".join(c for c in text if _kept(c)).replace(" ", "-")


def _unique(base: str, occurrences: dict[str, int]) -> str:
    result = base
    while result in occurrences:
        occurrences[base] += 1
        result = f"{base}-{occurrences[base]}"
    occurrences[result] = 0
    return result


def _body(lines: list[str]) -> list[str]:
    if lines[:1] == ["---"] and "---" in lines[1:]:
        return lines[lines.index("---", 1) + 1 :]
    return lines


def anchors(path: str) -> set[str]:
    with open(path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    found: set[str] = set()
    occurrences: dict[str, int] = {}
    fence = ""
    for line in _body(lines):
        if fence:
            close = _FENCE_CLOSE.match(line)
            if close and close[1][0] == fence[0] and len(close[1]) >= len(fence):
                fence = ""
            continue
        opener = _FENCE_OPEN.match(line)
        if opener:
            fence = opener[1]
            continue
        found.update(value.lower() for value in _EXPLICIT.findall(line))
        heading = _HEADING.match(line)
        if heading:
            text = _CLOSING_HASHES.sub("", heading[1] or "")
            found.add(_unique(slug(text), occurrences))
    return found


def main() -> int:
    fields = sys.stdin.buffer.read().split(b"\0")
    if fields.pop() != b"" or len(fields) % 3:
        print("check_doc_anchors: expected NUL-terminated triples", file=sys.stderr)
        return 2
    cache: dict[str, set[str]] = {}
    broken = False
    for index in range(0, len(fields), 3):
        source, target, link = fields[index : index + 3]
        path = os.path.normpath(os.fsdecode(target))
        try:
            if path not in cache:
                cache[path] = anchors(path)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"check_doc_anchors: cannot read anchors from {path}: {exc}", file=sys.stderr)
            return 2
        fragment = unquote(link.decode("utf-8", "replace").partition("#")[2]).lower()
        if fragment not in cache[path]:
            sys.stderr.buffer.write(b"broken anchor: " + source + b" -> " + link + b"\n")
            broken = True
    return _MISS if broken else 0


if __name__ == "__main__":
    sys.exit(main())

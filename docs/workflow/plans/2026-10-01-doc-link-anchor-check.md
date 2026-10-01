# Markdown anchor check for `docs-links` (#3048) — plan

Goal: `just docs-links` fails on a relative or same-file markdown link whose `#fragment` names
no heading anchor in its `.md` target. Architecture: `scripts/check-doc-links.sh` keeps
enumeration and extraction and hands fragment links, NUL-separated, to a new stdlib-only
`scripts/check_doc_anchors.py` that builds GitHub heading slugs. Spec:
[design](../specs/2026-10-01-doc-link-anchor-check-design.md).

Expected implementation size: 210–280 changed lines (M) — about 105 lines of helper, about 35
lines of shell change, and about 115 lines of new and adjusted tests.

## Global Constraints

- Shell: bash 3.2 compatible (no `mapfile`, no namerefs), `shellcheck` and `shfmt -i 2` clean
  (`just lint-shell`). Helper: Python stdlib only, runnable by the `python3` on `PATH`
  (CI's Ubuntu system Python), so no syntax newer than 3.9; ruff and `ty` clean.
- No new dependency, ADR or migration. `check-served-doc-links.sh` is not changed.
- File scope: `scripts/check-doc-links.sh`, `scripts/check_doc_anchors.py`,
  `tests/scripts/test_check_doc_links.py`, and this design set.
- Gates: `just lint`, `just type`, `just lint-shell`, `just docs-links`,
  `just test-verbose tests/scripts/test_check_doc_links.py`, pre-push
  `just ci > <file> 2>&1 < /dev/null`.

## Task 1 — anchor-check fragment links

Files: create `scripts/check_doc_anchors.py`; modify `scripts/check-doc-links.sh`; test
`tests/scripts/test_check_doc_links.py`.

Interfaces: the helper reads stdin as NUL-terminated `source`, `target path`, `link` triples
(paths relative to the scanned root); writes `broken anchor: <source> -> <link>` lines to
stderr; exits 0 (all match), 3 (a miss), 2 (bad input or unreadable target). Python exits 1 on
an uncaught exception, so the script treats every status other than 0 and 3 as a stage
failure. Borrowed test helpers, confirmed in `tests/scripts/test_check_doc_links.py`:
`_run(root: Path) -> subprocess.CompletedProcess[str]` and
`_tool_stub(tmp_path, monkeypatch, name: str, body: str) -> None`.

Verification:

- Contract: a cross-file and a same-file missing fragment each fail and are named. Mode:
  focused-test. `test_missing_anchor_fails_and_names_link[cross-file|same-file]`. Red after
  step 1: exit 0 with `markdown links resolve`. Green:
  `just test-verbose tests/scripts/test_check_doc_links.py`.
- Contract: GitHub slugs and explicit anchors resolve. Mode: focused-test.
  `test_heading_anchor_resolves[...]`. These pass vacuously before step 4 (the old script
  never reads fragments); they bite through the slug fault in step 7. Same green command.
- Contract: lines inside backtick and tilde fences and front matter are not anchors. Mode:
  focused-test. `test_non_heading_lines_are_not_anchors[...]`. Red after step 1: exit 0.
- Contract: fragments on non-`.md` targets are not anchor-checked. Mode: focused-test.
  `test_fragment_on_non_markdown_target_ignored`. Passes before and after.
- Contract: a failing or crashing `python3` cannot certify links. Mode: focused-test.
  `test_anchor_stage_failure_cannot_certify_links[...]` over exit 1 and 23, with and without
  partial output. Red after step 1: no `cannot check markdown anchors` line.
- Contract: the scanned tree, including this design set, starts green. Mode: focused-test
  (gate run). `just docs-links` prints `markdown links resolve`, exit 0.

### Step 1 — tests

In `tests/scripts/test_check_doc_links.py`, replace `test_external_and_anchor_only_links_ignored`
and the third `test_no_matches_pass` parameter, and append the new tests:

```python
def test_external_links_ignored(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("[x](https://example.com) [y](#section)\n## Section\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("text", ["", "No links\n", "[empty]() [anchor](#here)\n# Here\n"])
def test_no_matches_pass(tmp_path: Path, text: str) -> None:
    (tmp_path / "a.md").write_text(text)
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "markdown links resolve" in result.stdout


@pytest.mark.parametrize("same_file", [False, True], ids=["cross-file", "same-file"])
def test_missing_anchor_fails_and_names_link(tmp_path: Path, same_file: bool) -> None:
    (tmp_path / "b.md").write_text("## Real heading\n")
    link = "#no-such-anchor" if same_file else "b.md#no-such-anchor"
    (tmp_path / "a.md").write_text(f"## Real heading\n[x](b.md#real-heading) [y]({link})\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert f"broken anchor: a.md -> {link}\n" in result.stderr
    assert "markdown links resolve" not in result.stdout


@pytest.mark.parametrize(
    ("heading", "fragment"),
    [
        ("## Real heading", "real-heading"),
        ("## `just ci` — the gate", "just-ci--the-gate"),
        ("## Foo\n\n## Foo", "foo-1"),
        ("### Step 2: run (fast)!", "step-2-run-fast"),
        ("## [Linked](a.md) text", "linked-text"),
        ("## Closing hashes ##", "closing-hashes"),
        ("## Café §3 → next", "caf%C3%A9-3--next"),
        ("## Issue ① — X", "issue---x"),
        ("## zero `<token>.ready`", "zero-tokenready"),
        ("## A &amp; B", "a--b"),
        ('<a id="custom-spot"></a>', "custom-spot"),
        ('<a name="Legacy"></a>', "legacy"),
        ("## KDIVE_FOO setting", "KDIVE_FOO-setting"),
        ("---\ntitle: x\n# note\n---\n# note", "note"),
    ],
)
def test_heading_anchor_resolves(tmp_path: Path, heading: str, fragment: str) -> None:
    (tmp_path / "b.md").write_text(f"{heading}\n")
    (tmp_path / "a.md").write_text(f"[x](b.md#{fragment})\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("\x60\x60\x60\n## Hidden\n\x60\x60\x60\n", "hidden"),
        ("~~~~\n## Hidden\n~~~\n~~~~\n", "hidden"),
        ("---\ntitle: x\n# note\n---\n# note\n", "note-1"),
    ],
    ids=["backtick-fence", "tilde-fence", "front-matter"],
)
def test_non_heading_lines_are_not_anchors(tmp_path: Path, text: str, fragment: str) -> None:
    (tmp_path / "b.md").write_text(text)
    (tmp_path / "a.md").write_text(f"[x](b.md#{fragment})\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert f"broken anchor: a.md -> b.md#{fragment}" in result.stderr


def test_fragment_on_non_markdown_target_ignored(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text("pass\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "a.md").write_text("[x](tool.py#L1) [y](docs/#z) [z](b.md#)\n")
    (tmp_path / "b.md").write_text("hi\n")
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("status", [1, 23])
@pytest.mark.parametrize("partial", [False, True], ids=["empty", "partial"])
def test_anchor_stage_failure_cannot_certify_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int, partial: bool
) -> None:
    (tmp_path / "a.md").write_text("# Here\n[x](#here)\n")
    output = "echo 'broken anchor: x' >&2\n" if partial else ""
    _tool_stub(tmp_path, monkeypatch, "python3", f"{output}echo injected >&2\nexit {status}")
    result = _run(tmp_path)
    assert result.returncode != 0
    assert "injected" in result.stderr
    assert "cannot check markdown anchors" in result.stderr
    assert "markdown links resolve" not in result.stdout
```

### Step 2 — observe red

Run `just test-verbose tests/scripts/test_check_doc_links.py`. Expected: the missing-anchor,
non-anchor and stage-failure tests fail; everything else passes.

### Step 3 — helper

Create `scripts/check_doc_anchors.py`:

```python
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
```

### Step 4 — shell wiring

In `scripts/check-doc-links.sh`, replace header lines 3-4 with:

```bash
# Reports only; exits 1 if a relative link target is missing, or if a link's #fragment
# (same-file, or into a .md target) matches no heading or <a id|name> anchor there, using
# GitHub's heading slugs (scripts/check_doc_anchors.py). External (scheme://, mailto:) links
# are ignored.
```

Before `cd "${ROOT}"` add:

```bash
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF_DIR
```

Before the outer `while` loop add `anchored=()`. Replace the inner loop body after its
`[[ -z "$target" ]] && continue` line with:

```bash
    case "$target" in
    *"://"* | mailto:*) continue ;;
    esac
    # strip a trailing CommonMark title:  [t](dest "title")  -> dest
    target="${target%% *}"
    path="${target%%#*}"
    fragment="${target#"$path"}"
    if [[ -n "$path" && ! -e "${dir}/${path}" ]]; then
      printf "broken link: %s -> %s\n" "$f" "$path" >&2
      broken=1
      continue
    fi
    ((${#fragment} > 1)) || continue
    anchor_file="${dir}/${path}"
    [[ -n "$path" ]] || anchor_file="$f"
    if [[ "$anchor_file" == *.md && -f "$anchor_file" ]]; then
      anchored+=("$f" "$anchor_file" "$target")
    fi
```

After the outer loop, before the final `if ((broken))`, add:

```bash
if ((${#anchored[@]})); then
  anchor_status=0
  printf '%s\0' "${anchored[@]}" | python3 "${SELF_DIR}/check_doc_anchors.py" || anchor_status=$?
  case "$anchor_status" in
  0) ;;
  3) broken=1 ;;
  *)
    printf 'cannot check markdown anchors; check python3 diagnostics above\n' >&2
    exit 1
    ;;
  esac
fi
```

### Step 5 — green

Run `just test-verbose tests/scripts/test_check_doc_links.py`, `just lint-shell`, `just lint`,
`just type` and `just docs-links`. Expected: each exits 0; `just docs-links` prints
`markdown links resolve`.

### Step 6 — commit

Commit `feat(scripts): check markdown link anchors in docs-links (#3048)`.

### Step 7 — controlled faults, after the commit

- Change one existing `#fragment` link in a scanned doc to `#fragment-x`. `just docs-links`
  exits 1 with `broken anchor: <doc> -> ...#fragment-x`. Restore it with
  `git checkout -- <that doc>`.
- In `_kept`, also keep category `Sm`/`Pd` (so `—` and `→` survive).
  `test_heading_anchor_resolves` goes red on the `—`/`→` cases. Restore with
  `git checkout -- scripts/check_doc_anchors.py`.
- Rerun `just docs-links` and the focused tests: exit 0.

Rollback: revert the commit; no state or data is involved.

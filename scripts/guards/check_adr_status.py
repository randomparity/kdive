"""Guard the ADR status lifecycle against drift (docs/adr/, ADR ratification rule).

The ADR README pins the rule: an ADR opens as **Proposed** and becomes **Accepted** when the
PR implementing its decision merges, flipping the ADR's ``Status`` in that same PR. This guard
enforces the two invariants that keep the status field honest:

1. **Valid status.** Every ADR file under ``docs/adr/`` (``NNNN-*.md`` except the template)
   has a parseable ``Status`` whose leading keyword is one of Proposed / Accepted / Rejected /
   Superseded (a trailing qualifier like "Superseded for runtime assembly by 0063" or
   "Accepted — …" is allowed).
2. **No shipped-but-Proposed drift.** No ADR whose status keyword is ``Proposed`` is cited
   in production source (``src/``) or in the test suite (``tests/``). A citation there means
   the decision is implemented — including guard-type ADRs whose enforcement ships purely as
   tests, never as ``src/`` code — so the ADR should have been advanced to Accepted (or
   superseded). This is the drift the backfill cleaned up; the guard stops it returning.

The former **index sync** invariant is gone with the index itself (ADR-0504): the directory
listing is the index, so there is no table to keep in step. Record *shape* and anti-erasure
are the `records` workflow's job (``.github/scripts/check-records.sh``). Invariant 1 is not
redundant with that gate's status check: the records gate grandfathers the pre-0504 corpus to
``W-LEGACY-SHAPE`` warnings, so this is what keeps every ADR's status keyword checked at full
severity, in kdive's own ``- **Status:** X`` bullet form.

Stdlib only (plain ``python3``, no ``uv sync``), so CI runs it without a synced env.
Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_ADR_DIR = _ROOT / "docs" / "adr"
_SRC = _ROOT / "src"
_TESTS = _ROOT / "tests"

_VALID = ("Proposed", "Accepted", "Rejected", "Superseded")
_ADR_FILE = re.compile(r"^(\d{4})-.+\.md$")
# A Status line in any of the inline formats the pre-0504 ADRs use: "- **Status:** X",
# "- Status: X", "Status: X" — capture the value, dropping an inline HTML legend comment.
_STATUS_LINE = re.compile(r"^\s*[-*]*\s*\**Status:?\**\s*(.+?)\s*$")
# A "## Status" *heading*, whose value sits on a following line. This is the shape the
# records gate requires (ADR-0504), so every ADR from 0504 on uses it while the
# grandfathered corpus keeps the inline form above. Both are read; see the
# docs/debt/0001-legacy-adr-shape-is-grandfathered.md deferral record.
_STATUS_HEADING = re.compile(r"^#{2,}\s*Status\s*$")
# A citation of an ADR in source: "ADR-0048", "ADR 0048", or "adr/0048".
_CITATION = re.compile(r"(?:ADR[-\s]?|adr/)(\d{4})")


def _keyword(status: str) -> str:
    """Return the leading status keyword (first word), stripping markdown/comment noise."""
    status = re.sub(r"<!--.*", "", status)
    status = status.replace("*", "").strip()
    return status.split()[0] if status else ""


def _file_status(path: Path) -> str | None:
    """The status value from an ADR file, in either supported shape, or None if absent.

    Two shapes are read, because the corpus is deliberately two-shaped
    (docs/debt/0001-legacy-adr-shape-is-grandfathered.md):
    the pre-0504 inline bullet (``- **Status:** Accepted``), and the ``## Status`` heading
    whose value is on a following line (``Accepted (2026-07-29)``), which is what the
    records gate requires of every ADR from 0504 on.

    Under a heading, a supersession banner (``> **Superseded by …**``) is skipped so the
    underlying status keyword is still what gets validated — the banner accompanies the
    status line for an ADR rather than replacing it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if _STATUS_HEADING.match(line):
            for follow in lines[i + 1 :]:
                if follow.startswith("#"):
                    break  # next section began; this heading had no value under it
                stripped = follow.strip()
                if stripped and not stripped.startswith(">"):
                    return stripped
            return None
        m = _STATUS_LINE.match(line)
        if m and "Status" in line:
            return m.group(1)
    return None


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(_ROOT))
    except ValueError:
        return str(path)


def _format_read_error(path: Path, exc: UnicodeDecodeError | OSError) -> str:
    return f"{_relative_path(path)}: could not read file ({type(exc).__name__}: {exc})"


def _is_scannable_source(path: Path) -> bool:
    return "__pycache__" not in path.parts and path.suffix != ".pyc"


def _cited_in_src_or_tests(read_errors: list[str] | None = None) -> set[str]:
    """ADR numbers cited anywhere under src/ (production code) or tests/ (guard enforcement).

    Some ADRs — notably CI-guard decisions — ship entirely as tests and are never cited by
    src/, so both trees count as "implemented" evidence.
    """
    cited: set[str] = set()
    for path in (*_SRC.rglob("*"), *_TESTS.rglob("*")):
        if not path.is_file() or not _is_scannable_source(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            if read_errors is not None:
                read_errors.append(_format_read_error(path, exc))
            continue
        cited.update(_CITATION.findall(text))
    return cited


def main() -> int:
    errors: list[str] = []
    read_errors: list[str] = []

    file_status: dict[str, str] = {}
    for path in sorted(_ADR_DIR.glob("[0-9]*.md")):
        m = _ADR_FILE.match(path.name)
        if not m or path.name == "0000-template.md":
            continue
        num = m.group(1)
        raw = _file_status(path)
        if raw is None:
            errors.append(f"{path.name}: no Status line found")
            continue
        kw = _keyword(raw)
        if kw not in _VALID:
            errors.append(f"{path.name}: invalid Status keyword {kw!r} (expected one of {_VALID})")
            continue
        file_status[num] = kw

    cited = _cited_in_src_or_tests(read_errors)
    for num in sorted(file_status):
        if file_status[num] == "Proposed" and num in cited:
            errors.append(
                f"ADR {num}: status is Proposed but it is cited in src/ or tests/ — the "
                f"decision appears implemented. Advance it to Accepted (or supersede it)."
            )

    if errors:
        print("ADR status guard found problems:\n")
        for e in errors:
            print(f"  - {e}")
        print("\nSee docs/adr/README.md for the ratification rule.")
    if read_errors:
        print("ADR status guard could not read scanned files:\n", file=sys.stderr)
        for e in read_errors:
            print(f"  - {e}", file=sys.stderr)
    if errors or read_errors:
        return 1

    print(f"ADR status guard: {len(file_status)} ADRs, no shipped-but-Proposed drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

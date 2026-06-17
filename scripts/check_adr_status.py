"""Guard the ADR status lifecycle against drift (docs/adr/, ADR ratification rule).

The ADR README pins the rule: an ADR opens as **Proposed** and becomes **Accepted** when
the PR implementing its decision merges, flipping *both* the ADR's ``Status`` line and its
row in the index in that same PR. This guard enforces the three invariants that keep the
status field honest:

1. **Valid status.** Every ADR file (``docs/adr/NNNN-*.md`` except the template) has a
   parseable ``Status`` whose leading keyword is one of Proposed / Accepted / Rejected /
   Superseded (a trailing qualifier like "Superseded for runtime assembly by 0063" or
   "Accepted — …" is allowed).
2. **Index sync.** The README index has exactly one row per ADR file (and no row for a
   missing file), and each row's status keyword matches the ADR file's — so the index can
   never claim Proposed while the file says Accepted, or vice versa.
3. **No shipped-but-Proposed drift.** No ADR whose status keyword is ``Proposed`` is cited
   in production source (``src/``). A citation there means the decision is implemented, so
   the ADR should have been advanced to Accepted (or superseded). This is the drift the
   backfill cleaned up; the guard stops it returning.

Stdlib only (plain ``python3``, no ``uv sync``), so CI runs it without a synced env.
Exit 0 clean, 1 on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_ADR_DIR = _ROOT / "docs" / "adr"
_INDEX = _ADR_DIR / "README.md"
_SRC = _ROOT / "src"

_VALID = ("Proposed", "Accepted", "Rejected", "Superseded")
_ADR_FILE = re.compile(r"^(\d{4})-.+\.md$")
# A Status line in any of the formats the ADRs use: "- **Status:** X", "- Status: X",
# "Status: X" — capture the value, dropping an inline HTML legend comment.
_STATUS_LINE = re.compile(r"^\s*[-*]*\s*\**Status:?\**\s*(.+?)\s*$")
# An index row: "| [0048](0048-....md) | decision | Accepted |".
_INDEX_ROW = re.compile(r"^\|\s*\[(\d{4})\]\([^)]+\)\s*\|.*\|\s*([^|]+?)\s*\|\s*$")
# A citation of an ADR in source: "ADR-0048", "ADR 0048", or "adr/0048".
_CITATION = re.compile(r"(?:ADR[-\s]?|adr/)(\d{4})")


def _keyword(status: str) -> str:
    """Return the leading status keyword (first word), stripping markdown/comment noise."""
    status = re.sub(r"<!--.*", "", status)
    status = status.replace("*", "").strip()
    return status.split()[0] if status else ""


def _file_status(path: Path) -> str | None:
    """The status value from an ADR file's first Status line, or None if absent."""
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _STATUS_LINE.match(line)
        if m and "Status" in line:
            return m.group(1)
    return None


def _index_statuses() -> dict[str, str]:
    """Map ADR number -> index-row status keyword."""
    out: dict[str, str] = {}
    for line in _INDEX.read_text(encoding="utf-8").splitlines():
        m = _INDEX_ROW.match(line)
        if m:
            out[m.group(1)] = _keyword(m.group(2))
    return out


def _cited_in_src() -> set[str]:
    """ADR numbers cited anywhere under src/ (production code)."""
    cited: set[str] = set()
    for path in _SRC.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        cited.update(_CITATION.findall(text))
    return cited


def main() -> int:
    errors: list[str] = []

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

    index_status = _index_statuses()

    only_files = sorted(set(file_status) - set(index_status))
    only_index = sorted(set(index_status) - set(file_status))
    for num in only_files:
        errors.append(f"ADR {num}: present as a file but missing from the README index")
    for num in only_index:
        errors.append(f"ADR {num}: an index row exists but no ADR file does")

    for num in sorted(set(file_status) & set(index_status)):
        if file_status[num] != index_status[num]:
            errors.append(
                f"ADR {num}: status drift — file says {file_status[num]!r}, "
                f"index says {index_status[num]!r}. Flip both in the same PR."
            )

    cited = _cited_in_src()
    for num in sorted(file_status):
        if file_status[num] == "Proposed" and num in cited:
            errors.append(
                f"ADR {num}: status is Proposed but it is cited in src/ — the decision "
                f"appears implemented. Advance it to Accepted (or supersede it)."
            )

    if errors:
        print("ADR status guard found problems:\n")
        for e in errors:
            print(f"  - {e}")
        print("\nSee docs/adr/README.md for the ratification rule.")
        return 1

    print(
        f"ADR status guard: {len(file_status)} ADRs, index in sync, no shipped-but-Proposed drift."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

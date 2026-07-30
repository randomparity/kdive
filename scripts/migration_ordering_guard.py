"""Structural guard: a new migration is numbered above every migration on the base branch
(ADR-0517).

Migration numbers are pre-assigned to concurrent branches, which prevents *filename*
collisions but says nothing about the order those branches merge in. `0085` landed after
`0086` was already on `origin/main` (#1553 / #1718): `apply_migrations` records versions
individually and skips ones already recorded, so a database already at 0086 simply applies
0085 afterwards. That was harmless only because the two migrations were independent — a
property nobody checked. A lower-numbered migration that *depends* on schema introduced by
a higher-numbered one applies cleanly on a fresh database and fails, or silently misbehaves,
on an existing one: green in CI, broken only where it matters (issue #1720).

This guard compares the migrations on disk against those on a base ref (default
``origin/main``) and rejects any file the branch *adds* whose version is not strictly above
the highest version the base branch already carries. Strictly-above, not exactly-plus-one:
abandoned numbers leave legitimate gaps.

It is deliberately offline — it reads a local ref and never fetches, so it cannot make a
core gate depend on network reachability (ADR-0505). Resolving the base ref is therefore the
caller's job; CI fetches it in the step before this one, and locally ``git fetch origin``
before ``just ci`` is the existing convention. An unresolvable base ref is a hard failure,
never a silent pass — a guard that cannot fail is worthless (#1723).

Stdlib-only (``subprocess`` + ``git``) so it runs without a synced venv. Exit 0 clean,
1 on violations or an unresolvable base ref.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_DIR = Path("src/kdive/db/schema")
DEFAULT_BASE_REF = "origin/main"

# Mirrors kdive.db.migrate._FILENAME_RE: the runner rejects anything else outright.
_FILENAME_RE = re.compile(r"^(\d{4})_.+\.sql$")


class BaseRefError(RuntimeError):
    """The base ref could not be resolved, so the guard has nothing to compare against."""


@dataclass(frozen=True)
class Violation:
    """A newly added migration that does not sort above the base branch."""

    filename: str
    version: str | None  # None when the filename does not parse as NNNN_*.sql
    base_max: str | None  # highest version on the base ref, None when it has no migrations

    def message(self) -> str:
        """A one-line, actionable description naming the file, its number, and the max."""
        if self.version is None:
            return (
                f"{self.filename}: not a migration filename — expected NNNN_<name>.sql "
                "with a four-digit version (the runner rejects anything else)"
            )
        return (
            f"{self.filename}: version {self.version} is not above the base branch maximum "
            f"{self.base_max} — renumber it above {self.base_max}. Migrations must apply in "
            "ascending order on databases that are already migrated, and a number at or "
            "below the base maximum has already been passed there (#1720)"
        )


def parse_version(filename: str) -> str | None:
    """Return the four-digit version of a migration filename, or None if it has none."""
    match = _FILENAME_RE.match(filename)
    return match.group(1) if match else None


def find_violations(
    base_filenames: Iterable[str], head_filenames: Iterable[str]
) -> list[Violation]:
    """Flag every migration the branch adds that does not sort above the base branch.

    Args:
        base_filenames: Bare migration filenames present on the base ref.
        head_filenames: Bare migration filenames present on the branch.

    Returns:
        One :class:`Violation` per offending added file, ordered by filename. Files
        already on the base ref are ignored — an in-place edit or a deletion is the
        schema-immutability guard's concern (ADR-0015), not this one.
    """
    base = set(base_filenames)
    base_versions = [v for v in (parse_version(name) for name in base) if v is not None]
    base_max = max(base_versions, key=int) if base_versions else None
    violations: list[Violation] = []
    for filename in sorted(set(head_filenames) - base):
        version = parse_version(filename)
        if version is None or (base_max is not None and int(version) <= int(base_max)):
            violations.append(Violation(filename, version, base_max))
    return violations


def _base_filenames(base_ref: str) -> list[str]:
    """Return the migration filenames on ``base_ref``, or raise if it does not resolve."""
    result = subprocess.run(
        ["git", "ls-tree", "--name-only", f"{base_ref}:{SCHEMA_DIR}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BaseRefError(
            f"cannot read {SCHEMA_DIR}/ at {base_ref!r}: {result.stderr.strip()}. "
            f"Run 'git fetch origin {base_ref.removeprefix('origin/')}' first — this guard "
            "reads a local ref and never fetches on its own"
        )
    return [name for name in result.stdout.splitlines() if name.endswith(".sql")]


def _head_filenames() -> list[str]:
    """Return the migration filenames in the working tree (so an uncommitted add counts)."""
    return [path.name for path in SCHEMA_DIR.glob("*.sql")]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base_ref = args[0] if args else DEFAULT_BASE_REF
    try:
        base = _base_filenames(base_ref)
    except BaseRefError as exc:
        print(f"::error::migration-ordering guard could not run: {exc}", file=sys.stderr)
        return 1
    violations = find_violations(base, _head_filenames())
    for violation in violations:
        print(f"::error::{violation.message()}", file=sys.stderr)
    if violations:
        print(
            f"{len(violations)} migration(s) added below the highest version on {base_ref}; "
            "renumber them so migrations stay strictly ascending across merges",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

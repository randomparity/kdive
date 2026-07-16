"""Structural guard: applied SQL migrations are byte-immutable (ADR-0015).

The migration runner (:mod:`kdive.db.migrate`) records the SHA-256 of each applied
file's *whole bytes* — comments and whitespace included — and hard-fails startup if a
recorded file's hash no longer matches disk. So a cosmetic edit (a doc-comment
reword, a whitespace change) to an already-committed migration silently breaks every
database migrated by an earlier build: it can no longer upgrade (issue #1218).

This guard forbids modifying, deleting, or renaming any existing
``src/kdive/db/schema/*.sql`` file. Only *adding* a new migration is allowed. It runs
as a prek hook at commit time and in ``just ci``: ``git diff --name-status`` against a
reference (default ``HEAD``) is fed to :func:`find_violations`, and any change other
than an addition of a schema file is rejected. A legitimate schema change is a new
numbered file, never an in-place edit of an applied one.

Stdlib-only (``subprocess`` + ``git``) so CI runs it without a synced venv. Exit 0
clean, 1 on violations.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass

_SCHEMA_PREFIX = "src/kdive/db/schema/"
_SCHEMA_SUFFIX = ".sql"


@dataclass(frozen=True)
class Violation:
    """A disallowed change to an existing migration file."""

    status: str  # git name-status code: M, D, R…, T, etc.
    path: str  # the offending schema-file path (the destination for a rename)


def _is_schema_file(path: str) -> bool:
    return path.startswith(_SCHEMA_PREFIX) and path.endswith(_SCHEMA_SUFFIX)


def find_violations(name_status: Iterable[str]) -> list[Violation]:
    """Flag every change to an existing schema file in ``git diff --name-status`` output.

    Each line is a tab-separated record: ``<STATUS>\\t<PATH>`` for adds/edits/deletes,
    or ``<STATUS>\\t<OLD>\\t<NEW>`` for renames and copies (status ``R``/``C`` with a
    similarity score, e.g. ``R100``).

    A migration is an immutable snapshot: only *adding* a new ``schema/*.sql`` file is
    allowed. Modifying (``M``/``T``), deleting (``D``), or renaming (``R``) an existing
    schema file is a violation — a rename changes the numbered identity a released DB
    recorded. A pure add (``A``), and a copy (``C``) whose source is left intact, are
    fine.

    Args:
        name_status: Lines of ``git diff --name-status`` output.

    Returns:
        One :class:`Violation` per disallowed change, in input order.
    """
    violations: list[Violation] = []
    for line in name_status:
        record = line.rstrip("\n")
        if not record:
            continue
        fields = record.split("\t")
        status = fields[0]
        code = status[0] if status else ""
        if code in {"A", "C"}:
            # A new file (or a copy whose destination is a new file) is allowed; the
            # copy's source path is untouched, so an existing migration is unchanged.
            continue
        if code == "R":
            # Rename: the source (an existing migration) is being removed/renamed.
            old_path = fields[1] if len(fields) > 1 else ""
            if _is_schema_file(old_path):
                violations.append(Violation(status, old_path))
            continue
        # M, D, T, and any other in-place change to a tracked path.
        path = fields[1] if len(fields) > 1 else ""
        if _is_schema_file(path):
            violations.append(Violation(status, path))
    return violations


def _git_name_status(ref: str) -> list[str]:
    """Return ``git diff --name-status <ref>`` lines scoped to the schema directory."""
    result = subprocess.run(
        ["git", "diff", "--name-status", ref, "--", _SCHEMA_PREFIX],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    ref = args[0] if args else "HEAD"
    violations = find_violations(_git_name_status(ref))
    for v in violations:
        print(
            f"{v.path}: applied migration changed ({v.status}); "
            "migration files are byte-immutable — add a new NNNN_*.sql instead (ADR-0015)",
            file=sys.stderr,
        )
    if violations:
        print(
            f"{len(violations)} disallowed change(s) to existing migration file(s); "
            "see ADR-0015 (schema files are immutable snapshots)",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

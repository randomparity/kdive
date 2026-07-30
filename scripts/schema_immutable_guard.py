"""Structural guard: applied SQL migrations are byte-immutable (ADR-0015).

The migration runner (:mod:`kdive.db.migrate`) records the SHA-256 of each applied
file's *whole bytes* — comments and whitespace included — and hard-fails startup if a
recorded file's hash no longer matches disk. So a cosmetic edit (a doc-comment
reword, a whitespace change) to an already-committed migration silently breaks every
database migrated by an earlier build: it can no longer upgrade (issue #1218).

This guard forbids modifying, deleting, or renaming any existing
``src/kdive/db/schema/*.sql`` file. Only *adding* a new migration is allowed. It compares
the tree against a base ref (default ``origin/main``): ``git diff --name-status`` scoped to
the schema directory is fed to :func:`find_violations`, and any change other than an
addition of a schema file is rejected.

The base ref is the whole point. Diffing against ``HEAD`` — the shape this guard shipped
with — asks a question about the working tree, so a clean checkout compares HEAD with
itself and passes unconditionally. CI checks out a clean tree, so the guard enforced nothing
there on any PR (issue #1723). Against ``origin/main`` the comparison is the branch's actual
contribution, which is the same question at commit time and in CI.

It is deliberately offline — it reads a local ref and never fetches, so the guard itself
does not depend on network reachability (ADR-0505) and reproduces exactly from a checkout.
Making the base ref resolvable is the caller's job; CI fetches it in a step before this one,
and locally ``git fetch origin`` before ``just ci`` is the existing convention.

Every way the comparison can come up empty is a hard failure, never a clean run: an
unreadable base ref, a base ref carrying no migrations, a cwd outside the repository. A
guard that cannot fail is worthless (#1723).

Stdlib-only (``subprocess`` + ``git``) so CI runs it without a synced venv. Exit 0
clean, 1 on violations or on any of those failures.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

SCHEMA_SUBDIR = "src/kdive/db/schema"
DEFAULT_BASE_REF = "origin/main"

_SCHEMA_PREFIX = f"{SCHEMA_SUBDIR}/"
_SCHEMA_SUFFIX = ".sql"


class GuardError(RuntimeError):
    """The guard cannot compare anything, so it must fail rather than report a clean run."""


@dataclass(frozen=True)
class Violation:
    """A disallowed change to an existing migration file."""

    status: str  # git name-status code: M, D, R…, T, etc.
    path: str  # the offending schema-file path (the source for a rename)

    def message(self, base_ref: str) -> str:
        """A one-line, actionable description naming the file and what happened to it."""
        line = (
            f"{self.path}: applied migration changed ({self.status}) relative to {base_ref}; "
            "migration files are byte-immutable — add a new NNNN_*.sql instead (ADR-0015)"
        )
        if self.status.startswith("D"):
            # A file on the base and absent here also describes a branch that simply has
            # not caught up: the base gained a migration after this branch diverged. Same
            # diff, opposite cause, and the reader cannot tell them apart without the hint.
            line += (
                f". If this branch never touched the file, {base_ref} gained it after the "
                f"branch diverged — merge {base_ref} in and re-run"
            )
        return line


def _is_schema_file(path: str) -> bool:
    return path.startswith(_SCHEMA_PREFIX) and path.endswith(_SCHEMA_SUFFIX)


def find_violations(base_filenames: Iterable[str], name_status: Iterable[str]) -> list[Violation]:
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
        base_filenames: Bare migration filenames present on the base ref, used only to
            prove the base was read. The diff itself carries the comparison.
        name_status: Lines of ``git diff --name-status`` output against that base ref.

    Returns:
        One :class:`Violation` per disallowed change, in input order.

    Raises:
        GuardError: The base ref carries no migration at all. There is no such state in
            this repository, so it means the base read went wrong — and against a base
            without the schema directory every existing migration diffs as an *addition*,
            which this guard allows. The empty comparison would pass everything.
    """
    if not any(name.endswith(_SCHEMA_SUFFIX) for name in base_filenames):
        raise GuardError(f"the base ref carries no {SCHEMA_SUBDIR}/*.sql migration")
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


def _fetch_hint(base_ref: str) -> str:
    """How to make ``base_ref`` resolvable, given only its shape.

    A ref of the form ``<remote>/<branch>`` names its own remote, so the command can be
    exact — splitting on the *first* slash, since a branch may contain more of them. Any
    other shape (a bare branch, a SHA, ``HEAD~1``) tells us nothing about where it comes
    from, so suggesting a specific fetch would be a guess.
    """
    remote, _, branch = base_ref.partition("/")
    if not branch:
        return f"make {base_ref!r} resolvable (it names no remote, so there is no fetch to suggest)"
    return f"run 'git fetch {remote} {branch}'"


def _repo_root() -> Path:
    """The repository root, so neither git read depends on the caller's cwd."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise GuardError(f"not inside a git repository: {result.stderr.strip()}")
    return Path(result.stdout.strip())


def _base_filenames(root: Path, base_ref: str) -> list[str]:
    """Return the migration filenames on ``base_ref``, or raise if it cannot be read."""
    # `cwd=root` is what makes this cwd-independent; `--full-tree` says so a second time,
    # because `ls-tree` filters entries by the cwd prefix without it and would return an
    # empty list at exit 0 — a silent pass — if the anchoring above were ever lost.
    result = subprocess.run(
        ["git", "ls-tree", "--full-tree", "--name-only", f"{base_ref}:{SCHEMA_SUBDIR}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(
            f"cannot read {SCHEMA_SUBDIR}/ there: {result.stderr.strip()}. "
            f"{_fetch_hint(base_ref)} — this guard reads a local ref and never fetches"
        )
    return [name for name in result.stdout.splitlines() if name.endswith(_SCHEMA_SUFFIX)]


def _name_status(root: Path, base_ref: str) -> list[str]:
    """Return ``git diff --name-status <base_ref>`` lines scoped to the schema directory.

    Run from the repository root because a `git diff` pathspec resolves against the *cwd*:
    from a subdirectory it would match nothing and report a clean tree. `-M` pins rename
    detection on rather than inheriting whatever `diff.renames` the caller has configured,
    so a rename is always reported against its source path. `core.quotePath=false` stops git
    wrapping a non-ASCII path in quotes and octal escapes, which would no longer match the
    schema prefix and would drop the file from the comparison silently.
    """
    result = subprocess.run(
        # fmt: off
        [
            "git", "-c", "core.quotePath=false",
            "diff", "--name-status", "-M", base_ref, "--", _SCHEMA_PREFIX,
        ],
        # fmt: on
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise GuardError(f"cannot diff against it: {result.stderr.strip()}")
    return result.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base_ref = args[0] if args else DEFAULT_BASE_REF
    try:
        root = _repo_root()
        violations = find_violations(_base_filenames(root, base_ref), _name_status(root, base_ref))
    except GuardError as exc:
        print(
            f"::error::schema-immutability guard could not run against {base_ref!r}: {exc}",
            file=sys.stderr,
        )
        return 1
    for v in violations:
        print(f"::error::{v.message(base_ref)}", file=sys.stderr)
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

"""Map a git diff to the pytest targets a change touches (ADR-0420, issue #1334).

Prints one pytest target path per line, or the single sentinel line ``__ALL__`` when the
change set is unmappable and the caller should run the full suite. Empty output means "no
changed tests — nothing to run". Consumed by the ``just test-changed`` recipe.

The selection logic (``select_targets``) is a pure function so it is unit-tested with
injected inputs; the git and filesystem access around it are thin wrappers. Stdlib-only.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

_TESTS_PREFIX = "tests/"
_SRC_PREFIX = "src/kdive/"
_FULL_SUITE = "__ALL__"

# Base branches to diff against, in order of preference. The first that resolves wins.
_BASE_REFS = ("origin/main", "main")


def _test_stem(filename: str) -> str | None:
    """Return the stem a pytest filename maps to, or None if it is not a test file.

    ``test_errors.py`` -> ``errors``; ``widget_test.py`` -> ``widget``.
    """
    if not filename.endswith(".py"):
        return None
    base = filename[: -len(".py")]
    if base.startswith("test_"):
        return base[len("test_") :]
    if base.endswith("_test"):
        return base[: -len("_test")]
    return None


def is_test_file(path: str) -> bool:
    """True for a ``tests/**/test_*.py`` or ``tests/**/*_test.py`` path."""
    return path.startswith(_TESTS_PREFIX) and _test_stem(Path(path).name) is not None


def is_src_file(path: str) -> bool:
    """True for a ``src/kdive/**/*.py`` path."""
    return path.startswith(_SRC_PREFIX) and path.endswith(".py")


def build_test_index(repo_root: Path) -> dict[str, list[str]]:
    """Index every test file on disk by the stem it maps to (sorted, repo-relative)."""
    index: dict[str, list[str]] = {}
    for path in sorted((repo_root / "tests").rglob("*.py")):
        stem = _test_stem(path.name)
        if stem is None:
            continue
        index.setdefault(stem, []).append(path.relative_to(repo_root).as_posix())
    return index


def select_targets(
    changed: Iterable[str],
    test_index: Mapping[str, list[str]],
) -> list[str] | None:
    """Map a changed-file set to pytest target paths, or None to run the full suite.

    Errs toward over-running: any changed file that cannot be mapped to a named test
    forces the full suite (None), because a green run that silently skipped the affected
    test is worse than one that ran too much. ``test_index`` maps a stem to its existing
    ``tests/**/test_<stem>.py`` paths — its values are the filesystem source of truth for
    which test files exist.

    Args:
        changed: Repo-relative paths that differ from the base (see ``changed_files``).
        test_index: Stem -> existing test-file paths, from ``build_test_index``.

    Returns:
        None to signal "run the full suite" (a changed source file with no matching test,
        or any change that is neither a test file nor a mappable source file). Otherwise
        the de-duplicated, sorted list of pytest targets — possibly empty ("nothing to
        run") when the only changes were deleted test files.
    """
    existing_tests = {path for paths in test_index.values() for path in paths}
    targets: set[str] = set()
    for path in changed:
        if is_test_file(path):
            # A test file that still exists is a direct target; a deleted one is dropped
            # (passing a missing path to pytest is a hard error, not a safe over-run).
            if path in existing_tests:
                targets.add(path)
            continue
        if is_src_file(path):
            # A source file's tests are named after its basename (foo.py -> test_foo).
            matches = test_index.get(Path(path).stem)
            if not matches:
                return None  # unmapped source change -> full suite (the safe default)
            targets.update(matches)
            continue
        return None  # conftest, non-Python, config, docs, justfile -> full suite
    return sorted(targets)


def _git(args: list[str], repo_root: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def _git_lines(args: list[str], repo_root: Path) -> list[str]:
    return [line for line in _git(args, repo_root).splitlines() if line]


def _resolve_base_ref(repo_root: Path) -> str | None:
    for ref in _BASE_REFS:
        if _git(["rev-parse", "--verify", "--quiet", ref], repo_root).strip():
            return ref
    return None


def changed_files(repo_root: Path, base_ref: str) -> list[str]:
    """Files differing between the branch's merge-base with ``base_ref`` and the worktree.

    Covers committed branch work and uncommitted edits (``git diff --name-only`` against
    the merge-base tree) plus untracked files. No fetch — the inner loop stays offline.
    """
    merge_base = _git(["merge-base", base_ref, "HEAD"], repo_root).strip()
    paths = set(_git_lines(["diff", "--name-only", merge_base or base_ref], repo_root))
    paths.update(_git_lines(["ls-files", "--others", "--exclude-standard"], repo_root))
    return sorted(paths)


def main() -> int:
    repo_root = Path(_git(["rev-parse", "--show-toplevel"], Path.cwd()).strip() or ".")
    base_ref = _resolve_base_ref(repo_root)
    if base_ref is None:
        # No base branch resolves (default branch not 'main', no 'origin', shallow clone):
        # the changed set is unknowable, so run the full suite rather than an
        # uncommitted-only HEAD diff that would silently skip committed branch work.
        print(_FULL_SUITE)
        return 0
    targets = select_targets(changed_files(repo_root, base_ref), build_test_index(repo_root))
    if targets is None:
        print(_FULL_SUITE)
    elif targets:
        print("\n".join(targets))
    # An empty target list prints nothing, so the recipe reads zero lines as "nothing to run".
    return 0


if __name__ == "__main__":
    sys.exit(main())

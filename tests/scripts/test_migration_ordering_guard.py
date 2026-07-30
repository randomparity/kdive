"""The migration-ordering guard (issue #1720).

Migration numbers are pre-assigned to parallel branches, which prevents *filename*
collisions but not *ordering* ones: 0085 merged after 0086 was already on `origin/main`
(#1553 / #1718). A database already at 0086 then applies 0085 afterwards — safe only
because those two happened to be independent. The guard rejects a newly added migration
numbered at or below the highest version already on the base branch.

The comparison logic is exercised directly; the entry point is exercised end to end against
a throwaway repo, because the guard's dangerous failure is not a wrong verdict but a clean
run over nothing — an unreadable ref, an empty base, an empty schema directory, or a cwd
that makes git report either as empty.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.migration_ordering_guard import GuardError, find_violations, parse_version


def _names(hits) -> list[str]:
    return [v.filename for v in hits]


def test_new_migration_above_the_base_maximum_is_allowed() -> None:
    base = ["0085_a.sql", "0086_b.sql"]
    assert find_violations(base, [*base, "0087_c.sql"]) == []


def test_new_migration_below_the_base_maximum_is_a_violation() -> None:
    # The exact #1553 / #1718 shape: 0085 authored while main had already reached 0086.
    base = ["0084_a.sql", "0086_b.sql"]
    hits = find_violations(base, [*base, "0085_c.sql"])
    assert _names(hits) == ["0085_c.sql"]
    assert hits[0].version == "0085"
    assert hits[0].base_max == "0086"


def test_new_migration_equal_to_the_base_maximum_is_a_violation() -> None:
    # Two branches pre-assigned the same number: whichever merges second collides.
    base = ["0086_b.sql"]
    hits = find_violations(base, [*base, "0086_other.sql"])
    assert _names(hits) == ["0086_other.sql"]


def test_a_gap_above_the_maximum_is_allowed() -> None:
    # The rule is strictly-greater-than-max, not exactly-max-plus-one: numbers may be
    # abandoned (a reverted branch) and the survivors still apply in order.
    base = ["0086_b.sql"]
    assert find_violations(base, [*base, "0090_c.sql"]) == []


def test_unchanged_tree_passes() -> None:
    base = ["0085_a.sql", "0086_b.sql"]
    assert find_violations(base, list(base)) == []


def test_a_base_with_no_migrations_is_a_hard_error() -> None:
    # There is no such state in this repository, so an empty base means the read went
    # wrong. Without a maximum every added file would pass — the guard would report a
    # clean run over nothing, which is the one failure mode it exists to avoid.
    with pytest.raises(GuardError):
        find_violations([], ["0001_initial.sql"])


def test_a_base_of_only_unparseable_names_is_a_hard_error() -> None:
    with pytest.raises(GuardError):
        find_violations(["notes.sql"], ["notes.sql", "0001_initial.sql"])


def test_every_offending_file_is_reported() -> None:
    base = ["0086_b.sql"]
    hits = find_violations(base, [*base, "0084_c.sql", "0085_d.sql", "0087_e.sql"])
    assert _names(hits) == ["0084_c.sql", "0085_d.sql"]
    assert [v.version for v in hits] == ["0084", "0085"]


def test_a_migration_removed_from_head_is_not_our_concern() -> None:
    # Deleting an applied migration is a violation of ADR-0015, caught by the sibling
    # schema-immutability guard. This guard only looks at what the branch adds.
    assert find_violations(["0085_a.sql", "0086_b.sql"], ["0085_a.sql"]) == []


def test_unparseable_new_filename_is_a_violation() -> None:
    # Never skip a file we cannot classify: a guard that silently ignores its own
    # blind spot is worse than no guard.
    hits = find_violations(["0086_b.sql"], ["0086_b.sql", "0087a_c.sql"])
    assert _names(hits) == ["0087a_c.sql"]
    assert hits[0].version is None


def test_unparseable_base_filename_does_not_become_the_maximum() -> None:
    hits = find_violations(["0086_b.sql", "junk.sql"], ["0086_b.sql", "junk.sql", "0087_c.sql"])
    assert hits == []


def test_violation_message_names_the_file_its_number_and_the_maximum() -> None:
    hits = find_violations(["0086_b.sql"], ["0086_b.sql", "0085_c.sql"])
    message = hits[0].message()
    assert "0085_c.sql" in message
    assert "0086" in message


def test_unparseable_violation_message_names_the_expected_shape() -> None:
    hits = find_violations(["0086_b.sql"], ["0086_b.sql", "nope.sql"])
    assert "NNNN_" in hits[0].message()


# The fixture repo must not inherit the developer's global git config: a global
# commit.gpgsign or core.hooksPath would break `git commit` here for reasons that have
# nothing to do with the guard.
_ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=os.environ | _ISOLATED_GIT,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo whose `base` branch carries migrations 0085 and 0086."""
    repo_dir = tmp_path / "repo"
    schema = repo_dir / "src/kdive/db/schema"
    schema.mkdir(parents=True)
    for name in ("0085_a.sql", "0086_b.sql"):
        (schema / name).write_text("-- SELECT 1;\n")
    _git(repo_dir, "init", "-q", "-b", "base")
    _git(repo_dir, "config", "user.email", "guard@test")
    _git(repo_dir, "config", "user.name", "guard")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "base migrations")
    return repo_dir


def _run(
    repo: Path, cwd: Path | None = None, base_ref: str | None = "base"
) -> subprocess.CompletedProcess[str]:
    """Invoke the guard's entry point as a subprocess, so the exit code is the real one.

    ``base_ref=None`` omits the argument entirely, exercising DEFAULT_BASE_REF — which is
    how `just migration-order-check` invokes it, and therefore the only path CI takes.
    Running it as `python3` with no venv also holds it to its stdlib-only contract.
    """
    guard = Path(__file__).resolve().parents[2] / "scripts/migration_ordering_guard.py"
    argv = ["python3", str(guard)] + ([] if base_ref is None else [base_ref])
    return subprocess.run(argv, cwd=cwd or repo, capture_output=True, text=True)


def test_end_to_end_a_migration_below_the_base_maximum_is_rejected(repo: Path) -> None:
    (repo / "src/kdive/db/schema/0084_late.sql").write_text("-- SELECT 1;\n")
    result = _run(repo)
    assert result.returncode == 1
    assert "0084_late.sql" in result.stderr
    assert "0086" in result.stderr


def test_end_to_end_a_migration_above_the_base_maximum_is_accepted(repo: Path) -> None:
    (repo / "src/kdive/db/schema/0087_next.sql").write_text("-- SELECT 1;\n")
    assert _run(repo).returncode == 0


def test_end_to_end_names_the_violation_when_invoked_from_a_subdirectory(repo: Path) -> None:
    # `git ls-tree` filters entries by the cwd prefix unless --full-tree is passed, so from
    # a subdirectory it reports an empty base — which must not degrade into a different
    # error, let alone a pass. The guard names the same offending file from anywhere.
    (repo / "src/kdive/db/schema/0084_late.sql").write_text("-- SELECT 1;\n")
    result = _run(repo, cwd=repo / "src/kdive/db")
    assert result.returncode == 1
    assert "0084_late.sql" in result.stderr


def test_end_to_end_an_unresolvable_remote_ref_is_a_failure_with_a_fetch_hint(repo: Path) -> None:
    result = _run(repo, base_ref="origin/no-such-branch")
    assert result.returncode == 1
    assert "git fetch origin no-such-branch" in result.stderr


def test_end_to_end_an_unresolvable_bare_ref_fails_without_guessing_a_remote(repo: Path) -> None:
    # A bare name names no remote, so there is no honest fetch command to suggest.
    result = _run(repo, base_ref="no-such-ref")
    assert result.returncode == 1
    assert "no-such-ref" in result.stderr
    assert "git fetch" not in result.stderr


def test_end_to_end_an_empty_schema_directory_is_a_failure(repo: Path) -> None:
    # The head-side twin of an empty base: nothing added is indistinguishable from
    # nothing read, so it must not be reported as a clean run.
    for path in (repo / "src/kdive/db/schema").glob("*.sql"):
        path.unlink()
    result = _run(repo)
    assert result.returncode == 1
    assert "nothing to check" in result.stderr


def test_end_to_end_outside_a_repository_is_a_failure(repo: Path) -> None:
    # tmp_path itself is outside the fixture repo, and outside this checkout.
    result = _run(repo, cwd=repo.parent)
    assert result.returncode == 1
    assert "not inside a git repository" in result.stderr


def test_end_to_end_a_clean_branch_is_accepted(repo: Path) -> None:
    assert _run(repo).returncode == 0


def test_end_to_end_the_default_base_ref_is_the_one_ci_compares_against(repo: Path) -> None:
    # `just migration-order-check` passes no argument, so DEFAULT_BASE_REF is the only
    # base CI ever uses. Every other test here names a ref explicitly, which would leave
    # the constant free to drift to something that compares the head against itself —
    # `HEAD`, say — and pass every PR forever.
    #
    # The migration is committed, not just written, because that is the state CI checks
    # out. It is also what makes the assertion discriminate: against `origin/main` the
    # file is an addition, while against `HEAD` it is already in the base.
    _git(repo, "update-ref", "refs/remotes/origin/main", "base")
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/kdive/db/schema/0084_late.sql").write_text("-- SELECT 1;\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "add a migration below the base maximum")
    result = _run(repo, base_ref=None)
    assert result.returncode == 1
    assert "0084_late.sql" in result.stderr


def test_parse_version_reads_the_four_digit_prefix() -> None:
    assert parse_version("0086_restore_incomplete_category.sql") == "0086"
    assert parse_version("0086.sql") is None
    assert parse_version("86_short.sql") is None
    assert parse_version("00086_long.sql") is None
    assert parse_version("0086_x.txt") is None

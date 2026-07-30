"""The schema-immutability guard (ADR-0015, issues #1218 and #1723).

Applied migrations are byte-immutable: the runner hashes whole-file bytes, so a
cosmetic edit to an already-committed migration breaks upgrades of any DB migrated by
an earlier build. The guard parses ``git diff --name-status`` against a base ref and must
allow a new migration file while rejecting any modify/delete/rename of an existing one.

The end-to-end half of this file exists because the unit half cannot see the defect that
#1723 reported: :func:`find_violations` was always correct, and the guard still enforced
nothing in CI because it was handed a diff of ``HEAD`` against ``HEAD``. Only running the
entry point against a real repository shows which comparison it actually makes.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.schema_immutable_guard import GuardError, find_violations

_EXISTING = "src/kdive/db/schema/0003_reprovision_job_kind.sql"
_NEW = "src/kdive/db/schema/0069_new_migration.sql"

# A base ref that carries migrations, so every unit case below exercises the comparison
# rather than the "base could not be read" hard failure.
_BASE = ["0003_reprovision_job_kind.sql", "0018_resources_kind_fault_inject.sql"]


def _paths(hits) -> list[str]:
    return [v.path for v in hits]


def test_modifying_existing_schema_file_is_a_violation() -> None:
    # The exact #1218 regression: a comment-only edit to an applied migration.
    hits = find_violations(_BASE, [f"M\t{_EXISTING}"])
    assert _paths(hits) == [_EXISTING]
    assert hits[0].status == "M"


def test_adding_a_new_schema_file_is_allowed() -> None:
    assert find_violations(_BASE, [f"A\t{_NEW}"]) == []


def test_deleting_an_existing_schema_file_is_a_violation() -> None:
    assert _paths(find_violations(_BASE, [f"D\t{_EXISTING}"])) == [_EXISTING]


def test_renaming_an_existing_schema_file_flags_the_source() -> None:
    # A rename changes the numbered identity a released DB recorded; the old path is
    # the offending one.
    hits = find_violations(_BASE, [f"R100\t{_EXISTING}\t{_NEW}"])
    assert _paths(hits) == [_EXISTING]


def test_copying_to_a_new_schema_file_is_allowed() -> None:
    # A copy leaves the source migration untouched; the destination is effectively new.
    assert find_violations(_BASE, [f"C100\t{_EXISTING}\t{_NEW}"]) == []


def test_type_change_on_existing_schema_file_is_a_violation() -> None:
    assert _paths(find_violations(_BASE, [f"T\t{_EXISTING}"])) == [_EXISTING]


def test_non_schema_files_are_ignored() -> None:
    assert find_violations(_BASE, ["M\tsrc/kdive/db/migrate.py", "A\tdocs/adr/0015.md"]) == []


def test_sql_outside_schema_dir_is_ignored() -> None:
    assert find_violations(_BASE, ["M\ttests/db/fixtures/sample.sql"]) == []


def test_blank_lines_are_skipped() -> None:
    assert find_violations(_BASE, ["", f"A\t{_NEW}", ""]) == []


def test_mixed_batch_reports_only_disallowed_changes() -> None:
    hits = find_violations(
        _BASE,
        [
            f"A\t{_NEW}",
            f"M\t{_EXISTING}",
            "M\tsrc/kdive/db/migrate.py",
            "D\tsrc/kdive/db/schema/0018_resources_kind_fault_inject.sql",
        ],
    )
    assert _paths(hits) == [
        _EXISTING,
        "src/kdive/db/schema/0018_resources_kind_fault_inject.sql",
    ]


def test_a_clean_diff_against_a_populated_base_passes() -> None:
    assert find_violations(_BASE, []) == []


def test_the_message_names_the_file_the_status_and_the_base() -> None:
    message = find_violations(_BASE, [f"M\t{_EXISTING}"])[0].message("origin/main")
    assert _EXISTING in message
    assert "(M)" in message
    assert "origin/main" in message


def test_a_deletion_message_offers_the_stale_branch_reading() -> None:
    # A file on the base and absent here is equally "the branch deleted it" and "the base
    # gained it after the branch diverged"; the reader cannot tell without being told.
    message = find_violations(_BASE, [f"D\t{_EXISTING}"])[0].message("origin/main")
    assert "merge origin/main in and re-run" in message


def test_a_modification_message_does_not_offer_it() -> None:
    # A modification has only one cause, so the hint would be noise at best.
    message = find_violations(_BASE, [f"M\t{_EXISTING}"])[0].message("origin/main")
    assert "re-run" not in message


def test_a_base_with_no_migrations_is_a_hard_error() -> None:
    # Against a base that has no schema directory every existing migration diffs as an
    # addition, which this guard allows — so an unread base would pass everything.
    with pytest.raises(GuardError):
        find_violations([], [f"M\t{_EXISTING}"])


def test_a_base_of_only_non_sql_files_is_a_hard_error() -> None:
    with pytest.raises(GuardError):
        find_violations(["README.md"], [f"M\t{_EXISTING}"])


# The fixture repo must not inherit the developer's global git config: a global
# commit.gpgsign or core.hooksPath would break `git commit` here for reasons that have
# nothing to do with the guard.
_ISOLATED_GIT = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}

_SQL = "-- SELECT 1;\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=os.environ | _ISOLATED_GIT,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo whose `origin/main` carries 0085 and 0086, checked out on a `feature` branch.

    `origin/main` is a real remote-tracking ref rather than a local branch so the default
    base ref — the only one CI ever passes — is exercised by default.
    """
    repo_dir = tmp_path / "repo"
    schema = repo_dir / "src/kdive/db/schema"
    schema.mkdir(parents=True)
    for name in ("0085_a.sql", "0086_b.sql"):
        (schema / name).write_text(_SQL)
    _git(repo_dir, "init", "-q", "-b", "main")
    _git(repo_dir, "config", "user.email", "guard@test")
    _git(repo_dir, "config", "user.name", "guard")
    _git(repo_dir, "add", "-A")
    _git(repo_dir, "commit", "-qm", "base migrations")
    _git(repo_dir, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo_dir, "checkout", "-q", "-b", "feature")
    return repo_dir


def _run(
    repo: Path, cwd: Path | None = None, base_ref: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Invoke the guard's entry point as a subprocess, so the exit code is the real one.

    ``base_ref=None`` — the default here — omits the argument entirely, exercising
    DEFAULT_BASE_REF, which is how both `just schema-guard` and the prek hook invoke it and
    therefore the only base CI ever compares against. Running it as `python3` with no venv
    also holds it to its stdlib-only contract.
    """
    guard = Path(__file__).resolve().parents[2] / "scripts/schema_immutable_guard.py"
    argv = ["python3", str(guard)] + ([] if base_ref is None else [base_ref])
    return subprocess.run(argv, cwd=cwd or repo, capture_output=True, text=True)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


def test_end_to_end_a_committed_edit_on_a_clean_tree_is_rejected(repo: Path) -> None:
    # The #1723 defect itself. The edit is *committed*, so the working tree is clean —
    # exactly the state CI checks out, and exactly the state the old `HEAD` base scored
    # as a pass. Nothing else in this file would catch a regression to that base.
    (repo / "src/kdive/db/schema/0085_a.sql").write_text("-- SELECT 2;\n")
    _commit(repo, "reword a comment in an applied migration")
    result = _run(repo)
    assert result.returncode == 1
    assert "0085_a.sql" in result.stderr


def test_end_to_end_an_uncommitted_edit_is_rejected(repo: Path) -> None:
    # The commit-time half: the prek hook has to bite before the edit is committed.
    (repo / "src/kdive/db/schema/0085_a.sql").write_text("-- SELECT 2;\n")
    result = _run(repo)
    assert result.returncode == 1
    assert "0085_a.sql" in result.stderr


def test_end_to_end_a_committed_deletion_is_rejected(repo: Path) -> None:
    (repo / "src/kdive/db/schema/0086_b.sql").unlink()
    _commit(repo, "delete an applied migration")
    result = _run(repo)
    assert result.returncode == 1
    assert "0086_b.sql" in result.stderr


def test_end_to_end_a_committed_rename_names_the_source(repo: Path) -> None:
    _git(repo, "mv", "src/kdive/db/schema/0086_b.sql", "src/kdive/db/schema/0087_b.sql")
    _commit(repo, "renumber an applied migration")
    result = _run(repo)
    assert result.returncode == 1
    assert "0086_b.sql" in result.stderr


def test_end_to_end_a_new_higher_numbered_migration_is_accepted(repo: Path) -> None:
    # The clean-PR case: adding a migration must stay a pass, committed or not.
    (repo / "src/kdive/db/schema/0087_next.sql").write_text(_SQL)
    _commit(repo, "add a migration")
    assert _run(repo).returncode == 0


def test_end_to_end_an_unrelated_change_is_accepted(repo: Path) -> None:
    (repo / "src/kdive/db/migrate.py").parent.mkdir(parents=True, exist_ok=True)
    (repo / "src/kdive/db/migrate.py").write_text("x = 1\n")
    _commit(repo, "touch the runner, not the migrations")
    assert _run(repo).returncode == 0


def test_end_to_end_a_clean_branch_is_accepted(repo: Path) -> None:
    assert _run(repo).returncode == 0


def test_end_to_end_rejects_from_a_subdirectory(repo: Path) -> None:
    # A `git diff` pathspec resolves against the cwd, so from a subdirectory an
    # unanchored one matches nothing and reports a clean tree. The guard runs its reads
    # from the repository root, so it names the same offending file from anywhere.
    (repo / "src/kdive/db/schema/0085_a.sql").write_text("-- SELECT 2;\n")
    _commit(repo, "reword a comment in an applied migration")
    result = _run(repo, cwd=repo / "src/kdive/db")
    assert result.returncode == 1
    assert "0085_a.sql" in result.stderr


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


def test_end_to_end_a_base_without_migrations_is_a_failure(repo: Path) -> None:
    # Against such a base every existing migration diffs as an addition, so the guard
    # would report a clean run on a branch that rewrote all of them.
    _git(repo, "checkout", "-q", "--orphan", "empty")
    _git(repo, "rm", "-rq", "--cached", ".")
    for path in (repo / "src/kdive/db/schema").glob("*.sql"):
        path.unlink()
    (repo / "src/kdive/db/schema/README.md").write_text("migrations live here\n")
    _commit(repo, "a base that carries no migration")
    result = _run(repo, base_ref="empty")
    assert result.returncode == 1
    assert "no src/kdive/db/schema/*.sql migration" in result.stderr


def test_end_to_end_outside_a_repository_is_a_failure(repo: Path) -> None:
    # tmp_path itself is outside the fixture repo, and outside this checkout.
    result = _run(repo, cwd=repo.parent)
    assert result.returncode == 1
    assert "not inside a git repository" in result.stderr

"""Best-effort warm-tree provenance probes (#861, ADR-0265).

``working_tree_dirty`` and ``staged_tree_sha`` describe whether an operator-staged warm tree
diverges from its ``HEAD`` and, when it does, give a content-deterministic digest of the
tracked working-tree state. Both are best-effort: any git/OS failure returns ``None`` and never
raises, so a build never fails on a provenance probe.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kdive.build_artifacts.provenance import (
    rev_parse_head,
    staged_tree_sha,
    working_tree_dirty,
)

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(tree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(tree), *args],
        check=True,
        capture_output=True,
        text=True,
        env={**_GIT_ENV},
    ).stdout.strip()


def _init_commit(tree: Path) -> str:
    _git(tree, "init", "-q")
    (tree / "f").write_text("x")
    _git(tree, "add", ".")
    _git(tree, "commit", "-q", "-m", "c")
    return _git(tree, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# working_tree_dirty
# ---------------------------------------------------------------------------


def test_dirty_false_on_clean_tree(tmp_path: Path) -> None:
    _init_commit(tmp_path)
    assert working_tree_dirty(str(tmp_path)) is False


def test_dirty_true_on_tracked_edit(tmp_path: Path) -> None:
    _init_commit(tmp_path)
    (tmp_path / "f").write_text("y")
    assert working_tree_dirty(str(tmp_path)) is True


def test_dirty_true_on_untracked_file(tmp_path: Path) -> None:
    _init_commit(tmp_path)
    (tmp_path / "new").write_text("z")
    assert working_tree_dirty(str(tmp_path)) is True


def test_dirty_none_on_non_git_tree(tmp_path: Path) -> None:
    assert working_tree_dirty(str(tmp_path)) is None


def test_dirty_none_on_empty_path() -> None:
    assert working_tree_dirty("") is None


def test_dirty_none_when_git_status_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failed `git status` (non-zero exit: index.lock contention, corrupt index, ...) on a real
    # work tree must degrade to None, NOT a spurious False that would call a dirty build clean.
    _init_commit(tmp_path)
    (tmp_path / "f").write_text("edited")  # genuinely dirty

    def _failing_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="locked")

    monkeypatch.setattr("kdive.build_artifacts.provenance.subprocess.run", _failing_run)
    assert working_tree_dirty(str(tmp_path)) is None


# ---------------------------------------------------------------------------
# staged_tree_sha
# ---------------------------------------------------------------------------


def test_tree_sha_none_on_clean_tree(tmp_path: Path) -> None:
    _init_commit(tmp_path)
    assert staged_tree_sha(str(tmp_path)) is None


def test_tree_sha_none_on_untracked_only(tmp_path: Path) -> None:
    # git stash create captures tracked changes only; an untracked-only tree yields no stash.
    _init_commit(tmp_path)
    (tmp_path / "new").write_text("z")
    assert staged_tree_sha(str(tmp_path)) is None


def test_tree_sha_is_tracked_content_tree_object(tmp_path: Path) -> None:
    head = _init_commit(tmp_path)
    (tmp_path / "f").write_text("edited")
    sha = staged_tree_sha(str(tmp_path))
    assert sha is not None
    # It is a tree object (not a commit), and it is the tree of the working-tree content,
    # which differs from the HEAD commit's own tree.
    assert _git(tmp_path, "cat-file", "-t", sha) == "tree"
    assert sha != _git(tmp_path, "rev-parse", f"{head}^{{tree}}")


def test_tree_sha_is_content_deterministic(tmp_path: Path) -> None:
    # Same tracked content => same tree sha, regardless of when/who staged it.
    _init_commit(tmp_path)
    (tmp_path / "f").write_text("edited")
    first = staged_tree_sha(str(tmp_path))
    second = staged_tree_sha(str(tmp_path))
    assert first is not None
    assert first == second


def test_tree_sha_none_on_non_git_tree(tmp_path: Path) -> None:
    assert staged_tree_sha(str(tmp_path)) is None


def test_tree_sha_none_on_empty_path() -> None:
    assert staged_tree_sha("") is None


# Anchor: the new probes live beside the existing rev-parse probe.
def test_rev_parse_head_round_trips(tmp_path: Path) -> None:
    head = _init_commit(tmp_path)
    assert rev_parse_head(str(tmp_path)) == head

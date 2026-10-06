"""The source fixture must identify the requested commit without altering existing work."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.scripts.kernel_fixture_support import git as _git
from tests.scripts.kernel_fixture_support import repository as repository

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts/fetch-kernel-tree.sh"
_BASH = shutil.which("bash") or "/usr/bin/bash"


def _run(repo: Path, ref: str, dest: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(_SCRIPT), str(dest)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "KDIVE_KERNEL_REPO": str(repo), "KDIVE_KERNEL_REF": ref},
    )


@pytest.mark.parametrize("by_sha", [False, True])
def test_fetches_requested_tag_or_exact_commit(repository, tmp_path: Path, by_sha: bool) -> None:
    repo, sha = repository
    dest = tmp_path / "linux"
    result = _run(repo, sha if by_sha else "v-test", dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(dest.resolve())]
    assert _git(dest, "rev-parse", "HEAD") == sha


def test_matching_checkout_is_reused_without_reset(repository) -> None:
    repo, sha = repository
    (repo / "build-output").write_text("ignored build output")
    result = _run(repo, sha, repo)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(repo.resolve())]
    assert (repo / "build-output").read_text() == "ignored build output"


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked", "wrong-head"])
def test_mismatched_or_modified_source_is_rejected(repository, dirty: str) -> None:
    repo, sha = repository
    name = "extra.c" if dirty == "untracked" else "source.c"
    (repo / name).write_text("changed\n")
    if dirty in {"staged", "wrong-head"}:
        _git(repo, "add", name)
    if dirty == "wrong-head":
        _git(
            repo,
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "changed",
        )
    head = _git(repo, "rev-parse", "HEAD")
    status = _git(repo, "status", "--porcelain")
    result = _run(repo, sha, repo)
    assert result.returncode != 0
    assert result.stdout == ""
    assert _git(repo, "rev-parse", "HEAD") == head
    assert _git(repo, "status", "--porcelain") == status
    assert (repo / name).read_text() == "changed\n"


def test_reuses_git_worktree(repository, tmp_path: Path) -> None:
    repo, sha = repository
    dest = tmp_path / "linked"
    _git(repo, "worktree", "add", "--detach", str(dest), sha)
    result = _run(repo, sha, dest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str(dest.resolve())]


def test_ref_is_checked_even_when_destination_exists(repository) -> None:
    repo, _ = repository
    result = _run(repo, "missing-ref", repo)
    assert result.returncode != 0
    assert result.stdout == ""


def test_rejects_nonrepository_destination(repository, tmp_path: Path) -> None:
    repo, sha = repository
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "keep").write_text("user data")
    result = _run(repo, sha, dest)
    assert result.returncode != 0
    assert result.stdout == ""
    assert (dest / "keep").read_text() == "user data"


def test_relative_destination_emits_absolute_path(repository, tmp_path: Path, monkeypatch) -> None:
    repo, _ = repository
    monkeypatch.chdir(tmp_path)
    result = _run(repo, "v-test", Path("linux"))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [str((tmp_path / "linux").resolve())]

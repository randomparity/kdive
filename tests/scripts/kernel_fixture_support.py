"""Real Git source fixtures shared by kernel acquisition and provenance tests."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "upstream"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "source.c").write_text("original\n")
    (repo / ".gitignore").write_text("build-output\n")
    git(repo, "add", ".")
    git(
        repo,
        "-c",
        "user.name=fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "fixture",
    )
    git(repo, "tag", "v-test")
    return repo, git(repo, "rev-parse", "HEAD")

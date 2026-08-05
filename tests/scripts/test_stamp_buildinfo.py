"""Behavioral tests for scripts/stamp-buildinfo.sh (ADR-0370).

The script writes ``src/kdive/_buildinfo.py`` (COMMIT + RELEASE) that the container build
bakes so a running image reports honest provenance. The container build has no ``.git``, so
the commit is conveyed in via ``KDIVE_BUILDINFO_COMMIT``; these tests drive the script in an
isolated tree (a copy of the script beside a throwaway ``src/kdive/``) so the repo working
tree is never touched, and assert the generated module for each commit/release input.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "stamp-buildinfo.sh"
PYPROJECT_VERSION_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "pyproject-version.sh"
BASH = shutil.which("bash")
GIT = shutil.which("git")

# A non-secret sentinel commit token: the container passes an explicit short SHA, so the test
# only needs to prove the override value lands verbatim, not that it is a real hash.
_SENTINEL = "feedfacecafe"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required to run stamp-buildinfo.sh")

_ESC = "\033"


def _isolated_tree(tmp_path: Path) -> Path:
    """Copy the script into a throwaway repo tree so it writes tmp_path/src/kdive/_buildinfo.py.

    The script derives ``repo_root`` from its own location (``BASH_SOURCE/..``), so placing a
    copy at ``tmp_path/scripts/`` makes ``repo_root == tmp_path`` and the target land under the
    throwaway ``src/kdive/`` — never the real one.
    """
    (tmp_path / "scripts").mkdir()
    (tmp_path / "src" / "kdive").mkdir(parents=True)
    dst = tmp_path / "scripts" / "stamp-buildinfo.sh"
    shutil.copy2(SCRIPT, dst)
    return dst


def _isolated_tree_with_git(tmp_path: Path, *, tag: str) -> Path:
    """Like `_isolated_tree`, but also wires up the git-derived RELEASE path (#1886).

    Copies `scripts/pyproject-version.sh` alongside the script under test (the script calls it
    by repo-relative path), then commits everything and tags HEAD, so the empty-$1 branch's
    `git describe --tags --exact-match` and `git status --porcelain` reads see a clean, tagged
    tree — exercising the branch none of the explicit-arg tests above reach.
    """
    script = _isolated_tree(tmp_path)
    shutil.copy2(PYPROJECT_VERSION_SCRIPT, tmp_path / "scripts" / "pyproject-version.sh")
    (tmp_path / "scripts" / "pyproject-version.sh").chmod(0o755)
    (tmp_path / "src" / "kdive" / ".gitkeep").write_text("", encoding="utf-8")
    git_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for args in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Test"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
        ["tag", "-a", tag, "-m", tag],
    ):
        subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True, env=git_env
        )
    return script


def _stub_uv(stub_root: Path, *, version: str) -> Path:
    """A stub `uv` that always emits a coloured `version --short`, like the real trigger (#1886):
    `uv` colours its output when `FORCE_COLOR` is set in the caller's environment.

    `stub_root` must be OUTSIDE the isolated git tree: the script under test treats its own
    location's parent as `repo_root` and checks `git status --porcelain` there, so a stub
    living inside it would itself show up as an untracked file and always dirty the tree.
    """
    stub_dir = stub_root / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "uv"
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "{_ESC}[36m%s{_ESC}[39m\\n" "{version}"\n', encoding="utf-8"
    )
    stub.chmod(0o755)
    return stub_dir


def _run(
    script: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _buildinfo(tmp_path: Path) -> str:
    return (tmp_path / "src" / "kdive" / "_buildinfo.py").read_text(encoding="utf-8")


def test_override_commit_release_true(tmp_path: Path) -> None:
    script = _isolated_tree(tmp_path)
    res = _run(script, "true", env={"KDIVE_BUILDINFO_COMMIT": _SENTINEL, "PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    content = _buildinfo(tmp_path)
    assert f'COMMIT = "{_SENTINEL}"' in content
    assert "RELEASE = True" in content


def test_override_commit_release_false(tmp_path: Path) -> None:
    script = _isolated_tree(tmp_path)
    res = _run(script, "false", env={"KDIVE_BUILDINFO_COMMIT": _SENTINEL, "PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    content = _buildinfo(tmp_path)
    assert f'COMMIT = "{_SENTINEL}"' in content
    assert "RELEASE = False" in content


def test_override_wins_without_git_repo(tmp_path: Path) -> None:
    # The isolated tree has no .git; the override must be used verbatim rather than falling
    # back to git (which would yield "unknown"). This mirrors the container build, where the
    # slim builder stage has no git binary and no repo.
    script = _isolated_tree(tmp_path)
    res = _run(script, "false", env={"KDIVE_BUILDINFO_COMMIT": _SENTINEL, "PATH": "/usr/bin:/bin"})
    assert res.returncode == 0, res.stderr
    content = _buildinfo(tmp_path)
    assert f'COMMIT = "{_SENTINEL}"' in content
    assert "unknown" not in content


def test_rejects_invalid_release_arg(tmp_path: Path) -> None:
    script = _isolated_tree(tmp_path)
    res = _run(script, "yes", env={"KDIVE_BUILDINFO_COMMIT": _SENTINEL, "PATH": "/usr/bin:/bin"})
    assert res.returncode != 0
    assert not (tmp_path / "src" / "kdive" / "_buildinfo.py").exists()


@pytest.mark.skipif(GIT is None, reason="git is required for the git-derived RELEASE path")
def test_empty_release_arg_derives_true_despite_coloured_uv_output(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Regression for #1886: with no $1, RELEASE is derived from an exact-tag/clean-tree git
    check against the pyproject version. Before the fix, a coloured `uv version --short` never
    string-equalled the plain tag, so an exact, clean release checkout was misclassified
    RELEASE=False — not "artifact poisoning" (the version string itself is never written to
    `_buildinfo.py`), but still baking a wrong provenance flag into the build."""
    script = _isolated_tree_with_git(tmp_path, tag="v1.2.3")
    stub_dir = _stub_uv(tmp_path_factory.mktemp("uv-stub"), version="1.2.3")
    assert GIT is not None
    git_dir = str(Path(GIT).parent)
    res = _run(script, env={"PATH": f"{stub_dir}:{git_dir}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    content = _buildinfo(tmp_path)
    assert "RELEASE = True" in content, content


@pytest.mark.skipif(GIT is None, reason="git is required for the git-derived RELEASE path")
def test_empty_release_arg_still_false_on_a_genuine_mismatch(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Control for the test above: a real tag/version mismatch must still derive RELEASE=False,
    coloured uv output or not — a fix that merely stops erroring must not start rubber-stamping
    every checkout as a release."""
    script = _isolated_tree_with_git(tmp_path, tag="v9.9.9")
    stub_dir = _stub_uv(tmp_path_factory.mktemp("uv-stub"), version="1.2.3")
    assert GIT is not None
    git_dir = str(Path(GIT).parent)
    res = _run(script, env={"PATH": f"{stub_dir}:{git_dir}:/usr/bin:/bin", "HOME": str(tmp_path)})
    assert res.returncode == 0, res.stderr
    content = _buildinfo(tmp_path)
    assert "RELEASE = False" in content, content

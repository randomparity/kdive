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
BASH = shutil.which("bash")

# A non-secret sentinel commit token: the container passes an explicit short SHA, so the test
# only needs to prove the override value lands verbatim, not that it is a real hash.
_SENTINEL = "feedfacecafe"

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required to run stamp-buildinfo.sh")


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

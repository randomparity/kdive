"""Behavioral tests for the Mermaid documentation guardrail recipe (#2156)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")


def test_missing_mermaid_dependencies_name_the_recovery_command(tmp_path: Path) -> None:
    """A fresh worktree fails before Node with the repository-supported setup command."""
    shutil.copy2(_JUSTFILE, tmp_path / "justfile")
    shutil.copytree(
        _ROOT / ".github" / "scripts" / "mermaid-check",
        tmp_path / ".github" / "scripts" / "mermaid-check",
        ignore=shutil.ignore_patterns("node_modules"),
    )
    (tmp_path / "README.md").write_text("# Fresh worktree\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)

    assert _JUST is not None
    result = subprocess.run(
        [
            _JUST,
            "--justfile",
            str(tmp_path / "justfile"),
            "--working-directory",
            str(tmp_path),
            "check-mermaid",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
    )

    assert result.returncode != 0
    assert (
        "Mermaid checker dependencies are missing; run `just install-mermaid-deps` "
        "from the repository root."
    ) in result.stderr
    assert "ERR_MODULE_NOT_FOUND" not in result.stderr

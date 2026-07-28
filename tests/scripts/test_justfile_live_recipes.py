# tests/scripts/test_justfile_live_recipes.py
"""Behavioral tests for the `just test-live-remote` recipe's exit-code handling (#1627).

The recipe selects a marker family with pytest. pytest exit 5 ("no tests collected") cannot
mean "no remote environment": an absent environment makes a *carrier* skip at run time, which
is exit 0 with skips reported. Exit 5 means the family has zero carriers — the recipe proved
nothing — so it must not read as success.

These drive the real recipe with a stub `uv` on PATH that returns a chosen exit code, so the
assertion is over the recipe's observable behavior rather than over its source text.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

# The whole repo is driven through `just` (CI runs `just lint` / `just type` / `just test`), so
# this gate does not fire in CI; it keeps a `just`-less direct-pytest invocation from erroring.
pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")


def _run_recipe(recipe: str, tmp_path: Path, uv_exit_code: int) -> subprocess.CompletedProcess[str]:
    """Run ``recipe`` with a stub ``uv`` that exits ``uv_exit_code`` instead of running pytest."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    stub.write_text(f"#!/usr/bin/env bash\nexit {uv_exit_code}\n", encoding="utf-8")
    stub.chmod(0o755)
    assert _JUST is not None
    return subprocess.run(
        [_JUST, "--justfile", str(_JUSTFILE), "--working-directory", str(_ROOT), recipe],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_no_tests_collected_fails_the_remote_recipe(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=5)
    assert result.returncode != 0, (
        "pytest exit 5 means the live_vm_remote family has zero carriers, so the run proved "
        f"nothing — the recipe must not exit 0 (got {result.returncode}); {result.stdout}"
    )
    assert "live_vm_remote" in result.stderr


def test_passing_remote_run_succeeds(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=0)
    assert result.returncode == 0, result.stderr


def test_failing_remote_run_propagates_its_exit_code(tmp_path: Path) -> None:
    result = _run_recipe("test-live-remote", tmp_path, uv_exit_code=1)
    assert result.returncode == 1, result.stderr

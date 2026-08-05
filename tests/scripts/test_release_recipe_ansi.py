# tests/scripts/test_release_recipe_ansi.py
"""Behavioral test for the `release` recipe's ANSI handling (#1886).

`release`'s pyproject-version compare (justfile, `release VERSION`) had the same latent defect
as `chart-version-check` (#1883): a coloured `uv version --short` read never string-equalled the
plain `VERSION` argument, so a real release matching the given version would abort with a false
"pyproject version X != X" error under `FORCE_COLOR` — aborting a release that needed no change.

This drives the real recipe with a stub `git` (never touches the real repository — every git
subcommand the recipe calls is stubbed) and a stub `uv` that always emits a coloured version
string, isolating the version compare from the recipe's git-sync preconditions.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")

pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")

_ESC = "\033"

# Stubs every git subcommand `release` calls, always reporting a clean, synced, on-main state,
# so the recipe reaches the version compare without touching a real repository.
_STUB_GIT = """#!/usr/bin/env bash
set -euo pipefail
cmd="${1:-}"
shift || true
case "$cmd" in
  branch) echo "main" ;;
  status) : ;;
  fetch) : ;;
  rev-parse) echo "deadbeefcafe" ;;
  tag) echo "stub: tag $*" >&2 ;;
  push) echo "stub: push $*" >&2 ;;
  *)
    echo "unstubbed git subcommand: $cmd $*" >&2
    exit 99
    ;;
esac
"""


def _run_release(
    tmp_path: Path, *, uv_version: str, release_version: str
) -> subprocess.CompletedProcess[str]:
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    git_stub = stub_dir / "git"
    git_stub.write_text(_STUB_GIT, encoding="utf-8")
    git_stub.chmod(0o755)

    uv_stub = stub_dir / "uv"
    uv_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "{_ESC}[36m%s{_ESC}[39m\\n" "{uv_version}"\n',
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)

    assert _JUST is not None
    return subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(tmp_path),
            "release",
            release_version,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_matching_version_passes_despite_coloured_uv_output(tmp_path: Path) -> None:
    result = _run_release(tmp_path, uv_version="1.2.3", release_version="1.2.3")
    assert result.returncode == 0, (
        "a matching release version must pass even when uv's read is ANSI-coloured "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_genuinely_different_version_still_fails(tmp_path: Path) -> None:
    result = _run_release(tmp_path, uv_version="1.2.3", release_version="9.9.9")
    assert result.returncode == 1, (
        "a real version mismatch must still abort the release "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert "1.2.3" in result.stderr
    assert "9.9.9" in result.stderr

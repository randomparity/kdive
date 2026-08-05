# tests/scripts/test_chart_version_check_ansi.py
"""Behavioral test for the `chart-version-check` recipe's ANSI handling (#1883).

`uv version --short` colours its output when `FORCE_COLOR` is set in the environment (#1883
reproduced this with a dev shell exporting `FORCE_COLOR=3`). The Chart.yaml-side read is plain
text, so the comparison `[[ "$chart" != "$pyproject" ]]` never matched even when the versions
were equal, false-failing `just chart-version-check` (and, transitively, `just ci`, which stops
at the first failing recipe).

Two complementary drives:

- A stub `uv` that always emits a coloured version string, isolating the strip logic from any
  particular colour source and from the repo's current version.
- The real `uv` against the real repo with `FORCE_COLOR=3` set, reproducing the actual reported
  trigger end to end.

Both are paired with a genuine-mismatch control, so a fix that merely stops failing (rather than
comparing correctly) cannot pass silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_JUSTFILE = _ROOT / "justfile"
_JUST = shutil.which("just")
_UV = shutil.which("uv")

# The whole repo is driven through `just` (CI runs `just lint` / `just type` / `just test`), so
# this gate does not fire in CI; it keeps a `just`-less direct-pytest invocation from erroring.
pytestmark = pytest.mark.skipif(_JUST is None, reason="just is required to drive a justfile recipe")

_ESC = "\033"


def _run_recipe(
    tmp_path: Path, *, uv_version: str, chart_version: str
) -> subprocess.CompletedProcess[str]:
    """Run `chart-version-check` with a stub `uv` that always emits a coloured version.

    The recipe runs against a throwaway working directory carrying its own
    `deploy/helm/kdive/Chart.yaml`, so the test controls both sides of the compare without
    touching the real repo files or coupling to the repo's current version.
    """
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    stub = stub_dir / "uv"
    # Mimic the observed real-world behaviour: colour is emitted regardless of args, so the
    # recipe must not rely on a caller flag or environment variable to get plain output.
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "{_ESC}[36m%s{_ESC}[39m\\n" "{uv_version}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)

    chart_dir = tmp_path / "deploy" / "helm" / "kdive"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text(f'appVersion: "{chart_version}"\n', encoding="utf-8")

    assert _JUST is not None
    return subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(tmp_path),
            "chart-version-check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{stub_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_matching_versions_pass_despite_coloured_uv_output(tmp_path: Path) -> None:
    result = _run_recipe(tmp_path, uv_version="0.4.1", chart_version="0.4.1")
    assert result.returncode == 0, (
        "equal versions must pass even when uv's read is ANSI-coloured "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


def test_genuinely_different_versions_still_fail(tmp_path: Path) -> None:
    result = _run_recipe(tmp_path, uv_version="0.4.1", chart_version="0.9.9")
    assert result.returncode == 1, (
        f"a real drift must still be caught (stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert "0.4.1" in result.stderr
    assert "0.9.9" in result.stderr


@pytest.mark.skipif(_UV is None, reason="uv is required to reproduce the real trigger")
def test_force_color_env_reproduces_the_real_bug_and_still_passes() -> None:
    """Drives the real `uv` against the real repo with the actual reported trigger.

    The stub-based tests above isolate the strip logic; this one proves the trigger reported in
    #1883 is actually handled end to end — `FORCE_COLOR` set in the caller's environment, the
    real `uv version --short`, and the real Chart.yaml/pyproject.toml.
    """
    assert _JUST is not None
    result = subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(_ROOT),
            "chart-version-check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "3"},
    )
    assert result.returncode == 0, (
        "the real recipe must pass under FORCE_COLOR=3 "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )


@pytest.mark.skipif(_UV is None, reason="uv is required to reproduce the real trigger")
def test_force_color_env_still_fails_on_a_real_mismatch(tmp_path: Path) -> None:
    """Same real-`uv`/`FORCE_COLOR=3` drive as above, but against a throwaway Chart.yaml that
    genuinely disagrees with the real pyproject version — the control for the test above: a fix
    that merely silences the recipe (rather than comparing correctly) must not pass this one.

    `uv version --short` needs a `pyproject.toml` findable from the working directory (it walks
    up and errors otherwise), so the real one is copied in standalone rather than pointed at the
    real repo root, which would also pick up the real (matching) Chart.yaml.
    """
    shutil.copy(_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    chart_dir = tmp_path / "deploy" / "helm" / "kdive"
    chart_dir.mkdir(parents=True)
    (chart_dir / "Chart.yaml").write_text('appVersion: "0.0.0-does-not-exist"\n', encoding="utf-8")

    assert _JUST is not None
    result = subprocess.run(
        [
            _JUST,
            "--justfile",
            str(_JUSTFILE),
            "--working-directory",
            str(tmp_path),
            "chart-version-check",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "FORCE_COLOR": "3"},
    )
    assert result.returncode == 1, (
        "a real mismatch must still fail under FORCE_COLOR=3 "
        f"(stdout={result.stdout!r} stderr={result.stderr!r})"
    )
    assert "0.0.0-does-not-exist" in result.stderr

# tests/scripts/test_chart_version_check_ansi.py
"""Behavioral test for the `chart-version-check` recipe's ANSI handling (#1883).

`uv version --short` emits ANSI colour escapes around the version string unconditionally on
some `uv` versions — not just under a TTY or `FORCE_COLOR`. The Chart.yaml-side read is plain
text, so the comparison `[[ "$chart" != "$pyproject" ]]` never matched even when the versions
were equal, false-failing `just chart-version-check` (and, transitively, `just ci`, which stops
at the first failing recipe).

This drives the real recipe with a stub `uv` that always emits a coloured version string —
forcing the exact condition that broke the comparison — and a throwaway `Chart.yaml` so the
test controls both sides of the compare without touching the real repo files.
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

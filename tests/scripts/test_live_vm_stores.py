"""Behavioral tests for scripts/live-vm/lib.sh via subprocess-source (ADR-0388)."""

from __future__ import annotations

import shutil
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "scripts" / "live-vm" / "lib.sh"
BASH = shutil.which("bash")


def _run(
    snippet: str, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Source lib.sh, then run ``snippet`` with positional args $1.. — capturing output."""
    assert BASH is not None, "bash is required"
    return subprocess.run(
        [BASH, "-c", f'source "{LIB}" && {snippet}', "_", *args],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _stub(bindir: Path, name: str, body: str) -> None:
    p = bindir / name
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_du_bytes_reports_size(tmp_path: Path) -> None:
    f = tmp_path / "blob"
    f.write_bytes(b"x" * 4096)
    r = _run('du_bytes "$1"', str(f))
    assert r.returncode == 0
    assert int(r.stdout.strip()) >= 4096


def test_enforce_budget_passes_at_and_under_ceiling(tmp_path: Path) -> None:
    d = tmp_path / "set"
    d.mkdir()
    (d / "f").write_bytes(b"y" * 1000)
    size = int(_run('du_bytes "$1"', str(d)).stdout.strip())
    ok = _run('enforce_budget "$1" "$2" "$3"', str(d), str(size), "staged set")
    assert ok.returncode == 0, ok.stderr


def test_enforce_budget_fails_loud_over_ceiling(tmp_path: Path) -> None:
    d = tmp_path / "set"
    d.mkdir()
    (d / "f").write_bytes(b"y" * 4096)
    size = int(_run('du_bytes "$1"', str(d)).stdout.strip())
    over = _run('enforce_budget "$1" "$2" "$3"', str(d), str(size - 1), "staged set")
    assert over.returncode != 0
    assert "staged set" in over.stderr
    assert str(size - 1) in over.stderr


def test_require_free_space_passes_and_fails(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(bindir, "df", 'echo "Avail"; echo "5000"')  # 5000 bytes available
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    ok = _run('require_free_space "$1" "$2" "$3"', str(tmp_path), "4000", "tcg", env=env)
    assert ok.returncode == 0, ok.stderr
    no = _run('require_free_space "$1" "$2" "$3"', str(tmp_path), "6000", "tcg", env=env)
    assert no.returncode != 0
    assert "tcg" in no.stderr

"""fetch-kernel-tree.sh prints the checkout path on stdout, human progress on stderr.

The live.yml tcg block captures the destination as `KDIVE_KERNEL_SRC="$(fetch-kernel-tree.sh)"`,
but the script wrote every line — including "kernel tree ready: <dest>" — to stderr and nothing to
stdout, so the variable was always empty. It stayed latent until the gate got far enough to reach
the preflight that asserts it. Mirrors the stdout contract the live-vm store scripts document:
stdout is the machine-readable value only.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch-kernel-tree.sh"
_BASH = shutil.which("bash") or "/usr/bin/bash"


def _run(*args: str, path: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": path if path is not None else "/usr/bin:/bin"},
    )


def test_prints_the_existing_checkout_path_on_stdout(tmp_path: Path) -> None:
    dest = tmp_path / "linux"
    (dest / ".git").mkdir(parents=True)
    r = _run(str(dest))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(dest.resolve())


def test_stdout_carries_only_the_path(tmp_path: Path) -> None:
    """A caller evaluates this in a command substitution; prose on stdout would corrupt it."""
    dest = tmp_path / "linux"
    (dest / ".git").mkdir(parents=True)
    r = _run(str(dest))
    assert r.stdout.strip().splitlines() == [str(dest.resolve())]
    assert "kernel tree" not in r.stdout  # the human line belongs on stderr
    assert "already present" in r.stderr


def test_prints_the_path_after_a_clone(tmp_path: Path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dest = tmp_path / "linux"
    # Stub git: create the checkout the way a real clone would, so the post-clone path is exercised.
    (bindir / "git").write_text(
        '#!/bin/sh\nfor a in "$@"; do last="$a"; done\nmkdir -p "$last/.git"\nexit 0\n'
    )
    (bindir / "git").chmod(0o755)
    r = _run(str(dest), path=f"{bindir}:/usr/bin:/bin")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(dest.resolve())
    assert "cloning" in r.stderr


def test_path_is_absolute_even_for_a_relative_dest(tmp_path: Path) -> None:
    """The caller exports this for children that may run from a different cwd."""
    dest = tmp_path / "linux"
    (dest / ".git").mkdir(parents=True)
    r = subprocess.run(
        [_BASH, str(_SCRIPT), "./linux"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(dest.resolve())

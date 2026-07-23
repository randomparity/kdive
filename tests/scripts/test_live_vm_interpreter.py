"""scripts/live-vm/lib.sh resolves its interpreter from KDIVE_PYTHON, else the workspace .venv.

The hosted ``live_vm_tcg`` job installs kdive with ``uv sync`` into the workspace ``.venv`` and
sets no ``KDIVE_PYTHON``, so a bare ``python3`` default resolved to the runner's *system*
interpreter — which has no kdive, making ``python -m kdive build-fs`` die with "No module named
kdive" and the staging script fail at its "produced no rootfs" guard. The self-hosted native job
overrides the interpreter to /opt/kdive's libguestfs venv, which must still win.

Mirrors tests/scripts/test_live_stack_interpreter.py, which pins the same convention for the
live-stack script family (scripts/live-stack/lib.sh).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_LIB = _ROOT / "scripts" / "live-vm" / "lib.sh"
_BASH = shutil.which("bash") or "bash"
_CONSUMERS = ("warm-store.sh", "stage-tcg-images.sh")


def _resolved_py(env_kdive_python: str | None) -> str:
    """Source lib.sh with KDIVE_PYTHON set (or absent) and return the interpreter it picks."""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env_kdive_python is not None:
        env["KDIVE_PYTHON"] = env_kdive_python
    out = subprocess.run(
        [_BASH, "-c", f'source "{_LIB}" && kdive_python'],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout


def test_interpreter_honors_kdive_python_when_set() -> None:
    assert _resolved_py("/opt/kdive/.venv/bin/python") == "/opt/kdive/.venv/bin/python"


def test_interpreter_defaults_to_the_workspace_venv() -> None:
    resolved = _resolved_py(None)
    assert resolved == f"{_ROOT}/.venv/bin/python"


def test_interpreter_never_defaults_to_a_bare_python3() -> None:
    """A bare `python3` is the system interpreter on a uv-managed runner: no kdive installed."""
    assert _resolved_py(None) != "python3"


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_builder_runs_build_fs_under_the_venv_not_path_python3(tmp_path: Path) -> None:
    """The end-to-end regression: with KDIVE_PYTHON unset the build must use the workspace venv.

    lib.sh is copied into a synthetic repo root so ``kdive_python`` resolves the venv beside the
    copy. PATH's ``python3`` is stubbed to fail the way the hosted runner's system interpreter did
    ("No module named kdive"), so falling back to it fails this test rather than silently passing.
    """
    lib = tmp_path / "scripts" / "live-vm" / "lib.sh"
    lib.parent.mkdir(parents=True)
    lib.write_text(_LIB.read_text())
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    dest = tmp_path / "set"
    dest.mkdir()

    # The venv interpreter is the only one that "builds"; PATH's python3 mimics the CI failure.
    _stub(
        venv_python,
        'dest=""; want=""; for a in "$@"; do case "$want" in dest) dest="$a";; esac; want=""; '
        '[ "$a" = "--dest" ] && want=dest; done; : > "$dest"',
    )
    _stub(bindir / "python3", 'echo "/usr/bin/python3: No module named kdive" >&2; exit 1')
    _stub(bindir / "virt-ls", 'printf "vmlinuz-6.1-test\\n"')
    _stub(
        bindir / "virt-copy-out",
        'src=""; for a in "$@"; do case "$a" in /boot/*) src="$a";; esac; destdir="$a"; done; '
        'printf "\\177ELF" > "${destdir}/$(basename "$src")"',
    )
    _stub(bindir / "eu-readelf", 'echo "    Build ID: beef01"')

    snippet = f'source "{lib}" && produce_rootfs_and_kernel "$1" "$2"'
    result = subprocess.run(
        [_BASH, "-c", snippet, "_", str(dest), "img"],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "No module named kdive" not in result.stderr
    assert (dest / "rootfs.qcow2").exists()
    assert result.stdout.strip() == "beef01"


def _require_module(interpreter: Path) -> subprocess.CompletedProcess[str]:
    snippet = f'source "{_LIB}" && require_kdive_module'
    return subprocess.run(
        [_BASH, "-c", snippet],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "KDIVE_PYTHON": str(interpreter)},
    )


def test_require_kdive_module_accepts_an_interpreter_that_imports_kdive(tmp_path: Path) -> None:
    interpreter = tmp_path / "python"
    _stub(interpreter, "exit 0")
    assert _require_module(interpreter).returncode == 0


def test_require_kdive_module_dies_when_the_interpreter_cannot_import_kdive(tmp_path: Path) -> None:
    """`command -v` proves the binary exists; only an import proves the venv carries kdive."""
    interpreter = tmp_path / "python"
    _stub(interpreter, 'echo "No module named kdive" >&2; exit 1')
    result = _require_module(interpreter)
    assert result.returncode != 0
    assert str(interpreter) in result.stderr  # names WHICH interpreter is wrong
    assert "uv sync" in result.stderr  # and how to fix it


def test_consumers_preflight_the_same_interpreter_they_build_with() -> None:
    """require_tools must probe the resolved interpreter, not a separately-defaulted `python3`.

    Duplicating the fallback expression in the consumer scripts lets the preflight pass on a host
    where the interpreter that actually runs build-fs is missing.
    """
    for name in _CONSUMERS:
        body = (_ROOT / "scripts" / "live-vm" / name).read_text()
        assert "$(kdive_python)" in body, f"{name} does not preflight the resolved interpreter"
        assert "KDIVE_PYTHON:-python3" not in body, f"{name} still defaults to a bare python3"
        assert "require_kdive_module" in body, f"{name} does not probe kdive importability"

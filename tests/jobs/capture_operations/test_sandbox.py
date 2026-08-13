"""Native seccomp matrix for the capture-operation child boundary (ADR-0558)."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[3]
_SOURCE = _ROOT / "src"


def _sandbox_probe(source: str) -> subprocess.CompletedProcess[str]:
    script = (
        "from kdive.jobs.capture_operations.sandbox import install_capture_filter\n"
        "install_capture_filter()\n" + source
    )
    return subprocess.run(
        [sys.executable, "-S", "-c", script],
        cwd=_ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(_SOURCE), "LANG": "C.UTF-8"},
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_filter_denies_fork_and_allows_real_threads() -> None:
    result = _sandbox_probe(
        "import errno, os, threading\n"
        "seen = []\n"
        "t = threading.Thread(target=lambda: seen.append('thread'))\n"
        "t.start(); t.join()\n"
        "try:\n"
        "    os.fork()\n"
        "except OSError as exc:\n"
        "    print(seen[0], exc.errno)\n"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "thread 1"


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_filter_returns_enosys_for_clone3_and_denies_later_exec() -> None:
    result = _sandbox_probe(
        "import ctypes, errno, os, platform\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "numbers = {'x86_64': 435, 'ppc64le': 435}\n"
        "rc = libc.syscall(numbers[platform.machine()], 0, 0)\n"
        "clone3_errno = ctypes.get_errno()\n"
        "try:\n"
        "    os.execve('/bin/true', ['true'], {})\n"
        "except OSError as exc:\n"
        "    print(rc, clone3_errno, exc.errno)\n"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-1 38 1"


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_filter_denies_vfork_execveat_and_clone_missing_thread_bits() -> None:
    result = _sandbox_probe(
        "import ctypes, os, platform\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "numbers = {\n"
        " 'x86_64': {'clone': 56, 'execveat': 322},\n"
        " 'ppc64le': {'clone': 120, 'execveat': 362},\n"
        "}[platform.machine()]\n"
        "clone_rc = libc.syscall(numbers['clone'], 0, 0, 0, 0, 0)\n"
        "clone_errno = ctypes.get_errno()\n"
        "exec_rc = libc.syscall(numbers['execveat'], -1, 0, 0, 0, 0)\n"
        "exec_errno = ctypes.get_errno()\n"
        "vfork_rc = libc.vfork()\n"
        "vfork_errno = ctypes.get_errno()\n"
        "print(clone_rc, clone_errno, exec_rc, exec_errno, vfork_rc, vfork_errno)\n"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-1 1 -1 1 -1 1"


@pytest.mark.skipif(platform.system() != "Linux", reason="seccomp is Linux-only")
def test_filter_refuses_unsupported_architecture(monkeypatch: pytest.MonkeyPatch) -> None:
    from kdive.jobs.capture_operations import sandbox

    monkeypatch.setattr(sandbox.platform, "machine", lambda: "s390x")
    with pytest.raises(RuntimeError, match="unsupported audit architecture"):
        sandbox.install_capture_filter()

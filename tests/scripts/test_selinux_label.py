"""Tests for the ``kdive_label_svirt_image`` helper in examples/local-libvirt/selinux-label.sh.

The helper is sourced, never executed, so the harness sources it into a bash subprocess and
calls the function directly: ``bash -c 'source <helper>; kdive_label_svirt_image <dir>'`` with
PATH pinned to a tmp_path stub directory. Every stub (getenforce, semanage, restorecon) appends
its argv to one shared log file, which is the whole assertion surface; the sudo stub is
transparent (``exec "$@"``) so the log records exactly what the helper passed to semanage and
restorecon. The semanage stub's exit status for the ``-m`` arm is the test parameter: 1 stands
for a fresh host where only ``-a`` can succeed, 0 for a host that already carries the rule. The
``-a`` arm's exit status is a separate parameter, non-zero only to simulate a broken policy store
where neither arm succeeds.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tests.host_capabilities import requires_bash

HELPER = Path(__file__).resolve().parents[2] / "examples" / "local-libvirt" / "selinux-label.sh"
BASH = shutil.which("bash")

# The helper uses `[[ ]]` and `local`.
pytestmark = requires_bash(4, 3, "[[ ]] and local in the labeling helper")

_LOG = "calls.log"


def _stub(bindir: Path, name: str, body: str) -> None:
    path = bindir / name
    path.write_text(body)
    path.chmod(0o755)


def _bindir(
    tmp_path: Path,
    *,
    enforcing: bool = True,
    have_semanage: bool = True,
    semanage_m_status: int = 1,
    semanage_a_status: int = 0,
) -> Path:
    """A PATH with stubs for getenforce/semanage/restorecon; sudo is a transparent passthrough."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    log = tmp_path / _LOG
    _stub(b, "sudo", '#!/bin/sh\nexec "$@"\n')
    _stub(
        b,
        "getenforce",
        f"#!/bin/sh\necho {'Enforcing' if enforcing else 'Permissive'}\n",
    )
    if have_semanage:
        _stub(
            b,
            "semanage",
            f'#!/bin/sh\nprintf \'%s\\n\' "semanage $*" >> "{log}"\n'
            f'if [ "$1" = "fcontext" ] && [ "$2" = "-m" ]; then\n'
            f'  [ {semanage_m_status} -ne 0 ] && echo "semanage: mock -m failure" >&2\n'
            f"  exit {semanage_m_status}\n"
            "fi\n"
            f'if [ "$1" = "fcontext" ] && [ "$2" = "-a" ]; then\n'
            f'  [ {semanage_a_status} -ne 0 ] && echo "semanage: mock -a failure" >&2\n'
            f"  exit {semanage_a_status}\n"
            "fi\n"
            "exit 0\n",
        )
    _stub(b, "restorecon", f'#!/bin/sh\nprintf \'%s\\n\' "restorecon $*" >> "{log}"\n')
    return b


def _run(directory: str, bindir: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert BASH is not None, "bash is required to run the helper"
    return subprocess.run(
        [BASH, "-c", f'source "{HELPER}"; kdive_label_svirt_image "{directory}"'],
        env={"PATH": str(bindir)},
        capture_output=True,
        text=True,
        check=check,
    )


def _log_lines(tmp_path: Path) -> list[str]:
    log = tmp_path / _LOG
    return log.read_text().splitlines() if log.exists() else []


def test_adds_rule_when_absent(tmp_path: Path) -> None:
    """No existing rule: -m fails, -a adds it, then restorecon runs."""
    bindir = _bindir(tmp_path, semanage_m_status=1)

    _run("/var/lib/kdive/rootfs", bindir)

    lines = _log_lines(tmp_path)
    assert any(line.startswith("semanage fcontext -m") for line in lines)
    assert any(
        line == "semanage fcontext -a -t svirt_image_t /var/lib/kdive/rootfs(/.*)?"
        for line in lines
    )
    assert any(line == "restorecon -R /var/lib/kdive/rootfs" for line in lines)


def test_migrates_stale_rule(tmp_path: Path) -> None:
    """An existing rule (even a stale virt_image_t one): -m succeeds, -a never runs."""
    bindir = _bindir(tmp_path, semanage_m_status=0)

    _run("/var/lib/kdive/rootfs", bindir)

    lines = _log_lines(tmp_path)
    assert any(
        line == "semanage fcontext -m -t svirt_image_t /var/lib/kdive/rootfs(/.*)?"
        for line in lines
    )
    assert not any(line.startswith("semanage fcontext -a") for line in lines)
    assert any(line == "restorecon -R /var/lib/kdive/rootfs" for line in lines)


def test_noop_when_not_enforcing(tmp_path: Path) -> None:
    """Permissive/disabled hosts: no semanage or restorecon call is recorded."""
    bindir = _bindir(tmp_path, enforcing=False)

    _run("/var/lib/kdive/rootfs", bindir)

    assert _log_lines(tmp_path) == []


def test_reports_missing_semanage(tmp_path: Path) -> None:
    """semanage absent on an enforcing host: nothing is written, and the helper still returns 0."""
    bindir = _bindir(tmp_path, have_semanage=False)

    result = _run("/var/lib/kdive/rootfs", bindir)

    assert _log_lines(tmp_path) == []
    assert result.returncode == 0
    assert "policycoreutils-python-utils" in result.stderr
    assert "/var/lib/kdive/rootfs" in result.stderr


def test_aborts_when_migrate_and_add_both_fail(tmp_path: Path) -> None:
    """A broken policy store (both -m and -a fail): the helper aborts non-zero and names why."""
    bindir = _bindir(tmp_path, semanage_m_status=1, semanage_a_status=1)

    result = _run("/var/lib/kdive/rootfs", bindir, check=False)

    assert result.returncode != 0
    assert not any(line.startswith("restorecon") for line in _log_lines(tmp_path))
    assert "mock -m failure" in result.stderr
    assert "mock -a failure" in result.stderr

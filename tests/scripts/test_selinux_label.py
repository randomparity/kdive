"""Tests for the ``kdive_label_svirt_image`` helper in examples/local-libvirt/selinux-label.sh.

The helper is sourced, never executed, so the harness sources it into a bash subprocess and
calls the function directly: ``bash -c 'source <helper>; kdive_label_svirt_image <dir>'`` with
PATH pinned to a tmp_path stub directory. Every stub (getenforce, semanage, restorecon) appends
its argv to one shared log file, which is the whole assertion surface; the sudo stub is
transparent (``exec "$@"``) so the log records exactly what the helper passed to semanage and
restorecon.

The helper issues a single ``semanage fcontext -a``, because ``seobject.FcontextRecords.add()``
rewrites an existing rule rather than failing on it (verified against the installed
implementation on both target families). These stubs therefore do not model an "already defined"
failure, since the real tool has none; ``semanage_status`` and ``restorecon_status`` exist only
to drive the two abort paths.
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
    semanage_status: int = 0,
    restorecon_status: int = 0,
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
            f'[ {semanage_status} -ne 0 ] && echo "semanage: mock failure" >&2\n'
            f"exit {semanage_status}\n",
        )
    _stub(
        b,
        "restorecon",
        f'#!/bin/sh\nprintf \'%s\\n\' "restorecon $*" >> "{log}"\n'
        f'[ {restorecon_status} -ne 0 ] && echo "restorecon: mock failure" >&2\n'
        f"exit {restorecon_status}\n",
    )
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


def test_labels_the_directory(tmp_path: Path) -> None:
    """One -a call carrying the new type and the recursive pattern, then restorecon."""
    bindir = _bindir(tmp_path)

    _run("/var/lib/kdive/rootfs", bindir)

    assert _log_lines(tmp_path) == [
        "semanage fcontext -a -t svirt_image_t /var/lib/kdive/rootfs(/.*)?",
        "restorecon -R /var/lib/kdive/rootfs",
    ]


def test_issues_exactly_one_semanage_call(tmp_path: Path) -> None:
    """The migrate-then-add split is gone: -a alone converges every host shape.

    ``seobject.FcontextRecords.add()`` prints "already defined, modifying instead" and delegates
    to the modify path when the pattern is already in the base or local store, so a stale
    pre-ADR-0639 ``virt_image_t`` rule is rewritten by this same call. A second ``semanage``
    invocation — in particular a ``-m`` probe — would be dead weight on a false premise.
    """
    bindir = _bindir(tmp_path)

    _run("/var/lib/kdive/rootfs", bindir)

    semanage_calls = [line for line in _log_lines(tmp_path) if line.startswith("semanage ")]
    assert len(semanage_calls) == 1
    assert not any(line.startswith("semanage fcontext -m") for line in semanage_calls)


def test_strips_a_trailing_slash_from_the_pattern(tmp_path: Path) -> None:
    """A trailing slash would produce `/var/lib/kdive/rootfs/(/.*)?`, which matches nothing."""
    bindir = _bindir(tmp_path)

    _run("/var/lib/kdive/rootfs/", bindir)

    assert _log_lines(tmp_path) == [
        "semanage fcontext -a -t svirt_image_t /var/lib/kdive/rootfs(/.*)?",
        "restorecon -R /var/lib/kdive/rootfs",
    ]


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


def test_aborts_when_semanage_fails(tmp_path: Path) -> None:
    """A policy store that refuses the write: abort non-zero, and do not restorecon over it."""
    bindir = _bindir(tmp_path, semanage_status=1)

    result = _run("/var/lib/kdive/rootfs", bindir, check=False)

    assert result.returncode != 0
    assert not any(line.startswith("restorecon") for line in _log_lines(tmp_path))
    assert "mock failure" in result.stderr


def test_aborts_when_restorecon_fails(tmp_path: Path) -> None:
    """The rule landed but relabeling did not: the caller must not read that as a success."""
    bindir = _bindir(tmp_path, restorecon_status=1)

    result = _run("/var/lib/kdive/rootfs", bindir, check=False)

    assert result.returncode != 0
    assert any(line.startswith("restorecon ") for line in _log_lines(tmp_path))

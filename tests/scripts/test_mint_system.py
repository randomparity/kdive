"""mint-system.sh validates its preconditions before any stack call (#1293, ADR-0389).

The live mint (allocate -> provision -> ready) needs a running stack and is proven by the operator
nightly / the local native smoke (plan Task 7), not CI. This test pins the fail-loud preconditions:
an absent warm rootfs or stack URL dies before any HTTP call, so a misconfigured job fails at the
boundary, not deep in provisioning.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live-vm" / "mint-system.sh"


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
    )


def test_dies_without_rootfs() -> None:
    r = _run({"KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000"})
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr


def test_dies_without_stack_url(tmp_path: Path) -> None:
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    r = _run({"KDIVE_LIVE_VM_ROOTFS": str(rootfs)})
    assert r.returncode != 0
    assert "KDIVE_STACK_BASE_URL" in r.stderr


def test_dies_when_rootfs_path_missing(tmp_path: Path) -> None:
    r = _run(
        {
            "KDIVE_LIVE_VM_ROOTFS": str(tmp_path / "nope.qcow2"),
            "KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000",
        }
    )
    assert r.returncode != 0
    assert "KDIVE_LIVE_VM_ROOTFS" in r.stderr

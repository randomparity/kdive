"""mint-system.sh validates its preconditions before any stack call (#1293, ADR-0389).

The live mint (allocate -> provision -> ready) needs a running stack and is proven by the operator
nightly / the local native smoke (plan Task 7), not CI. This test pins the fail-loud preconditions:
an absent warm rootfs or stack URL dies before any HTTP call, so a misconfigured job fails at the
boundary, not deep in provisioning.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "live-vm" / "mint-system.sh"


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


def test_onboard_failure_aborts_at_the_preflight(tmp_path: Path) -> None:
    """A non-zero onboard.sh stops the mint naming the preflight, not the token (#2568, ADR-0666).

    `eval "$(onboard.sh | grep ...)"` discards onboard.sh's exit status — a command substitution
    in an argument list does not fire errexit — so a preflight stop used to surface as the
    `did not mint a token` die, a cause that never happened. Drive the real script against a
    stub onboard.sh in a copied tree, with KDIVE_TOKEN already exported so the token check
    cannot mask a missing status check.
    """
    live_vm = tmp_path / "scripts" / "live-vm"
    live_vm.mkdir(parents=True)
    (tmp_path / "scripts" / "live-stack").mkdir()
    for name in ("mint-system.sh", "lib.sh"):
        (live_vm / name).write_text((_ROOT / "scripts" / "live-vm" / name).read_text())
    onboard = tmp_path / "scripts" / "live-stack" / "onboard.sh"
    # The stub prints no word this test asserts on: the attribution must come from mint-system.sh.
    onboard.write_text(
        "#!/bin/sh\necho 'FAIL  a host kernel under /boot is unreadable' >&2\nexit 1\n"
    )
    onboard.chmod(onboard.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    # Staging would SUCCEED if the gate were skipped, so reaching it is observable rather than
    # masked by a later failure — without this the test passes on the staging die instead.
    provider_root = tmp_path / "provider-root"
    provider_root.mkdir()

    r = subprocess.run(
        ["bash", str(live_vm / "mint-system.sh")],
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ["PATH"],
            "KDIVE_LIVE_VM_ROOTFS": str(rootfs),
            "KDIVE_STACK_BASE_URL": "http://127.0.0.1:8000",
            "KDIVE_ROOTFS_DIR": str(provider_root),
            "KDIVE_TOKEN": "stale-token-from-the-environment",
        },
    )

    assert r.returncode != 0
    assert "onboard.sh failed its local-libvirt preflight" in r.stderr
    assert "did not mint a token" not in r.stderr
    # Staging is the first step past the gate; a discarded exit status would have reached it.
    assert not (provider_root / "live-vm-provisioned-rootfs.qcow2").exists()

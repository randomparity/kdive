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


def _die_line(stderr: str) -> str | None:
    """The last `live-vm store: ...` line — lib.sh's die(), i.e. what mint-system.sh itself said."""
    lines = [ln for ln in stderr.splitlines() if ln.startswith("live-vm store:")]
    return lines[-1] if lines else None


def _mint_tree(tmp_path: Path) -> tuple[Path, Path]:
    """A copied scripts/ tree whose onboard.sh the caller stubs, plus a writable provider root."""
    live_vm = tmp_path / "scripts" / "live-vm"
    live_vm.mkdir(parents=True)
    (tmp_path / "scripts" / "live-stack").mkdir()
    for name in ("mint-system.sh", "lib.sh"):
        (live_vm / name).write_text((_SCRIPT.parent / name).read_text())
    # Staging would SUCCEED if the gate were skipped, so reaching it is observable rather than
    # masked by a later failure — without this a test passes on the staging die instead.
    provider_root = tmp_path / "provider-root"
    provider_root.mkdir()
    return live_vm, provider_root


def _run_mint(
    live_vm: Path, tmp_path: Path, provider_root: Path
) -> subprocess.CompletedProcess[str]:
    """Run the copied mint-system.sh with KDIVE_TOKEN preset, so the token check cannot mask
    a missing exit-status check."""
    rootfs = tmp_path / "rootfs.qcow2"
    rootfs.write_bytes(b"x")
    return subprocess.run(
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


def test_onboard_failure_aborts_at_the_preflight(tmp_path: Path) -> None:
    """A non-zero onboard.sh stops the mint, not the token check (#2568, ADR-0666).

    `eval "$(onboard.sh | grep ...)"` discards onboard.sh's exit status — a command substitution
    in an argument list does not fire errexit — so a preflight stop used to surface as the
    `did not mint a token` die, a cause that never happened.
    """
    live_vm, provider_root = _mint_tree(tmp_path)
    onboard = tmp_path / "scripts" / "live-stack" / "onboard.sh"
    # The stub only FAILs when the caller declared `required`, so this covers BOTH halves of
    # ADR-0666's decision: drop the declaration from mint-system.sh and the stub mints a token,
    # staging is reached, and the assertions below go red.
    onboard.write_text(
        "#!/bin/sh\n"
        '[ "${ONBOARD_PREFLIGHT:-advisory}" = required ] || '
        "{ echo 'export KDIVE_TOKEN=advisory-token'; exit 0; }\n"
        # A token already on stdout when the failure happens: this step's stderr is a
        # world-readable CI log, so the re-emit must not carry it there.
        "echo 'export KDIVE_TOKEN=leaked-token'\n"
        "echo 'FAIL  a host kernel under /boot is unreadable' >&2\n"
        "exit 1\n"
    )
    onboard.chmod(onboard.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    r = _run_mint(live_vm, tmp_path, provider_root)

    assert r.returncode != 0
    assert "did not mint a token" not in r.stderr
    assert _die_line(r.stderr) is not None
    # onboard.sh's own diagnosis survives as the reason the reader sees.
    assert "FAIL  a host kernel under /boot is unreadable" in r.stderr
    assert "leaked-token" not in r.stderr, "the re-emit carried the minted token into the log"
    # Staging is the first step past the gate; a discarded exit status would have reached it.
    assert not (provider_root / "live-vm-provisioned-rootfs.qcow2").exists()


def test_onboard_database_failure_is_not_blamed_on_the_preflight(tmp_path: Path) -> None:
    """The stop names no cause: `||` fires on migrate/verify failures too (#2568 review).

    onboard.sh's own hard gates need the database and no libvirt, so a die asserting "the
    preflight" would reproduce the wrong-diagnosis defect this branch exists to remove.
    """
    live_vm, provider_root = _mint_tree(tmp_path)
    onboard = tmp_path / "scripts" / "live-stack" / "onboard.sh"
    # verify-project prints its diagnosis to STDOUT (kdive/__main__.py `_handle_verify_project`),
    # which mint-system.sh captures — so the stub uses stdout too, and the assertion below proves
    # the capture is re-emitted rather than swallowed.
    onboard.write_text(
        "#!/bin/sh\n"
        "echo '=== local-libvirt preflight ===' >&2\n"
        "echo '=== local-libvirt host is ready ===' >&2\n"
        "echo \"project 'demo' is NOT funded: no budget row; no quota row\"\n"
        "exit 1\n"
    )
    onboard.chmod(onboard.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    r = _run_mint(live_vm, tmp_path, provider_root)

    assert r.returncode != 0
    die = _die_line(r.stderr)
    assert die is not None
    assert "preflight" not in die, f"the stop asserted a cause it cannot know: {die}"
    # The die claims "its output above states the reason" — so the captured stdout must be there.
    assert "is NOT funded" in r.stderr
    assert not (provider_root / "live-vm-provisioned-rootfs.qcow2").exists()

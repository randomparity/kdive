"""Live proof of the ADR-0576 worker-owned console inode against a real libvirt daemon.

``live_vm``-gated (throwaway family, #1290): the operator points ``KDIVE_LIVE_VM_ROOTFS`` at a
bootable qcow2. The proof boots a throwaway domain whose serial ``<log>`` points at a file the
worker prepared through ``storage._prepare_console_log`` with ``console_append=True`` — the
production shape (ADR-0576) — and asserts the daemon did **not** replace the inode: after the
boot the file is still the worker's inode, worker-owned, mode ``0664``, and holds the boot's
bytes. The group write bit is the operator-owned session daemon's append authority through
the shared group. A second boot over the same log seeds a stale marker first and asserts it is
gone, proving the per-start truncate yields a byte-exact current-boot window on a real host.
Skips cleanly without the env or libvirt.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import uuid4

import pytest

from kdive.providers.local_libvirt.lifecycle import storage as storage_module
from kdive.testing.live_vm import boot_throwaway_domain
from tests.live_vm import require_live_vm_throwaway

_STALE_MARKER = b"STALE-PRIOR-BOOT-MARKER\n"


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_console_inode_survives_boots_and_truncates_per_start(
    tmp_path: Any,
) -> None:  # pragma: no cover - live_vm
    contract = require_live_vm_throwaway("qemu:///system")
    console = tmp_path / "console" / "throwaway.log"
    storage_module._prepare_console_log(console)
    inode_before = console.stat().st_ino
    name = f"kdive-console-live-{uuid4().hex[:12]}"

    with boot_throwaway_domain(
        contract.rootfs,
        arch="x86_64",
        name=name,
        mode=contract.libvirt_uri,
        console_log=console,
        console_append=True,
        wait_for="active",
        settle_s=1.0,
    ):
        st = console.stat()
        # The daemon appended to the worker-created inode instead of recreating it root:0600
        # — the #1940 failure this ADR closes.
        assert st.st_ino == inode_before
        assert st.st_uid == os.geteuid()
        assert st.st_mode & 0o777 == 0o664
        assert st.st_size > 0  # this boot's bytes are readable by the worker

    # A second start over the same log: seed prior-boot bytes, prepare (truncate), boot, and
    # confirm the new window holds none of them.
    with open(console, "ab") as handle:
        handle.write(_STALE_MARKER)
    storage_module._prepare_console_log(console)
    assert _STALE_MARKER not in console.read_bytes()  # truncate happened before the start
    inode_before = console.stat().st_ino

    with boot_throwaway_domain(
        contract.rootfs,
        arch="x86_64",
        name=name,
        mode=contract.libvirt_uri,
        console_log=console,
        console_append=True,
        wait_for="active",
        settle_s=1.0,
    ):
        st = console.stat()
        assert st.st_ino == inode_before
        assert _STALE_MARKER not in console.read_bytes()
        assert st.st_size > 0

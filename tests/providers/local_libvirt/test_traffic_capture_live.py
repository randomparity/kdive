"""Live proof of ``LocalLibvirtTrafficCapture`` against a real KVM domain (#1258, ADR-0385).

``live_vm``-gated (throwaway family, #1290): the operator points ``KDIVE_LIVE_VM_ROOTFS`` at a
bootable qcow2 (any kdive-ready rootfs works — the guest need not finish booting, only the QEMU
process must run so its SLIRP netdev exists). The shared ``boot_throwaway_domain`` harness
(``kdive.testing.live_vm``) owns the overlay, the connect + short-``XDG`` session handling, and the
guaranteed teardown; it renders the SSH-forward netdev when given ``ssh_hostfwd_port`` (the same
``kdivessh`` netdev real provisioning uses). Skips cleanly without the env or libvirt.

Exercises the real QEMU ``filter-dump`` QMP path the worker handler drives: attach a filter-dump on
the SSH-forward netdev, generate packets by connecting to the host-forwarded port (SLIRP injects the
forwarded connection onto that netdev), detach, and assert the on-disk pcap is a valid libpcap file
with at least one captured record. The MCP admission, egress, and store transitions are unit-tested
separately; this proves the provider mechanic against a real hypervisor.

Requires ``qemu:///session`` (``session_required=True``) so the QEMU process runs as the invoking
user and the pcap is readable without the ADR-0223 root-readback wall (that wall is unit-tested via
``read_pcap_bytes``); the gate fails loud if ``KDIVE_LIBVIRT_URI`` would move it to system mode.
"""

from __future__ import annotations

import contextlib
import socket
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from kdive.testing.live_vm import boot_throwaway_domain
from tests.live_vm import require_live_vm_throwaway


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.live_vm
@pytest.mark.live_vm_throwaway
def test_live_vm_traffic_capture_filter_dump() -> None:  # pragma: no cover - live_vm
    contract = require_live_vm_throwaway("qemu:///session", session_required=True)
    try:
        import libvirt  # noqa: PLC0415  # operator-provided
        import libvirt_qemu  # noqa: PLC0415  # operator-provided (QEMU-specific binding)
    except ImportError:
        pytest.skip("libvirt-python / libvirt_qemu unavailable")

    from kdive.artifacts.pcap_count import count_pcap_packets  # noqa: PLC0415
    from kdive.providers.local_libvirt.lifecycle.traffic_capture import (  # noqa: PLC0415
        LocalLibvirtTrafficCapture,
    )

    name = f"kdive-cap-live-{uuid.uuid4().hex[:12]}"
    port = _free_port()
    qom_id = f"kdive-dump-{name}"
    pcap_dir = Path(tempfile.mkdtemp(prefix="kdive-pcap-live-"))
    pcap_file = pcap_dir / f"{name}.pcap"
    capturer = LocalLibvirtTrafficCapture(
        connect=lambda: libvirt.open(contract.libvirt_uri), monitor=libvirt_qemu.qemuMonitorCommand
    )
    try:
        with boot_throwaway_domain(
            contract.rootfs,
            arch="x86_64",
            name=name,
            mode=contract.libvirt_uri,
            ssh_hostfwd_port=port,
            wait_for="active",
            settle_s=2.0,
        ):
            capturer.attach(name, qom_id=qom_id, dest_path=str(pcap_file), snaplen=128)
            # Generate traffic on the SSH-forward netdev: each connect makes SLIRP inject packets
            # toward the guest, which the filter-dump captures regardless of a guest-side listener.
            for _ in range(8):
                with (
                    contextlib.suppress(OSError),
                    socket.create_connection(("127.0.0.1", port), timeout=1) as sock,
                ):
                    sock.sendall(b"kdive-capture-probe\n")
                time.sleep(0.2)
            capturer.detach(name, qom_id=qom_id)

            data = pcap_file.read_bytes()
            assert count_pcap_packets(data) > 0, "filter-dump captured no packets"
    finally:
        pcap_file.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            pcap_dir.rmdir()

"""Traffic-capture provider port (ADR-0385/0432): host-side pcap of a running guest's netdev.

The worker handler owns the bounded poll loop and cancellation, so the provider stays thin: it
attaches/detaches a capture sink keyed on the provider domain name (DB-free, like the Controller
port), and owns the *file side* of the capture — where the pcap lives, its growing size, reading it
back, and reclaiming it. That file side is provider-dispatched (ADR-0432) rather than assumed
worker-local: local-libvirt writes a worker-readable file, remote-libvirt writes on the remote host
and streams it back over ``qemu+tls``. The handler names only the sink (``qom_id``), the snaplen,
and an opaque ``dest_path`` token the provider returns from :meth:`prepare`.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class TrafficCapturer(Protocol):
    """Attach/detach a host-side packet-capture sink and own the pcap file lifecycle.

    Which netdev is captured is a provider-internal detail (local-libvirt's SSH-forward netdev;
    remote-libvirt's runtime-discovered data-plane netdev), so it is not a port parameter. The
    ``dest_path`` returned by :meth:`prepare` is an opaque provider token — a worker path for
    local-libvirt, a remote storage-pool path for remote-libvirt — that the handler threads through
    :meth:`attach`/:meth:`detach`/:meth:`captured_size`/:meth:`fetch`/:meth:`reclaim` without
    interpreting it.
    """

    @property
    def write_remediation(self) -> str:
        """Operator guidance when a short/absent pcap means the hypervisor could not write it.

        The handler attaches this to the ``pcap_not_written`` configuration error; each provider
        returns the remedy for *its* write path (local: the qemu:///system pcap dir; remote: the
        storage pool).
        """
        ...

    def prepare(self, system_id: UUID, job_id: UUID) -> str:
        """Prepare the capture destination and return the opaque ``dest_path`` token.

        Local-libvirt prepares the per-System pcap directory (QEMU-writable, SELinux-labelled) and
        returns the worker path. Remote-libvirt sweeps any stale pcap volumes for the System (so a
        worker-death orphan is reclaimed here) and returns the remote storage-pool path. Called once
        before :meth:`attach`.
        """
        ...

    def attach(self, domain_name: str, *, qom_id: str, dest_path: str, snaplen: int) -> None:
        """Start capturing into libpcap file ``dest_path`` (``snaplen`` bytes/pkt).

        Idempotent: any pre-existing sink under ``qom_id`` is removed first, tolerating not-found
        (the first-ever capture has no stale sink). Raises ``CategorizedError`` with a
        ``CONTROL_FAILURE`` category on any monitor failure other than not-found.
        """
        ...

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the capture sink ``qom_id`` (tolerating not-found)."""
        ...

    def captured_size(self, dest_path: str) -> int:
        """Current byte size of the growing pcap (0 if not yet written), for the poll loop."""
        ...

    def fetch(self, dest_path: str, *, max_bytes: int) -> bytes:
        """Read the captured pcap back to worker memory; an absent capture is empty ``bytes``.

        Bounded by ``max_bytes`` (a mid-stream overrun on the remote download raises). Raises
        ``CategorizedError`` on a genuine read fault (e.g. the ADR-0223 root-readback wall).
        """
        ...

    def reclaim(self, dest_path: str) -> None:
        """Delete the host-side pcap; best-effort, never masks the handler's real result."""
        ...

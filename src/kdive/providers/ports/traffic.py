"""Traffic-capture provider port (ADR-0384): host-side pcap of a running guest's netdev.

Thin primitives — the worker handler owns the bounded poll loop and cancellation, so a provider
only attaches/detaches a capture sink keyed on the provider domain name (DB-free, like the
Controller port). This keeps the size-poll and the async job-cancel read in the handler, off the
synchronous libvirt thread.
"""

from __future__ import annotations

from typing import Protocol


class TrafficCapturer(Protocol):
    """Attach/detach a host-side packet-capture sink on a running guest's netdev."""

    def attach(
        self, domain_name: str, *, qom_id: str, netdev_id: str, dest_path: str, snaplen: int
    ) -> None:
        """Start capturing ``netdev_id`` into libpcap file ``dest_path`` (``snaplen`` bytes/pkt).

        Idempotent: any pre-existing sink under ``qom_id`` is removed first, tolerating not-found
        (the first-ever capture has no stale sink). Raises ``CategorizedError`` with a
        ``CONTROL_FAILURE`` category on any monitor failure other than not-found.
        """
        ...

    def detach(self, domain_name: str, *, qom_id: str) -> None:
        """Remove the capture sink ``qom_id`` (tolerating not-found)."""
        ...

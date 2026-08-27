"""Local-libvirt live-domain host-dump capture (ADR-0211)."""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import UUID

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.retrieve.guestfs import _libvirt_uri, _remove_spool
from kdive.providers.shared.debug_common.core_file import MAX_CORE_BYTES
from kdive.providers.shared.runtime_paths import domain_name_for


def _real_host_dump_capture(system_id: UUID) -> Path | None:  # pragma: no cover - live_vm (libvirt)
    """Dump the System's live domain memory to a worker temp file via the libvirt core dump.

    Returns the spooled core path (caller-owned, streamed + cleaned up by ``capture``), or ``None``
    when there is no live domain to dump — a missing or inactive domain. Unlike the kdump path this
    never force-offs the domain: ``coreDumpWithFormat`` dumps the *active* domain in place
    (ADR-0211), so an inactive domain is a readiness failure, not something to quiesce.

    Worker-readability precondition (B6 live drive): under the default ``qemu:///system`` URI the
    dump file is written by the QEMU/root process, not the worker, so the worker must be able to
    read (and remove) what QEMU writes under the system temp dir — provision group access to the
    qemu-written path, or run the worker against ``qemu:///session`` (worker-owned QEMU). This
    mirrors the kdump console-log ownership constraint; the libguestfs kdump harvest sidesteps it
    by reading the overlay itself.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the produced core exceeds ``MAX_CORE_BYTES``;
            ``INFRASTRUCTURE_FAILURE`` when the libvirt core dump fails on an active domain.
    """
    conn = libvirt.open(_libvirt_uri())
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return None  # no domain — nothing to dump (READINESS_FAILURE upstream)
        if not domain.isActive():
            return None  # crashed-then-shutoff: no live memory to dump (READINESS_FAILURE)
        spool_dir = Path(tempfile.mkdtemp(prefix="kdive-host-dump-"))
        dest = spool_dir / "vmcore"
        try:
            domain.coreDumpWithFormat(
                str(dest),
                libvirt.VIR_DOMAIN_CORE_DUMP_FORMAT_RAW,
                libvirt.VIR_DUMP_MEMORY_ONLY,
            )
        except libvirt.libvirtError as exc:
            _remove_spool(dest)
            raise CategorizedError(
                "local host_dump core-dump failed",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            ) from exc
        size_bytes = dest.stat().st_size
        if size_bytes > MAX_CORE_BYTES:
            _remove_spool(dest)
            raise CategorizedError(
                "host_dump core exceeds the single-object ceiling",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"size_bytes": size_bytes, "max_bytes": MAX_CORE_BYTES},
            )
        return dest
    finally:
        conn.close()

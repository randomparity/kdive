"""Offline guestfs kdump harvesting and domain-settlement coordination (ADR-0217)."""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import libvirt

import kdive.config as config
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.local_libvirt.retrieve.kdump import HarvestOutcome, VmcoreEntry, harvest_vmcore
from kdive.providers.local_libvirt.settings import LIBVIRT_URI
from kdive.providers.shared.debug_common.core_file import MAX_CORE_BYTES
from kdive.providers.shared.runtime_paths import domain_name_for

_log = logging.getLogger(__name__)

_VAR_CRASH_GLOB = "/var/crash/*/vmcore"
# kdump writes ``vmcore-incomplete`` while saving and renames it to ``vmcore`` on success, so a
# surviving ``-incomplete`` file means the save never finished. The glob is literal-suffixed, so
# ``*/vmcore`` never matches ``vmcore-incomplete``; the two are listed separately (ADR-0251).
_VAR_CRASH_INCOMPLETE_GLOB = "/var/crash/*/vmcore-incomplete"

# After the force_crash NMI panics the guest, the in-guest kdump kexecs a crash kernel, boots
# it, mounts root, and writes /var/crash/<ts>/vmcore before its kdumpctl ``final_action`` runs.
# That sequence is tens of seconds, so the harvest must wait for kdump to COMPLETE — signalled
# by the domain reaching SHUTOFF (kdump ``final_action poweroff``, staged in the kdive-ready
# rootfs) — before forcing the domain off and reading the overlay. The window bounds that wait;
# on timeout we force-off and harvest anyway (the core, if written, persists on the overlay even
# across a kdump reboot), preserving the existing "no core → readiness_failure" contract.
_KDUMP_SETTLE_TIMEOUT_S = 120.0
_KDUMP_SETTLE_POLL_INTERVAL_S = 3.0

type _DomainSettled = Callable[[], bool]
type _Sleep = Callable[[float], None]


class _GuestfsHandle(Protocol):  # pragma: no cover - live_vm (libguestfs binding surface)
    """The subset of the unstubbed guestfs handle this reader drives."""

    def add_drive_opts(self, filename: str, *, readonly: bool) -> None: ...
    def launch(self) -> None: ...
    def inspect_os(self) -> list[str]: ...
    def mount_ro(self, root: str, mountpoint: str) -> None: ...
    def glob_expand(self, pattern: str) -> list[str]: ...
    def statns(self, path: str) -> dict[str, int]: ...
    def download(self, path: str, dest: str) -> None: ...
    def close(self) -> None: ...


def _libvirt_uri() -> str:
    """The provider's configured libvirt URI (``KDIVE_LIBVIRT_URI``, default ``qemu:///system``)."""
    return config.require(LIBVIRT_URI)


class _LibguestfsCoreReader:  # pragma: no cover - live_vm (libguestfs)
    """Read-only libguestfs view of a System's overlay, listing/reading /var/crash cores.

    The libguestfs appliance is launched once in the constructor and reused for both the
    listing and the read; the caller closes it via ``close()``. Every guestfs call is wrapped
    so a corrupt/locked overlay or a vanished core surfaces as a typed ``CategorizedError``
    (the provider contract), not a raw ``guestfs.Error``.
    """

    def __init__(self, overlay: str) -> None:
        self._overlay = overlay
        self._guest = self._mount(overlay)

    def list_vmcores(self, overlay: str) -> list[VmcoreEntry]:
        try:
            entries: list[VmcoreEntry] = []
            for path in self._guest.glob_expand(_VAR_CRASH_GLOB):
                entries.append(self._entry(path, incomplete=False))
            for path in self._guest.glob_expand(_VAR_CRASH_INCOMPLETE_GLOB):
                entries.append(self._entry(path, incomplete=True))
            return entries
        except Exception as exc:
            raise self._io_failure("listing /var/crash cores", exc) from exc

    def _entry(self, path: str, *, incomplete: bool) -> VmcoreEntry:
        stat = self._guest.statns(path)
        return VmcoreEntry(
            path=path,
            mtime=stat["st_mtime_sec"],
            size_bytes=stat["st_size"],
            incomplete=incomplete,
        )

    def download_vmcore(self, overlay: str, path: str, dest: Path) -> None:
        """Stream the core at ``path`` to ``dest`` (constant memory), not into RAM (#657)."""
        try:
            self._guest.download(path, str(dest))
        except Exception as exc:
            raise self._io_failure("downloading the kdump core", exc) from exc

    def close(self) -> None:
        try:
            self._guest.close()
        except Exception:
            _log.warning("libguestfs handle close failed; continuing", exc_info=True)

    def _io_failure(self, op: str, exc: Exception) -> CategorizedError:
        return CategorizedError(
            f"libguestfs failed {op} from the System overlay",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"overlay": self._overlay, "error": type(exc).__name__},
        )

    @staticmethod
    def _mount(overlay: str) -> _GuestfsHandle:
        try:
            import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided
        except ImportError as exc:
            raise CategorizedError(
                "libguestfs (the guestfs Python binding) is required for local kdump capture",
                category=ErrorCategory.MISSING_DEPENDENCY,
            ) from exc
        guest = cast("_GuestfsHandle", guestfs.GuestFS(python_return_dict=True))
        try:
            guest.add_drive_opts(overlay, readonly=True)
            guest.launch()
            roots = guest.inspect_os()
            if roots:
                guest.mount_ro(roots[0], "/")
        except Exception as exc:
            _close_guestfs_handle(guest, "after failed read-only overlay open")
            raise CategorizedError(
                "libguestfs failed to open the System overlay",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay, "error": type(exc).__name__},
            ) from exc
        if not roots:
            _close_guestfs_handle(guest, "after empty read-only overlay inspection")
            raise CategorizedError(
                "could not inspect the System overlay to find /var/crash",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"overlay": overlay},
            )
        return guest


def _close_guestfs_handle(guest: _GuestfsHandle, context: str) -> None:
    try:
        guest.close()
    except Exception:
        _log.warning(
            "libguestfs close failed %s; preserving original failure", context, exc_info=True
        )


def _poll_until_settled(
    is_settled: _DomainSettled,
    sleep: _Sleep,
    *,
    timeout_s: float,
    poll_interval_s: float,
) -> bool:
    """Poll ``is_settled`` until it is true or the bounded window elapses (ADR-0217).

    Returns ``True`` the moment the domain is observed settled (kdump finished and the guest
    self-shut-off), or ``False`` if the window elapses first. The first probe is taken before
    any sleep so an already-settled domain returns immediately with no wait; thereafter it
    sleeps ``poll_interval_s`` between probes. The probe budget is bounded by
    ``ceil(timeout_s / poll_interval_s)`` so the total wait never exceeds ``timeout_s``.
    """
    probes = max(1, math.ceil(timeout_s / poll_interval_s))
    for probe in range(probes):
        if is_settled():
            return True
        if probe < probes - 1:
            sleep(poll_interval_s)
    return False


def _force_off_domain(system_id: UUID) -> None:  # pragma: no cover - live_vm (libvirt)
    """Force the System's domain off (idempotent) so its overlay is safe to read offline.

    Opens the provider's configured URI (``KDIVE_LIBVIRT_URI``), the same source as
    ``control.py``/``discovery.py`` — never ``libvirt.open(None)``. By the time the harvest
    reaches here the in-guest kdump has been waited out (``_real_wait_for_vmcore``), so a
    force-off only quiesces a guest that kdump-rebooted back to running, or one whose kdump
    never finished — libguestfs reads of a disk a running guest is mutating are unsafe
    (ADR-0203/0217).
    """
    conn = libvirt.open(_libvirt_uri())
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return  # already gone — nothing running to quiesce
        if domain.isActive():
            domain.destroy()
    finally:
        conn.close()


def _real_domain_settled(system_id: UUID) -> bool:  # pragma: no cover - live_vm (libvirt)
    """True when the System's domain is shut off or gone — the kdump-complete signal (ADR-0217).

    A domain that kdump halted/shut-down after writing its core reports ``VIR_DOMAIN_SHUTOFF``;
    a domain that has been undefined/removed is also "settled". Any other state (running, the
    crash kernel still booting/dumping) is not settled, so the poll keeps waiting.
    """
    conn = libvirt.open(_libvirt_uri())
    try:
        try:
            domain = conn.lookupByName(domain_name_for(system_id))
        except libvirt.libvirtError:
            return True  # gone — nothing left running, treat as settled
        state, _reason = domain.state()
        return state == libvirt.VIR_DOMAIN_SHUTOFF
    finally:
        conn.close()


def _real_wait_for_vmcore(system_id: UUID) -> HarvestOutcome:  # pragma: no cover - live_vm
    """Wait for in-guest kdump to finish, then harvest the newest overlay core (ADR-0217).

    Polls the domain for the kdump-complete signal (self-shut-off) within a bounded window
    before forcing the domain off and reading its overlay. ``force_crash`` only *starts* kdump
    (the NMI panic); the crash kernel then boots and writes ``/var/crash/<ts>/vmcore``, which
    takes tens of seconds — so destroying the domain immediately (the pre-ADR-0217 behaviour)
    raced the dump and harvested an empty ``/var/crash``. On timeout we force-off and harvest
    anyway: a core already written persists on the overlay, and an absent core stays a
    ``READINESS_FAILURE`` exactly as before.

    Returns the harvest outcome: the spooled complete core when one was written, plus whether a
    ``vmcore-incomplete`` was seen so ``capture`` can disclose an incomplete-core readiness
    failure (ADR-0251). Owns the spool's lifecycle only up to a complete core: it creates a
    private temp directory, streams the chosen core into it, and removes the directory when no
    complete core is found or the harvest raises. On a complete core the caller (``capture``)
    owns cleanup, so the file survives this function's ``finally``.
    """
    _poll_until_settled(
        lambda: _real_domain_settled(system_id),
        time.sleep,
        timeout_s=_KDUMP_SETTLE_TIMEOUT_S,
        poll_interval_s=_KDUMP_SETTLE_POLL_INTERVAL_S,
    )
    _force_off_domain(system_id)
    overlay = overlay_path(system_id)
    spool_dir = Path(tempfile.mkdtemp(prefix="kdive-kdump-"))
    dest = spool_dir / "vmcore"
    reader = _LibguestfsCoreReader(overlay)
    outcome = HarvestOutcome(core=None, incomplete_found=False)
    try:
        outcome = harvest_vmcore(reader, overlay, dest=dest, max_bytes=MAX_CORE_BYTES)
        return outcome
    finally:
        reader.close()
        if outcome.core is None:
            _remove_spool(dest)


def _remove_spool(core: Path) -> None:
    """Remove the spooled core and the private temp directory holding it (best effort)."""
    shutil.rmtree(core.parent, ignore_errors=True)

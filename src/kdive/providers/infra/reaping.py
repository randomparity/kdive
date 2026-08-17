"""Provider-owned infrastructure reaper contracts."""

from __future__ import annotations

from typing import NamedTuple, Protocol, runtime_checkable
from uuid import UUID


@runtime_checkable
class OwnedDomain(Protocol):
    """A provider-owned domain plus its optional kdive System metadata tag.

    The members are read-only: implementers are immutable value objects (frozen
    dataclasses), and consumers only ever read ``name``/``system_id``.
    """

    @property
    def name(self) -> str: ...
    @property
    def system_id(self) -> UUID | None: ...


@runtime_checkable
class InfraReaper(Protocol):
    """The narrow provider port the reconciler consumes for leaked infrastructure."""

    async def list_owned(self) -> list[OwnedDomain]: ...
    async def destroy(self, name: str) -> None: ...


class NullReaper:
    """The default reaper: owns nothing, destroys nothing."""

    async def list_owned(self) -> list[OwnedDomain]:
        return []

    async def destroy(self, name: str) -> None:
        return None


class DumpVolume(NamedTuple):
    """A provider's host_dump volume: its name, owning System, and store-side mtime (epoch s).

    ``system_id`` is parsed from the deterministic dump-volume name (ADR-0094); a volume whose
    name does not encode a System is reported with ``system_id=None`` so the reconciler can
    age-reap it without ever skipping it on a (non-existent) live capture.
    """

    name: str
    system_id: UUID | None
    mtime_epoch_s: float


@runtime_checkable
class DumpVolumeReaper(Protocol):
    """The narrow provider port the reconciler consumes for orphaned host_dump volumes.

    Lists the provider's host_dump volumes with their store mtime, and deletes one **by name and
    sampled identity**. Deletion is idempotent — a volume already gone is not an error (a live
    capture's own ``finally`` may have removed it between the list and the delete).

    ``expected_mtime_epoch_s`` is required rather than optional because the name alone does not
    identify a volume over time (ADR-0562): the deterministic ``kdive-host-dump-<system_id>.kdump``
    name is reused by every capture of that System, and a capture's delete-stale-then-dump pair puts
    a new volume there. An implementation must re-read the volume it looked up and decline when its
    mtime differs from the value the reconciler sampled, so the delete cannot resolve onto a volume
    the reconciler never classified. A default would make that a guard that silently does nothing.

    The return reports whether **this call deleted the volume** — not whether the name is now
    absent. ``False`` covers both the identity decline and a name no reachable host held, because
    neither reclaimed anything and ``reaped_dump_volumes`` counts what the sweep reclaimed. An
    absent volume is still not an error; it is simply not a reap.
    """

    async def list_dump_volumes(self) -> list[DumpVolume]: ...
    async def delete_dump_volume(self, name: str, *, expected_mtime_epoch_s: float) -> bool: ...


class NullDumpVolumeReaper:
    """The default dump-volume reaper: owns nothing, deletes nothing."""

    async def list_dump_volumes(self) -> list[DumpVolume]:
        return []

    async def delete_dump_volume(self, name: str, *, expected_mtime_epoch_s: float) -> bool:
        # Unreachable through the reconciler, which only deletes what this reaper listed — and it
        # lists nothing. ``False`` is the honest answer either way: nothing was deleted.
        return False

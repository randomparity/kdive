"""Provider-owned infrastructure reaper contracts."""

from __future__ import annotations

from collections.abc import Mapping
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


class OrphanedCapture(NamedTuple):
    """One terminal capture job's host state, named from its persisted ownership chain.

    Carries exactly what a reaper needs to name the owning capture and nothing wider
    (ADR-0556): the provider kind that selected it, the Resource it must bind to (ADR-0187
    binds by ``resource_name``), the stored-or-derived domain name, the owning System, and the
    job id every reaper turns into a sink name through
    :func:`kdive.providers.ports.traffic.capture_qom_id`.

    The sink name is deliberately *not* a field. It is derived from ``job_id`` by the one shared
    convention, so a reaper cannot be handed a name that disagrees with what the producer
    attached.
    """

    provider_kind: str
    resource_id: UUID
    resource_name: str
    system_id: UUID
    domain_name: str
    job_id: UUID


@runtime_checkable
class CaptureReaper(Protocol):
    """The narrow provider port the reconciler consumes for orphaned traffic captures.

    An implementation detaches the capture's QOM object **before** removing its destination and
    tolerates an already-missing filter, domain, or destination: a crash between a successful
    provider call and the reconciler's completion write repeats an already-effective call, so the
    contract is at-least-once attempts with a convergent effect, not exactly-once execution.

    The return reports whether **this call** left no capture state behind. ``False`` is not an
    error — it covers a host no longer reachable and any other reason the call declined — but it
    is also not a reclaim, so the sweep neither counts it nor marks the row complete and the row
    becomes eligible again after its retry deadline. Raising and returning ``False`` differ only
    in whether the sweep logs a traceback; neither can mark a row.
    """

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool: ...


class NullCaptureReaper:
    """Disabled wiring for a provider kind whose concrete reaper has not landed (ADR-0556).

    A kind wired to this is not merely a no-op call: :func:`dispatchable_capture_kinds` leaves it
    out of the eligible-kind set, so the sweep never selects its rows and never reaches a
    completion write for one. #1947 and #1948 each enable only their own kind by replacing this
    with a concrete reaper.
    """

    async def reclaim_capture(self, capture: OrphanedCapture) -> bool:
        # Unreachable through the reconciler, which never selects a row for an undispatchable
        # kind. ``False`` is the honest answer either way: nothing was reclaimed.
        return False


def dispatchable_capture_kinds(reapers: Mapping[str, CaptureReaper]) -> frozenset[str]:
    """The provider kinds a **concrete** capture reaper is registered for (ADR-0556).

    Filtering the registry rather than letting a ``Null`` reaper answer keeps disablement a
    selection property: rows of a disabled kind never enter the batch, so they cannot consume the
    per-pass bound or starve a kind that does have a reaper.
    """
    return frozenset(
        kind for kind, reaper in reapers.items() if not isinstance(reaper, NullCaptureReaper)
    )

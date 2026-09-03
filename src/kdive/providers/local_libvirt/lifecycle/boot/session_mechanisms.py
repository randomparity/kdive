"""Host mechanisms for the local external-boot session factory (ADR-0591).

ADR-0587 defined `LocalExternalBootSessionFactory` and its six injected host mechanisms and
deferred binding them. This module supplies five of the six. `RunningObserver` is deliberately
absent: local domains render no qemu-guest-agent channel, so there is no host-reachable read of a
running guest, and the factory keeps its fail-closed `_unconfigured_observation` default (#2212).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.boot.session import (
    LocalExternalBootOperationLease,
    OperationOwnership,
    PinnedOperationOwnership,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding


@dataclass
class LocalOperationLease:
    """The provider-local nominal capability ADR-0587 requires.

    Nominal rather than structural on purpose: the lane accepts only this concrete type, so an
    arbitrary object carrying `system_id` and `binding` attributes is not a lease. Issuance stays
    with the serialization-lane context (#2212); this type carries no way to mint itself behind a
    lock it does not hold.
    """

    system_id: UUID
    binding: ExternalBootActivationBinding
    released: bool = False
    _pins: int = 0

    def release(self) -> None:
        """Release the lane, refusing while any pin is outstanding.

        ADR-0587: the database lane cannot be released while a guest context can still observe
        or mutate the overlay, so the pin count — not the caller's intent — decides.
        """
        if self._pins:
            raise RuntimeError("operation lease is pinned")
        self.released = True


class _Pin:
    """One retained proof that the issuing lane is still held.

    Idempotent on close: a session's cleanup path attempts every owned resource, so a second
    close must not decrement the count twice and release a lease another pin still holds.
    """

    def __init__(self, lease: LocalOperationLease) -> None:
        self._lease: LocalOperationLease | None = lease
        lease._pins += 1

    def close(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease._pins -= 1


class LocalOperationLane:
    """Validates a lease and produces the retained pin the session holds for its lifetime."""

    def pin(self, lease: LocalExternalBootOperationLease) -> PinnedOperationOwnership:
        # isinstance, not duck-typing: `LocalExternalBootOperationLease` is a Protocol, so a
        # structural check would accept any object carrying `system_id` and `binding` and the
        # lane would pin an identity no serialization-lane context ever issued.
        if not isinstance(lease, LocalOperationLease):
            raise TypeError("foreign operation lease")
        if lease.released:
            raise RuntimeError("operation lease is released")
        return PinnedOperationOwnership(
            OperationOwnership(lease.system_id, lease.binding),
            _Pin(lease),
        )

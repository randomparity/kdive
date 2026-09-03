"""Host mechanisms for the local external-boot session factory (ADR-0591).

ADR-0587 defined `LocalExternalBootSessionFactory` and its six injected host mechanisms and
deferred binding them. This module supplies five of the six. `RunningObserver` is deliberately
absent: local domains render no qemu-guest-agent channel, so there is no host-reachable read of a
running guest, and the factory keeps its fail-closed `_unconfigured_observation` default (#2212).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    _open_or_create_private_child,
    _require_private_owned_directory,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    LocalExternalBootOperationLease,
    OperationOwnership,
    PinnedOperationOwnership,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding

_ARTIFACT_ROOT_REFUSED = "artifact root is not an owner-only service-owned directory"
_NOT_CANONICAL = "artifact root component is not a canonical identifier"


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


def _canonical_name(value: str) -> str:
    """Return `value` unchanged if it is a canonical UUID, refusing anything else.

    `ExternalBootActivationBinding` already types both component names as `CanonicalUuid`, so
    this re-assertion is redundant against today's binding. It is here so a future loosening
    of that type cannot silently turn a component into a traversal: `str(UUID(value))` yields
    only the lowercase hyphenated form, which contains neither `/` nor `..`, so requiring
    equality with it admits exactly the canonical spelling.
    """
    try:
        canonical = str(UUID(value))
    except AttributeError, TypeError, ValueError:
        raise ValueError(_NOT_CANONICAL) from None
    if canonical != value:
        raise ValueError(_NOT_CANONICAL)
    return value


class LocalArtifactRoot:
    """Opens `<recovery_root>/<system_id>/<run_id>`, creating the two children when absent.

    ADR-0591 binds this walk to the configured recovery root: the root is held from
    construction and every later resolution is descriptor-relative from it, so the only
    per-call input is an `OperationOwnership` carrying two canonical UUIDs.

    **It writes.** #2210 provisions the per-slot recovery root and nothing beneath it, so an
    open-only walk would fail closed on every first activation. Creation carries the same
    guards as opening: `_open_or_create_private_child` creates mode 0700 and then delegates to
    `_open_private_directory`, so `O_NOFOLLOW` and the mode and euid checks apply either way.
    Nothing here reclaims the created directories; that is #2212's.
    """

    def __init__(self, recovery_root: Path) -> None:
        self._root = recovery_root

    def open(self, ownership: OperationOwnership) -> int:
        system = _canonical_name(str(ownership.system_id))
        run = _canonical_name(ownership.binding.run_id)
        try:
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                _require_private_owned_directory(root_fd, "artifact root")
                system_fd = _open_or_create_private_child(root_fd, system)
            finally:
                os.close(root_fd)
            try:
                return _open_or_create_private_child(system_fd, run)
            finally:
                os.close(system_fd)
        except OSError:
            # `from None`, not `from exc`: the root is opened by path, so its `OSError` holds
            # the host path in `.filename`, and chaining would re-attach it to the traceback
            # that reaches a log. Only `OSError` is wrapped —
            # `_require_private_owned_directory` raises a `ValueError` that already carries no
            # path and names the failing check more precisely than this message could.
            raise ValueError(_ARTIFACT_ROOT_REFUSED) from None

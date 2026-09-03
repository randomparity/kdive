"""Host mechanisms for the local external-boot session factory (ADR-0591).

ADR-0587 defined `LocalExternalBootSessionFactory` and its six injected host mechanisms and
deferred binding them. This module supplies five of the six. `RunningObserver` is deliberately
absent: local domains render no qemu-guest-agent channel, so there is no host-reachable read of a
running guest, and the factory keeps its fail-closed `_unconfigured_observation` default (#2212).
"""

from __future__ import annotations

import os
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    _open_or_create_private_child,
    _open_private_directory,
    _require_private_owned_directory,
)

# The name the sink actually writes, imported rather than restated so the two cannot
# drift: a second literal here would silently stop matching if the sink ever renamed it.
from kdive.providers.local_libvirt.lifecycle.boot.recovery import _ARCHIVE_NAME
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    LocalExternalBootOperationLease,
    OperationOwnership,
    PinnedOperationOwnership,
    _Guest,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding

_ARTIFACT_ROOT_REFUSED = "artifact root is not an owner-only service-owned directory"
_NOT_CANONICAL = "external-boot path component is not a canonical identifier"
_RECOVERY_REFUSED = "recovery directory is not an owner-only service-owned directory"

PAYLOAD_NAMES: tuple[str, ...] = ("kernel", "initrd", "modules")


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


class LocalPayloadCleanup:
    """Removes an activation's boot payloads by exact name, treating absence as success."""

    def __init__(self, recovery_root: Path) -> None:
        self._root = recovery_root

    def cleanup(self, root_fd: int, binding: ExternalBootActivationBinding) -> None:
        # Composed before anything is removed, so a non-canonical binding refuses without
        # having already deleted the payloads.
        directory = f"{_canonical_name(binding.system_id)}.{_canonical_name(binding.activation_id)}"
        for name in PAYLOAD_NAMES:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=root_fd)
        self._remove_archive(directory)

    def _remove_archive(self, directory: str) -> None:
        """Remove the activation's published archive from its own recovery directory.

        `RecoveryArchiveSink.publish` writes the archive during prepare and
        `publish_tombstone` unlinks only `intent.json`, so a cleanup confined to `root_fd`
        leaves it behind — and `finalize_tombstone`, which requires the directory to hold
        exactly `tombstone.json`, then fails permanently for every activation that captured
        one. The mechanism holds a `Path`, so it must open the root itself: without
        `O_NOFOLLOW` that open would follow a substituted symlink, and without re-validating
        it would trust that the root is still what startup checked, which the read path
        explicitly refuses to do. Both controls are on the deleting path here.
        """
        try:
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return
        except OSError:
            raise ValueError(_RECOVERY_REFUSED) from None
        try:
            _require_private_owned_directory(root_fd, "recovery root")
            try:
                recovery_fd = _open_private_directory(root_fd, directory)
            except FileNotFoundError:
                # An activation whose recovery directory never existed has no archive.
                return
            except OSError:
                raise ValueError(_RECOVERY_REFUSED) from None
            try:
                with suppress(FileNotFoundError):
                    os.unlink(_ARCHIVE_NAME, dir_fd=recovery_fd)
            finally:
                os.close(recovery_fd)
        finally:
            os.close(root_fd)


def open_libguestfs_guest() -> _Guest:
    """Return an unlaunched libguestfs handle.

    It attaches no drive, launches nothing and mounts nothing. `_ConcreteSession`'s
    `_open_guest_context` owns all of that, and only after `require_inactive()`, so an opener
    that did any of it here would move guest access outside that gate.
    """
    import guestfs  # noqa: PLC0415  # ty: ignore[unresolved-import]  # operator-provided

    return cast("_Guest", guestfs.GuestFS(python_return_dict=True))

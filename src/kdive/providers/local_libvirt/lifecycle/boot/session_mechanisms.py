"""Host mechanisms for the local external-boot session factory (ADR-0591).

ADR-0587 defined `LocalExternalBootSessionFactory` and its six injected host mechanisms and
deferred binding them. This module supplies five of the six; one remains deliberately absent:

- `ReadinessProbe` (amendment 7) — `_real_readiness` reads a console log that only
  `LocalLibvirtInstall`'s prepare truncates, and `_ConcreteSession.start()` truncates nothing, so
  on the arm that reaches it the previous boot's `kdive-ready` marker makes the gate pass for a
  panicked target. A correct probe must anchor its window at `start()`, which is in `session.py`.

The factory keeps its fail-closed `_unconfigured_readiness` default, so #2212 cannot ship a live
port without that final mechanism.
"""

from __future__ import annotations

import errno
import os
import xml.etree.ElementTree as ET  # noqa: S405 - serialization follows a defused parse
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import UUID

import libvirt
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring
from pydantic import ValidationError

from kdive.build_artifacts.validation import parse_gnu_build_id
from kdive.domain.errors import CategorizedError, ErrorCategory
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
    RunningDomain,
    _Guest,
)
from kdive.providers.ports.external_boot import (
    Architecture,
    ExternalBootActivationBinding,
    RunningKernelObservation,
)
from kdive.providers.shared.guest_agent import (
    AgentCommand,
    AgentExecResult,
    GuestAgentExec,
    GuestDomain,
    qemu_agent_command,
)

_ARTIFACT_ROOT_REFUSED = "artifact root is not an owner-only service-owned directory"
_NOT_CANONICAL = "external-boot path component is not a canonical identifier"
_RECOVERY_REFUSED = "recovery directory is not an owner-only service-owned directory"

PAYLOAD_NAMES: tuple[str, ...] = ("kernel", "initrd", "modules")

UNAME_PROGRAM = "/usr/bin/uname"
CAT_PROGRAM = "/usr/bin/cat"
PROC_CMDLINE_PATH = "/proc/cmdline"
KERNEL_NOTES_PATH = "/sys/kernel/notes"
OBSERVATION_PROGRAMS = frozenset({UNAME_PROGRAM, CAT_PROGRAM})
_GUEST_AGENT_CHANNEL = "org.qemu.guest_agent.0"
MAX_GUEST_CONTENT_BYTES = 2_048
MAX_GUEST_FIELD_CHARS = 64
_ARCHITECTURES: tuple[Architecture, ...] = ("x86_64", "ppc64le")


def _refused(message: str, exc: OSError) -> str:
    """Append the symbolic errno to a fixed refusal message.

    Without it, every `OSError` in these walks reads as an ownership refusal — including the
    most likely real failure of a path that now *writes*: a full or quota-exhausted recovery
    filesystem. An operator told the root's mode or owner is wrong checks `stat`, finds 0700
    and the right uid, and has nothing pointing at `ENOSPC`. `errno.errorcode` values are
    symbolic constants and carry no host path, so naming one costs nothing the `from None`
    suppression is protecting.

    `OSError.errno` can be `None`, which would render as the bare word "None"; an
    unnumbered error is reported as `EUNKNOWN` instead.
    """
    return f"{message} ({errno.errorcode.get(exc.errno, 'EUNKNOWN') if exc.errno else 'EUNKNOWN'})"


@dataclass
class LocalOperationLease:
    """The provider-local nominal capability ADR-0587 requires.

    **Not thread-safe, by contract.** `_pins` is incremented and decremented without
    synchronisation, so one lease must not be pinned concurrently from more than one thread.
    ADR-0587 issues a lease inside the per-System serialization-lane context, which already
    serialises that System's operations, so the constraint is satisfied by construction rather
    than by a lock here. #2212, which owns issuance, inherits the constraint — adding a lock
    now would be machinery for a caller that does not exist.

    Nominal rather than structural on purpose: the lane accepts only this concrete type, so an
    arbitrary object carrying `system_id` and `binding` attributes is not a lease.

    That is the whole of the claim. This is a public, unfrozen dataclass, so any code in the
    process can construct one — the nominal type bounds *what shape* the lane accepts, never *who
    may build it*. What stops a forged identity reaching host resources is the factory's
    `binding_matches_expected` comparison against the caller's `ExpectedOperationOwnership`, which
    `test_session.py` already covers. Issuing a lease behind the per-System lane lock is #2212's
    (ADR-0587), and the lane deliberately exposes no method for it.
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


class LocalRunningObserver:
    """Read one running local guest through the constrained guest-agent executor."""

    def __init__(self, *, agent_command: AgentCommand = qemu_agent_command) -> None:
        self._agent = GuestAgentExec(
            agent_command=agent_command,
            allowed_programs=OBSERVATION_PROGRAMS,
        )

    def __call__(self, system_id: UUID, domain: RunningDomain) -> RunningKernelObservation:
        guest_domain = cast(GuestDomain, domain)
        expected_cmdline = self._expected_cmdline(domain, system_id)
        release = self._single_field(
            self._read(guest_domain, [UNAME_PROGRAM, "-r"], system_id, "release"),
            system_id,
            "release",
        )
        machine = self._single_field(
            self._read(guest_domain, [UNAME_PROGRAM, "-m"], system_id, "architecture"),
            system_id,
            "architecture",
        )
        cmdline = self._read_cmdline(guest_domain, system_id)
        notes = self._read(
            guest_domain,
            [CAT_PROGRAM, KERNEL_NOTES_PATH],
            system_id,
            "kernel notes",
        )
        try:
            build_id = parse_gnu_build_id(notes)
        except CategorizedError as exc:
            raise _observation_failure(
                system_id, "the running kernel has no readable GNU build id"
            ) from exc
        if machine not in _ARCHITECTURES:
            raise _observation_failure(system_id, "the guest reported an unsupported architecture")
        try:
            return RunningKernelObservation(
                identity={
                    "architecture": machine,
                    "release": release,
                    "gnu_build_id": build_id,
                },
                cmdline=cmdline,
                expected_cmdline=expected_cmdline,
            )
        except ValidationError:
            raise _observation_failure(
                system_id, "the guest returned an out-of-contract running observation"
            ) from None

    def _read(
        self,
        domain: GuestDomain,
        argv: list[str],
        system_id: UUID,
        what: str,
        *,
        max_bytes: int = MAX_GUEST_CONTENT_BYTES,
    ) -> bytes:
        result: AgentExecResult = self._agent.run(domain, argv)
        if result.exit_status != 0:
            raise _observation_failure(system_id, f"the guest could not read {what}")
        if len(result.stdout) > max_bytes:
            raise _observation_failure(system_id, f"the guest returned oversized {what}")
        return result.stdout

    def _read_cmdline(self, domain: GuestDomain, system_id: UUID) -> bytes:
        raw = self._read(
            domain,
            [CAT_PROGRAM, PROC_CMDLINE_PATH],
            system_id,
            "kernel command-line evidence",
            max_bytes=MAX_GUEST_CONTENT_BYTES + 1,
        )
        if not raw.endswith(b"\n"):
            raise _observation_failure(system_id, "the kernel command-line read was truncated")
        content = raw[:-1]
        if len(content) > MAX_GUEST_CONTENT_BYTES:
            raise _observation_failure(
                system_id, "the guest returned oversized kernel command-line evidence"
            )
        return content

    @staticmethod
    def _single_field(raw: bytes, system_id: UUID, what: str) -> str:
        text = raw.decode("utf-8", errors="replace")
        if text.endswith("\n"):
            text = text[:-1]
        if (
            not text
            or len(text) > MAX_GUEST_FIELD_CHARS
            or "\n" in text
            or any(character.isspace() for character in text)
        ):
            raise _observation_failure(system_id, f"the guest returned malformed {what}")
        return text

    @staticmethod
    def _expected_cmdline(domain: RunningDomain, system_id: UUID) -> bytes:
        try:
            root = _safe_fromstring(domain.XMLDesc(0))
        except (libvirt.libvirtError, ET.ParseError, DefusedXmlException) as exc:
            raise _observation_failure(
                system_id, "the target domain XML could not be read"
            ) from exc
        expected = root.findtext("./os/cmdline")
        channel = root.find(f"./devices/channel/target[@name='{_GUEST_AGENT_CHANNEL}']")
        if channel is None or channel.get("type") != "virtio":
            raise _observation_failure(
                system_id,
                "the qemu-guest-agent channel is absent; reprovision this System",
            )
        if expected is None:
            raise _observation_failure(
                system_id, "the target domain XML has no expected command line"
            )
        encoded = expected.encode()
        if len(encoded) > MAX_GUEST_CONTENT_BYTES:
            raise _observation_failure(
                system_id, "the target command line exceeds the observation bound"
            )
        return encoded


def _observation_failure(system_id: UUID, reason: str) -> CategorizedError:
    return CategorizedError(
        f"local-libvirt external-boot observation failed: {reason}",
        category=ErrorCategory.READINESS_FAILURE,
        details={"system_id": str(system_id)},
        terminal=True,
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
        except OSError as exc:
            # `from None`, not `from exc`: the root is opened by path, so its `OSError` holds
            # the host path in `.filename`, and chaining would re-attach it to the traceback
            # that reaches a log. Only the symbolic errno is carried across. Only `OSError` is
            # wrapped — `_require_private_owned_directory` raises a `ValueError` that already
            # carries no path and names the failing check more precisely than this message.
            raise ValueError(_refused(_ARTIFACT_ROOT_REFUSED, exc)) from None


class LocalPayloadCleanup:
    """Removes an activation's boot payloads by exact name, treating absence as success."""

    def __init__(self, recovery_root: Path) -> None:
        self._root = recovery_root

    def cleanup(self, root_fd: int, binding: ExternalBootActivationBinding) -> None:
        """Remove the activation's payloads and its published archive, validating first.

        **Every check runs before the first unlink.** Deleting the payloads and only then
        refusing on the recovery directory strands the activation:
        `_RealLocalExternalBootOperation.cleanup` never reaches `publish_tombstone`, so every
        retry re-raises the same refusal with the payloads it would have needed already gone —
        and if the directory was a symlink an attacker planted, the payloads are destroyed while
        the archive `finalize_tombstone` blocks on is untouched. Nothing required that ordering.
        """
        # Composed first, so a non-canonical binding refuses before anything is opened.
        directory = f"{_canonical_name(binding.system_id)}.{_canonical_name(binding.activation_id)}"
        recovery_fd = self._open_recovery_directory(directory)
        try:
            for name in PAYLOAD_NAMES:
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=root_fd)
            if recovery_fd is not None:
                with suppress(FileNotFoundError):
                    os.unlink(_ARCHIVE_NAME, dir_fd=recovery_fd)
        finally:
            if recovery_fd is not None:
                os.close(recovery_fd)

    def _open_recovery_directory(self, directory: str) -> int | None:
        """Open and validate the activation's recovery directory, or `None` when it is absent.

        `RecoveryArchiveSink.publish` writes the archive during prepare and `publish_tombstone`
        unlinks only `intent.json`, so a cleanup confined to `root_fd` leaves it behind — and
        `finalize_tombstone`, which requires the directory to hold exactly `tombstone.json`,
        then fails permanently for every activation that captured one. The mechanism holds a
        `Path`, so it must open the root itself: without `O_NOFOLLOW` that open would follow a
        substituted symlink, and without re-validating it would trust that the root is still
        what startup checked, which the read path explicitly refuses to do. Both controls are on
        the deleting path here.

        **Absence is success for the per-activation directory and a failure for the root.** An
        activation whose recovery directory never existed has no archive, so cleanup stays
        idempotent. An absent *root* is a misconfiguration, and it is exactly the shape the
        #2212 divergence hazard takes: this mechanism resolves `KDIVE_LIBVIRT_RECOVERY_ROOT`
        while `_RealLocalExternalBootOperation.cleanup` resolves the root its store was
        constructed with. Treating that as success would delete the payloads, skip the archive,
        let `publish_tombstone` run, and leave `finalize_tombstone` failing permanently — the
        silent failure this mechanism exists to prevent. So it refuses instead.
        """
        try:
            root_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise ValueError(_refused(_RECOVERY_REFUSED, exc)) from None
        try:
            _require_private_owned_directory(root_fd, "recovery root")
            try:
                return _open_private_directory(root_fd, directory)
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise ValueError(_refused(_RECOVERY_REFUSED, exc)) from None
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

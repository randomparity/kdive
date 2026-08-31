"""Operation-scoped local-libvirt external-boot host capability (ADR-0587)."""

from __future__ import annotations

import hashlib
import json
import os
import unicodedata
import xml.etree.ElementTree as ET  # noqa: S405 - serialization follows a defused parse
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol
from uuid import UUID

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.providers.local_libvirt.lifecycle.boot.readiness import ReadinessResult
from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    RunningKernelObservation,
)
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from kdive.providers.shared.runtime_paths import domain_name_for


@dataclass(frozen=True)
class OverlayIdentity:
    """Stable identity of the expected System overlay."""

    device: int
    inode: int


@dataclass(frozen=True)
class _BoundOverlay:
    device: int
    inode: int
    path: str


@dataclass(frozen=True)
class ClosedDomainInspection:
    """Immutable facts derived from one persistent/inactive domain definition."""

    xml: bytes
    active: bool
    definition_identity: str
    source_boot_identity: str
    domain_name: str
    overlay: OverlayIdentity


class LocalExternalBootOperationLease(Protocol):
    """Opaque capability issued by the owner of the live System lane."""

    system_id: UUID
    binding: ExternalBootActivationBinding


class LocalExternalBootOperationPin(Protocol):
    """Retained proof that the issuing lane remains held."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class PinnedOperationOwnership:
    """Atomic lane-owned snapshot returned together with its retained pin."""

    pin: LocalExternalBootOperationPin
    system_id: UUID
    binding: ExternalBootActivationBinding


type PinOperationLease = Callable[[LocalExternalBootOperationLease], PinnedOperationOwnership]


class _Domain(Protocol):
    def XMLDesc(self, flags: int) -> str: ...  # noqa: N802
    def isActive(self) -> int: ...  # noqa: N802
    def destroy(self) -> int: ...
    def create(self) -> int: ...
    def free(self) -> object: ...


class _Connection(Protocol):
    def lookupByName(self, name: str) -> _Domain: ...  # noqa: N802
    def defineXML(self, xml: str) -> _Domain: ...  # noqa: N802
    def close(self) -> object: ...


class _Guest(Protocol):
    def add_drive_opts(self, overlay: str, *, format: str) -> None: ...
    def launch(self) -> None: ...
    def shutdown(self) -> None: ...
    def close(self) -> None: ...

    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def find(self, path: str) -> list[str]: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def download(self, remotefilename: str, filename: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...


class LocalExternalBootSession(Protocol):
    def inspect_closed(self) -> ClosedDomainInspection: ...
    def require_inactive(self) -> None: ...
    def stop_and_require_inactive(self) -> None: ...
    def open_artifact(self, name: str, flags: int, mode: int = 0o600) -> int: ...
    def unlink_artifact(self, name: str) -> None: ...
    def guest(self) -> AbstractContextManager[InactiveGuest]: ...
    def define_xml(self, xml: str) -> None: ...
    def start(self) -> None: ...
    def readiness(self) -> ReadinessResult: ...
    def observe_running(self) -> RunningKernelObservation: ...
    def restore_power(self, prior: Literal["running", "inactive"]) -> None: ...
    def cleanup_payloads(self) -> None: ...
    def close(self) -> None: ...


class InactiveGuest(Protocol):
    """The session's deliberately small libguestfs capability."""

    def exists(self, path: str) -> int: ...
    def is_dir(self, path: str, *, followsymlinks: bool) -> int: ...
    def find(self, path: str) -> list[str]: ...
    def lstatns(self, path: str) -> dict[str, int]: ...
    def readlink(self, path: str) -> str: ...
    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]: ...
    def download(self, remotefilename: str, filename: str) -> None: ...
    def mkdir(self, path: str) -> None: ...
    def upload(self, filename: str, remotefilename: str) -> None: ...
    def ln_s(self, target: str, linkname: str) -> None: ...
    def chmod(self, mode: int, path: str) -> None: ...
    def chown(self, owner: int, group: int, path: str) -> None: ...
    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None: ...
    def rm_rf(self, path: str) -> None: ...


class _GuestContext(AbstractContextManager[InactiveGuest]):
    def __init__(self, session: _ConcreteSession) -> None:
        self._session = session
        self._guest: _Guest | None = None
        self._closed = False

    def __enter__(self) -> InactiveGuest:
        if self._closed:
            raise RuntimeError("guest wrapper is closed")
        self._guest = self._session._open_guest_context(self)
        return _GuardedGuest(self, self._guest)

    def __exit__(self, *_exc: object) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        guest, self._guest = self._guest, None
        self._session._discard_guest(self)
        if guest is not None:
            errors = _attempt_guest_close(guest)
            if errors:
                raise ExceptionGroup("failed to close libguestfs handle", errors)

    def _poison(self) -> _Guest | None:
        self._closed = True
        guest, self._guest = self._guest, None
        return guest


class _GuardedGuest:
    def __init__(self, owner: _GuestContext, guest: _Guest) -> None:
        self._owner = owner
        self._guest = guest

    def exists(self, path: str) -> int:
        return self._handle().exists(path)

    def is_dir(self, path: str, *, followsymlinks: bool) -> int:
        return self._handle().is_dir(path, followsymlinks=followsymlinks)

    def find(self, path: str) -> list[str]:
        return self._handle().find(path)

    def lstatns(self, path: str) -> dict[str, int]:
        return self._handle().lstatns(path)

    def readlink(self, path: str) -> str:
        return self._handle().readlink(path)

    def lgetxattrs(self, path: str) -> list[dict[str, str | bytes]]:
        return self._handle().lgetxattrs(path)

    def download(self, remotefilename: str, filename: str) -> None:
        self._handle().download(remotefilename, filename)

    def mkdir(self, path: str) -> None:
        self._handle().mkdir(path)

    def upload(self, filename: str, remotefilename: str) -> None:
        self._handle().upload(filename, remotefilename)

    def ln_s(self, target: str, linkname: str) -> None:
        self._handle().ln_s(target, linkname)

    def chmod(self, mode: int, path: str) -> None:
        self._handle().chmod(mode, path)

    def chown(self, owner: int, group: int, path: str) -> None:
        self._handle().chown(owner, group, path)

    def lsetxattr(self, xattr: str, val: bytes, vallen: int, path: str) -> None:
        self._handle().lsetxattr(xattr, val, vallen, path)

    def rm_rf(self, path: str) -> None:
        self._handle().rm_rf(path)

    def _handle(self) -> _Guest:
        if self._owner._closed:
            raise RuntimeError("guest wrapper is closed")
        self._owner._session._guard_guest_operation(self._owner)
        return self._guest


class _ConcreteSession:
    def __init__(
        self,
        *,
        system_id: UUID,
        binding: ExternalBootActivationBinding,
        pin: LocalExternalBootOperationPin,
        connection: _Connection,
        domain: _Domain,
        artifact_fd: int,
        overlay: _BoundOverlay,
        open_guest: Callable[[], _Guest],
        stat_overlay: Callable[[str], tuple[int, int]],
        close_descriptor: Callable[[int], None],
        open_relative: Callable[[int, str, int, int], int],
        unlink_relative: Callable[[int, str], None],
        readiness: Callable[[UUID], ReadinessResult],
        observe_running: Callable[[UUID], RunningKernelObservation],
        cleanup_payloads: Callable[[int, ExternalBootActivationBinding], None],
    ) -> None:
        self._system_id = system_id
        self._binding = binding
        self._pin: LocalExternalBootOperationPin | None = pin
        self._connection: _Connection | None = connection
        self._domain: _Domain | None = domain
        self._artifact_fd: int | None = artifact_fd
        self._overlay = overlay
        self._open_guest = open_guest
        self._stat_overlay = stat_overlay
        self._close_descriptor = close_descriptor
        self._open_relative = open_relative
        self._unlink_relative = unlink_relative
        self._readiness = readiness
        self._observe_running = observe_running
        self._cleanup_payloads = cleanup_payloads
        self._guests: set[_GuestContext] = set()
        self._closed = False

    def inspect_closed(self) -> ClosedDomainInspection:
        domain = self._require_open_domain()
        xml = domain.XMLDesc(0)
        root = _parse_owned_xml(xml, self._system_id, self._overlay.path)
        active = _active(domain)
        return ClosedDomainInspection(
            xml=xml.encode(),
            active=active,
            definition_identity=_preserved_identity(root),
            source_boot_identity=_boot_identity(root),
            domain_name=domain_name_for(self._system_id),
            overlay=OverlayIdentity(self._overlay.device, self._overlay.inode),
        )

    def require_inactive(self) -> None:
        if _active(self._require_open_domain()):
            raise RuntimeError("domain must be inactive before overlay mutation")

    def stop_and_require_inactive(self) -> None:
        domain = self._require_open_domain()
        if _active(domain):
            domain.destroy()
        self.require_inactive()

    def open_artifact(self, name: str, flags: int, mode: int = 0o600) -> int:
        self._require_open_domain()
        assert self._artifact_fd is not None
        return self._open_relative(self._artifact_fd, _relative_name(name), flags, mode)

    def unlink_artifact(self, name: str) -> None:
        self._require_open_domain()
        assert self._artifact_fd is not None
        self._unlink_relative(self._artifact_fd, _relative_name(name))

    def guest(self) -> _GuestContext:
        self._require_open_domain()
        return _GuestContext(self)

    def define_xml(self, xml: str) -> None:
        self.require_inactive()
        _parse_owned_xml(xml, self._system_id, self._overlay.path)
        assert self._connection is not None
        prior = self._domain
        replacement = self._connection.defineXML(xml)
        self._domain = replacement
        if prior is not None and prior is not replacement:
            prior.free()

    def start(self) -> None:
        self._require_no_guest_context()
        self._require_open_domain().create()

    def readiness(self) -> ReadinessResult:
        self._require_open_domain()
        return self._readiness(self._system_id)

    def observe_running(self) -> RunningKernelObservation:
        self._require_open_domain()
        return self._observe_running(self._system_id)

    def restore_power(self, prior: Literal["running", "inactive"]) -> None:
        domain = self._require_open_domain()
        if prior == "running":
            self._require_no_guest_context()
        active = _active(domain)
        if prior == "running" and not active:
            domain.create()
        elif prior == "inactive" and active:
            domain.destroy()

    def cleanup_payloads(self) -> None:
        self.require_inactive()
        assert self._artifact_fd is not None
        self._cleanup_payloads(self._artifact_fd, self._binding)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        guests = list(self._guests)
        self._guests.clear()
        handles = [guest._poison() for guest in guests]
        artifact_fd, self._artifact_fd = self._artifact_fd, None
        domain, self._domain = self._domain, None
        connection, self._connection = self._connection, None
        pin, self._pin = self._pin, None
        errors: list[Exception] = []
        for guest in handles:
            if guest is not None:
                errors.extend(_attempt_guest_close(guest))
        for closer in (
            (lambda: self._close_descriptor(artifact_fd)) if artifact_fd is not None else None,
            domain.free if domain is not None else None,
            connection.close if connection is not None else None,
            pin.close if pin is not None else None,
        ):
            if closer is not None:
                try:
                    closer()
                except Exception as exc:  # cleanup must attempt every owned resource
                    errors.append(exc)
        if errors:
            raise ExceptionGroup("failed to close local external-boot session", errors)

    def _open_guest_context(self, wrapper: _GuestContext) -> _Guest:
        if self._guests:
            raise RuntimeError("an inactive guest context is already open")
        self.require_inactive()
        if self._stat_overlay(self._overlay.path) != (
            self._overlay.device,
            self._overlay.inode,
        ):
            raise ValueError("System overlay changed before guest open")
        guest = self._open_guest()
        try:
            guest.add_drive_opts(self._overlay.path, format="qcow2")
            guest.launch()
        except BaseException as exc:
            for close_error in _attempt_guest_close(guest):
                exc.add_note(f"cleanup failed: {close_error!r}")
            raise
        self._guests.add(wrapper)
        return guest

    def _discard_guest(self, guest: _GuestContext) -> None:
        self._guests.discard(guest)

    def _guard_guest_operation(self, guest: _GuestContext) -> None:
        if guest not in self._guests:
            raise RuntimeError("guest wrapper is closed")
        self.require_inactive()
        if self._stat_overlay(self._overlay.path) != (
            self._overlay.device,
            self._overlay.inode,
        ):
            raise ValueError("System overlay changed before guest operation")

    def _require_no_guest_context(self) -> None:
        self._require_open_domain()
        if self._guests:
            raise RuntimeError("power activation is forbidden while a guest context is open")

    def _require_open_domain(self) -> _Domain:
        if self._closed or self._domain is None:
            raise RuntimeError("local external-boot session is closed")
        return self._domain


class LocalExternalBootSessionFactory:
    def __init__(
        self,
        *,
        pin_lease: PinOperationLease,
        connect: Callable[[], _Connection],
        open_artifact_root: Callable[[PinnedOperationOwnership], int],
        open_guest: Callable[[], _Guest],
        stat_overlay: Callable[[str], tuple[int, int]] | None = None,
        close_descriptor: Callable[[int], None] = os.close,
        open_relative: Callable[[int, str, int, int], int] | None = None,
        unlink_relative: Callable[[int, str], None] | None = None,
        readiness: Callable[[UUID], ReadinessResult] | None = None,
        observe_running: Callable[[UUID], RunningKernelObservation] | None = None,
        cleanup_payloads: Callable[[int, ExternalBootActivationBinding], None] | None = None,
    ) -> None:
        self._pin_lease = pin_lease
        self._connect = connect
        self._open_artifact_root = open_artifact_root
        self._open_guest = open_guest
        self._stat_overlay = stat_overlay or _stat_identity
        self._close_descriptor = close_descriptor
        self._open_relative = open_relative or _open_relative
        self._unlink_relative = unlink_relative or _unlink_relative
        self._readiness = readiness or _unconfigured_readiness
        self._observe_running = observe_running or _unconfigured_observation
        self._cleanup_payloads = cleanup_payloads or _unconfigured_cleanup

    def open(self, lease: LocalExternalBootOperationLease) -> LocalExternalBootSession:
        ownership = self._pin_lease(lease)
        pin = ownership.pin
        system_id = ownership.system_id
        binding = ownership.binding
        if binding.system_id != str(system_id):
            pin.close()
            raise ValueError("operation lease binding does not own the System")
        connection: _Connection | None = None
        domain: _Domain | None = None
        artifact_fd: int | None = None
        try:
            connection = self._connect()
            expected_name = domain_name_for(system_id)
            domain = connection.lookupByName(expected_name)
            expected_overlay = overlay_path(system_id)
            xml = domain.XMLDesc(0)
            _parse_owned_xml(xml, system_id, expected_overlay)
            device, inode = self._stat_overlay(expected_overlay)
            overlay = _BoundOverlay(device, inode, expected_overlay)
            artifact_fd = self._open_artifact_root(ownership)
            return _ConcreteSession(
                system_id=system_id,
                binding=binding,
                pin=pin,
                connection=connection,
                domain=domain,
                artifact_fd=artifact_fd,
                overlay=overlay,
                open_guest=self._open_guest,
                stat_overlay=self._stat_overlay,
                close_descriptor=self._close_descriptor,
                open_relative=self._open_relative,
                unlink_relative=self._unlink_relative,
                readiness=self._readiness,
                observe_running=self._observe_running,
                cleanup_payloads=self._cleanup_payloads,
            )
        except BaseException as exc:
            errors: list[Exception] = []
            for closer in (
                (lambda: self._close_descriptor(artifact_fd)) if artifact_fd is not None else None,
                domain.free if domain is not None else None,
                connection.close if connection is not None else None,
                pin.close,
            ):
                if closer is not None:
                    try:
                        closer()
                    except Exception as close_error:
                        errors.append(close_error)
            for error in errors:
                exc.add_note(f"cleanup failed: {error!r}")
            raise


def _active(domain: _Domain) -> bool:
    value = domain.isActive()
    if value not in (0, 1):
        raise RuntimeError("libvirt returned an indeterminate domain state")
    return bool(value)


def _stat_identity(path: str) -> tuple[int, int]:
    value = os.stat(path, follow_symlinks=False)
    return value.st_dev, value.st_ino


def _relative_name(name: str) -> str:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("artifact name must be one canonical relative segment")
    return name


def _open_relative(root_fd: int, name: str, flags: int, mode: int) -> int:
    return os.open(name, flags | os.O_NOFOLLOW, mode, dir_fd=root_fd)


def _unlink_relative(root_fd: int, name: str) -> None:
    os.unlink(name, dir_fd=root_fd)


def _unconfigured_readiness(_system_id: UUID) -> ReadinessResult:
    raise RuntimeError("local external-boot readiness is not configured")


def _unconfigured_observation(_system_id: UUID) -> RunningKernelObservation:
    raise RuntimeError("local external-boot running observation is not configured")


def _unconfigured_cleanup(_root_fd: int, _binding: ExternalBootActivationBinding) -> None:
    raise RuntimeError("local external-boot payload cleanup is not configured")


def _parse_owned_xml(xml: str, system_id: UUID, expected_overlay: str) -> ET.Element:
    if unicodedata.normalize("NFC", xml) != xml:
        raise ValueError("domain XML must be NFC")
    try:
        root = _safe_fromstring(xml)
    except (ET.ParseError, DefusedXmlException) as exc:
        raise ValueError("domain XML is malformed or forbidden") from exc
    if root.tag != "domain" or root.findtext("name") != domain_name_for(system_id):
        raise ValueError("domain ownership does not match the operation lease")
    if root.findtext(f"metadata/{{{KDIVE_METADATA_NS}}}system") != str(system_id):
        raise ValueError("domain ownership metadata does not match the operation lease")
    disks = []
    for disk in root.findall("devices/disk"):
        source = disk.find("source")
        driver = disk.find("driver")
        if (
            disk.get("device") == "disk"
            and source is not None
            and source.get("file") == expected_overlay
            and driver is not None
            and driver.get("type") == "qcow2"
        ):
            disks.append(disk)
    if len(disks) != 1:
        raise ValueError("domain overlay ownership is absent or ambiguous")
    return root


def _digest(prefix: bytes, payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(prefix + b"\0" + payload).hexdigest()


def _preserved_identity(root: ET.Element) -> str:
    cloned = ET.fromstring(ET.tostring(root, encoding="unicode"))  # noqa: S314 - defused above
    os_element = cloned.find("os")
    if os_element is not None:
        for tag in ("kernel", "initrd", "cmdline"):
            element = os_element.find(tag)
            if element is not None:
                os_element.remove(element)
    for element in cloned.iter():
        if len(element) and element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
    canonical = ET.canonicalize(
        ET.tostring(cloned, encoding="unicode"),
        with_comments=False,
        strip_text=False,
        rewrite_prefixes=True,
    ).encode()
    return _digest(b"kdive-libvirt-preserved-v1", canonical)


def _boot_identity(root: ET.Element) -> str:
    os_element = root.find("os")
    value = {
        "cmdline": os_element.findtext("cmdline") if os_element is not None else None,
        "initrd": os_element.findtext("initrd") if os_element is not None else None,
        "kernel": os_element.findtext("kernel") if os_element is not None else None,
        "schema": "libvirt-boot-projection-v1",
    }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return _digest(b"kdive-libvirt-boot-projection-v1", payload)


def _attempt_guest_close(guest: _Guest) -> list[Exception]:
    errors: list[Exception] = []
    for closer in (guest.shutdown, guest.close):
        try:
            closer()
        except Exception as exc:
            errors.append(exc)
    return errors

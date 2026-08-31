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
from typing import Protocol, cast
from uuid import UUID

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.providers.local_libvirt.lifecycle.storage import overlay_path
from kdive.providers.ports.external_boot import ExternalBootActivationBinding
from kdive.providers.shared.libvirt_xml import KDIVE_METADATA_NS
from kdive.providers.shared.runtime_paths import domain_name_for


@dataclass(frozen=True)
class OverlayIdentity:
    """Stable identity of the expected System overlay, with no public path accessor."""

    device: int
    inode: int
    canonical_path: str


@dataclass(frozen=True)
class ClosedDomainInspection:
    """Immutable facts derived from one persistent/inactive domain definition."""

    xml: bytes
    active: bool
    definition_identity: str
    source_boot_identity: str
    domain_name: str
    overlay: OverlayIdentity


class _LeasePin:
    def __init__(self, lease: LocalExternalBootOperationLease) -> None:
        self._lease: LocalExternalBootOperationLease | None = lease

    def close(self) -> None:
        lease, self._lease = self._lease, None
        if lease is not None:
            lease._unpin(self)


class LocalExternalBootOperationLease:
    """Nominal ownership capability held while the caller owns the System lane."""

    def __init__(
        self,
        system_id: UUID,
        binding: ExternalBootActivationBinding,
        *,
        events: list[str] | None = None,
    ) -> None:
        if binding.system_id != str(system_id):
            raise ValueError("operation lease binding does not own the System")
        self.system_id = system_id
        self.binding = binding
        self._released = False
        self._pins: set[_LeasePin] = set()
        self._events = events

    @property
    def released(self) -> bool:
        return self._released

    def pin(self) -> _LeasePin:
        if self._released:
            raise RuntimeError("operation lease is released")
        pin = _LeasePin(self)
        self._pins.add(pin)
        if self._events is not None:
            self._events.append("pin.open")
        return pin

    def release(self) -> None:
        if self._pins:
            raise RuntimeError("operation lease is pinned")
        self._released = True

    def _unpin(self, pin: _LeasePin) -> None:
        self._pins.discard(pin)
        if self._events is not None:
            self._events.append("pin.close")


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


class LocalExternalBootSession(Protocol):
    def inspect_closed(self) -> ClosedDomainInspection: ...
    def require_inactive(self) -> None: ...
    def stop_and_require_inactive(self) -> None: ...
    def artifact_root_descriptor(self) -> int: ...
    def guest(self) -> AbstractContextManager[_Guest]: ...
    def define_xml(self, xml: str) -> None: ...
    def start(self) -> None: ...
    def close(self) -> None: ...


class _GuestContext(AbstractContextManager[_Guest]):
    def __init__(self, session: _ConcreteSession) -> None:
        self._session = session
        self._guest: _Guest | None = None
        self._closed = False

    def __enter__(self) -> _Guest:
        if self._closed:
            raise RuntimeError("guest wrapper is closed")
        self._guest = self._session._open_guest_context(self)
        return cast("_Guest", _GuardedGuest(self, self._guest))

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

    def __getattr__(self, name: str) -> object:
        if self._owner._closed:
            raise RuntimeError("guest wrapper is closed")
        return getattr(self._guest, name)


class _ConcreteSession:
    def __init__(
        self,
        *,
        lease: LocalExternalBootOperationLease,
        pin: _LeasePin,
        connection: _Connection,
        domain: _Domain,
        artifact_fd: int,
        overlay: OverlayIdentity,
        open_guest: Callable[[], _Guest],
        stat_overlay: Callable[[str], tuple[int, int]],
        close_descriptor: Callable[[int], None],
    ) -> None:
        self._lease = lease
        self._pin: _LeasePin | None = pin
        self._connection: _Connection | None = connection
        self._domain: _Domain | None = domain
        self._artifact_fd: int | None = artifact_fd
        self._overlay = overlay
        self._open_guest = open_guest
        self._stat_overlay = stat_overlay
        self._close_descriptor = close_descriptor
        self._guests: set[_GuestContext] = set()
        self._closed = False

    def inspect_closed(self) -> ClosedDomainInspection:
        domain = self._require_open_domain()
        xml = domain.XMLDesc(0)
        root = _parse_owned_xml(xml, self._lease.system_id, self._overlay.canonical_path)
        active = _active(domain)
        return ClosedDomainInspection(
            xml=xml.encode(),
            active=active,
            definition_identity=_preserved_identity(root),
            source_boot_identity=_boot_identity(root),
            domain_name=domain_name_for(self._lease.system_id),
            overlay=self._overlay,
        )

    def require_inactive(self) -> None:
        if _active(self._require_open_domain()):
            raise RuntimeError("domain must be inactive before overlay mutation")

    def stop_and_require_inactive(self) -> None:
        domain = self._require_open_domain()
        if _active(domain):
            domain.destroy()
        self.require_inactive()

    def artifact_root_descriptor(self) -> int:
        self._require_open_domain()
        assert self._artifact_fd is not None
        return self._artifact_fd

    def guest(self) -> _GuestContext:
        self._require_open_domain()
        return _GuestContext(self)

    def define_xml(self, xml: str) -> None:
        self.require_inactive()
        _parse_owned_xml(xml, self._lease.system_id, self._overlay.canonical_path)
        assert self._connection is not None
        self._domain = self._connection.defineXML(xml)

    def start(self) -> None:
        self._require_open_domain().create()

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
        self.require_inactive()
        if self._stat_overlay(self._overlay.canonical_path) != (
            self._overlay.device,
            self._overlay.inode,
        ):
            raise ValueError("System overlay changed before guest open")
        guest = self._open_guest()
        try:
            guest.add_drive_opts(self._overlay.canonical_path, format="qcow2")
            guest.launch()
        except BaseException as exc:
            for close_error in _attempt_guest_close(guest):
                exc.add_note(f"cleanup failed: {close_error!r}")
            raise
        self._guests.add(wrapper)
        return guest

    def _discard_guest(self, guest: _GuestContext) -> None:
        self._guests.discard(guest)

    def _require_open_domain(self) -> _Domain:
        if self._closed or self._domain is None:
            raise RuntimeError("local external-boot session is closed")
        return self._domain


class LocalExternalBootSessionFactory:
    def __init__(
        self,
        *,
        connect: Callable[[], _Connection],
        open_artifact_root: Callable[[LocalExternalBootOperationLease], int],
        open_guest: Callable[[], _Guest],
        stat_overlay: Callable[[str], tuple[int, int]] | None = None,
        close_descriptor: Callable[[int], None] = os.close,
    ) -> None:
        self._connect = connect
        self._open_artifact_root = open_artifact_root
        self._open_guest = open_guest
        self._stat_overlay = stat_overlay or _stat_identity
        self._close_descriptor = close_descriptor

    def open(self, lease: LocalExternalBootOperationLease) -> LocalExternalBootSession:
        if type(lease) is not LocalExternalBootOperationLease:
            raise TypeError("a local external-boot operation lease is required")
        if lease.released:
            raise RuntimeError("operation lease is released")
        pin = lease.pin()
        connection: _Connection | None = None
        domain: _Domain | None = None
        artifact_fd: int | None = None
        try:
            connection = self._connect()
            expected_name = domain_name_for(lease.system_id)
            domain = connection.lookupByName(expected_name)
            expected_overlay = overlay_path(lease.system_id)
            xml = domain.XMLDesc(0)
            _parse_owned_xml(xml, lease.system_id, expected_overlay)
            device, inode = self._stat_overlay(expected_overlay)
            overlay = OverlayIdentity(device, inode, expected_overlay)
            artifact_fd = self._open_artifact_root(lease)
            return _ConcreteSession(
                lease=lease,
                pin=pin,
                connection=connection,
                domain=domain,
                artifact_fd=artifact_fd,
                overlay=overlay,
                open_guest=self._open_guest,
                stat_overlay=self._stat_overlay,
                close_descriptor=self._close_descriptor,
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

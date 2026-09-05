"""Remote-libvirt control/capture test doubles (duplicated, no shared layer — ADR-0076).

The storage double renders ``XMLDesc`` from a modelled field set rather than echoing the
submitted document, so it discards exactly what libvirt discards for a dir-pool volume
(#2164). A double that returns its input agrees with the caller instead of the platform: an
implementation writing run ownership into a volume ``<metadata>`` child read it straight back
out under a green suite, and libvirt does not persist that element.

ADR-0076 still governs the *package* boundary above — these doubles are not shared with
``local_libvirt``. The storage double is shared only among the modules under
``tests/providers/remote_libvirt/``, which ADR-0076 treats as one package.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass

import libvirt

from kdive.artifacts.storage import ArtifactWriteRequest, FetchedArtifact, StoredArtifact
from kdive.domain.catalog.artifacts import Sensitivity
from tests.providers.remote_libvirt.conftest import libvirt_error


class FakeObjectStore:
    """An in-memory object store for remote-libvirt console tests."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_artifact(self, request: ArtifactWriteRequest) -> StoredArtifact:
        key = request.key()
        self.objects[key] = request.data
        return StoredArtifact(
            key,
            f"etag-{len(self.objects)}",
            request.sensitivity,
            "console",
            version_id="test-version",
        )

    def get_artifact(self, key: str, etag: str | None) -> FetchedArtifact:
        del etag
        return FetchedArtifact(self.objects[key], Sensitivity.REDACTED, "console")

    def list_prefix(self, prefix: str) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]


class FakeDomain:
    """The domain slice the remote Control plane drives, recording calls."""

    def __init__(self, name: str, *, raise_on: dict[str, int] | None = None) -> None:
        self._name = name
        self._raise_on = raise_on or {}
        self.calls: list[str] = []

    def name(self) -> str:  # noqa: N802 - libvirt binding name
        return self._name

    def _maybe_raise(self, call: str) -> None:
        if call in self._raise_on:
            raise libvirt_error(self._raise_on[call])

    def create(self) -> int:
        self.calls.append("create")
        self._maybe_raise("create")
        return 0

    def destroy(self) -> int:
        self.calls.append("destroy")
        self._maybe_raise("destroy")
        return 0

    def reset(self, flags: int) -> int:
        self.calls.append("reset")
        self._maybe_raise("reset")
        return 0

    def reboot(self, flags: int) -> int:
        self.calls.append("reboot")
        self._maybe_raise("reboot")
        return 0

    def injectNMI(self, flags: int) -> int:  # noqa: N802 - libvirt binding name
        self.calls.append("injectNMI")
        self._maybe_raise("injectNMI")
        return 0

    def sendKey(  # noqa: N802 - libvirt binding name
        self, codeset: int, holdtime: int, keycodes: list[int], nkeycodes: int, flags: int
    ) -> int:
        self.calls.append(f"sendKey:{codeset}:{holdtime}:{keycodes}:{nkeycodes}:{flags}")
        self._maybe_raise("sendKey")
        return 0

    def qemuMonitorCommand(self, cmd: str, flags: int) -> str:  # noqa: N802 - libvirt binding name
        self.calls.append(f"monitor:{cmd}")
        self._maybe_raise("qemuMonitorCommand")
        return ""


class FakeControlConn:
    """A libvirt connection slice with lookupByName + close for the control fakes."""

    def __init__(self, lookup: dict[str, FakeDomain]) -> None:
        self._lookup = lookup
        self.closed = False

    def lookupByName(self, name: str) -> FakeDomain:  # noqa: N802 - libvirt binding name
        try:
            return self._lookup[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_DOMAIN) from exc

    def close(self) -> None:
        self.closed = True


# Read off real libvirt 12.0.0: every multiplier below came from a readback, none was inferred.
# Keys are lower-cased and lookup lower-cases the submitted suffix, because libvirt matches
# case-insensitively. An absent or empty unit means bytes and never reaches this table.
CAPACITY_SUFFIXES: dict[str, int] = {
    "b": 1,
    "byte": 1,
    "bytes": 1,
    "k": 1024,
    "kib": 1024,
    "kb": 1000,
    "m": 1048576,
    "mib": 1048576,
    "mb": 1000000,
    "g": 2**30,
    "gib": 2**30,
    "gb": 10**9,
    "t": 2**40,
    "tib": 2**40,
    "tb": 10**12,
    "p": 2**50,
    "pib": 2**50,
    "pb": 10**15,
    "e": 2**60,
    "eib": 2**60,
    "eb": 10**18,
}


@dataclass(frozen=True, slots=True)
class VolumeState:
    """The whole of what the double models. The submitted document is not part of it."""

    name: str
    capacity_bytes: int
    format_type: str
    mode: str
    path: str
    backing_path: str | None
    backing_format: str | None


def _render_branch(parent: ET.Element, tag: str, path: str, format_type: str, mode: str) -> None:
    """Render a target or backingStore branch. Permissions and timestamps are placeholders."""
    branch = ET.SubElement(parent, tag)
    ET.SubElement(branch, "path").text = path
    ET.SubElement(branch, "format", type=format_type)
    permissions = ET.SubElement(branch, "permissions")
    ET.SubElement(permissions, "mode").text = mode
    ET.SubElement(permissions, "owner").text = "0"
    ET.SubElement(permissions, "group").text = "0"
    # Real libvirt replaces a submitted label with the host's own; a double cannot know it.
    ET.SubElement(permissions, "label").text = ""
    timestamps = ET.SubElement(branch, "timestamps")
    for field in ("atime", "mtime", "ctime", "btime"):
        ET.SubElement(timestamps, field).text = "0"


class FakeStorageVolume:
    """A volume double that renders its readback from frozen state, never from the request."""

    def __init__(self, state: VolumeState, pool: FakeStoragePool) -> None:
        self._state = state
        self._pool = pool
        self._data = b""

    def name(self) -> str:
        return self._state.name

    def key(self) -> str:
        return self._state.path

    def path(self) -> str:
        return self._state.path

    def info(self) -> list[int]:
        # Real libvirt answers info()[2] with the allocation, which is a host fact.
        return [0, self._state.capacity_bytes, 0]

    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del flags
        if not isinstance(stream, FakeStorageStream) or offset != 0 or length < 0:
            raise libvirt_error(libvirt.VIR_ERR_INVALID_ARG)
        stream.begin_upload(self)
        return 0

    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        del length, flags
        if not isinstance(stream, FakeStorageStream) or offset != 0:
            raise libvirt_error(libvirt.VIR_ERR_INVALID_ARG)
        stream.begin_download(self._data)
        return 0

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802 - libvirt binding name
        del flags
        state = self._state
        # The dir-pool backend decides the type; a submitted 'block' is overridden.
        volume = ET.Element("volume", type="file")
        ET.SubElement(volume, "name").text = state.name
        ET.SubElement(volume, "key").text = state.path
        ET.SubElement(volume, "capacity", unit="bytes").text = str(state.capacity_bytes)
        # Allocation and physical are facts about the file on disk, so they are placeholders
        # rather than a plausible-looking derivation from capacity.
        ET.SubElement(volume, "allocation", unit="bytes").text = "0"
        ET.SubElement(volume, "physical", unit="bytes").text = "0"
        _render_branch(volume, "target", state.path, state.format_type, state.mode)
        if state.backing_path is not None:
            _render_branch(
                volume, "backingStore", state.backing_path, state.backing_format or "raw", "0600"
            )
        return ET.tostring(volume, encoding="unicode")

    def delete(self, flags: int = 0) -> int:
        del flags
        self._pool._remove(self._state.name)
        return 0


class FakeStoragePool:
    """A dir pool double that derives what libvirt derives and refuses what libvirt refuses."""

    def __init__(
        self, *, name: str = "default", target_path: str = "/var/lib/libvirt/images"
    ) -> None:
        self._name = name
        self._target_path = target_path
        self._volumes: dict[str, FakeStorageVolume] = {}
        # The request is a separate question from what the platform keeps; tests that assert on
        # what the provider sent read this, never the readback.
        self.created_xml: list[str] = []

    def name(self) -> str:
        return self._name

    def _parse_capacity(self, root: ET.Element) -> int:
        capacity = root.find("capacity")
        if capacity is None:
            raise libvirt_error(libvirt.VIR_ERR_XML_ERROR)
        text = (capacity.text or "").strip()
        if not text.isdigit():
            raise libvirt_error(libvirt.VIR_ERR_XML_ERROR)
        unit = capacity.get("unit")
        if not unit:
            # An absent or empty unit means bytes. render_volume_xml emits exactly this, so
            # refusing it here would refuse the provider's own document.
            return int(text)
        try:
            multiplier = CAPACITY_SUFFIXES[unit.lower()]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_INVALID_ARG) from exc
        return int(text) * multiplier

    def createXML(self, xml: str, flags: int = 0) -> FakeStorageVolume:  # noqa: N802 - libvirt name
        del flags
        root = ET.fromstring(xml)
        name_element = root.find("name")
        name = "" if name_element is None else (name_element.text or "").strip()
        if not name:
            raise libvirt_error(libvirt.VIR_ERR_XML_ERROR)
        capacity_bytes = self._parse_capacity(root)
        # Every refusal lands before the volume map is touched, so a rejected create leaves the
        # pool unchanged. ensure_named_overlay guards on this one and maps it to
        # PROVISIONING_FAILURE, so a double that silently replaced would make that path untestable.
        if name in self._volumes:
            raise libvirt_error(libvirt.VIR_ERR_STORAGE_VOL_EXIST)
        format_element = root.find("target/format")
        mode_element = root.find("target/permissions/mode")
        backing_path_element = root.find("backingStore/path")
        backing_format_element = root.find("backingStore/format")
        state = VolumeState(
            name=name,
            capacity_bytes=capacity_bytes,
            format_type="raw" if format_element is None else format_element.get("type", "raw"),
            mode="0600" if mode_element is None else (mode_element.text or "0600").strip(),
            path=f"{self._target_path}/{name}",
            backing_path=None
            if backing_path_element is None
            else (backing_path_element.text or "").strip(),
            backing_format=None
            if backing_format_element is None
            else backing_format_element.get("type", "raw"),
        )
        volume = FakeStorageVolume(state, self)
        self._volumes[name] = volume
        self.created_xml.append(xml)
        return volume

    def createXMLFrom(  # noqa: N802 - libvirt binding name
        self, xml: str, volume: object, flags: int = 0
    ) -> FakeStorageVolume:
        del flags
        if not isinstance(volume, FakeStorageVolume):
            raise libvirt_error(libvirt.VIR_ERR_INVALID_ARG)
        clone = self.createXML(xml)
        clone._data = volume._data
        return clone

    def storageVolLookupByName(self, name: str) -> FakeStorageVolume:  # noqa: N802 - libvirt name
        try:
            return self._volumes[name]
        except KeyError as exc:
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_VOL) from exc

    def refresh(self, flags: int = 0) -> int:
        del flags
        return 0

    def listVolumes(self) -> list[str]:  # noqa: N802 - libvirt binding name
        return list(self._volumes)

    def listAllVolumes(self, flags: int = 0) -> list[FakeStorageVolume]:  # noqa: N802
        del flags
        return list(self._volumes.values())

    def _remove(self, name: str) -> None:
        self._volumes.pop(name, None)


class FakeStorageStream:
    """A libvirt stream double that commits uploads only when ``finish`` succeeds."""

    def __init__(self) -> None:
        self._upload_target: FakeStorageVolume | None = None
        self._download_data: bytes | None = None
        self._sent = bytearray()
        self._aborted = False

    def begin_upload(self, volume: FakeStorageVolume) -> None:
        self._upload_target = volume

    def begin_download(self, data: bytes) -> None:
        self._download_data = data

    def sendAll(  # noqa: N802 - libvirt binding name
        self, callback: Callable[[object, int, object], bytes], opaque: object
    ) -> None:
        while chunk := callback(self, 1 << 20, opaque):
            self._sent.extend(chunk)

    def recvAll(  # noqa: N802 - libvirt binding name
        self, callback: Callable[[object, bytes, object], None], opaque: object
    ) -> None:
        if self._download_data:
            callback(self, self._download_data, opaque)

    def finish(self) -> int:
        if self._aborted:
            raise libvirt_error(libvirt.VIR_ERR_OPERATION_INVALID)
        if self._upload_target is not None:
            self._upload_target._data = bytes(self._sent)
        return 0

    def abort(self) -> int:
        self._aborted = True
        self._sent.clear()
        return 0


class FakeStorageConn:
    """A connection double exposing one named storage pool and fresh streams."""

    def __init__(self, pool: FakeStoragePool) -> None:
        self._pool = pool

    def storagePoolLookupByName(self, name: str) -> FakeStoragePool:  # noqa: N802
        if name != self._pool.name():
            raise libvirt_error(libvirt.VIR_ERR_NO_STORAGE_POOL)
        return self._pool

    def newStream(self, flags: int = 0) -> FakeStorageStream:  # noqa: N802
        del flags
        return FakeStorageStream()

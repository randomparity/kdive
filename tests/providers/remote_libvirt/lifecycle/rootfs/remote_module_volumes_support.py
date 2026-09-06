"""Attempt-scoped remote module volume ownership."""

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import libvirt

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    BuiltSourceImage,
    ModuleTreeEntry,
    SourceFilesystemEvidence,
    VolumeRequest,
)


class Writer:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls = 0
        self.fail_inspect = False

    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
        self.calls += 1
        self.path.write_bytes(b"source-image")
        return BuiltSourceImage(
            path=self.path,
            capacity_bytes=len(b"source-image"),
            evidence=SourceFilesystemEvidence(
                operation=operation,
                manifest="sha256:" + "d" * 64,
                entry_count=len(entries),
                content_bytes=sum(len(entry.content or b"") for entry in entries),
            ),
        )

    def inspect(self, path: Path) -> SourceFilesystemEvidence:
        if self.fail_inspect:
            raise CategorizedError("unreadable ext4", category=ErrorCategory.BUILD_FAILURE)
        if path.read_bytes() != b"source-image":
            return SourceFilesystemEvidence(b"corrupt", "sha256:" + "f" * 64, 0, 0)
        return SourceFilesystemEvidence(
            operation=OPERATION.to_wire_bytes(),
            manifest="sha256:" + "d" * 64,
            entry_count=1,
            content_bytes=3,
        )


class Volume:
    def __init__(self, xml: str, capacity: int, name: str = "volume") -> None:
        self.xml = xml
        self.capacity = capacity
        self.deleted = False
        self.payload = bytearray()
        self.fail_metadata_read = False
        self.backing_path = f"/var/lib/libvirt/images/{name}"
        self.fail_path = False

    def path(self) -> str:
        if self.fail_path:
            import libvirt

            raise libvirt.libvirtError("path transport failed")
        return self.backing_path

    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        if self.fail_metadata_read:
            import libvirt

            raise libvirt.libvirtError("metadata transport failed")
        return self.xml

    def info(self) -> list[int]:
        return [0, self.capacity, len(self.payload)]

    def upload(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        cast(Stream, stream).volume = self
        return 0

    def download(self, stream: object, offset: int, length: int, flags: int = 0) -> int:
        typed_stream = cast(Stream, stream)
        if typed_stream.fail_download_stage == "download":
            import libvirt

            raise libvirt.libvirtError("download transport failed")
        typed_stream.download_payload = bytes(self.payload)
        return 0

    def delete(self, flags: int = 0) -> int:
        self.deleted = True
        return 0


class Pool:
    def __init__(self) -> None:
        self.volumes: dict[str, Volume] = {}
        self.fail_on_scratch = False

    def storageVolLookupByName(self, name: str) -> Volume:  # noqa: N802

        if name not in self.volumes or self.volumes[name].deleted:
            error = libvirt.libvirtError("missing")
            error.err = [libvirt.VIR_ERR_NO_STORAGE_VOL] + [None] * 8
            raise error
        return self.volumes[name]

    def createXML(self, xml: str, flags: int = 0) -> Volume:  # noqa: N802
        import xml.etree.ElementTree as ET

        root = ET.fromstring(xml)
        name = root.findtext("name")
        assert name is not None
        if self.fail_on_scratch and "scratch.ext4" in name:
            raise RuntimeError("scratch create failed")
        volume = Volume(xml, int(root.findtext("capacity") or 0), name)
        self.volumes[name] = volume
        return volume


class Stream:
    volume: Volume
    download_payload: bytes = b""
    fail_download_stage: str | None = None

    def sendAll(  # noqa: N802
        self, handler: Callable[[object, int, object], bytes], opaque: object
    ) -> None:
        while chunk := handler(self, 1024, opaque):
            self.volume.payload.extend(chunk)

    def finish(self) -> int:
        if self.fail_download_stage == "finish":
            import libvirt

            raise libvirt.libvirtError("finish transport failed")
        return 0

    def recvAll(  # noqa: N802
        self, handler: Callable[[object, bytes, object], None], opaque: object
    ) -> None:
        if self.fail_download_stage == "recvAll":
            import libvirt

            raise libvirt.libvirtError("receive transport failed")
        handler(self, self.download_payload, opaque)

    def abort(self) -> int:
        return 0


class Conn:
    def __init__(self) -> None:
        self.pool = Pool()
        self.fail_pool_lookup = False
        self.fail_new_stream = False
        self.fail_download_stage: str | None = None

    def storagePoolLookupByName(self, name: str) -> Pool:  # noqa: N802
        if self.fail_pool_lookup:
            import libvirt

            raise libvirt.libvirtError("pool transport failed")
        return self.pool

    def newStream(self, flags: int = 0) -> Stream:  # noqa: N802
        if self.fail_new_stream:
            import libvirt

            raise libvirt.libvirtError("stream transport failed")
        stream = Stream()
        stream.fail_download_stage = self.fail_download_stage
        return stream


OPERATION = RemoteModuleOperationV1.model_validate(
    {
        "protocol": "remote-module-operation-v1",
        "operation": "capture_install",
        "system_id": "00000000-0000-4000-8000-000000000001",
        "run_id": "00000000-0000-4000-8000-000000000002",
        "plan_identity": "sha256:" + "a" * 64,
        "operation_nonce": "a" * 32,
        "release": "6.12.0-kdive",
        "root_volume": {"key": "root-1", "identity": "sha256:" + "c" * 64},
        "source_manifest": "sha256:" + "d" * 64,
        "appliance_image_digest": "sha256:" + "e" * 64,
    }
)


def request(tmp_path: Path, **changes: object) -> VolumeRequest:
    values: dict[str, object] = {
        "pool": "systems",
        "system_id": "00000000-0000-4000-8000-000000000001",
        "run_id": "00000000-0000-4000-8000-000000000002",
        "operation_nonce": "a" * 32,
        "operation": OPERATION,
        "source_manifest": "sha256:" + "d" * 64,
        "entries": (ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),),
        "writer": Writer(tmp_path / "source.ext4"),
        "inspect_attachments": lambda: AttachmentInspection(True, True, False, frozenset()),
        "work_dir": tmp_path,
    }
    values.update(changes)
    return VolumeRequest(**cast(Any, values))


def _duplicate_name(volume: Volume) -> None:
    import xml.etree.ElementTree as ET

    root = ET.fromstring(volume.xml)
    name = root.find("./name")
    assert name is not None
    ET.SubElement(root, "name").text = name.text
    volume.xml = ET.tostring(root, encoding="unicode")


class UniqueTrackingWriter(Writer):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory / "unused")
        self.directory = directory
        self.paths: list[Path] = []

    def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
        path = self.directory / f"unique-{len(self.paths)}.ext4"
        self.path = path
        self.paths.append(path)
        return super().build(operation, entries)


SOURCE_NAME = (
    "kdive-module-00000000-0000-4000-8000-000000000001-"
    "00000000-0000-4000-8000-000000000002-" + "a" * 32 + "-source.ext4"
)


def _detached_source() -> AttachmentInspection:
    return AttachmentInspection(True, True, False, frozenset({("systems", SOURCE_NAME)}))


def _kill_upload(conn: Conn) -> Callable[[], None]:
    """Model worker death partway through the source upload; returns the revival."""
    original = conn.newStream

    def dying(flags: int = 0) -> Stream:
        stream = original(flags)
        cast(Any, stream).sendAll = _die
        return stream

    cast(Any, conn).newStream = dying
    return lambda: cast(Any, conn).__dict__.pop("newStream", None)


def _die(*_args: object, **_kwargs: object) -> None:
    raise SystemExit("worker died mid-upload")

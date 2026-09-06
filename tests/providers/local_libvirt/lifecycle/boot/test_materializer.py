"""Concrete local external-boot materialization through exact object versions."""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import struct
import tarfile
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.build_artifacts import validation
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    RealLocalExternalBootMaterializer,
    TargetProjectionStore,
    TargetProjectionV1,
)
from kdive.providers.local_libvirt.lifecycle.boot.session import (
    ClosedDomainInspection,
    LocalExternalBootSession,
    OverlayIdentity,
    _boot_identity,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPlan,
    OpaqueProviderRef,
)
from kdive.store.objectstore import ObjectStore
from tests.providers.local_libvirt.external_boot_support import _SOURCE_XML

_SYSTEM = "11111111-1111-1111-1111-111111111111"
_RUN = "22222222-2222-2222-2222-222222222222"
_ACTIVATION = "33333333-3333-3333-3333-333333333333"


def _boot_elf() -> bytes:
    body = bytearray(0x40)
    body[0:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", body, 0x12, 62)
    struct.pack_into("<Q", body, 0x20, 64)
    struct.pack_into("<H", body, 0x36, 56)
    struct.pack_into("<H", body, 0x38, 2)
    note = struct.pack("<III", 4, 4, 3) + b"GNU\x00" + bytes.fromhex("deadbeef")
    banner = b"Linux version 6.9.0 test\x00"
    note_offset = 64 + 112
    note_header = bytearray(56)
    struct.pack_into("<I", note_header, 0, 4)
    struct.pack_into("<Q", note_header, 8, note_offset)
    struct.pack_into("<Q", note_header, 32, len(note))
    load_header = bytearray(56)
    struct.pack_into("<I", load_header, 0, 1)
    struct.pack_into("<Q", load_header, 8, note_offset + len(note))
    struct.pack_into("<Q", load_header, 32, len(banner))
    return bytes(body + note_header + load_header) + note + banner


def _bundle() -> bytes:
    header = bytearray(0x400)
    header[0x202:0x206] = b"HdrS"
    struct.pack_into("<H", header, 0x20E, 0x100)
    header[0x300:0x306] = b"6.9.0\x00"
    boot = bytes(header) + gzip.compress(_boot_elf())
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as archive:
        for name, data in (
            ("boot/vmlinuz", boot),
            ("lib/modules/6.9.0/modules.dep", b""),
            ("lib/modules/6.9.0/kernel/foo.ko", b"\x7fELFmod"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return result.getvalue()


class _RangeStore:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def get_range(
        self, _key: str, *, start: int, length: int, version_id: str | None = None
    ) -> bytes:
        del version_id
        return self.data[start : start + length]


class _Body(io.BytesIO):
    closed_by_store = False

    def close(self) -> None:
        self.closed_by_store = True
        super().close()


class _Client:
    def __init__(self, objects: dict[tuple[str, str], bytes]) -> None:
        self.objects = objects
        self.requests: list[tuple[str, str]] = []
        self.bodies: list[_Body] = []

    def get_object(self, **request: object) -> dict[str, object]:
        key = cast(str, request["Key"])
        version = cast(str, request["VersionId"])
        self.requests.append((key, version))
        body = _Body(self.objects[(key, version)])
        self.bodies.append(body)
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "build"},
            "Body": body,
        }


class _Session:
    def __init__(self, root: Path) -> None:
        self.binding = ExternalBootActivationBinding(
            system_id=_SYSTEM, run_id=_RUN, activation_id=_ACTIVATION
        )
        self.root = root
        self.root.mkdir(mode=0o700)

    @contextmanager
    def projection_directory(self, projection: TargetProjectionV1):
        assert projection.ownership.system_id == self.binding.system_id
        digest = projection.digest.removeprefix("sha256:")
        directory = self.root / digest
        directory.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield descriptor
        finally:
            os.close(descriptor)

    def reopen_projection(self, artifact: object) -> TargetProjectionV1:
        reference = OpaqueProviderRef.model_validate(artifact)
        digest = reference.ref.split("/")[4]
        descriptor = os.open(self.root / digest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            data = (self.root / digest / "target-projection.json").read_bytes()
            projection = TargetProjectionV1.model_validate_json(data)
            return TargetProjectionStore.reopen_at(descriptor, projection)
        finally:
            os.close(descriptor)

    def projection_artifact_path(self, projection: TargetProjectionV1, name: str) -> str:
        return str(self.root / projection.digest.removeprefix("sha256:") / name)

    def boot_identity(self, xml: str) -> str:
        return _boot_identity(_safe_fromstring(xml))


def _plan(bundle: bytes) -> ExternalBootPlan:
    evidence = validation._scan_external_boot_archive(  # noqa: SLF001
        cast(validation.ValidatorStore, _RangeStore(bundle)), "bundle", len(bundle), "x86_64"
    )
    return ExternalBootPlan.model_validate(
        {
            "architecture": "x86_64",
            "ownership": {
                "system_id": _SYSTEM,
                "run_id": _RUN,
                "build_generation": "44444444-4444-4444-4444-444444444444",
            },
            "bundle": {
                "key": "build/kernel",
                "version": "kernel-v1",
                "sha256": "sha256:" + hashlib.sha256(bundle).hexdigest(),
                "vmlinuz_sha256": evidence["vmlinuz_sha256"],
                "member_count": evidence["archive_member_count"],
                "uncompressed_bytes": evidence["archive_uncompressed_bytes"],
                "vmlinuz_size_bytes": evidence["vmlinuz_size_bytes"],
                "decoded_kernel_size_bytes": evidence["decoded_kernel_size_bytes"],
                "elf_metadata_bytes": evidence["elf_metadata_bytes"],
                "gnu_build_id_size_bytes": evidence["gnu_build_id_size_bytes"],
            },
            "initrd": None,
            "cmdline": "root=UUID=x",
            "debug_cmdline": None,
            "platform_arguments": ["root=UUID=x"],
            "module_obligation": {
                "mode": "system-root-tree",
                "release": evidence["release"],
                "source_manifest": evidence["module_source_manifest"],
                "member_count": evidence["module_member_count"],
                "uncompressed_bytes": evidence["module_uncompressed_bytes"],
            },
            "root": {
                "architecture": "x86_64",
                "root": "UUID=x",
                "arguments": ["root=UUID=x"],
                "authority": "stage-inspection",
                "source": {"kind": "staged-image", "identity": "sha256:" + "a" * 64},
            },
        }
    )


def test_materialize_streams_exact_version_publishes_last_and_retries(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")

    first = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    second = materializer.materialize(plan, cast(LocalExternalBootSession, session))

    assert second == first
    digest_dir = session.root / first.artifacts.kernel.ref.split("/")[4]
    assert sorted(path.name for path in digest_dir.iterdir()) == [
        "kernel",
        "modules",
        "target-projection.json",
    ]
    assert all(body.closed_by_store for body in client.bodies)
    assert set(client.requests) == {("build/kernel", "kernel-v1")}


def test_inspect_prepare_uses_reopened_bytes_and_preserves_source(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    result = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    inspection = ClosedDomainInspection(
        xml=_SOURCE_XML.encode(),
        active=True,
        definition_identity="sha256:" + "b" * 64,
        source_boot_identity="sha256:" + "c" * 64,
        domain_name="kdive-test",
        overlay=OverlayIdentity(1, 2),
    )

    intent = materializer.inspect_prepare(
        result, session.binding, inspection, cast(LocalExternalBootSession, session)
    )

    assert intent.prior_power == "running"
    assert intent.source_xml == _SOURCE_XML
    assert intent.expected_running == result.kernel_observation
    assert intent.materialized_modules_bytes > 0


def test_exact_retry_rejects_changed_source_digest_without_replacing(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    result = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    kernel = Path(
        session.projection_artifact_path(
            session.reopen_projection(result.artifacts.kernel), "kernel"
        )
    )
    kernel.write_bytes(b"changed")

    with pytest.raises(ValueError, match="kernel bytes"):
        materializer.materialize(plan, cast(LocalExternalBootSession, session))
    assert kernel.read_bytes() == b"changed"


def test_materialize_rejects_changed_manifest_before_projection_commit(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    plan = plan.model_copy(
        update={
            "module_obligation": plan.module_obligation.model_copy(
                update={"source_manifest": "sha256:" + "f" * 64}
            )
        }
    )
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")

    with pytest.raises(ValueError, match="evidence"):
        materializer.materialize(plan, cast(LocalExternalBootSession, session))

    assert not list(session.root.rglob("target-projection.json"))
    assert all(body.closed_by_store for body in client.bodies)

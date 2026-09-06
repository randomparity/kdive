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
from typing import BinaryIO, cast

import pytest
from botocore.exceptions import ReadTimeoutError
from defusedxml.ElementTree import fromstring as _safe_fromstring

from kdive.build_artifacts import validation
from kdive.providers.local_libvirt.lifecycle.boot import external_boot as external_boot_module
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


def _bundle(*, module_data: bytes = b"\x7fELFmod") -> bytes:
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
            ("lib/modules/6.9.0/kernel/foo.ko", module_data),
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
        self.root.mkdir(mode=0o700, exist_ok=True)

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

    def reopen_projection(self, artifact: OpaqueProviderRef) -> TargetProjectionV1:
        digest = artifact.ref.split("/")[4]
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


def _projection_for(plan: ExternalBootPlan) -> TargetProjectionV1:
    return TargetProjectionV1(
        ownership={"system_id": _SYSTEM, "run_id": _RUN},
        activation_id=_ACTIVATION,
        plan_identity=plan.identity,
        architecture=plan.architecture,
        cmdline=plan.cmdline,
        initrd_filename=None,
    )


def test_materialize_streams_exact_version_publishes_last_and_retries(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")

    first = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    restarted = _Session(session.root)
    second = materializer.materialize(plan, cast(LocalExternalBootSession, restarted))

    assert second == first
    digest_dir = session.root / first.artifacts.kernel.ref.split("/")[4]
    assert sorted(path.name for path in digest_dir.iterdir()) == [
        "kernel",
        "modules",
        "target-projection.json",
    ]
    assert all(body.closed_by_store for body in client.bodies)
    assert set(client.requests) == {("build/kernel", "kernel-v1")}


class _InterruptedBody(_Body):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self._reads = 0

    def read(self, size: int | None = -1, /) -> bytes:
        self._reads += 1
        if self._reads > 1:
            raise ReadTimeoutError(endpoint_url="http://object-store")
        return super().read(16 if size is None else min(size, 16))


class _InterruptedClient(_Client):
    def get_object(self, **request: object) -> dict[str, object]:
        key = cast(str, request["Key"])
        version = cast(str, request["VersionId"])
        self.requests.append((key, version))
        body = _InterruptedBody(self.objects[(key, version)])
        self.bodies.append(body)
        return {
            "Metadata": {"sensitivity": "redacted", "retention-class": "build"},
            "Body": body,
        }


def test_interrupted_fetch_closes_stream_and_retry_commits_cleanly(tmp_path: Path) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    interrupted = _InterruptedClient({("build/kernel", "kernel-v1"): bundle})
    session = _Session(tmp_path / "activation")

    with pytest.raises(Exception, match="get_object"):
        RealLocalExternalBootMaterializer(ObjectStore(interrupted, "bucket")).materialize(
            plan, cast(LocalExternalBootSession, session)
        )

    assert all(body.closed_by_store for body in interrupted.bodies)
    assert not list(session.root.rglob("target-projection.json"))
    clean = _Client({("build/kernel", "kernel-v1"): bundle})
    result = RealLocalExternalBootMaterializer(ObjectStore(clean, "bucket")).materialize(
        plan, cast(LocalExternalBootSession, _Session(session.root))
    )
    assert result.verified_bundle_sha256 == plan.bundle.sha256


def test_interrupted_module_conversion_cleans_temporaries_for_new_session_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    original = external_boot_module.convert_kernel_bundle_modules

    def interrupt_conversion(
        source: BinaryIO, destination: BinaryIO, *, release: str
    ) -> tuple[str, int]:
        del source, release
        destination.write(b"partial")
        raise OSError("injected module conversion interruption")

    monkeypatch.setattr(external_boot_module, "convert_kernel_bundle_modules", interrupt_conversion)
    with pytest.raises(OSError, match="conversion interruption"):
        materializer.materialize(plan, cast(LocalExternalBootSession, session))

    digest_dirs = [path for path in session.root.iterdir() if path.is_dir()]
    assert len(digest_dirs) == 1
    assert list(digest_dirs[0].iterdir()) == []
    monkeypatch.setattr(external_boot_module, "convert_kernel_bundle_modules", original)
    result = materializer.materialize(plan, cast(LocalExternalBootSession, _Session(session.root)))
    assert result.plan_identity == plan.identity


def test_prior_process_modules_temporary_retries_without_touching_unknown_entry(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    session = _Session(tmp_path / "activation")
    projection = _projection_for(plan)
    digest_dir = session.root / projection.digest.removeprefix("sha256:")
    digest_dir.mkdir(mode=0o700)
    (digest_dir / ".modules.next").write_bytes(b"prior-process-partial")
    (digest_dir / ".modules.next").chmod(0o600)
    (digest_dir / "unknown").write_bytes(b"preserve")
    (digest_dir / "unknown").chmod(0o600)
    client = _Client({("build/kernel", "kernel-v1"): bundle})

    result = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket")).materialize(
        plan, cast(LocalExternalBootSession, _Session(session.root))
    )

    assert result.plan_identity == plan.identity
    assert (digest_dir / "unknown").read_bytes() == b"preserve"
    assert not (digest_dir / ".modules.next").exists()


def test_committed_partial_verify_retry_preserves_payloads_and_unknown_entry(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    first = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    projection = session.reopen_projection(first.artifacts.kernel)
    digest_dir = session.root / projection.digest.removeprefix("sha256:")
    before = {name: (digest_dir / name).read_bytes() for name in ("kernel", "modules")}
    (digest_dir / ".bundle.verify").write_bytes(bundle[:17])
    (digest_dir / ".bundle.verify").chmod(0o600)
    (digest_dir / "unknown").write_bytes(b"preserve")
    (digest_dir / "unknown").chmod(0o600)

    second = materializer.materialize(plan, cast(LocalExternalBootSession, _Session(session.root)))

    assert second == first
    assert {name: (digest_dir / name).read_bytes() for name in before} == before
    assert (digest_dir / "unknown").read_bytes() == b"preserve"
    assert not (digest_dir / ".bundle.verify").exists()


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

    forged = result.model_copy(update={"installed_module_tree": "sha256:" + "f" * 64})
    with pytest.raises(ValueError, match="module tree"):
        materializer.inspect_prepare(
            forged, session.binding, inspection, cast(LocalExternalBootSession, session)
        )


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


def test_exact_retry_rejects_different_valid_canonical_module_archive(
    tmp_path: Path,
) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    result = materializer.materialize(plan, cast(LocalExternalBootSession, session))
    projection = session.reopen_projection(result.artifacts.kernel)
    modules = Path(session.projection_artifact_path(projection, "modules"))
    replacement = io.BytesIO()
    external_boot_module.convert_kernel_bundle_modules(
        io.BytesIO(_bundle(module_data=b"\x7fELFdifferent")),
        replacement,
        release="6.9.0",
    )
    replacement_bytes = replacement.getvalue()
    modules.write_bytes(replacement_bytes)
    modules.chmod(0o600)

    with pytest.raises(ValueError, match="canonical module archive"):
        materializer.materialize(plan, cast(LocalExternalBootSession, _Session(session.root)))

    assert modules.read_bytes() == replacement_bytes


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


def test_corrupt_final_payload_publishes_no_sidecar_and_clean_retry_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle()
    plan = _plan(bundle)
    client = _Client({("build/kernel", "kernel-v1"): bundle})
    materializer = RealLocalExternalBootMaterializer(ObjectStore(client, "bucket"))
    session = _Session(tmp_path / "activation")
    original = materializer._validate_local_bundle  # noqa: SLF001
    corrupted = False

    def corrupt_then_validate(value: ExternalBootPlan, directory_fd: int):
        nonlocal corrupted
        if not corrupted:
            corrupted = True
            descriptor = os.open("modules", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                os.ftruncate(descriptor, 1)
            finally:
                os.close(descriptor)
        return original(value, directory_fd)

    monkeypatch.setattr(materializer, "_validate_local_bundle", corrupt_then_validate)
    with pytest.raises(ValueError, match="canonical module archive"):
        materializer.materialize(plan, cast(LocalExternalBootSession, session))

    assert not list(session.root.rglob("target-projection.json"))
    assert not list(session.root.rglob("kernel"))
    assert not list(session.root.rglob("modules"))
    monkeypatch.setattr(materializer, "_validate_local_bundle", original)
    result = materializer.materialize(plan, cast(LocalExternalBootSession, _Session(session.root)))
    assert result.plan_identity == plan.identity

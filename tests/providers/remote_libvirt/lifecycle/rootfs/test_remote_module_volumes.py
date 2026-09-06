"""Attempt-scoped remote module volume ownership."""

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import libvirt
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    MAX_ENTRIES,
    SCRATCH_CAPACITY_BYTES,
    Ext4SourceFilesystemWriter,
    ModuleTreeEntry,
    SourceFilesystemEvidence,
    delete_owned_attempt_volume,
    prepare_attempt_volumes,
    source_image_capacity_bytes,
    source_image_inode_count,
    validate_attempt_volumes,
    validate_module_tree_bounds,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import (  # noqa: F401
    OPERATION,
    SOURCE_NAME,
    Conn,
    Pool,
    Stream,
    UniqueTrackingWriter,
    Volume,
    Writer,
    _detached_source,
    _die,
    _duplicate_name,
    _kill_upload,
    request,
)


def test_prepare_uses_deterministic_names_uploads_and_reuses(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)

    first = prepare_attempt_volumes(conn, wanted)
    second = prepare_attempt_volumes(conn, wanted)

    assert first == second
    assert first.source.name.endswith("-source.ext4")
    assert first.scratch.name.endswith("-scratch.ext4")
    assert first.source.digest == "sha256:" + "d" * 64
    assert first.scratch.capacity_bytes == SCRATCH_CAPACITY_BYTES == 10 * 1024**3
    assert len(conn.pool.volumes) == 2


@pytest.mark.parametrize("restoration", [False, True])
def test_expired_after_source_create_starts_no_later_volume_mutation(
    tmp_path: Path, restoration: bool
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    if restoration:
        prepared = prepare_attempt_volumes(conn, wanted)
        conn.pool.volumes.pop(prepared.source.name)

    expired = False
    original_create = conn.pool.createXML

    def create_then_expire(xml: str, flags: int = 0) -> Volume:  # noqa: N802
        nonlocal expired
        created = original_create(xml, flags)
        expired = True
        return created

    cast(Any, conn.pool).createXML = create_then_expire

    def admit() -> None:
        if expired:
            raise TimeoutError

    with pytest.raises(TimeoutError):
        prepare_attempt_volumes(conn, wanted, admit_mutation=admit)
    source_name = next(name for name in conn.pool.volumes if name.endswith("-source.ext4"))
    assert conn.pool.volumes[source_name].payload == b""
    if not restoration:
        assert not any(name.endswith("-scratch.ext4") for name in conn.pool.volumes)


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
@pytest.mark.parametrize("restoration", [False, True])
def test_unresolved_upload_stage_starts_no_later_effect(
    tmp_path: Path, stage: int, restoration: bool
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    if restoration:
        prepared = prepare_attempt_volumes(conn, wanted)
        conn.pool.volumes.pop(prepared.source.name)
    calls = 0

    def bounded(operation: Callable[[], object]) -> object:
        nonlocal calls
        calls += 1
        if calls == stage:
            raise TimeoutError
        return operation()

    with pytest.raises(TimeoutError):
        prepare_attempt_volumes(conn, wanted, call_stream=bounded)
    assert calls == stage
    assert not any(name.endswith("-scratch.ext4") for name in conn.pool.volumes) or restoration


@pytest.mark.parametrize("cleanup", ["success", "failure", "expired", "unresolved"])
@pytest.mark.parametrize("restoration", [False, True])
def test_upload_failure_preserves_primary_and_bounds_abort(
    tmp_path: Path, cleanup: str, restoration: bool
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    if restoration:
        prepared = prepare_attempt_volumes(conn, wanted)
        conn.pool.volumes.pop(prepared.source.name)
    calls = 0
    aborts = 0

    def bounded(operation: Callable[[], object]) -> object:
        nonlocal calls, aborts
        calls += 1
        if calls == 2:
            if cleanup == "unresolved":
                raise TimeoutError
            raise libvirt.libvirtError("synthetic resolved upload failure")
        if calls == 3:
            if cleanup in {"failure", "expired"}:
                raise TimeoutError
            aborts += 1
        return operation()

    expected = TimeoutError if cleanup == "unresolved" else CategorizedError
    with pytest.raises(expected) as raised:
        prepare_attempt_volumes(conn, wanted, call_stream=bounded)
    assert aborts == (1 if cleanup == "success" else 0)
    assert calls == (2 if cleanup == "unresolved" else 3)
    if cleanup in {"failure", "expired"}:
        cause = cast(CategorizedError, raised.value).__cause__
        assert cause is not None
        assert any("abort cleanup unresolved" in note for note in cause.__notes__)


@pytest.mark.parametrize(
    "entries",
    [
        tuple(
            ModuleTreeEntry(f"{index}", 0o100644, content=b"") for index in range(MAX_ENTRIES + 1)
        ),
        (ModuleTreeEntry("/absolute", 0o100644, content=b"x"),),
        (ModuleTreeEntry("../parent", 0o100644, content=b"x"),),
        (ModuleTreeEntry("device", 0o20666),),
    ],
)
def test_invalid_or_over_limit_tree_is_rejected_before_creation(
    tmp_path: Path, entries: tuple[ModuleTreeEntry, ...]
) -> None:
    conn = Conn()
    with pytest.raises(ValueError):
        prepare_attempt_volumes(conn, request(tmp_path, entries=entries))
    assert not conn.pool.volumes


def test_content_byte_limit_rejects_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kdive.providers.remote_libvirt.lifecycle.rootfs import remote_module_volumes

    monkeypatch.setattr(remote_module_volumes, "MAX_CONTENT_BYTES", 2)
    conn = Conn()
    with pytest.raises(ValueError, match="content bytes"):
        prepare_attempt_volumes(conn, request(tmp_path))
    assert not conn.pool.volumes


def test_source_capacity_scales_past_64_mib_without_allocating_payload() -> None:
    capacity = source_image_capacity_bytes(65 * 1024**2, 1)
    assert capacity > 65 * 1024**2
    assert capacity > 64 * 1024**2


def test_exact_8_gib_bound_is_accepted_and_one_over_rejected_without_allocation() -> None:
    validate_module_tree_bounds(entry_count=MAX_ENTRIES, content_bytes=8 * 1024**3)
    capacity = source_image_capacity_bytes(8 * 1024**3, MAX_ENTRIES)
    assert capacity > 8 * 1024**3

    with pytest.raises(ValueError, match="content bytes"):
        validate_module_tree_bounds(entry_count=MAX_ENTRIES, content_bytes=8 * 1024**3 + 1)


def test_inode_count_covers_exact_entry_limit_and_filesystem_root_overhead() -> None:
    assert source_image_inode_count(MAX_ENTRIES) > MAX_ENTRIES


def test_real_ext4_image_provisions_inode_count_beyond_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    requested_entries = 20_000
    monkeypatch.setattr(
        Ext4SourceFilesystemWriter,
        "inspect",
        lambda _self, _path: SourceFilesystemEvidence(b"operation", "sha256:" + "a" * 64, 0, 0),
    )
    entries = tuple(
        ModuleTreeEntry(f"directory-{index}", 0o40755) for index in range(requested_entries)
    )
    image = Ext4SourceFilesystemWriter(tmp_path).build(b"operation", entries)
    output = subprocess.run(  # noqa: S603 - fixed tool and generated test image
        ["tune2fs", "-l", str(image.path)],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    inode_count = int(
        next(
            line.split(":", 1)[1] for line in output.splitlines() if line.startswith("Inode count:")
        )
    )
    assert inode_count >= source_image_inode_count(requested_entries)


def test_mismatched_owned_volume_is_retained(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    source.xml = source.xml.replace("source.ext4</name>", "scratch.ext4</name>")

    with pytest.raises(CategorizedError) as caught:
        prepare_attempt_volumes(conn, request(tmp_path))

    assert caught.value.category is ErrorCategory.CONFLICT
    assert not source.deleted


def test_retry_rejects_corrupt_or_truncated_source_bytes(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    source.payload[-1:] = b"!"

    with pytest.raises(CategorizedError, match="readback"):
        prepare_attempt_volumes(conn, request(tmp_path))
    assert not source.deleted


def test_retry_rejects_wrong_source_capacity(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    source.capacity += 1

    with pytest.raises(CategorizedError, match="capacity"):
        prepare_attempt_volumes(conn, request(tmp_path))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('type="raw"', 'type="qcow2"'),
        (
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000099",
        ),
        ("source.ext4</name>", "scratch.ext4</name>"),
    ],
)
def test_reuse_rejects_incomplete_format_or_persisted_name(
    tmp_path: Path, old: str, new: str
) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    source.xml = source.xml.replace(old, new)

    with pytest.raises(CategorizedError, match="facts|ownership"):
        prepare_attempt_volumes(conn, request(tmp_path))


def test_reuse_maps_volume_metadata_transport_failure(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    conn.pool.volumes[prepared.source.name].fail_metadata_read = True

    with pytest.raises(CategorizedError) as caught:
        prepare_attempt_volumes(conn, request(tmp_path))
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


def test_standalone_validation_derives_and_checks_expected_source_capacity(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)
    conn.pool.volumes[prepared.source.name].capacity += 1

    with pytest.raises(CategorizedError, match="capacity"):
        validate_attempt_volumes(conn, wanted)


@pytest.mark.parametrize("mutation", ["payload", "capacity"])
def test_validation_with_supplied_volumes_still_reopens_source(
    tmp_path: Path, mutation: str
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)
    stored = conn.pool.volumes[prepared.source.name]
    if mutation == "payload":
        stored.payload[0] ^= 0xFF
    else:
        stored.capacity += 1

    with pytest.raises(CategorizedError, match="capacity|readback"):
        validate_attempt_volumes(
            conn,
            wanted,
            source=prepared.source,
            scratch=prepared.scratch,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pool", "foreign"),
        ("name", "foreign"),
        ("system_id", "00000000-0000-4000-8000-000000000099"),
        ("run_id", "00000000-0000-4000-8000-000000000099"),
        ("operation_nonce", "f" * 32),
        ("purpose", "scratch"),
        ("digest", "sha256:" + "f" * 64),
        ("capacity_bytes", 1),
    ],
)
def test_validation_rejects_every_supplied_source_identity_mutation(
    tmp_path: Path, field: str, value: object
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)

    with pytest.raises(CategorizedError, match="provided remote module volume"):
        validate_attempt_volumes(
            conn,
            wanted,
            source=replace(prepared.source, **{field: value}),
            scratch=prepared.scratch,
        )


def test_validation_maps_unreadable_downloaded_filesystem_to_conflict(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)

    assert isinstance(wanted.writer, Writer)
    wanted.writer.fail_inspect = True
    with pytest.raises(CategorizedError) as caught:
        validate_attempt_volumes(conn, wanted, source=prepared.source, scratch=prepared.scratch)
    assert caught.value.category is ErrorCategory.CONFLICT


def test_initial_local_readback_failure_preserves_primary_error(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    assert isinstance(wanted.writer, Writer)
    wanted.writer.fail_inspect = True

    with pytest.raises(CategorizedError, match="unreadable ext4"):
        prepare_attempt_volumes(conn, wanted)
    assert conn.pool.volumes == {}


def test_readback_stream_creation_failure_closes_local_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)
    descriptor = os.open("/dev/null", os.O_WRONLY)
    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes.tempfile.mkstemp",
        lambda **_kwargs: (descriptor, str(tmp_path / "readback.ext4")),
    )
    conn.fail_new_stream = True

    with pytest.raises(CategorizedError):
        validate_attempt_volumes(conn, wanted, source=prepared.source, scratch=prepared.scratch)
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_explicit_delete_rejects_duplicate_persisted_name(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    _duplicate_name(conn.pool.volumes[prepared.source.name])
    inspection = AttachmentInspection(
        True,
        True,
        False,
        frozenset({(prepared.source.pool, prepared.source.name)}),
    )

    with pytest.raises(CategorizedError, match="facts"):
        delete_owned_attempt_volume(conn, prepared.source, inspection=inspection)
    assert not conn.pool.volumes[prepared.source.name].deleted


def test_failed_prepare_retains_duplicate_persisted_name(tmp_path: Path) -> None:
    conn = Conn()
    conn.pool.fail_on_scratch = True

    def inspection() -> AttachmentInspection:
        source_name = next(name for name in conn.pool.volumes if "source" in name)
        _duplicate_name(conn.pool.volumes[source_name])
        return AttachmentInspection(True, True, False, frozenset({("systems", source_name)}))

    with pytest.raises(RuntimeError, match="scratch create failed"):
        prepare_attempt_volumes(conn, request(tmp_path, inspect_attachments=inspection))
    source = next(volume for name, volume in conn.pool.volumes.items() if "source" in name)
    assert not source.deleted


def test_prepare_rollback_does_not_delete_after_last_owner_read_expires(tmp_path: Path) -> None:
    conn = Conn()
    conn.pool.fail_on_scratch = True
    expired = False

    def inspection() -> AttachmentInspection:
        source_name = next(name for name in conn.pool.volumes if "source" in name)
        source = conn.pool.volumes[source_name]
        original_info = source.info

        def info_then_expire() -> list[int]:
            nonlocal expired
            observed = original_info()
            expired = True
            return observed

        cast(Any, source).info = info_then_expire
        return AttachmentInspection(True, True, False, frozenset({("systems", source_name)}))

    def admit() -> None:
        if expired:
            raise TimeoutError

    with pytest.raises(RuntimeError, match="scratch create failed"):
        prepare_attempt_volumes(
            conn,
            request(tmp_path, inspect_attachments=inspection),
            admit_mutation=admit,
        )
    source = next(volume for name, volume in conn.pool.volumes.items() if "source" in name)
    assert not source.deleted


@pytest.mark.parametrize("operation", ["validate", "delete"])
def test_public_storage_paths_map_pool_lookup_transport_failure(
    tmp_path: Path, operation: str
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)
    conn.fail_pool_lookup = True

    with pytest.raises(CategorizedError) as caught:
        if operation == "validate":
            validate_attempt_volumes(conn, wanted)
        else:
            inspection = AttachmentInspection(
                True,
                True,
                False,
                frozenset({(prepared.source.pool, prepared.source.name)}),
            )
            delete_owned_attempt_volume(conn, prepared.source, inspection=inspection)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize("stage", ["newStream", "download", "recvAll", "finish"])
@pytest.mark.parametrize("operation", ["prepare", "validate"])
def test_source_stream_transport_failures_are_infrastructure(
    tmp_path: Path, stage: str, operation: str
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    if operation == "validate":
        prepare_attempt_volumes(conn, wanted)
    if stage == "newStream":
        conn.fail_new_stream = True
    else:
        conn.fail_download_stage = stage

    with pytest.raises(CategorizedError) as caught:
        if operation == "prepare":
            prepare_attempt_volumes(conn, wanted)
        else:
            validate_attempt_volumes(conn, wanted)
    assert caught.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE


@pytest.mark.parametrize("cleanup", ["success", "failure", "expired", "unresolved"])
def test_source_readback_failure_uses_bounded_abort(tmp_path: Path, cleanup: str) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    prepared = prepare_attempt_volumes(conn, wanted)
    conn.fail_download_stage = "download"
    effects: list[str] = []

    def bounded(operation: Callable[[], object]) -> object:
        name = getattr(operation, "__name__", "operation")
        effects.append(name)
        if cleanup == "unresolved" and name == "<lambda>":
            raise TimeoutError
        if name == "abort" and cleanup in {"failure", "expired"}:
            raise TimeoutError
        return operation()

    if cleanup == "unresolved":
        with pytest.raises(TimeoutError):
            validate_attempt_volumes(conn, wanted, call_stream=bounded)
        assert "abort" not in effects
    else:
        with pytest.raises(CategorizedError) as raised:
            validate_attempt_volumes(conn, wanted, call_stream=bounded)
        assert raised.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        assert effects.count("abort") == 1
        if cleanup in {"failure", "expired"}:
            cause = raised.value.__cause__
            assert cause is not None
            assert any("abort cleanup unresolved" in note for note in cause.__notes__)
    assert prepared.source.name in conn.pool.volumes


def test_failed_prepare_retains_created_volume_when_attachment_probe_says_attached(
    tmp_path: Path,
) -> None:
    conn = Conn()
    wanted = request(tmp_path)
    conn.pool.fail_on_scratch = True
    with pytest.raises(RuntimeError, match="scratch"):
        prepare_attempt_volumes(conn, wanted)

    source = next(volume for name, volume in conn.pool.volumes.items() if "source" in name)
    assert not source.deleted


def test_failed_prepare_preserves_primary_error_and_retains_malformed_created_volume(
    tmp_path: Path,
) -> None:
    conn = Conn()
    conn.pool.fail_on_scratch = True

    def inspection() -> AttachmentInspection:
        source_name = next(name for name in conn.pool.volumes if "source" in name)
        conn.pool.volumes[source_name].xml = "<malformed"
        return AttachmentInspection(True, True, False, frozenset({("systems", source_name)}))

    with pytest.raises(RuntimeError, match="scratch create failed"):
        prepare_attempt_volumes(conn, request(tmp_path, inspect_attachments=inspection))

    assert not next(iter(conn.pool.volumes.values())).deleted


def test_failed_prepare_uses_full_delete_gate_and_confirms_absence(tmp_path: Path) -> None:
    conn = Conn()
    conn.pool.fail_on_scratch = True

    def inspection() -> AttachmentInspection:
        source_name = next(name for name in conn.pool.volumes if "source" in name)
        return AttachmentInspection(True, True, False, frozenset({("systems", source_name)}))

    with pytest.raises(RuntimeError, match="scratch create failed"):
        prepare_attempt_volumes(conn, request(tmp_path, inspect_attachments=inspection))

    assert next(iter(conn.pool.volumes.values())).deleted


def test_failed_prepare_retains_created_volume_after_owner_tuple_mutation(tmp_path: Path) -> None:
    conn = Conn()
    conn.pool.fail_on_scratch = True

    def inspection() -> AttachmentInspection:
        source_name = next(name for name in conn.pool.volumes if "source" in name)
        volume = conn.pool.volumes[source_name]
        volume.xml = volume.xml.replace("source.ext4</name>", "scratch.ext4</name>")
        return AttachmentInspection(True, True, False, frozenset({("systems", source_name)}))

    with pytest.raises(RuntimeError, match="scratch create failed"):
        prepare_attempt_volumes(conn, request(tmp_path, inspect_attachments=inspection))

    assert not next(iter(conn.pool.volumes.values())).deleted


def test_real_ext4_writer_has_closed_layout_and_appliance_manifest_parity(tmp_path: Path) -> None:
    import importlib.util

    entries = (
        ModuleTreeEntry("kernel", 0o40755),
        ModuleTreeEntry("kernel/a.ko", 0o100644, content=b"module"),
        ModuleTreeEntry("alias", 0o120777, link_target="kernel/a.ko"),
    )
    writer = Ext4SourceFilesystemWriter(tmp_path)
    operation = OPERATION.to_wire_bytes()
    image = writer.build(operation, entries)
    evidence = writer.inspect(image.path)

    assert image.capacity_bytes == source_image_capacity_bytes(6, 3)
    assert evidence.operation == operation
    assert evidence.entry_count == 3
    assert evidence.content_bytes == 6
    spec = importlib.util.spec_from_file_location(
        "task_2_appliance", Path("deploy/remote_module_appliance/appliance.py")
    )
    assert spec is not None and spec.loader is not None
    appliance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(appliance)
    extracted = writer.extract(image.path, tmp_path / "readback")
    assert appliance._read_operation(extracted / "operation-v1.json") == OPERATION.model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    assert appliance._tree_manifest(extracted / "modules") == (
        evidence.manifest,
        evidence.entry_count,
        evidence.content_bytes,
    )


def test_same_operation_ext4_builds_use_distinct_secure_images(tmp_path: Path) -> None:
    writer = Ext4SourceFilesystemWriter(tmp_path)
    operation = b'{"protocol":"remote-module-operation-v1"}'
    entries = (ModuleTreeEntry("kernel.ko", 0o100644, content=b"module"),)

    with ThreadPoolExecutor(max_workers=2) as executor:
        images = tuple(executor.map(lambda _index: writer.build(operation, entries), range(2)))

    try:
        assert images[0].path != images[1].path
        assert all(image.path.is_file() for image in images)
        assert all(image.path.stat().st_mode & 0o077 == 0 for image in images)
    finally:
        for image in images:
            image.path.unlink(missing_ok=True)


def test_ext4_writer_removes_image_when_mkfs_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, "mkfs.ext4")
        ),
    )

    with pytest.raises(CategorizedError, match="build remote module"):
        Ext4SourceFilesystemWriter(tmp_path).build(b"operation", ())

    assert not tuple(tmp_path.glob("kdive-module-source-*.ext4"))


def test_ext4_writer_removes_image_when_readback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Ext4SourceFilesystemWriter(tmp_path)
    monkeypatch.setattr(
        writer,
        "inspect",
        lambda _path: (_ for _ in ()).throw(
            CategorizedError("unreadable", category=ErrorCategory.BUILD_FAILURE)
        ),
    )

    with pytest.raises(CategorizedError, match="build remote module"):
        writer.build(b"operation", ())

    assert not tuple(tmp_path.glob("kdive-module-source-*.ext4"))


def test_ext4_writer_removes_image_when_readback_raises_unexpected_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Ext4SourceFilesystemWriter(tmp_path)
    monkeypatch.setattr(
        writer,
        "inspect",
        lambda _path: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        writer.build(b"operation", ())

    assert not tuple(tmp_path.glob("kdive-module-source-*.ext4"))


def test_ext4_writer_removes_image_when_staging_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = Ext4SourceFilesystemWriter(tmp_path)
    monkeypatch.setattr(
        writer,
        "inspect",
        lambda _path: SourceFilesystemEvidence(b"operation", "sha256:" + "0" * 64, 0, 0),
    )
    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup failed")),
    )

    with pytest.raises(OSError, match="cleanup failed"):
        writer.build(b"operation", ())

    assert not tuple(tmp_path.glob("kdive-module-source-*.ext4"))


@pytest.mark.parametrize("fail", [False, True])
def test_prepare_cleans_every_local_source_image(tmp_path: Path, fail: bool) -> None:
    writer = UniqueTrackingWriter(tmp_path)
    conn = Conn()
    conn.pool.fail_on_scratch = fail
    wanted = request(tmp_path, writer=writer)

    if fail:
        with pytest.raises(RuntimeError, match="scratch create failed"):
            prepare_attempt_volumes(conn, wanted)
    else:
        prepare_attempt_volumes(conn, wanted)

    assert writer.paths
    assert all(not path.exists() for path in writer.paths)


def test_delete_requires_detachment_and_exact_owner_then_reads_absence(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))

    with pytest.raises(CategorizedError):
        delete_owned_attempt_volume(
            conn,
            prepared.source,
            inspection=AttachmentInspection(True, True, False, frozenset()),
        )
    assert not conn.pool.volumes[prepared.source.name].deleted

    detached = AttachmentInspection(
        True,
        True,
        False,
        frozenset({(prepared.source.pool, prepared.source.name)}),
    )
    delete_owned_attempt_volume(conn, prepared.source, inspection=detached)
    assert conn.pool.volumes[prepared.source.name].deleted
    delete_owned_attempt_volume(conn, prepared.source, inspection=detached)


def test_recovery_delete_does_not_start_after_last_owner_read_expires(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    expired = False
    original_info = source.info

    def info_then_expire() -> list[int]:
        nonlocal expired
        observed = original_info()
        expired = True
        return observed

    cast(Any, source).info = info_then_expire
    detached = AttachmentInspection(
        True, True, False, frozenset({(prepared.source.pool, prepared.source.name)})
    )

    with pytest.raises(TimeoutError):
        delete_owned_attempt_volume(
            conn,
            prepared.source,
            inspection=detached,
            admit_mutation=lambda: (_ for _ in ()).throw(TimeoutError) if expired else None,
        )
    assert not source.deleted


def test_delete_rejects_mutated_complete_owner_tuple(tmp_path: Path) -> None:
    conn = Conn()
    prepared = prepare_attempt_volumes(conn, request(tmp_path))
    source = conn.pool.volumes[prepared.source.name]
    source.xml = source.xml.replace("-" + "a" * 32 + "-", "-" + "b" * 32 + "-")
    detached = AttachmentInspection(
        True,
        True,
        False,
        frozenset({(prepared.source.pool, prepared.source.name)}),
    )

    with pytest.raises(CategorizedError, match="ownership"):
        delete_owned_attempt_volume(conn, prepared.source, inspection=detached)
    assert not source.deleted


def test_retry_repairs_a_source_upload_a_dead_worker_left_partial(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path, inspect_attachments=_detached_source)
    revive = _kill_upload(conn)
    with pytest.raises(SystemExit):
        prepare_attempt_volumes(conn, wanted)

    # The volume exists with correct owner metadata but no content, so
    # existence alone must not be read as a finished upload.
    assert SOURCE_NAME in conn.pool.volumes
    assert bytes(conn.pool.volumes[SOURCE_NAME].payload) == b""

    revive()
    prepared = prepare_attempt_volumes(conn, wanted)  # same nonce, fresh worker
    assert prepared.source.name == SOURCE_NAME
    assert bytes(conn.pool.volumes[SOURCE_NAME].payload) == b"source-image"


def test_retry_does_not_rewrite_a_source_an_appliance_still_holds(tmp_path: Path) -> None:
    conn = Conn()

    def attached() -> AttachmentInspection:
        return AttachmentInspection(True, True, True, frozenset())

    wanted = request(tmp_path, inspect_attachments=attached)
    revive = _kill_upload(conn)
    with pytest.raises(SystemExit):
        prepare_attempt_volumes(conn, wanted)

    revive()
    with pytest.raises(CategorizedError) as raised:
        prepare_attempt_volumes(conn, wanted)
    assert raised.value.category is ErrorCategory.CONFLICT
    assert bytes(conn.pool.volumes[SOURCE_NAME].payload) == b""


def test_a_complete_source_is_not_reuploaded_on_reopen(tmp_path: Path) -> None:
    conn = Conn()
    wanted = request(tmp_path, inspect_attachments=_detached_source)
    prepare_attempt_volumes(conn, wanted)
    uploads = 0
    original_upload = Volume.upload

    def counting(self: Volume, stream: object, offset: int, length: int, flags: int = 0) -> int:
        nonlocal uploads
        uploads += 1
        return original_upload(self, stream, offset, length, flags)

    cast(Any, Volume).upload = counting
    try:
        prepare_attempt_volumes(conn, wanted)
    finally:
        cast(Any, Volume).upload = original_upload
    assert uploads == 0

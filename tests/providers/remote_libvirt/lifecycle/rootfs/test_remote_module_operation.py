"""Scratch-first reopen behavior."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptTerminalEvidence,
    ModuleAttemptWorkerWriteContext,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_attachments import (
    AttachmentInspection,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleApplianceExecution,
    RemoteModuleVolumePreparation,
    RemoteModuleVolumeRecovery,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    CompletionDeadlineExecutor,
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_result_reader import (
    SparseRemoteModuleResultReader,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volume_names import (
    render_module_volume_name,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    BuiltSourceImage,
    ModuleTreeEntry,
    PreparedVolume,
    SourceFilesystemEvidence,
    expected_attempt_volumes,
    prepare_attempt_volumes,
)
from kdive.services.remote_module_attempt_preparation import (
    ModuleAttemptObligationVerificationError,
    open_module_attempt_preparation,
)
from kdive.services.remote_module_operation import RemoteModuleOperationRuntime
from kdive.services.remote_module_phases import (
    CaptureInstallRequest,
    capture_install_modules,
    restore_modules,
)
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.db.remote_module_attempt_obligations_support import _seed
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    BlockingConsoleConn,
    success_result,
    volume,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Clock as ApplianceClock,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Conn as ApplianceConn,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Domain as ApplianceDomain,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    Executor as ApplianceExecutor,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    operation as appliance_operation,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_appliance_support import (
    request as appliance_request,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents_support import _result
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import (
    Conn,
    Stream,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes_support import (
    request as volume_request,
)


def _recovery(result: RemoteModuleResultV1) -> RemoteModuleRecoveryRefV2:
    capture = RemoteModuleOperationV1.model_validate(
        {
            "operation": "capture_install",
            "system_id": result.system_id,
            "run_id": result.run_id,
            "plan_identity": result.plan_identity,
            "operation_nonce": result.operation_nonce,
            "release": result.release,
            "root_volume": {
                "key": result.root_volume_key,
                "identity": result.root_volume_identity,
            },
            "source_manifest": result.source_manifest,
            "appliance_image_digest": result.appliance_image_digest,
        }
    )
    digest = "sha256:" + "a" * 64
    return RemoteModuleRecoveryRefV2.model_validate(
        {
            "system_id": result.system_id,
            "run_id": result.run_id,
            "plan_identity": result.plan_identity,
            "operation_nonce": result.operation_nonce,
            "pool": OpaqueProviderRef(ref="pool"),
            "root_volume": OpaqueProviderRef(ref="root"),
            "source_volume": OpaqueProviderRef(
                ref=render_module_volume_name(
                    capture.system_id, capture.run_id, capture.operation_nonce, "source.ext4"
                )
            ),
            "scratch_volume": OpaqueProviderRef(
                ref=render_module_volume_name(
                    capture.system_id, capture.run_id, capture.operation_nonce, "scratch.ext4"
                )
            ),
            "source_capacity_bytes": 64 * 1024**2,
            "operation_identity": identity_for(capture),
            "result_identity": identity_for(result),
            "appliance_image_digest": result.appliance_image_digest,
            "authority_identity": digest,
        }
    )


def _worker_context(recovery: RemoteModuleRecoveryRefV2) -> ModuleAttemptWorkerWriteContext:
    preparation = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": UUID(recovery.system_id),
                "run_id": UUID(recovery.run_id),
                "operation_nonce": recovery.operation_nonce,
            }
        }
    )
    return ModuleAttemptWorkerWriteContext(uuid4(), 1, SecretStr("test-worker"), preparation)


class Repo:
    async def read_terminal_evidence(self, conn: object, attempt: object):
        del conn, attempt
        return None


def _runtime(
    read: Callable[..., Awaitable[bytes | None]], repo: object | None = None
) -> RemoteModuleOperationRuntime:
    @asynccontextmanager
    async def connection() -> AsyncIterator[object]:
        yield SimpleNamespace()

    pool = SimpleNamespace(connection=connection)
    return RemoteModuleOperationRuntime(
        cast("AsyncConnectionPool", pool),
        cast("RemoteModuleAttemptObligationRepository", repo or Repo()),
        read,
    )


def test_scratch_result_reopens_before_terminal_evidence() -> None:
    result = RemoteModuleResultV1.model_validate(_result())

    async def read(_: RemoteModuleRecoveryRefV2) -> bytes | None:
        return result.to_wire_bytes()

    runtime = _runtime(read)
    assert asyncio.run(runtime.reopen_result(_recovery(result))) == result


def test_scratch_reopen_passes_inherited_absolute_deadline() -> None:
    result = RemoteModuleResultV1.model_validate(_result())
    observed: list[float] = []

    async def read(_: RemoteModuleRecoveryRefV2, deadline: float) -> bytes:
        observed.append(deadline)
        return result.to_wire_bytes()

    runtime = _runtime(cast(Any, read))
    assert asyncio.run(runtime.reopen_result(_recovery(result), 123.5)) == result
    assert observed == [123.5]


@pytest.mark.parametrize("raw", [b"not json\n", None])
def test_missing_or_malformed_scratch_is_redacted_conflict(raw: bytes | None) -> None:
    result = RemoteModuleResultV1.model_validate(_result())

    async def read(_: RemoteModuleRecoveryRefV2) -> bytes | None:
        return raw

    runtime = _runtime(read)
    with pytest.raises(CategorizedError) as caught:
        asyncio.run(runtime.reopen_result(_recovery(result)))
    assert caught.value.category is ErrorCategory.CONFLICT


def test_restore_ready_reopens_restore_but_keeps_installed_baseline_identity() -> None:
    installed = RemoteModuleResultV1.model_validate(_result())
    current = installed.model_copy(
        update={"phase": "restore-ready", "entry_count": None, "content_bytes": None}
    )
    recovery = _recovery(installed).model_copy(
        update={"installed_entry_count": 12, "installed_content_bytes": 4096}
    )

    async def read(_: RemoteModuleRecoveryRefV2) -> bytes:
        return current.to_wire_bytes()

    runtime = _runtime(read)
    operation = asyncio.run(runtime.reopen_operation(recovery))

    assert operation.operation == "restore"
    assert asyncio.run(runtime.reopen_result(recovery)) == current
    assert asyncio.run(runtime.reopen_installed_result(recovery)) == installed
    capture = asyncio.run(runtime.reopen_capture_operation(recovery))
    assert identity_for(capture) == recovery.operation_identity


def test_scratch_result_with_foreign_identity_is_rejected() -> None:
    installed = RemoteModuleResultV1.model_validate(_result())
    foreign = installed.model_copy(update={"source_manifest": "sha256:" + "0" * 64})

    async def read(_: RemoteModuleRecoveryRefV2) -> bytes:
        return foreign.to_wire_bytes()

    with pytest.raises(CategorizedError) as caught:
        asyncio.run(_runtime(read).reopen_result(_recovery(installed)))
    assert caught.value.category is ErrorCategory.CONFLICT


def test_absent_scratch_reopens_valid_terminal_evidence() -> None:
    result = RemoteModuleResultV1.model_validate(_result())
    recovery = _recovery(result)
    operation = RemoteModuleOperationV1.model_validate(
        {
            "operation": "capture_install",
            "system_id": result.system_id,
            "run_id": result.run_id,
            "plan_identity": result.plan_identity,
            "operation_nonce": result.operation_nonce,
            "release": result.release,
            "root_volume": {
                "key": result.root_volume_key,
                "identity": result.root_volume_identity,
            },
            "source_manifest": result.source_manifest,
            "appliance_image_digest": result.appliance_image_digest,
        }
    )

    class EvidenceRepo:
        async def read_terminal_evidence(self, conn: object, attempt: object):
            del conn, attempt
            return ModuleAttemptTerminalEvidence(
                terminal_operation=operation.model_dump(mode="json"),
                terminal_operation_identity=identity_for(operation),
                terminal_result=result.model_dump(mode="json"),
                terminal_result_identity=identity_for(result),
                baseline_operation_identity=recovery.operation_identity,
                baseline_result_identity=recovery.result_identity,
                installed_entry_count=12,
                installed_content_bytes=4096,
                recovery_reference=recovery.model_dump(mode="json"),
            )

    async def absent(_: RemoteModuleRecoveryRefV2) -> None:
        return None

    runtime = _runtime(absent, EvidenceRepo())
    assert asyncio.run(runtime.reopen_operation(recovery)) == operation
    assert asyncio.run(runtime.reopen_result(recovery)) == result


def test_absent_scratch_reads_terminal_evidence_through_owned_pool_connection(
    migrated_url: str,
) -> None:
    async def run() -> None:
        repository = RemoteModuleAttemptObligationRepository()
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=1) as pool:
            async with pool.connection() as conn:
                system_id, run_id = await _seed(conn)
                attempt = ModuleAttempt(system_id, run_id, "0" * 32)
                await repository.open_mutation_obligation(conn, attempt)
                result = RemoteModuleResultV1.model_validate(_result()).model_copy(
                    update={
                        "system_id": str(system_id),
                        "run_id": str(run_id),
                        "operation_nonce": attempt.operation_nonce,
                    }
                )
                recovery = _recovery(result)
                operation = RemoteModuleOperationRuntime._operation_from_result(result)
                evidence = ModuleAttemptTerminalEvidence(
                    terminal_operation=operation.model_dump(mode="json"),
                    terminal_operation_identity=identity_for(operation),
                    terminal_result=result.model_dump(mode="json"),
                    terminal_result_identity=identity_for(result),
                    baseline_operation_identity=recovery.operation_identity,
                    baseline_result_identity=recovery.result_identity,
                    installed_entry_count=12,
                    installed_content_bytes=4096,
                    recovery_reference=recovery.model_dump(mode="json"),
                )
                await repository.record_terminal_evidence(conn, attempt, evidence)

            async def absent(_: RemoteModuleRecoveryRefV2) -> None:
                return None

            runtime = RemoteModuleOperationRuntime(pool, repository, absent)
            assert await runtime.reopen_result(recovery) == result

    asyncio.run(run())


@pytest.mark.anyio
async def test_prepare_passes_exact_attempt_and_caller_receipt_to_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = RemoteModuleResultV1.model_validate(_result())
    operation = RemoteModuleOperationRuntime._operation_from_result(result)
    receipt = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": UUID(operation.system_id),
                "run_id": UUID(operation.run_id),
                "operation_nonce": operation.operation_nonce,
            }
        }
    )
    observed: list[object] = []

    prepared = cast(Any, SimpleNamespace())

    async def verified(*args: object, **kwargs: object) -> object:
        del kwargs
        observed.extend(args)
        consumer = cast(Any, args[7])
        return consumer(args[3], SimpleNamespace(), lambda: None)

    volume_requests: list[object] = []
    inspected_with: list[object] = []

    def prepare_volumes(_storage: object, volume_request: object, **_kwargs: object) -> object:
        volume_requests.append(volume_request)
        cast(Any, volume_request).inspect_attachments()
        return prepared

    monkeypatch.setattr(
        "kdive.services.remote_module_operation.prepare_verified_remote_module_attempt",
        verified,
    )
    monkeypatch.setattr(
        "kdive.services.remote_module_operation.prepare_attempt_volumes",
        prepare_volumes,
    )
    runtime = _runtime(lambda _recovery: asyncio.sleep(0, result=None))
    object.__setattr__(
        runtime,
        "volume_preparation",
        RemoteModuleVolumePreparation(
            storage=cast(Any, SimpleNamespace()),
            pool_name="modules",
            entries=(),
            writer=cast(Any, SimpleNamespace()),
            inspect_attachments=cast(Any, lambda identity: inspected_with.append(identity)),
            work_dir=Path("/tmp"),
        ),
    )
    answer = await runtime.prepare(
        receipt,
        operation,
        cast(Any, SimpleNamespace()),
        cast(Any, SimpleNamespace()),
        10.0,
    )

    assert answer is prepared
    assert observed[2] is receipt
    assert observed[3] == ModuleAttempt(
        UUID(operation.system_id), UUID(operation.run_id), operation.operation_nonce
    )
    assert len(volume_requests) == 1
    assert inspected_with == [observed[5]]


@pytest.mark.anyio
async def test_inspect_attempt_distinguishes_absence_and_valid_current_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = RemoteModuleResultV1.model_validate(_result())
    operation = RemoteModuleOperationRuntime._operation_from_result(result)
    receipt = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": UUID(operation.system_id),
                "run_id": UUID(operation.run_id),
                "operation_nonce": operation.operation_nonce,
            }
        }
    )

    preparable = True

    class InspectionRepo:
        async def attempt_is_preparable(self, conn: object, attempt: ModuleAttempt) -> bool:
            del conn
            return preparable and attempt.operation_nonce == operation.operation_nonce

    class InlineExecutor:
        async def run(self, action: Callable[[], object]) -> object:
            return action()

    storage = Conn()
    inspection_deadlines: list[float] = []
    runtime = _runtime(
        lambda _recovery: asyncio.sleep(0, result=None),
        cast(RemoteModuleAttemptObligationRepository, InspectionRepo()),
    )
    object.__setattr__(
        runtime,
        "volume_preparation",
        RemoteModuleVolumePreparation(
            storage,
            "systems",
            (),
            cast(Any, SimpleNamespace()),
            lambda identity_port, present_attempt_volumes=None: cast(Any, SimpleNamespace()),
            tmp_path,
        ),
    )
    object.__setattr__(
        runtime,
        "appliance_execution",
        SimpleNamespace(
            read_scratch_result=lambda _volume, deadline: (
                inspection_deadlines.append(deadline) or result.to_wire_bytes()
            ),
            deadline_executor=CompletionDeadlineExecutor(lambda: 0.0),
            monotonic=lambda: 0.0,
        ),
    )
    executor = cast(RemoteModulePreparationExecutor, InlineExecutor())

    assert await runtime.inspect_attempt(receipt, operation, executor, 10**12) is None

    source_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "source.ext4"
    )
    scratch_name = render_module_volume_name(
        operation.system_id, operation.run_id, operation.operation_nonce, "scratch.ext4"
    )
    storage.pool.volumes[source_name] = cast(Any, SimpleNamespace(deleted=False))
    storage.pool.volumes[scratch_name] = cast(Any, SimpleNamespace(deleted=False))
    volumes = cast(Any, SimpleNamespace(source=object(), scratch=object()))
    monkeypatch.setattr(
        "kdive.services.remote_module_operation.validate_attempt_volumes",
        lambda _storage, _request: volumes,
    )
    monkeypatch.setattr(
        "kdive.services.remote_module_operation.validate_scratch_volume",
        lambda _storage, _request: volumes.scratch,
    )

    inspected = await runtime.inspect_attempt(receipt, operation, executor, 10**12)
    assert inspected is not None
    assert inspected.volumes is volumes
    assert inspected.result == result
    assert inspection_deadlines == [10**12]

    for raw, message in (
        (b"not-json\n", "result is invalid"),
        (
            result.model_copy(update={"source_manifest": "sha256:" + "f" * 64}).to_wire_bytes(),
            "result is invalid",
        ),
    ):
        object.__setattr__(
            runtime,
            "appliance_execution",
            SimpleNamespace(
                read_scratch_result=lambda _volume, _deadline, value=raw: value,
                deadline_executor=CompletionDeadlineExecutor(lambda: 0.0),
                monotonic=lambda: 0.0,
            ),
        )
        with pytest.raises(CategorizedError, match=message) as caught:
            await runtime.inspect_attempt(receipt, operation, executor, 10**12)
        assert caught.value.category is ErrorCategory.CONFLICT

    monkeypatch.setattr(
        "kdive.services.remote_module_operation.build_remote_device_identity_port",
        lambda _authority, _deadline: object(),
    )
    phase_request = CaptureInstallRequest(
        receipt, operation, cast(Any, object()), OpaqueProviderRef(ref="authority")
    )
    original_names = set(storage.pool.volumes)
    del storage.pool.volumes[source_name]
    with pytest.raises(CategorizedError, match="volume order"):
        await capture_install_modules(
            phase_request, runtime=runtime, executor=executor, deadline=10**12
        )
    assert set(storage.pool.volumes) == {scratch_name}

    storage.pool.volumes[source_name] = cast(Any, SimpleNamespace(deleted=False))
    del storage.pool.volumes[scratch_name]
    for inspection in (
        AttachmentInspection(True, True, True, frozenset()),
        AttachmentInspection(True, True, False, frozenset()),
        TimeoutError("attachment inspection unresolved"),
    ):

        def inspect_partial(
            identity_port: object,
            present_attempt_volumes: frozenset[str] | None = None,
            value=inspection,
        ):
            if isinstance(value, BaseException):
                raise value
            return value

        object.__setattr__(runtime.volume_preparation, "inspect_attachments", inspect_partial)
        with pytest.raises((CategorizedError, TimeoutError)):
            await capture_install_modules(
                phase_request, runtime=runtime, executor=executor, deadline=10**12
            )
        assert set(storage.pool.volumes) == {source_name}
    assert original_names == {source_name, scratch_name}

    preparable = False
    with pytest.raises(CategorizedError, match="obligation is absent"):
        await capture_install_modules(
            phase_request, runtime=runtime, executor=executor, deadline=10**12
        )
    assert set(storage.pool.volumes) == {source_name}


def test_real_receipt_guards_two_real_volume_creates(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Writer:
        operation = b""

        def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
            self.operation = operation
            path = tmp_path / "source.ext4"
            path.write_bytes(b"image" + bytes(4096 - len(b"image")))
            evidence = SourceFilesystemEvidence(operation, "sha256:" + "d" * 64, 1, 3)
            return BuiltSourceImage(path, 4096, evidence)

        def inspect(self, path: Path) -> SourceFilesystemEvidence:
            assert path.read_bytes().startswith(b"image") and path.stat().st_size == 4096
            return SourceFilesystemEvidence(self.operation, "sha256:" + "d" * 64, 1, 3)

    async def run() -> None:
        repository = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
        operation = RemoteModuleOperationRuntime._operation_from_result(
            RemoteModuleResultV1.model_validate(_result()).model_copy(
                update={"system_id": str(system_id), "run_id": str(run_id)}
            )
        )
        attempt = ModuleAttempt(system_id, run_id, operation.operation_nonce)
        storage, executor = Conn(), RemoteModulePreparationExecutor()
        started, release = threading.Event(), threading.Event()
        create_xml = storage.pool.createXML

        def blocking_create(xml: str, flags: int = 0):
            volume = create_xml(xml, flags)
            if len(storage.pool.volumes) == 1:
                started.set()
                release.wait()
            return volume

        cast(Any, storage.pool).createXML = blocking_create
        monkeypatch.setattr(
            "kdive.services.remote_module_volume_preparation.build_remote_device_identity_port",
            lambda authority, _deadline: authority,
        )
        async with (
            AsyncConnectionPool(
                authority_role_dsns("kdive_server"), min_size=1, max_size=1
            ) as server,
            AsyncConnectionPool(
                authority_role_dsns("kdive_worker"), min_size=1, max_size=1
            ) as worker,
        ):
            receipt = await open_module_attempt_preparation(server, repository, attempt)
            runtime = RemoteModuleOperationRuntime(
                worker,
                repository,
                lambda _recovery: asyncio.sleep(0, result=None),
                RemoteModuleVolumePreparation(
                    storage,
                    "systems",
                    (ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),),
                    Writer(),
                    lambda identity_port, present_attempt_volumes=None: cast(
                        Any, SimpleNamespace()
                    ),
                    tmp_path,
                ),
            )
            foreign = receipt.model_copy(
                update={
                    "module_attempt_obligation": receipt.module_attempt_obligation.model_copy(
                        update={"operation_nonce": "a" * 32}
                    )
                }
            )
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await runtime.prepare(foreign, operation, executor, cast(Any, object()), 10**12)
            assert not storage.pool.volumes
            absent_operation = operation.model_copy(update={"operation_nonce": "c" * 32})
            absent_receipt = receipt.model_copy(
                update={
                    "module_attempt_obligation": receipt.module_attempt_obligation.model_copy(
                        update={"operation_nonce": "c" * 32}
                    )
                }
            )
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await runtime.prepare(
                    absent_receipt,
                    absent_operation,
                    executor,
                    cast(Any, object()),
                    10**12,
                )
            assert not storage.pool.volumes
            task = asyncio.create_task(
                runtime.prepare(receipt, operation, executor, cast(Any, object()), 10**12)
            )
            assert await asyncio.to_thread(started.wait, 3)
            async with server.connection() as conn:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '100ms'")
                        await repository.discharge_mutation_obligation(
                            conn, attempt, reason="restored"
                        )
            release.set()
            prepared = await task
            assert len(storage.pool.volumes) == 2
            installed = RemoteModuleResultV1.model_validate(_result()).model_copy(
                update={
                    "system_id": operation.system_id,
                    "run_id": operation.run_id,
                    "operation_nonce": operation.operation_nonce,
                }
            )
            restored = installed.model_copy(
                update={
                    "phase": "restored",
                    "capture_manifest": None,
                    "capture_absent": True,
                    "entry_count": None,
                    "content_bytes": None,
                }
            )
            installed_baseline = restored.model_copy(
                update={
                    "phase": "installed",
                    "entry_count": installed.entry_count,
                    "content_bytes": installed.content_bytes,
                }
            )
            recovery = _recovery(restored).model_copy(
                update={
                    "pool": OpaqueProviderRef(ref="systems"),
                    "source_volume": OpaqueProviderRef(ref=prepared.source.name),
                    "scratch_volume": OpaqueProviderRef(ref=prepared.scratch.name),
                    "source_capacity_bytes": prepared.source.capacity_bytes,
                    "installed_entry_count": installed.entry_count,
                    "installed_content_bytes": installed.content_bytes,
                    "result_identity": identity_for(installed_baseline),
                }
            )
            terminal = RemoteModuleOperationRuntime._restore_operation(operation, restored)
            evidence = ModuleAttemptTerminalEvidence(
                terminal_operation=terminal.model_dump(mode="json"),
                terminal_operation_identity=identity_for(terminal),
                terminal_result=restored.model_dump(mode="json"),
                terminal_result_identity=identity_for(restored),
                baseline_operation_identity=recovery.operation_identity,
                baseline_result_identity=recovery.result_identity,
                installed_entry_count=recovery.installed_entry_count or 0,
                installed_content_bytes=recovery.installed_content_bytes or 0,
                recovery_reference=recovery.model_dump(mode="json"),
            )
            async with server.connection() as conn, conn.transaction():
                await repository.record_terminal_evidence(conn, attempt, evidence)

            async def absent(_recovery: RemoteModuleRecoveryRefV2) -> None:
                return None

            restarted = RemoteModuleOperationRuntime(
                worker,
                repository,
                absent,
                volume_recovery=RemoteModuleVolumeRecovery(storage, "systems"),
            )
            assert await restarted.reopen_operation(recovery) == terminal
            assert restarted._recovery_volumes(terminal, recovery) == prepared
            async with server.connection() as conn, conn.transaction():
                await repository.discharge_mutation_obligation(conn, attempt, reason="restored")
            before = len(storage.pool.volumes)
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await runtime.prepare(receipt, operation, executor, cast(Any, object()), 10**12)
            assert len(storage.pool.volumes) == before
        executor.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize(
    "case", ["success", "missing", "changed", "provider-failure", "foreign", "swapped"]
)
def test_run_returns_only_exact_durable_appliance_result(case: str, tmp_path: Path) -> None:
    operation = appliance_operation()
    clock = ApplianceClock()
    reference = appliance_request(clock, result=success_result())
    reads = [success_result(), success_result()]
    if case == "missing":
        reads = [None]
    elif case == "changed":
        changed = RemoteModuleResultV1.from_wire_bytes(success_result()).model_copy(
            update={"installed_manifest": "sha256:" + "f" * 64}
        )
        reads = [success_result(), changed.to_wire_bytes()]
    appliance = ApplianceConn([1] if case == "provider-failure" else [], clock)

    observed_deadlines: list[float] = []

    def read(_scratch: object, deadline: float) -> bytes | None:
        observed_deadlines.append(deadline)
        return reads.pop(0)

    manifest = operation.source_manifest

    class Writer:
        operation = b""

        def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
            self.operation = operation
            path = tmp_path / "run-source.ext4"
            path.write_bytes(b"image")
            evidence = SourceFilesystemEvidence(operation, manifest, 1, 3)
            return BuiltSourceImage(path, 5, evidence)

        def inspect(self, path: Path) -> SourceFilesystemEvidence:
            assert path.read_bytes() == b"image"
            return SourceFilesystemEvidence(self.operation, manifest, 1, 3)

    storage = Conn()
    entries = (ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),)
    inspection: Callable[[], AttachmentInspection]
    volume_config = RemoteModuleVolumePreparation(
        storage,
        "pool",
        entries,
        Writer(),
        lambda identity_port, present_attempt_volumes=None: inspection(),
        tmp_path,
    )
    runtime = _runtime(lambda _recovery: asyncio.sleep(0, result=None))
    object.__setattr__(runtime, "volume_preparation", volume_config)
    wanted = runtime._volume_request(operation)
    volumes = prepare_attempt_volumes(storage, wanted)
    if case == "foreign":
        volumes = type(volumes)(replace(volumes.source, operation_nonce="f" * 32), volumes.scratch)
    elif case == "swapped":
        volumes = type(volumes)(volumes.scratch, volumes.source)

    def inspection() -> AttachmentInspection:
        return AttachmentInspection(
            True,
            True,
            False,
            frozenset({("pool", volumes.source.name), ("pool", volumes.scratch.name)}),
        )

    object.__setattr__(
        runtime,
        "appliance_execution",
        RemoteModuleApplianceExecution(
            appliance=appliance,
            architecture=reference.architecture,
            emulator_path=reference.emulator_path,
            memory_kib=reference.memory_kib,
            vcpus=reference.vcpus,
            appliance_volume=reference.appliance_volume,
            appliance_image_digest=reference.appliance_image_digest,
            root=lambda _operation: volume("root", "root"),
            read_scratch_result=read,
            inspect_attachments=inspection,
            secret_registry=reference.secret_registry,
            deadline_executor=ApplianceExecutor(),
            monotonic=clock,
        ),
    )
    executor = RemoteModulePreparationExecutor()
    if case == "success":
        result = asyncio.run(runtime.run(operation, volumes, executor, 300.0))
        assert result == RemoteModuleResultV1.from_wire_bytes(success_result())
    else:
        with pytest.raises((CategorizedError, RuntimeError)):
            asyncio.run(runtime.run(operation, volumes, executor, 300.0))
        if case in {"foreign", "swapped"}:
            assert appliance.domain is None
    if observed_deadlines:
        assert set(observed_deadlines) == {300.0}
    executor.shutdown()


@pytest.mark.parametrize(
    ("cleanup_fault", "preparation_fault"),
    [
        ("reaping-marker", "before-upload"),
        ("reaping-marker", "before-scratch"),
        ("reaping-marker", "before-appliance"),
        ("source-delete", None),
        ("scratch-delete", None),
        ("reaped-marker", None),
        ("discharge", None),
    ],
    ids=lambda value: str(value),
)
def test_real_runtime_and_database_resume_at_cleanup_boundaries(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fault: str,
    preparation_fault: str | None,
) -> None:
    class Writer:
        operation = b""

        def build(self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]) -> BuiltSourceImage:
            del entries
            self.operation = operation
            path = tmp_path / "phase-source.ext4"
            path.write_bytes(b"image" + bytes(4091))
            return BuiltSourceImage(
                path,
                4096,
                SourceFilesystemEvidence(operation, "sha256:" + "b" * 64, 0, 0),
            )

        def inspect(self, path: Path) -> SourceFilesystemEvidence:
            assert path.read_bytes().startswith(b"image")
            return SourceFilesystemEvidence(self.operation, "sha256:" + "b" * 64, 0, 0)

    class FailOnceDomain(ApplianceDomain):
        def __init__(self, xml: str) -> None:
            super().__init__(xml)
            self.destroy_calls = 0

        def destroy(self) -> int:
            self.destroy_calls += 1
            if self.destroy_calls == 1:
                raise RuntimeError("injected worker loss during teardown")
            return super().destroy()

    class PersistentAppliance(ApplianceConn):
        creates = 0

        def createXML(self, xml: str, flags: int = 0):  # noqa: N802
            self.creates += 1
            self.created_flags = flags
            self.domain = FailOnceDomain(xml)
            return self.domain

        def lookupByName(self, name: str):  # noqa: N802
            if self.domain is not None and self.domain.destroyed:
                self.domain = None
            return super().lookupByName(name)

    async def run() -> None:
        repository = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
        base = appliance_operation()
        operation = base.model_copy(update={"system_id": str(system_id), "run_id": str(run_id)})
        attempt = ModuleAttempt(system_id, run_id, operation.operation_nonce)
        credential = SecretStr("phase-worker-credential")
        job_id = uuid4()
        preparation = ModuleAttemptPreparationRequestV1.model_validate(
            {
                "module_attempt_obligation": {
                    "system_id": system_id,
                    "run_id": run_id,
                    "operation_nonce": operation.operation_nonce,
                }
            }
        )
        context = ModuleAttemptWorkerWriteContext(job_id, 1, credential, preparation)
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            await admin.execute(
                "INSERT INTO worker_incarnations "
                "(incarnation, authority_kind, authority_binding, fence_protocol, credential_hash) "
                "VALUES (%s, 'docker', %s, 4, %s)",
                ("phase-worker", Jsonb({"container_id": "a" * 64}), context.credential_hash),
            )
            await admin.execute(
                "INSERT INTO jobs "
                "(id, kind, payload, state, attempt, max_attempts, worker_id, "
                "lease_expires_at, authorizing, dedup_key) "
                "VALUES (%s, 'boot', %s, 'running', 1, 3, 'phase-worker', "
                "now() + interval '5 minutes', %s, %s)",
                (
                    job_id,
                    Jsonb(
                        {
                            "run_id": str(run_id),
                            "remote_module_attempt_v1": preparation.model_dump(
                                mode="json", by_alias=True
                            ),
                        }
                    ),
                    Jsonb({"principal": "phase-test", "project": "phase-test"}),
                    f"phase-{job_id}",
                ),
            )
            await admin.commit()
        installed = RemoteModuleResultV1.from_wire_bytes(success_result()).model_copy(
            update={"system_id": operation.system_id, "run_id": operation.run_id}
        )
        scratch_result: list[bytes | None] = [
            None if preparation_fault == "before-appliance" else installed.to_wire_bytes()
        ]
        storage = Conn()
        clock = ApplianceClock()
        appliance = PersistentAppliance([], clock)
        executor = RemoteModulePreparationExecutor()
        authority_reference = OpaqueProviderRef(ref="authority/resource-bound")
        source_name = render_module_volume_name(
            operation.system_id, operation.run_id, operation.operation_nonce, "source.ext4"
        )
        scratch_name = render_module_volume_name(
            operation.system_id, operation.run_id, operation.operation_nonce, "scratch.ext4"
        )

        def attachments() -> AttachmentInspection:
            return AttachmentInspection(
                True,
                True,
                False,
                frozenset({("systems", source_name), ("systems", scratch_name)}),
            )

        volume_config = RemoteModuleVolumePreparation(
            storage,
            "systems",
            (),
            Writer(),
            lambda identity_port, present_attempt_volumes=None: AttachmentInspection(
                True,
                True,
                False,
                frozenset(
                    ("systems", name)
                    for name in (present_attempt_volumes or {source_name, scratch_name})
                ),
            ),
            tmp_path,
        )
        appliance_config = RemoteModuleApplianceExecution(
            appliance,
            "x86_64",
            "/usr/bin/qemu-system-x86_64",
            262_144,
            1,
            "appliance-x86_64.qcow2",
            operation.appliance_image_digest,
            lambda value: PreparedVolume(
                "systems",
                value.root_volume.key,
                value.system_id,
                value.run_id,
                value.operation_nonce,
                "root",
                value.root_volume.identity,
                4096,
            ),
            lambda _scratch, _deadline: scratch_result[0],
            attachments,
            appliance_request(clock).secret_registry,
            CompletionDeadlineExecutor(clock),
            clock,
        )
        monkeypatch.setattr(
            "kdive.services.remote_module_volume_preparation.build_remote_device_identity_port",
            lambda authority, _deadline: authority,
        )
        monkeypatch.setattr(
            Stream,
            "sparseRecvAll",
            lambda stream, data, _hole, opaque: data(stream, stream.download_payload, opaque),
            raising=False,
        )
        monkeypatch.setattr(
            "kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_result_reader._read_debugfs",
            lambda _image, _deadline, _monotonic: scratch_result[0],
        )
        async with (
            AsyncConnectionPool(
                authority_role_dsns("kdive_server"), min_size=1, max_size=1
            ) as server,
            AsyncConnectionPool(
                authority_role_dsns("kdive_worker"), min_size=1, max_size=1
            ) as worker,
        ):
            receipt = await open_module_attempt_preparation(server, repository, attempt)

            def preparation_runtime() -> RemoteModuleOperationRuntime:
                async def read(
                    _recovery: RemoteModuleRecoveryRefV2, _deadline: float
                ) -> bytes | None:
                    return scratch_result[0]

                return RemoteModuleOperationRuntime(
                    worker,
                    repository,
                    read,
                    volume_config,
                    appliance_config,
                    worker_write_context=context,
                )

            reader = SparseRemoteModuleResultReader(
                storage,
                tmp_path,
                appliance_config.deadline_executor,
                appliance_config.monotonic,
                executor,
            )

            def recovery_runtime() -> RemoteModuleOperationRuntime:
                async def read(
                    recovery: RemoteModuleRecoveryRefV2, deadline: float
                ) -> bytes | None:
                    return await reader.read_recovery_async(recovery, deadline=deadline)

                return RemoteModuleOperationRuntime(
                    worker,
                    repository,
                    read,
                    appliance_execution=appliance_config,
                    volume_recovery=RemoteModuleVolumeRecovery(storage, "systems"),
                    worker_write_context=context,
                )

            request = CaptureInstallRequest(
                receipt,
                operation,
                cast(Any, object()),
                authority_reference,
            )
            if preparation_fault == "before-upload":
                create_xml = storage.pool.createXML
                interrupted_source: list[Any] = []

                def create_then_die_before_upload(xml: str, flags: int = 0):
                    created = create_xml(xml, flags)
                    if "source.ext4" in xml:
                        original_upload = created.upload

                        def die_before_upload(*_args: object, **_kwargs: object) -> int:
                            raise SystemExit("worker died before source upload")

                        cast(Any, created).upload = die_before_upload
                        interrupted_source[:] = [created, original_upload]
                    return created

                cast(Any, storage.pool).createXML = create_then_die_before_upload
                with pytest.raises(SystemExit, match="before source upload"):
                    await capture_install_modules(
                        request,
                        runtime=preparation_runtime(),
                        executor=executor,
                        deadline=10**12,
                    )
                cast(Any, storage.pool).createXML = create_xml
                source, original_upload = interrupted_source
                source.upload = original_upload
                assert set(storage.pool.volumes) == {source_name}
                assert bytes(source.payload) == b""

            elif preparation_fault == "before-scratch":
                create_xml = storage.pool.createXML
                upload_calls = 0

                def create_then_die_before_scratch(xml: str, flags: int = 0):
                    nonlocal upload_calls
                    if "scratch.ext4" in xml:
                        raise SystemExit("worker died before scratch create")
                    created = create_xml(xml, flags)
                    original_upload = created.upload

                    def count_upload(
                        stream: object, offset: int, length: int, flags: int = 0
                    ) -> int:
                        nonlocal upload_calls
                        upload_calls += 1
                        return original_upload(stream, offset, length, flags)

                    cast(Any, created).upload = count_upload
                    return created

                cast(Any, storage.pool).createXML = create_then_die_before_scratch
                with pytest.raises(SystemExit, match="before scratch create"):
                    await capture_install_modules(
                        request,
                        runtime=preparation_runtime(),
                        executor=executor,
                        deadline=10**12,
                    )
                cast(Any, storage.pool).createXML = create_xml
                assert set(storage.pool.volumes) == {source_name}
                assert upload_calls == 1

            elif preparation_fault == "before-appliance":
                create_appliance = appliance.createXML

                def die_before_appliance(*_args: object, **_kwargs: object):
                    raise SystemExit("worker died before appliance result")

                cast(Any, appliance).createXML = die_before_appliance
                with pytest.raises(SystemExit, match="before appliance result"):
                    await capture_install_modules(
                        request,
                        runtime=preparation_runtime(),
                        executor=executor,
                        deadline=10**12,
                    )

                def create_appliance_and_publish(xml: str, flags: int = 0):
                    created = create_appliance(xml, flags)
                    scratch_result[0] = installed.to_wire_bytes()
                    return created

                cast(Any, appliance).createXML = create_appliance_and_publish
                existing_volumes = dict(storage.pool.volumes)
                assert set(existing_volumes) == {source_name, scratch_name}

            with pytest.raises(RuntimeError, match="worker loss during teardown"):
                await capture_install_modules(
                    request, runtime=preparation_runtime(), executor=executor, deadline=10**12
                )
            restarted = preparation_runtime()
            recovery = await capture_install_modules(
                request, runtime=restarted, executor=executor, deadline=10**12
            )
            if preparation_fault == "before-appliance":
                assert all(
                    storage.pool.volumes[name] is volume
                    for name, volume in existing_volumes.items()
                )
            if preparation_fault == "before-scratch":
                assert upload_calls == 1
            assert appliance.creates == 1
            assert recovery.source_capacity_bytes == 4096
            async with server.connection() as conn:
                assert await repository.mutation_obligation_is_open(conn, attempt) is True

            restored = installed.model_copy(
                update={"phase": "restored", "entry_count": None, "content_bytes": None}
            )
            scratch_result[0] = restored.to_wire_bytes()
            foreign = object()
            storage.pool.volumes["foreign-volume"] = cast(Any, foreign)
            source = storage.pool.volumes[source_name]
            scratch = storage.pool.volumes[scratch_name]
            selected = source if cleanup_fault == "source-delete" else scratch
            original_delete = selected.delete
            fail_once = cleanup_fault in {"source-delete", "scratch-delete"}

            def delete_once(flags: int = 0) -> int:
                nonlocal fail_once
                if fail_once:
                    fail_once = False
                    raise RuntimeError("injected worker loss")
                return original_delete(flags)

            cast(Any, selected).delete = delete_once
            create_xml = storage.pool.createXML
            marker_fail_once = cleanup_fault in {"reaping-marker", "reaped-marker"}

            def create_marker_once(xml: str, flags: int = 0):
                nonlocal marker_fail_once
                if marker_fail_once and f"{cleanup_fault.removesuffix('-marker')}.journal" in xml:
                    marker_fail_once = False
                    raise RuntimeError("injected worker loss")
                return create_xml(xml, flags)

            cast(Any, storage.pool).createXML = create_marker_once
            discharge = repository.worker_discharge_reap_obligation
            discharge_fail_once = cleanup_fault == "discharge"

            async def discharge_once(*args: Any, **kwargs: Any) -> bool:
                nonlocal discharge_fail_once
                if discharge_fail_once:
                    discharge_fail_once = False
                    raise RuntimeError("injected worker loss")
                return await discharge(*args, **kwargs)

            monkeypatch.setattr(repository, "worker_discharge_reap_obligation", discharge_once)
            with pytest.raises(RuntimeError, match="worker loss"):
                await restore_modules(
                    recovery,
                    authority_reference,
                    runtime=recovery_runtime(),
                    executor=executor,
                    deadline=10**12,
                )
            completed = await restore_modules(
                recovery,
                authority_reference,
                runtime=recovery_runtime(),
                executor=executor,
                deadline=10**12,
            )
            assert completed == restored
            assert storage.pool.volumes["foreign-volume"] is foreign
            assert storage.pool.volumes[source_name].deleted is True
            assert storage.pool.volumes[scratch_name].deleted is True
            async with server.connection() as conn:
                retained = await repository.retained_owners(conn)
            owner = next(item for item in retained if item.attempt == attempt)
            assert owner.mutation_retained is True
            assert owner.reap_retained is False
        executor.shutdown()

    asyncio.run(run())


def test_run_cancellation_waits_for_provider_cleanup(tmp_path: Path) -> None:
    async def run() -> None:
        operation = appliance_operation()
        clock = ApplianceClock()
        reference = appliance_request(clock, result=success_result())
        release = threading.Event()
        appliance = BlockingConsoleConn(release, clock)
        storage = Conn()
        entries = (ModuleTreeEntry("kernel.ko", 0o100644, content=b"abc"),)
        manifest = operation.source_manifest

        class Writer:
            operation_bytes = b""

            def build(
                self, operation: bytes, entries: tuple[ModuleTreeEntry, ...]
            ) -> BuiltSourceImage:
                del entries
                self.operation_bytes = operation
                path = tmp_path / "cancel-source.ext4"
                path.write_bytes(b"image")
                evidence = SourceFilesystemEvidence(operation, manifest, 1, 3)
                return BuiltSourceImage(path, 5, evidence)

            def inspect(self, path: Path) -> SourceFilesystemEvidence:
                assert path.read_bytes() == b"image"
                return SourceFilesystemEvidence(self.operation_bytes, manifest, 1, 3)

        runtime = _runtime(lambda _recovery: asyncio.sleep(0, result=None))
        object.__setattr__(
            runtime,
            "volume_preparation",
            RemoteModuleVolumePreparation(
                storage,
                "pool",
                entries,
                Writer(),
                lambda identity_port, present_attempt_volumes=None: reference.inspect_attachments(),
                tmp_path,
            ),
        )
        object.__setattr__(
            runtime,
            "appliance_execution",
            RemoteModuleApplianceExecution(
                appliance,
                reference.architecture,
                reference.emulator_path,
                reference.memory_kib,
                reference.vcpus,
                reference.appliance_volume,
                reference.appliance_image_digest,
                lambda _operation: volume("root", "root"),
                lambda _scratch, _deadline: success_result(),
                reference.inspect_attachments,
                reference.secret_registry,
                ApplianceExecutor(),
                clock,
            ),
        )
        executor = RemoteModulePreparationExecutor()
        volumes = prepare_attempt_volumes(storage, runtime._volume_request(operation))
        task = asyncio.create_task(runtime.run(operation, volumes, executor, 300.0))
        while appliance.stream is None:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert len((volumes.source, volumes.scratch)) == 2
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        executor.shutdown()

    asyncio.run(run())


def test_delete_scratch_commits_reap_evidence_before_exact_owned_delete(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class Transaction:
        async def __aenter__(self) -> None:
            events.append("transaction-enter")

        async def __aexit__(self, *_exc: object) -> None:
            events.append("transaction-exit")

    class DbConnection:
        def transaction(self) -> Transaction:
            return Transaction()

    @asynccontextmanager
    async def connection() -> AsyncIterator[DbConnection]:
        yield DbConnection()

    class EvidenceRepository:
        evidence: ModuleAttemptTerminalEvidence | None = None

        async def read_terminal_evidence(self, _conn: object, _attempt: object):
            return self.evidence

        async def worker_record_terminal_evidence(
            self,
            _conn: object,
            _context: object,
            _attempt: object,
            evidence: ModuleAttemptTerminalEvidence,
        ) -> bool:
            events.append("evidence")
            self.evidence = evidence
            events.append("reap-open")
            return True

        async def worker_discharge_reap_obligation(self, *_args: object) -> bool:
            events.append("reap-discharge")
            return True

    storage = Conn()
    wanted = volume_request(tmp_path)
    volumes = prepare_attempt_volumes(storage, wanted)
    storage.pool.volumes[volumes.source.name].capacity = 64 * 1024**2
    volumes = replace(volumes, source=replace(volumes.source, capacity_bytes=64 * 1024**2))
    cast(Any, storage.pool).refresh = lambda _flags=0: 0
    cast(Any, storage.pool).listAllVolumes = lambda _flags=0: [
        item for item in storage.pool.volumes.values() if not item.deleted
    ]
    unrelated = storage.pool.createXML(
        "<volume><name>operator-volume</name><capacity>1</capacity>"
        "<target><format type='raw'/></target></volume>"
    )
    result = RemoteModuleResultV1.model_validate(_result()).model_copy(
        update={
            "system_id": wanted.operation.system_id,
            "run_id": wanted.operation.run_id,
            "operation_nonce": wanted.operation.operation_nonce,
            "release": wanted.operation.release,
            "root_volume_key": wanted.operation.root_volume.key,
            "root_volume_identity": wanted.operation.root_volume.identity,
            "appliance_image_digest": wanted.operation.appliance_image_digest,
        }
    )
    recovery = _recovery(result).model_copy(
        update={
            "pool": OpaqueProviderRef(ref="systems"),
            "source_volume": OpaqueProviderRef(ref=volumes.source.name),
            "scratch_volume": OpaqueProviderRef(ref=volumes.scratch.name),
            "installed_entry_count": result.entry_count,
            "installed_content_bytes": result.content_bytes,
        }
    )
    repository = EvidenceRepository()

    async def read_scratch(_recovery: RemoteModuleRecoveryRefV2) -> bytes | None:
        scratch = storage.pool.volumes[volumes.scratch.name]
        return None if scratch.deleted else result.to_wire_bytes()

    runtime = RemoteModuleOperationRuntime(
        cast(Any, SimpleNamespace(connection=connection)),
        cast(Any, repository),
        read_scratch,
        RemoteModuleVolumePreparation(
            storage,
            wanted.pool,
            wanted.entries,
            wanted.writer,
            lambda identity_port, present_attempt_volumes=None: wanted.inspect_attachments(),
            tmp_path,
        ),
        worker_write_context=_worker_context(recovery),
    )
    object.__setattr__(
        runtime,
        "appliance_execution",
        cast(
            Any,
            SimpleNamespace(
                inspect_attachments=lambda: AttachmentInspection(
                    True,
                    True,
                    False,
                    frozenset(
                        {
                            (wanted.pool, volumes.source.name),
                            (wanted.pool, volumes.scratch.name),
                        }
                    ),
                )
            ),
        ),
    )
    original_delete = storage.pool.volumes[volumes.scratch.name].delete

    def delete(flags: int = 0) -> int:
        events.append("scratch-delete")
        return original_delete(flags)

    cast(Any, storage.pool.volumes[volumes.scratch.name]).delete = delete
    executor = RemoteModulePreparationExecutor()
    altered = recovery.model_copy(
        update={"source_capacity_bytes": recovery.source_capacity_bytes + 4096}
    )
    with pytest.raises(CategorizedError):
        asyncio.run(runtime.delete_source(altered, executor))
    assert not storage.pool.volumes[volumes.source.name].deleted
    asyncio.run(runtime.delete_scratch(recovery, executor))
    asyncio.run(runtime.delete_scratch(recovery, executor))
    create_xml = storage.pool.createXML
    failed_once = False

    def fail_first_marker(xml: str, flags: int = 0):
        nonlocal failed_once
        if not failed_once and "reaping.journal" in xml:
            failed_once = True
            raise RuntimeError("marker create failed")
        return create_xml(xml, flags)

    cast(Any, storage.pool).createXML = fail_first_marker
    with pytest.raises(RuntimeError, match="marker create failed"):
        asyncio.run(runtime.record_reaping(recovery, executor))
    assert "reap-open" in events
    assert not any("reaping.journal" in name for name in storage.pool.volumes)
    asyncio.run(runtime.record_reaping(recovery, executor))
    restarted = replace(
        runtime,
        volume_preparation=None,
        volume_recovery=RemoteModuleVolumeRecovery(storage, wanted.pool),
    )
    asyncio.run(restarted.record_reaping(recovery, executor))
    asyncio.run(restarted.record_reaped(recovery, executor))
    for name, stored_volume in storage.pool.volumes.items():
        cast(Any, stored_volume).name = lambda name=name: name
    inventory = asyncio.run(runtime.inventory(executor))
    executor.shutdown()

    assert events.index("evidence") < events.index("reap-open") < events.index("scratch-delete")
    assert storage.pool.volumes[volumes.scratch.name].deleted
    assert not unrelated.deleted
    assert [item.kind for item in inventory] == [
        "source.ext4",
        "reaping.journal",
        "reaped.journal",
    ]
    assert events.index("reap-open") < events.index("reap-discharge")


def test_runtime_reap_uses_landed_reaper_with_live_retention_callback() -> None:
    events: list[str] = []

    class Reaper:
        async def reap_module_volumes(self, retained: Callable[[], Awaitable[object]]) -> int:
            events.append("enumerated")
            assert await retained() == ()
            return 3

    runtime = replace(
        _runtime(lambda _recovery: asyncio.sleep(0)),
        module_volume_reaper=cast(Any, Reaper()),
    )

    async def retained() -> tuple[()]:
        events.append("retained")
        return ()

    assert asyncio.run(runtime.reap(retained)) == 3
    assert events == ["enumerated", "retained"]


def test_runtime_teardown_provider_failure_is_retryable(tmp_path: Path) -> None:
    result = RemoteModuleResultV1.from_wire_bytes(success_result())
    operation = appliance_operation()
    recovery = _recovery(result)
    clock = ApplianceClock()
    reference = appliance_request(clock)

    class FlakyAppliance(ApplianceConn):
        fail = True

        def lookupByName(self, name: str):  # noqa: N802
            if self.fail:
                self.fail = False
                raise RuntimeError("provider lookup failed")
            return super().lookupByName(name)

    appliance = FlakyAppliance([], clock)
    wanted = volume_request(tmp_path)

    async def read(_recovery: RemoteModuleRecoveryRefV2, _deadline: float) -> bytes:
        return result.to_wire_bytes()

    runtime = _runtime(read)
    object.__setattr__(
        runtime,
        "volume_preparation",
        RemoteModuleVolumePreparation(
            Conn(),
            "pool",
            wanted.entries,
            wanted.writer,
            lambda identity_port, present_attempt_volumes=None: detached(),
            tmp_path,
        ),
    )
    expected = expected_attempt_volumes(
        runtime._volume_request(operation), recovery.source_capacity_bytes
    )

    def detached() -> AttachmentInspection:
        return AttachmentInspection(
            True,
            True,
            False,
            frozenset({("pool", expected.source.name), ("pool", expected.scratch.name)}),
        )

    object.__setattr__(
        runtime,
        "appliance_execution",
        RemoteModuleApplianceExecution(
            appliance,
            reference.architecture,
            reference.emulator_path,
            reference.memory_kib,
            reference.vcpus,
            reference.appliance_volume,
            reference.appliance_image_digest,
            lambda _operation: volume("root", "root"),
            lambda _scratch, _deadline: result.to_wire_bytes(),
            detached,
            reference.secret_registry,
            ApplianceExecutor(),
            clock,
        ),
    )
    executor = RemoteModulePreparationExecutor()
    with pytest.raises(RuntimeError, match="provider lookup failed"):
        asyncio.run(runtime.teardown(recovery, executor, 300.0))
    restarted = replace(
        runtime,
        volume_preparation=None,
        volume_recovery=RemoteModuleVolumeRecovery(
            cast(RemoteModuleVolumePreparation, runtime.volume_preparation).storage,
            "pool",
        ),
    )
    observed = asyncio.run(restarted.teardown(recovery, executor, 300.0))
    executor.shutdown()

    assert observed.complete
    assert operation.system_id == recovery.system_id


def test_delete_scratch_does_not_delete_when_reap_evidence_rolls_back(tmp_path: Path) -> None:
    class Transaction:
        async def __aenter__(self) -> None:
            return None

        async def __aexit__(self, *_exc: object) -> None:
            return None

    class DbConnection:
        def transaction(self) -> Transaction:
            return Transaction()

    @asynccontextmanager
    async def connection() -> AsyncIterator[DbConnection]:
        yield DbConnection()

    class FailingRepository:
        async def read_terminal_evidence(self, _conn: object, _attempt: object):
            return None

        async def worker_record_terminal_evidence(self, *_args: object) -> bool:
            raise RuntimeError("transaction rolled back")

    storage = Conn()
    wanted = volume_request(tmp_path)
    volumes = prepare_attempt_volumes(storage, wanted)
    storage.pool.volumes[volumes.source.name].capacity = 64 * 1024**2
    volumes = replace(volumes, source=replace(volumes.source, capacity_bytes=64 * 1024**2))
    result = RemoteModuleResultV1.model_validate(_result()).model_copy(
        update={
            "system_id": wanted.operation.system_id,
            "run_id": wanted.operation.run_id,
            "operation_nonce": wanted.operation.operation_nonce,
            "release": wanted.operation.release,
            "root_volume_key": wanted.operation.root_volume.key,
            "root_volume_identity": wanted.operation.root_volume.identity,
            "appliance_image_digest": wanted.operation.appliance_image_digest,
        }
    )
    recovery = _recovery(result).model_copy(
        update={
            "pool": OpaqueProviderRef(ref=wanted.pool),
            "source_volume": OpaqueProviderRef(ref=volumes.source.name),
            "scratch_volume": OpaqueProviderRef(ref=volumes.scratch.name),
            "source_capacity_bytes": volumes.source.capacity_bytes,
            "installed_entry_count": result.entry_count,
            "installed_content_bytes": result.content_bytes,
        }
    )

    async def read_scratch(_recovery: RemoteModuleRecoveryRefV2) -> bytes:
        return result.to_wire_bytes()

    runtime = RemoteModuleOperationRuntime(
        cast(Any, SimpleNamespace(connection=connection)),
        cast(Any, FailingRepository()),
        read_scratch,
        RemoteModuleVolumePreparation(
            storage,
            wanted.pool,
            wanted.entries,
            wanted.writer,
            lambda identity_port, present_attempt_volumes=None: wanted.inspect_attachments(),
            tmp_path,
        ),
        worker_write_context=_worker_context(recovery),
    )
    executor = RemoteModulePreparationExecutor()
    with pytest.raises(RuntimeError, match="rolled back"):
        asyncio.run(runtime.delete_scratch(recovery, executor))
    executor.shutdown()

    assert not storage.pool.volumes[volumes.scratch.name].deleted


def test_delete_source_cancellation_waits_for_blocked_provider_delete(tmp_path: Path) -> None:
    storage = Conn()
    wanted = volume_request(tmp_path)
    volumes = prepare_attempt_volumes(storage, wanted)
    storage.pool.volumes[volumes.source.name].capacity = 64 * 1024**2
    volumes = replace(volumes, source=replace(volumes.source, capacity_bytes=64 * 1024**2))
    result = RemoteModuleResultV1.model_validate(_result()).model_copy(
        update={
            "system_id": wanted.operation.system_id,
            "run_id": wanted.operation.run_id,
            "operation_nonce": wanted.operation.operation_nonce,
            "release": wanted.operation.release,
            "root_volume_key": wanted.operation.root_volume.key,
            "root_volume_identity": wanted.operation.root_volume.identity,
            "appliance_image_digest": wanted.operation.appliance_image_digest,
        }
    )
    recovery = _recovery(result).model_copy(
        update={
            "pool": OpaqueProviderRef(ref=wanted.pool),
            "source_volume": OpaqueProviderRef(ref=volumes.source.name),
            "scratch_volume": OpaqueProviderRef(ref=volumes.scratch.name),
            "source_capacity_bytes": volumes.source.capacity_bytes,
        }
    )

    async def read_scratch(_recovery: RemoteModuleRecoveryRefV2) -> bytes:
        return result.to_wire_bytes()

    runtime = _runtime(read_scratch)
    object.__setattr__(
        runtime,
        "volume_preparation",
        RemoteModuleVolumePreparation(
            storage,
            wanted.pool,
            wanted.entries,
            wanted.writer,
            lambda identity_port, present_attempt_volumes=None: wanted.inspect_attachments(),
            tmp_path,
        ),
    )
    object.__setattr__(
        runtime,
        "appliance_execution",
        cast(
            Any,
            SimpleNamespace(
                inspect_attachments=lambda: AttachmentInspection(
                    True,
                    True,
                    False,
                    frozenset(
                        {
                            (wanted.pool, volumes.source.name),
                            (wanted.pool, volumes.scratch.name),
                        }
                    ),
                )
            ),
        ),
    )
    entered = threading.Event()
    release = threading.Event()
    original_delete = storage.pool.volumes[volumes.source.name].delete

    def blocked_delete(flags: int = 0) -> int:
        entered.set()
        release.wait(timeout=5)
        return original_delete(flags)

    cast(Any, storage.pool.volumes[volumes.source.name]).delete = blocked_delete

    async def cancel_during_delete() -> None:
        executor = RemoteModulePreparationExecutor()
        task = asyncio.create_task(runtime.delete_source(recovery, executor))
        while not entered.is_set() and not task.done():
            await asyncio.sleep(0)
        if task.done():
            await task
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        executor.shutdown()

    asyncio.run(cancel_during_delete())
    assert storage.pool.volumes[volumes.source.name].deleted

"""Scratch-first reopen behavior."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptTerminalEvidence,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleApplianceExecution,
    RemoteModuleOperationRuntime,
    RemoteModuleVolumePreparation,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_preparation import (
    RemoteModulePreparationExecutor,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_volumes import (
    BuiltSourceImage,
    ModuleTreeEntry,
    SourceFilesystemEvidence,
)
from kdive.services.remote_module_attempt_preparation import (
    ModuleAttemptObligationVerificationError,
    open_module_attempt_preparation,
)
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.db.test_remote_module_attempt_obligations import _seed
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    BlockingConsoleConn,
    success_result,
    volume,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    Clock as ApplianceClock,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    Conn as ApplianceConn,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    Executor as ApplianceExecutor,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    operation as appliance_operation,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_appliance import (
    request as appliance_request,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_documents import _result
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_volumes import Conn


def _recovery(result: RemoteModuleResultV1) -> RemoteModuleRecoveryRefV1:
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
    return RemoteModuleRecoveryRefV1.model_validate(
        {
            "system_id": result.system_id,
            "run_id": result.run_id,
            "plan_identity": result.plan_identity,
            "operation_nonce": result.operation_nonce,
            "pool": OpaqueProviderRef(ref="pool"),
            "root_volume": OpaqueProviderRef(ref="root"),
            "source_volume": OpaqueProviderRef(ref="source"),
            "scratch_volume": OpaqueProviderRef(ref="scratch"),
            "operation_identity": identity_for(capture),
            "result_identity": identity_for(result),
            "appliance_image_digest": result.appliance_image_digest,
            "authority_identity": digest,
        }
    )


class Repo:
    async def read_terminal_evidence(self, conn: object, attempt: object):
        del conn, attempt
        return None


def _runtime(
    read: Callable[[RemoteModuleRecoveryRefV1], Awaitable[bytes | None]], repo: object | None = None
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

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes | None:
        return result.to_wire_bytes()

    runtime = _runtime(read)
    assert asyncio.run(runtime.reopen_result(_recovery(result))) == result


@pytest.mark.parametrize("raw", [b"not json\n", None])
def test_missing_or_malformed_scratch_is_redacted_conflict(raw: bytes | None) -> None:
    result = RemoteModuleResultV1.model_validate(_result())

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes | None:
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

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes:
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

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes:
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

    async def absent(_: RemoteModuleRecoveryRefV1) -> None:
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

            async def absent(_: RemoteModuleRecoveryRefV1) -> None:
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

    def prepare_volumes(_storage: object, volume_request: object, **_kwargs: object) -> object:
        volume_requests.append(volume_request)
        return prepared

    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation."
        "prepare_verified_remote_module_attempt",
        verified,
    )
    monkeypatch.setattr(
        "kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation."
        "prepare_attempt_volumes",
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
            inspect_attachments=cast(Any, lambda: None),
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
            path.write_bytes(b"image")
            evidence = SourceFilesystemEvidence(operation, "sha256:" + "d" * 64, 1, 3)
            return BuiltSourceImage(path, 5, evidence)

        def inspect(self, path: Path) -> SourceFilesystemEvidence:
            assert path.read_bytes() == b"image"
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
                    lambda: cast(Any, SimpleNamespace()),
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
            await task
            assert len(storage.pool.volumes) == 2
            async with server.connection() as conn, conn.transaction():
                await repository.discharge_mutation_obligation(conn, attempt, reason="restored")
            before = len(storage.pool.volumes)
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await runtime.prepare(receipt, operation, executor, cast(Any, object()), 10**12)
            assert len(storage.pool.volumes) == before
        executor.shutdown()

    asyncio.run(run())


@pytest.mark.parametrize("case", ["success", "missing", "changed", "provider-failure"])
def test_run_returns_only_exact_durable_appliance_result(case: str) -> None:
    operation = appliance_operation()
    clock = ApplianceClock()
    reference = appliance_request(clock, result=success_result())
    reads = [success_result(), success_result()]
    if case == "missing":
        reads = [None]
    elif case == "changed":
        changed = RemoteModuleResultV1.from_canonical_json(success_result()).model_copy(
            update={"installed_manifest": "sha256:" + "f" * 64}
        )
        reads = [success_result(), changed.to_canonical_json()]
    appliance = ApplianceConn([1] if case == "provider-failure" else [], clock)

    def read(_scratch: object) -> bytes | None:
        return reads.pop(0)

    runtime = _runtime(lambda _recovery: asyncio.sleep(0, result=None))
    object.__setattr__(runtime, "volume_preparation", cast(Any, SimpleNamespace(pool_name="pool")))
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
            inspect_attachments=reference.inspect_attachments,
            secret_registry=reference.secret_registry,
            deadline_executor=ApplianceExecutor(),
            monotonic=clock,
        ),
    )
    executor = RemoteModulePreparationExecutor()
    volumes = cast(
        Any,
        SimpleNamespace(source=volume("source", "source"), scratch=volume("scratch", "scratch")),
    )
    if case == "success":
        result = asyncio.run(runtime.run(operation, volumes, executor, 300.0))
        assert result == RemoteModuleResultV1.from_canonical_json(success_result())
    else:
        with pytest.raises((CategorizedError, RuntimeError)):
            asyncio.run(runtime.run(operation, volumes, executor, 300.0))
    executor.shutdown()


def test_run_cancellation_waits_for_provider_cleanup() -> None:
    async def run() -> None:
        operation = appliance_operation()
        clock = ApplianceClock()
        reference = appliance_request(clock, result=success_result())
        release = threading.Event()
        appliance = BlockingConsoleConn(release, clock)
        runtime = _runtime(lambda _recovery: asyncio.sleep(0, result=None))
        object.__setattr__(
            runtime, "volume_preparation", cast(Any, SimpleNamespace(pool_name="pool"))
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
                lambda _scratch: success_result(),
                reference.inspect_attachments,
                reference.secret_registry,
                ApplianceExecutor(),
                clock,
            ),
        )
        executor = RemoteModulePreparationExecutor()
        volumes = cast(
            Any,
            SimpleNamespace(
                source=volume("source", "source"), scratch=volume("scratch", "scratch")
            ),
        )
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

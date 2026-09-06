"""Scratch-first reopen behavior."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

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
    RemoteModuleOperationRuntime,
    RemoteModuleVolumePreparation,
)
from tests.db.test_remote_module_attempt_obligations import _seed
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_documents import _result


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

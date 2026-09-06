"""Scratch-first reopen behavior."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import cast

import pytest
from psycopg import AsyncConnection

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttemptTerminalEvidence,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleOperationRuntime,
)
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
    return RemoteModuleOperationRuntime(
        cast("AsyncConnection", SimpleNamespace()),
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

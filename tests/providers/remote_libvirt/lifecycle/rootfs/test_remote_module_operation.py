"""Scratch-first reopen behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from psycopg import AsyncConnection

from kdive.db.remote_module_attempt_obligations import RemoteModuleAttemptObligationRepository
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleRecoveryRefV1,
    RemoteModuleResultV1,
    identity_for,
)
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_operation import (
    RemoteModuleOperationRuntime,
)
from tests.providers.remote_libvirt.lifecycle.rootfs.test_remote_module_documents import _result


def _recovery(result: RemoteModuleResultV1) -> RemoteModuleRecoveryRefV1:
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
            "operation_identity": digest,
            "result_identity": identity_for(result),
            "appliance_image_digest": result.appliance_image_digest,
            "authority_identity": digest,
        }
    )


class Repo:
    async def read_terminal_evidence(self, conn: object, attempt: object):
        del conn, attempt
        return None


def test_scratch_result_reopens_before_terminal_evidence() -> None:
    result = RemoteModuleResultV1.model_validate(_result())

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes | None:
        return result.to_wire_bytes()

    runtime = RemoteModuleOperationRuntime(
        cast("AsyncConnection", SimpleNamespace()),
        cast("RemoteModuleAttemptObligationRepository", Repo()),
        read,
    )
    assert asyncio.run(runtime.reopen_result(_recovery(result))) == result


@pytest.mark.parametrize("raw", [b"not json\n", None])
def test_missing_or_malformed_scratch_is_redacted_conflict(raw: bytes | None) -> None:
    result = RemoteModuleResultV1.model_validate(_result())

    async def read(_: RemoteModuleRecoveryRefV1) -> bytes | None:
        return raw

    runtime = RemoteModuleOperationRuntime(
        cast("AsyncConnection", SimpleNamespace()),
        cast("RemoteModuleAttemptObligationRepository", Repo()),
        read,
    )
    with pytest.raises(CategorizedError) as caught:
        asyncio.run(runtime.reopen_result(_recovery(result)))
    assert caught.value.category is ErrorCategory.CONFLICT

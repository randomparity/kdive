"""Durable remote-module attempt obligation proofs (ADR-0588, migration 0126)."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import psycopg

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptTerminalEvidence,
)

_PLAN = "sha256:" + "a" * 64

_DIGEST = "sha256:" + "b" * 64

_MANIFEST = "sha256:" + "c" * 64

_TERMINAL_OPERATION_IDENTITY = "sha256:" + "1" * 64

_TERMINAL_RESULT_IDENTITY = "sha256:" + "2" * 64

_BASELINE_OPERATION_IDENTITY = "sha256:" + "3" * 64

_BASELINE_RESULT_IDENTITY = "sha256:" + "4" * 64


async def _seed(conn: psycopg.AsyncConnection) -> tuple[UUID, UUID]:
    """Insert the resource/allocation/system/investigation/run spine one attempt hangs off."""
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id, run_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'remote-libvirt', 'default', 'standard', 'available', 'qemu+tls://host/system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES "
        "(%s, %s, %s, 'remote-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return system_id, run_id


def _attempt(system_id: UUID, run_id: UUID, nonce: str = "0" * 32) -> ModuleAttempt:
    return ModuleAttempt(system_id=system_id, run_id=run_id, operation_nonce=nonce)


def _operation(attempt: ModuleAttempt) -> dict[str, Any]:
    return {
        "protocol": "remote-module-operation-v1",
        "operation": "restore",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "release": "6.12.0",
        "root_volume": {"key": "kdive-module-root", "identity": _DIGEST},
        "source_manifest": _MANIFEST,
        "installed_manifest": _MANIFEST,
        "capture_absent": True,
        "appliance_image_digest": _DIGEST,
    }


def _result(attempt: ModuleAttempt) -> dict[str, Any]:
    """A successful `restored` result, complete enough for RemoteModuleResultV1 to accept it.

    Its shape is the one `_validate_terminal_evidence` demands of a reap marker: the full
    nine-field identity, `installed_manifest` present, exactly one capture form, and the counts
    absent — which is what `restored` requires.
    """
    return {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "restored",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "appliance_image_digest": _DIGEST,
        "release": "6.12.0",
        "root_volume_key": "kdive-module-root",
        "root_volume_identity": _DIGEST,
        "source_manifest": _MANIFEST,
        "installed_manifest": _MANIFEST,
        "capture_absent": True,
    }


def _recovery_reference(attempt: ModuleAttempt) -> dict[str, Any]:
    return {
        "protocol": "remote-module-recovery-ref-v1",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "pool": {"ref": "pools/modules"},
        "root_volume": {"ref": "volumes/root"},
        "source_volume": {"ref": "volumes/source"},
        "scratch_volume": {"ref": "volumes/scratch"},
        "operation_identity": _TERMINAL_OPERATION_IDENTITY,
        "result_identity": _TERMINAL_RESULT_IDENTITY,
        "installed_entry_count": 42,
        "installed_content_bytes": 4096,
        "appliance_image_digest": _DIGEST,
        "authority_identity": _DIGEST,
    }


def _evidence(attempt: ModuleAttempt) -> ModuleAttemptTerminalEvidence:
    return ModuleAttemptTerminalEvidence(
        terminal_operation=_operation(attempt),
        terminal_operation_identity=_TERMINAL_OPERATION_IDENTITY,
        terminal_result=_result(attempt),
        terminal_result_identity=_TERMINAL_RESULT_IDENTITY,
        baseline_operation_identity=_BASELINE_OPERATION_IDENTITY,
        baseline_result_identity=_BASELINE_RESULT_IDENTITY,
        installed_entry_count=42,
        installed_content_bytes=4096,
        recovery_reference=_recovery_reference(attempt),
    )

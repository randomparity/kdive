"""Worker-side exact recovery-object quarantine disposition (#2204)."""

from __future__ import annotations

import hashlib
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.payloads import ResolveRecoveryOrphanPayload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    RecoveryObjectBinding,
)


def _refuse(message: str) -> CategorizedError:
    return CategorizedError(message, category=ErrorCategory.CONFLICT, terminal=True)


def _binding_digest(rows: list[dict[str, Any]]) -> str:
    canonical = "\0".join(
        f"{row['id']}:{row['ownership_digest']}:{row['observed_digest']}" for row in rows
    )
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


async def resolve_recovery_orphan_handler(
    conn: AsyncConnection, job: Job, *, resolver: ProviderResolver
) -> dict[str, object]:
    """Revalidate, observe, and disposition every object in one closed request."""
    payload = load_payload(job, ResolveRecoveryOrphanPayload)
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, system_id, disposition, binding_digest, object_ids, completed_at "
            "FROM external_boot_recovery_orphan_requests WHERE id = %s AND job_id = %s FOR UPDATE",
            (payload.request_id, job.id),
        )
        request = await cur.fetchone()
        if request is None or str(request["system_id"]) != payload.system_id:
            raise _refuse("recovery-object request does not match the claimed job")
        await cur.execute(
            "SELECT q.id, q.resource_id, q.system_id, q.activation_id, a.run_id, q.provider_kind, "
            "q.authority_instance, q.object_kind, q.object_reference, q.ownership_digest, "
            "q.observed_digest, q.status, r.kind AS resource_kind, "
            "q.operation_identity, q.attempt_id, q.mutation_journal_sequence, "
            "q.mutation_journal_digest "
            "FROM external_boot_recovery_quarantine AS q "
            "JOIN external_boot_activations AS a ON a.id = q.activation_id "
            "JOIN systems AS s ON s.id = q.system_id "
            "JOIN allocations AS al ON al.id = s.allocation_id "
            "JOIN resources AS r ON r.id = al.resource_id AND r.id = q.resource_id "
            "WHERE q.id = ANY(%s) ORDER BY q.object_identity FOR UPDATE OF q",
            (request["object_ids"],),
        )
        rows = await cur.fetchall()
    expected_status = "deleted" if request["disposition"] == "delete" else "adopted"
    if request["completed_at"] is not None and all(
        row["status"] == expected_status for row in rows
    ):
        return {
            "request_id": payload.request_id,
            "disposition": request["disposition"],
            "objects": len(rows),
        }
    if len(rows) != len(request["object_ids"]) or _binding_digest(rows) != payload.binding_digest:
        raise _refuse("recovery-object ownership evidence changed after admission")
    binding = await resolver.binding_for_system(conn, request["system_id"])
    port = binding.runtime.external_boot_recovery_objects
    if port is None or any(
        row["provider_kind"] != binding.kind.value or row["resource_kind"] != binding.kind.value
        for row in rows
    ):
        raise _refuse("recovery-object provider binding does not match the request")

    completed = 0
    for row in rows:
        expected = RecoveryObjectBinding(
            record_id=str(row["id"]),
            binding=ExternalBootActivationBinding(
                system_id=str(row["system_id"]),
                run_id=str(row["run_id"]),
                activation_id=str(row["activation_id"]),
            ),
            kind=row["object_kind"],
            reference=OpaqueProviderRef(ref=row["object_reference"]),
            ownership_digest=row["ownership_digest"],
            operation_identity=row["operation_identity"],
            attempt_id=str(row["attempt_id"]),
            mutation_journal_sequence=row["mutation_journal_sequence"],
            mutation_journal_digest=row["mutation_journal_digest"],
        )
        authority = OpaqueProviderRef(ref=row["authority_instance"])
        observation = port.observe_object(expected, authority)
        if observation.binding != expected:
            raise _refuse("provider observed a different recovery-object binding")
        disposition = request["disposition"]
        terminal = (disposition == "delete" and not observation.present) or (
            disposition == "adopt" and observation.present and observation.managed
        )
        if not terminal:
            if observation.observed_digest != row["observed_digest"]:
                raise _refuse("recovery-object observation changed after admission")
            observation = (
                port.delete_object(expected, authority, observation.observed_digest)
                if disposition == "delete"
                else port.adopt_object(expected, authority, observation.observed_digest)
            )
        if observation.binding != expected:
            raise _refuse("provider mutated a different recovery-object binding")
        if (disposition == "delete" and observation.present) or (
            disposition == "adopt" and (not observation.present or not observation.managed)
        ):
            raise _refuse("provider did not prove the requested recovery-object disposition")
        result = await conn.execute(
            "UPDATE external_boot_recovery_quarantine SET status = %s, observed_digest = %s, "
            "resolved_at = clock_timestamp(), disposition_job_id = %s "
            "WHERE id = %s AND status = 'quarantined' AND ownership_digest = %s",
            (
                "deleted" if disposition == "delete" else "adopted",
                observation.observed_digest,
                job.id,
                row["id"],
                row["ownership_digest"],
            ),
        )
        if result.rowcount != 1:
            raise _refuse("recovery-object disposition lost its durable compare-and-swap")
        completed += 1
    await conn.execute(
        "UPDATE external_boot_recovery_orphan_requests SET completed_at = clock_timestamp() "
        "WHERE id = %s AND completed_at IS NULL",
        (payload.request_id,),
    )
    return {
        "request_id": payload.request_id,
        "disposition": request["disposition"],
        "objects": completed,
    }


def register_handlers(registry: Any, *, resolver: ProviderResolver) -> None:
    """Register the platform-internal quarantine disposition handler."""
    from kdive.domain.operations.jobs import JobKind

    registry.register(
        JobKind.RESOLVE_RECOVERY_ORPHAN,
        lambda conn, job: resolve_recovery_orphan_handler(conn, job, resolver=resolver),
    )

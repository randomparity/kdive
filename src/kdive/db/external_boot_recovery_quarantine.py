"""Persist provider-proven cleanup residue as bounded quarantine inventory (#2204)."""

from __future__ import annotations

import hashlib
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection

from kdive.providers.ports.external_boot import RecoveryObjectObservation

MAX_QUARANTINE_OBJECTS = 64

_OWNER_SQL: LiteralString = (
    "SELECT e.system_id, e.run_id, e.plan_identity, a.provider_kind, a.authority_instance, "
    "al.resource_id, r.kind AS resource_kind FROM external_boot_activations e "
    "JOIN systems s ON s.id = e.system_id JOIN allocations al ON al.id = s.allocation_id "
    "JOIN resources r ON r.id = al.resource_id JOIN external_boot_authorities a "
    "ON a.activation_id = e.id AND a.system_id = e.system_id AND a.run_id = e.run_id "
    "AND a.plan_identity = e.plan_identity WHERE e.id = %s "
    "AND a.state IN ('current', 'retired') ORDER BY a.generation DESC LIMIT 1"
)


def _object_identity(observation: RecoveryObjectObservation) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            b"kdive-recovery-quarantine-object-v1\0" + observation.binding.to_canonical_json()
        ).hexdigest()
    )


async def record_cleanup_quarantine(
    conn: AsyncConnection,
    *,
    activation_id: UUID,
    observations: tuple[RecoveryObjectObservation, ...],
) -> tuple[str, ...]:
    """Record exact provider inventory using only durable activation/authority ownership."""
    if not 0 < len(observations) <= MAX_QUARANTINE_OBJECTS:
        raise ValueError("cleanup quarantine inventory must contain 1 through 64 objects")
    owner = await (await conn.execute(_OWNER_SQL, (activation_id,))).fetchone()
    if owner is None or owner[3] != owner[6]:
        raise ValueError("cleanup quarantine authority does not match the System Resource")
    system_id, run_id, _plan, provider_kind, authority_instance, resource_id, _kind = owner
    identities: list[str] = []
    async with conn.transaction():
        for observation in observations:
            binding = observation.binding
            if (
                not observation.present
                or observation.managed
                or binding.binding.activation_id != str(activation_id)
                or binding.binding.system_id != str(system_id)
                or binding.binding.run_id != str(run_id)
            ):
                raise ValueError("cleanup quarantine observation is not exact unmanaged residue")
            identity = _object_identity(observation)
            await conn.execute(
                "INSERT INTO external_boot_recovery_quarantine "
                "(id, object_identity, resource_id, system_id, activation_id, provider_kind, "
                "authority_instance, object_kind, object_reference, ownership_digest, "
                "observed_digest, operation_identity, attempt_id, mutation_journal_sequence, "
                "mutation_journal_digest, reserved_bytes) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (object_identity) DO NOTHING",
                (
                    UUID(binding.record_id),
                    identity,
                    resource_id,
                    system_id,
                    activation_id,
                    provider_kind,
                    authority_instance,
                    binding.kind,
                    binding.reference.ref,
                    binding.ownership_digest,
                    observation.observed_digest,
                    binding.operation_identity,
                    UUID(binding.attempt_id),
                    binding.mutation_journal_sequence,
                    binding.mutation_journal_digest,
                    binding.reserved_bytes,
                ),
            )
            identities.append(identity)
    return tuple(identities)

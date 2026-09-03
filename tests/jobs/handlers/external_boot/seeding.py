"""Seed the row set one authority-marked operation needs, and acknowledge it for real.

Modelled on ``tests/db/external_boot_authority_support.py::_seed_case``, but async and writing the
**vehicle's** ``RecoveryPoint``/``ExternalBootMaterialization`` canonical JSON rather than that
module's ``{schema, binding, plan_identity}`` stub — the stub carries no ``recovery_ref`` and does
not validate as a ``RecoveryPoint``, so a handler reading it back would fail on decode rather than
on the thing under test.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.jobs.payloads import EXTERNAL_BOOT_AUTHORITY_MARKER_KEY
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityTakeoverRequestV1,
)
from tests.jobs.handlers.external_boot.vehicle import Vehicle

AUTHORITY_INSTANCE = "authority-vehicle"
PROVIDER_KIND = "local-libvirt"

# States whose CHECK (external_boot_activation_state_evidence, 0121…sql:38-52) requires
# current_attempt_id. Seeding the attempt row for these is not optional: without it the activation
# INSERT itself fails with a CheckViolation, well before any handler runs.
_ATTEMPT_STATES = frozenset({"recovering", "recovery_conflict", "recovery_failed", "recovered"})

# States the CHECK admits only with terminal_evidence carrying a matching outcome.
_TERMINAL_OUTCOME = {"active": "active", "abandoned": "abandoned"}


def _digest(label: str) -> str:
    return "sha256:" + hashlib.sha256(label.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SeededCase:
    """The identifiers a test needs to drive and then inspect one seeded operation."""

    vehicle: Vehicle
    allocation_id: UUID
    investigation_id: UUID
    job_id: UUID
    attempt: int
    worker_incarnation: str
    credential: str
    purpose: str
    operation: str
    operation_identity: str
    attempt_id: UUID | None

    @property
    def marker(self) -> dict[str, Any]:
        return {
            "activation_id": str(self.vehicle.activation_id),
            "run_id": str(self.vehicle.run_id),
            "system_id": str(self.vehicle.system_id),
            "plan_identity": self.vehicle.plan_identity,
            "purpose": self.purpose,
            "provider_kind": PROVIDER_KIND,
            "authority_instance": AUTHORITY_INSTANCE,
            "operation": self.operation,
            "operation_identity": self.operation_identity,
        }


async def seed_case(  # noqa: PLR0913 - a row set, not a behaviour; every argument is one column
    conn: AsyncConnection,
    vehicle: Vehicle,
    *,
    purpose: str,
    operation: str | None = None,
    activation_state: str = "activating",
    system_state: str | None = None,
    run_state: str = "succeeded",
    with_materialization: bool = True,
    with_recovery_point: bool = True,
    cleanup_complete: bool = False,
    attempt_state: str = "recovering",
    marker_overrides: dict[str, Any] | None = None,
) -> SeededCase:
    """Insert resource → allocation → system → investigation → run → activation → worker → job.

    ``system_state`` defaults to what each purpose's ``allocate_external_boot_authority``
    precondition admits (``0122…sql:482-501``): ``failed`` for teardown, ``ready`` otherwise.
    """
    operation = operation or purpose
    system_state = system_state or ("failed" if purpose == "teardown" else "ready")
    resource_id, allocation_id, investigation_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    worker_incarnation = f"docker:external-boot-{uuid4()}"
    credential = f"worker-credential-{uuid4()}"
    operation_identity = f"{operation}-{uuid4()}"
    job_kind = "teardown" if purpose == "teardown" else "boot"
    attempt_id = uuid4() if activation_state in _ATTEMPT_STATES else None

    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', 'proj')",
        (vehicle.system_id, allocation_id, system_state),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'active')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES (%s, %s, %s, 'local-libvirt', %s, '{}'::jsonb, 'p', 'proj')",
        (vehicle.run_id, investigation_id, vehicle.system_id, run_state),
    )
    await _insert_activation(
        conn,
        vehicle,
        activation_state=activation_state,
        with_materialization=with_materialization,
        with_recovery_point=with_recovery_point,
        cleanup_complete=cleanup_complete,
        attempt_id=attempt_id,
        attempt_state=attempt_state,
    )
    await conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
        "VALUES (%s, 'docker', '{}'::jsonb, sha256(convert_to(%s, 'UTF8')), 4)",
        (worker_incarnation, credential),
    )
    case = SeededCase(
        vehicle=vehicle,
        allocation_id=allocation_id,
        investigation_id=investigation_id,
        job_id=job_id,
        attempt=1,
        worker_incarnation=worker_incarnation,
        credential=credential,
        purpose=purpose,
        operation=operation,
        operation_identity=operation_identity,
        attempt_id=attempt_id,
    )
    marker = case.marker | (marker_overrides or {})
    payload = {
        ("system_id" if job_kind == "teardown" else "run_id"): str(
            vehicle.system_id if job_kind == "teardown" else vehicle.run_id
        ),
        EXTERNAL_BOOT_AUTHORITY_MARKER_KEY: marker,
    }
    await conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
        "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
        "(%s, %s, %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), %s, %s)",
        (
            job_id,
            job_kind,
            Jsonb(payload),
            worker_incarnation,
            Jsonb({"principal": "p", "project": "proj"}),
            f"external-boot-{job_id}",
        ),
    )
    return case


async def _insert_activation(
    conn: AsyncConnection,
    vehicle: Vehicle,
    *,
    activation_state: str,
    with_materialization: bool,
    with_recovery_point: bool,
    cleanup_complete: bool,
    attempt_id: UUID | None,
    attempt_state: str,
) -> None:
    if attempt_id is not None:
        # Inserted before the activation row so the FK/CHECK pair is satisfiable, then pointed at.
        await conn.execute(
            "INSERT INTO external_boot_activations "
            "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
            "state, materialization, recovery_point) "
            "VALUES (%s, %s, %s, %s, %s, 1, 'prepared', %s, %s)",
            (
                vehicle.activation_id,
                vehicle.system_id,
                vehicle.run_id,
                vehicle.plan_identity,
                uuid4(),
                Jsonb(vehicle.materialization_json),
                Jsonb(vehicle.recovery_point_json),
            ),
        )
        await conn.execute(
            # recovery_readiness_deadline is required for state 'recovering'
            # (external_boot_attempt_deadline, 0121…sql:232-233) and forbidden nowhere else.
            "INSERT INTO external_boot_recovery_attempts "
            "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
            "state, recovery_readiness_deadline) "
            "VALUES (%s, 1, %s, 1, 'recovery_point', %s, %s)",
            (
                vehicle.activation_id,
                attempt_id,
                attempt_state,
                "2027-01-01T00:00:00Z" if attempt_state == "recovering" else None,
            ),
        )
        await conn.execute(
            "UPDATE external_boot_activations SET state = %s, current_attempt_id = %s, "
            "cleanup_complete = %s, materialization = %s, recovery_point = %s WHERE id = %s",
            (
                activation_state,
                attempt_id,
                cleanup_complete,
                Jsonb(vehicle.materialization_json) if with_materialization else None,
                Jsonb(vehicle.recovery_point_json) if with_recovery_point else None,
                vehicle.activation_id,
            ),
        )
        return

    outcome = _TERMINAL_OUTCOME.get(activation_state)
    await conn.execute(
        "INSERT INTO external_boot_activations "
        "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
        "state, materialization, recovery_point, terminal_evidence, "
        "activation_readiness_deadline, cleanup_complete) "
        "VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)",
        (
            vehicle.activation_id,
            vehicle.system_id,
            vehicle.run_id,
            vehicle.plan_identity,
            uuid4(),
            activation_state,
            Jsonb(vehicle.materialization_json) if with_materialization else None,
            Jsonb(vehicle.recovery_point_json) if with_recovery_point else None,
            Jsonb(_terminal_evidence(vehicle, outcome)) if outcome else None,
            "2027-01-01T00:00:00Z" if activation_state in {"activating", "active"} else None,
            cleanup_complete,
        ),
    )


def _terminal_evidence(vehicle: Vehicle, outcome: str) -> dict[str, Any]:
    return {
        "schema": "external-boot-terminal-evidence-v1",
        "activation_id": str(vehicle.activation_id),
        "system_id": str(vehicle.system_id),
        "outcome": outcome,
        "composite_state": _digest(f"composite/{vehicle.activation_id}"),
        "objects": [],
        "observed_at": "2026-08-29T00:00:00Z",
    }


class RecordingAcknowledger:
    """A seam implementation that performs the **real** ``acknowledge_external_boot_authority``.

    Not a stub: the database does the acknowledging over a ``kdive_provider_authority`` LOGIN
    connection, so a test that passes proves the SQL path rather than the fake. The four arguments
    the request model does not carry — ``allocation_id``, ``job_id``, ``job_attempt``,
    ``worker_incarnation`` — are read from ``external_boot_authorities``, exactly as the real
    authority host resolves them from the row it holds ``SELECT`` on.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self.requests: list[AuthorityTakeoverRequestV1] = []

    async def acknowledge(self, request: AuthorityTakeoverRequestV1) -> AuthorityAcknowledgementV1:
        self.requests.append(request)
        journal_digest = _digest(f"journal/{request.authority_id}/{request.generation}")
        quiescence = _digest(f"quiescence/{request.authority_id}/{request.generation}")
        async with (
            await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn,
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT allocation_id, job_id, job_attempt, worker_incarnation "
                "FROM external_boot_authorities WHERE id = %s",
                (request.authority_id,),
            )
            authority = await cur.fetchone()
            if authority is None:
                raise AssertionError(f"no authority row for {request.authority_id}")
            await cur.execute(
                "SELECT status, journal_sequence, journal_digest, positive_quiescence_digest "
                "FROM public.acknowledge_external_boot_authority("
                "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    request.authority_id,
                    request.generation,
                    authority["allocation_id"],
                    request.activation_id,
                    request.run_id,
                    request.system_id,
                    request.plan_identity,
                    authority["job_id"],
                    authority["job_attempt"],
                    request.purpose,
                    request.provider_kind,
                    request.authority_instance,
                    authority["worker_incarnation"],
                    request.operation.value,
                    request.operation_identity,
                    request.operation_digest,
                    1,
                    journal_digest,
                    quiescence,
                ),
            )
            row = await cur.fetchone()
        if row is None or row["status"] != "applied":
            raise AssertionError(f"acknowledgement was {row and row['status']!r}, not applied")
        return AuthorityAcknowledgementV1(
            schema="external-boot-authority-v1",
            authority_id=request.authority_id,
            generation=request.generation,
            system_id=request.system_id,
            journal_sequence=row["journal_sequence"],
            journal_digest=row["journal_digest"],
            positive_quiescence_digest=row["positive_quiescence_digest"],
        )

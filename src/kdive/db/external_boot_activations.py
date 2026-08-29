"""System-locked external-boot activation persistence (ADR-0583, ADR-0584)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import ExternalBootActivationState, ensure_transition
from kdive.domain.external_boot_activation import (
    ExternalBootActivation,
    ExternalBootCleanupEvidenceV1,
    ExternalBootConflictEvidenceV1,
    ExternalBootPreRecoveryEvidenceV1,
    ExternalBootRecoveryAttempt,
    ExternalBootRecoveryAttemptState,
    ExternalBootReleaseEvidenceV1,
    ExternalBootReservation,
    ExternalBootReservationRelease,
    ExternalBootTeardownEvidenceV1,
    ExternalBootTerminalEvidenceV1,
)
from kdive.providers.ports.external_boot import ExternalBootMaterialization, RecoveryPoint


class CasStatus(StrEnum):
    """Opaque outcome of one authority-fenced mutation."""

    APPLIED = "applied"
    SUPERSEDED = "superseded"
    NOT_FOUND = "not_found"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    RETAINED_CAPACITY = "retained_capacity"


@dataclass(frozen=True)
class CasResult:
    """A mutation result without disclosing which authority predicate mismatched."""

    status: CasStatus
    activation: ExternalBootActivation | None = None
    release: ExternalBootReservationRelease | None = None


def _json(value: Any) -> Jsonb:
    return Jsonb(value.model_dump(mode="json", by_alias=True))


def _activation(row: dict[str, Any] | None) -> ExternalBootActivation | None:
    return None if row is None else ExternalBootActivation.model_validate(row)


def _require_utc(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must be timezone-aware UTC")


class ExternalBootActivationRepository:
    """Own the durable activation CAS protocol; callers own transaction boundaries."""

    async def _get_row(self, conn: AsyncConnection, activation_id: UUID) -> dict[str, Any] | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM external_boot_activations WHERE id = %s", (activation_id,)
            )
            return await cur.fetchone()

    async def _miss(self, conn: AsyncConnection, activation_id: UUID) -> CasResult:
        return CasResult(
            CasStatus.NOT_FOUND
            if await self._get_row(conn, activation_id) is None
            else CasStatus.SUPERSEDED
        )

    async def _authorized_row(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
    ) -> dict[str, Any] | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM external_boot_activations "
                "WHERE id = %s AND system_id = %s AND operation_owner_id = %s "
                "AND authority_generation = %s AND state = %s AND NOT cleanup_complete",
                (
                    activation_id,
                    system_id,
                    operation_owner_id,
                    authority_generation,
                    expected_state,
                ),
            )
            return await cur.fetchone()

    async def create(
        self,
        conn: AsyncConnection,
        activation: ExternalBootActivation,
        reservation: ExternalBootReservation,
    ) -> ExternalBootActivation:
        """Atomically create a preparing activation and its pending reservation."""
        if activation.state is not ExternalBootActivationState.PREPARING:
            raise ValueError("new activation must be preparing")
        if reservation.state.value != "pending" or reservation.activation_id != activation.id:
            raise ValueError("new reservation must be pending and bound to the activation")
        async with (
            advisory_xact_lock(conn, LockScope.SYSTEM, activation.system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "INSERT INTO external_boot_activations "
                "(id, system_id, run_id, plan_identity, operation_owner_id, "
                "authority_generation, state, cleanup_complete) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'preparing', false) "
                "ON CONFLICT (system_id, run_id, plan_identity) DO NOTHING RETURNING *",
                (
                    activation.id,
                    activation.system_id,
                    activation.run_id,
                    activation.plan_identity,
                    activation.operation_owner_id,
                    activation.authority_generation,
                ),
            )
            row = await cur.fetchone()
            inserted = row is not None
            if row is None:
                await cur.execute(
                    "SELECT * FROM external_boot_activations "
                    "WHERE system_id = %s AND run_id = %s AND plan_identity = %s",
                    (activation.system_id, activation.run_id, activation.plan_identity),
                )
                row = await cur.fetchone()
                current = _activation(row)
                if current is None or (
                    current.id,
                    current.operation_owner_id,
                    current.authority_generation,
                ) != (
                    activation.id,
                    activation.operation_owner_id,
                    activation.authority_generation,
                ):
                    raise ValueError("activation retry key is already owned by another operation")
            if inserted:
                await cur.execute(
                    "INSERT INTO external_boot_reservations "
                    "(activation_id, store_identity, owner_key, reserved_bytes, state) "
                    "VALUES (%s, %s, %s, %s, 'pending')",
                    (
                        reservation.activation_id,
                        reservation.store_identity,
                        reservation.owner_key,
                        reservation.reserved_bytes,
                    ),
                )
            await cur.execute(
                "SELECT * FROM external_boot_reservations WHERE activation_id = %s",
                (activation.id,),
            )
            reservation_row = await cur.fetchone()
            if reservation_row is None:
                await cur.execute(
                    "SELECT store_identity, owner_key, reserved_bytes "
                    "FROM external_boot_reservation_releases WHERE activation_id = %s",
                    (activation.id,),
                )
                release_identity = await cur.fetchone()
                if release_identity is not None:
                    if (
                        release_identity["store_identity"],
                        release_identity["owner_key"],
                        release_identity["reserved_bytes"],
                    ) != (
                        reservation.store_identity,
                        reservation.owner_key,
                        reservation.reserved_bytes,
                    ):
                        raise ValueError(
                            "activation reservation retry does not match release identity"
                        )
                    current = _activation(row)
                    if current is None:
                        raise RuntimeError("activation retry returned no row")
                    return current
                raise ValueError("activation retry has neither a live debit nor release tombstone")
            current_reservation = ExternalBootReservation.model_validate(reservation_row)
            if (
                current_reservation.store_identity,
                current_reservation.owner_key,
                current_reservation.reserved_bytes,
            ) != (
                reservation.store_identity,
                reservation.owner_key,
                reservation.reserved_bytes,
            ):
                raise ValueError("activation reservation retry does not match durable identity")
        current = _activation(row)
        if current is None:
            raise RuntimeError("activation create returned no row")
        return current

    async def _require_ready_reservation(self, conn: AsyncConnection, activation_id: UUID) -> bool:
        reservation = await self.get_reservation(conn, activation_id)
        return reservation is not None and reservation.state.value == "ready"

    async def get(
        self, conn: AsyncConnection, activation_id: UUID
    ) -> ExternalBootActivation | None:
        return _activation(await self._get_row(conn, activation_id))

    async def get_reservation(
        self, conn: AsyncConnection, activation_id: UUID
    ) -> ExternalBootReservation | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM external_boot_reservations WHERE activation_id = %s",
                (activation_id,),
            )
            row = await cur.fetchone()
        return None if row is None else ExternalBootReservation.model_validate(row)

    async def get_current_recovery_attempt(
        self, conn: AsyncConnection, activation_id: UUID
    ) -> ExternalBootRecoveryAttempt | None:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT attempt.* FROM external_boot_activations activation "
                "JOIN external_boot_recovery_attempts attempt "
                "ON attempt.activation_id = activation.id "
                "AND attempt.attempt_id = activation.current_attempt_id "
                "WHERE activation.id = %s",
                (activation_id,),
            )
            row = await cur.fetchone()
        return None if row is None else ExternalBootRecoveryAttempt.model_validate(row)

    async def list_recovery_attempts(
        self,
        conn: AsyncConnection,
        activation_id: UUID,
        *,
        before_attempt_number: int | None = None,
        limit: int = 100,
    ) -> list[ExternalBootRecoveryAttempt]:
        if not 1 <= limit <= 100:
            raise ValueError("recovery-attempt limit must be 1 through 100")
        if before_attempt_number is not None and before_attempt_number <= 0:
            raise ValueError("before_attempt_number must be positive")
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM external_boot_recovery_attempts "
                "WHERE activation_id = %s AND (%s::integer IS NULL OR attempt_number < %s) "
                "ORDER BY attempt_number DESC LIMIT %s",
                (activation_id, before_attempt_number, before_attempt_number, limit),
            )
            rows = await cur.fetchall()
        return [ExternalBootRecoveryAttempt.model_validate(row) for row in rows]

    async def record_materialization(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        materialization: ExternalBootMaterialization,
    ) -> CasResult:
        if expected_state is not ExternalBootActivationState.PREPARING:
            raise ValueError("materialization may be recorded only while preparing")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if current is None:
                return await self._miss(conn, activation_id)
            if (
                materialization.ownership.system_id != str(system_id)
                or materialization.ownership.run_id != str(current["run_id"])
                or materialization.plan_identity != current["plan_identity"]
            ):
                raise ValueError("materialization ownership does not match the activation")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE external_boot_activations SET materialization = %s "
                    "WHERE id = %s AND system_id = %s AND operation_owner_id = %s "
                    "AND authority_generation = %s AND state = %s AND NOT cleanup_complete "
                    "AND (materialization IS NULL OR materialization = %s) RETURNING *",
                    (
                        _json(materialization),
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                        _json(materialization),
                    ),
                )
                row = await cur.fetchone()
        if row is None:
            return await self._miss(conn, activation_id)
        return CasResult(CasStatus.APPLIED, _activation(row))

    async def record_pre_recovery_evidence(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        evidence: ExternalBootPreRecoveryEvidenceV1,
    ) -> CasResult:
        if expected_state is not ExternalBootActivationState.PREPARING:
            raise ValueError("pre-recovery evidence may be recorded only while preparing")
        if (evidence.activation_id, evidence.system_id) != (activation_id, system_id):
            raise ValueError("pre-recovery evidence ownership does not match the activation")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if current is None:
                return await self._miss(conn, activation_id)
            if (
                evidence.run_id != current["run_id"]
                or evidence.plan_identity != current["plan_identity"]
            ):
                raise ValueError("pre-recovery evidence does not match the Run and plan")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE external_boot_activations SET pre_recovery_evidence = %s "
                    "WHERE id = %s AND system_id = %s AND operation_owner_id = %s "
                    "AND authority_generation = %s AND state = %s AND NOT cleanup_complete "
                    "AND materialization IS NOT NULL "
                    "AND (pre_recovery_evidence IS NULL OR pre_recovery_evidence = %s) RETURNING *",
                    (
                        _json(evidence),
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                        _json(evidence),
                    ),
                )
                row = await cur.fetchone()
        if row is None:
            return await self._miss(conn, activation_id)
        return CasResult(CasStatus.APPLIED, _activation(row))

    async def transition(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        new_state: ExternalBootActivationState,
        terminal_evidence: ExternalBootTerminalEvidenceV1 | None = None,
        recovery_point: RecoveryPoint | None = None,
        activation_readiness_deadline: datetime | None = None,
    ) -> CasResult:
        ensure_transition(expected_state, new_state)
        if new_state in {
            ExternalBootActivationState.RECOVERING,
            ExternalBootActivationState.RECOVERY_CONFLICT,
            ExternalBootActivationState.RECOVERED,
            ExternalBootActivationState.RECOVERY_FAILED,
        }:
            raise ValueError("recovery transitions use the recovery-attempt methods")
        if terminal_evidence is not None and (
            terminal_evidence.activation_id,
            terminal_evidence.system_id,
        ) != (activation_id, system_id):
            raise ValueError("terminal evidence ownership does not match the activation")
        if new_state is ExternalBootActivationState.ABANDONED and (
            terminal_evidence is None or terminal_evidence.outcome != "abandoned"
        ):
            raise ValueError("abandonment requires matching terminal evidence")
        if activation_readiness_deadline is not None:
            _require_utc(activation_readiness_deadline, "activation_readiness_deadline")
        if new_state is ExternalBootActivationState.ACTIVATING:
            if activation_readiness_deadline is None:
                raise ValueError("activating requires an absolute readiness deadline")
        elif activation_readiness_deadline is not None:
            raise ValueError("only the activating edge may set an activation deadline")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if current is None:
                return await self._miss(conn, activation_id)
            reservation_ready = await self._require_ready_reservation(conn, activation_id)
            if new_state is not ExternalBootActivationState.ABANDONED and not reservation_ready:
                return CasResult(CasStatus.SUPERSEDED)
            if new_state is ExternalBootActivationState.PREPARED:
                if current["materialization"] is None or recovery_point is None:
                    return CasResult(CasStatus.SUPERSEDED)
                materialization = ExternalBootMaterialization.model_validate(
                    current["materialization"]
                )
                if (
                    recovery_point.ownership.system_id != str(system_id)
                    or recovery_point.ownership.run_id != str(current["run_id"])
                    or recovery_point.plan_identity != current["plan_identity"]
                    or recovery_point.materialization_identity != materialization.identity
                ):
                    raise ValueError("recovery point does not bind the durable materialization")
            elif recovery_point is not None:
                raise ValueError("only the prepared edge may set a recovery point")
            if (
                new_state is ExternalBootActivationState.ACTIVATING
                and current["recovery_point"] is None
            ):
                return CasResult(CasStatus.SUPERSEDED)
            if new_state is ExternalBootActivationState.ACTIVE and (
                terminal_evidence is None or terminal_evidence.outcome != "active"
            ):
                raise ValueError("active requires matching terminal evidence")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE external_boot_activations SET state = %s, "
                    "terminal_evidence = COALESCE(terminal_evidence, %s), "
                    "recovery_point = COALESCE(recovery_point, %s), "
                    "activation_readiness_deadline = "
                    "COALESCE(activation_readiness_deadline, %s) "
                    "WHERE id = %s AND system_id = %s AND operation_owner_id = %s "
                    "AND authority_generation = %s AND state = %s AND NOT cleanup_complete "
                    "AND (%s::jsonb IS NULL OR terminal_evidence IS NULL "
                    "OR terminal_evidence = %s) "
                    "AND (%s::jsonb IS NULL OR recovery_point IS NULL "
                    "OR recovery_point = %s) "
                    "AND (%s::timestamptz IS NULL OR activation_readiness_deadline IS NULL "
                    "OR activation_readiness_deadline = %s) RETURNING *",
                    (
                        new_state,
                        _json(terminal_evidence) if terminal_evidence else None,
                        _json(recovery_point) if recovery_point else None,
                        activation_readiness_deadline,
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                        _json(terminal_evidence) if terminal_evidence else None,
                        _json(terminal_evidence) if terminal_evidence else None,
                        _json(recovery_point) if recovery_point else None,
                        _json(recovery_point) if recovery_point else None,
                        activation_readiness_deadline,
                        activation_readiness_deadline,
                    ),
                )
                row = await cur.fetchone()
        if row is None:
            return await self._miss(conn, activation_id)
        return CasResult(CasStatus.APPLIED, _activation(row))

    async def record_conflict(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        attempt_id: UUID,
        evidence: ExternalBootConflictEvidenceV1,
    ) -> CasResult:
        direct_sources = {
            ExternalBootActivationState.PREPARING,
            ExternalBootActivationState.PREPARED,
            ExternalBootActivationState.ACTIVATING,
            ExternalBootActivationState.ACTIVE,
        }
        if expected_state not in direct_sources:
            raise ValueError("record_conflict accepts only direct-conflict source states")
        ensure_transition(expected_state, ExternalBootActivationState.RECOVERY_CONFLICT)
        if (evidence.activation_id, evidence.system_id) != (activation_id, system_id):
            raise ValueError("conflict evidence ownership does not match the activation")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self.get_current_recovery_attempt(conn, activation_id)
            current_activation = await self.get(conn, activation_id)
            if (
                current is not None
                and current_activation is not None
                and current.attempt_id == attempt_id
                and current.state is ExternalBootRecoveryAttemptState.CONFLICT
                and current.conflict_evidence == evidence
                and current_activation.state is ExternalBootActivationState.RECOVERY_CONFLICT
                and current_activation.system_id == system_id
                and current_activation.operation_owner_id == operation_owner_id
                and current_activation.authority_generation == authority_generation
                and not current_activation.cleanup_complete
            ):
                return CasResult(CasStatus.APPLIED, current_activation)
            row = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if row is None:
                return await self._miss(conn, activation_id)
            if not await self._require_ready_reservation(conn, activation_id):
                return CasResult(CasStatus.SUPERSEDED)
            if row["materialization"] is None:
                return CasResult(CasStatus.SUPERSEDED)
            if expected_state is ExternalBootActivationState.PREPARING:
                if row["pre_recovery_evidence"] is None:
                    return CasResult(CasStatus.SUPERSEDED)
                basis = "pre_recovery"
            else:
                if row["recovery_point"] is None:
                    return CasResult(CasStatus.SUPERSEDED)
                basis = "recovery_point"
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT COALESCE(max(attempt_number), 0) + 1 AS next_number "
                    "FROM external_boot_recovery_attempts WHERE activation_id = %s",
                    (activation_id,),
                )
                number_row = await cur.fetchone()
                if number_row is None:
                    raise RuntimeError("recovery attempt sequence returned no row")
                attempt_number = int(number_row["next_number"])
                await cur.execute(
                    "INSERT INTO external_boot_recovery_attempts "
                    "(activation_id, attempt_number, attempt_id, authority_generation, "
                    "recovery_basis, state, conflict_evidence) "
                    "VALUES (%s, %s, %s, %s, %s, 'conflict', %s)",
                    (
                        activation_id,
                        attempt_number,
                        attempt_id,
                        authority_generation,
                        basis,
                        _json(evidence),
                    ),
                )
                await cur.execute(
                    "UPDATE external_boot_activations SET state = 'recovery_conflict', "
                    "current_attempt_id = %s WHERE id = %s AND system_id = %s "
                    "AND operation_owner_id = %s AND authority_generation = %s "
                    "AND state = %s AND NOT cleanup_complete RETURNING *",
                    (
                        attempt_id,
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                    ),
                )
                updated = await cur.fetchone()
        if updated is None:
            return CasResult(CasStatus.SUPERSEDED)
        return CasResult(CasStatus.APPLIED, _activation(updated))

    async def begin_recovery_attempt(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        attempt_id: UUID,
        recovery_readiness_deadline: datetime,
        resolution_operation: str | None = None,
        resolution_identity: str | None = None,
        acknowledged_composite_state: str | None = None,
    ) -> CasResult:
        ensure_transition(expected_state, ExternalBootActivationState.RECOVERING)
        _require_utc(recovery_readiness_deadline, "recovery_readiness_deadline")
        resolution = (resolution_operation, resolution_identity, acknowledged_composite_state)
        if expected_state is ExternalBootActivationState.RECOVERY_CONFLICT:
            if not all(value is not None for value in resolution):
                raise ValueError("conflict recovery requires all resolution fields")
            if not 1 <= len(resolution_operation or "") <= 255:
                raise ValueError("resolution_operation must contain 1 through 255 characters")
        elif any(value is not None for value in resolution):
            raise ValueError("ordinary recovery forbids resolution fields")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self.get_current_recovery_attempt(conn, activation_id)
            current_activation = await self.get(conn, activation_id)
            if (
                current is not None
                and current_activation is not None
                and current.attempt_id == attempt_id
                and current.state is ExternalBootRecoveryAttemptState.RECOVERING
                and current.recovery_readiness_deadline == recovery_readiness_deadline
                and current.resolution_operation == resolution_operation
                and current.resolution_identity == resolution_identity
                and current.acknowledged_composite_state == acknowledged_composite_state
                and current_activation.state is ExternalBootActivationState.RECOVERING
                and current_activation.system_id == system_id
                and current_activation.operation_owner_id == operation_owner_id
                and current_activation.authority_generation == authority_generation
                and not current_activation.cleanup_complete
            ):
                return CasResult(CasStatus.APPLIED, current_activation)
            row = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if row is None:
                return await self._miss(conn, activation_id)
            if expected_state is ExternalBootActivationState.RECOVERY_CONFLICT and (
                current is None
                or current.state is not ExternalBootRecoveryAttemptState.CONFLICT
                or current.conflict_evidence is None
                or current.conflict_evidence.composite_state != acknowledged_composite_state
            ):
                raise ValueError(
                    "acknowledged_composite_state must match current conflict evidence"
                )
            if not await self._require_ready_reservation(conn, activation_id):
                return CasResult(CasStatus.SUPERSEDED)
            basis = "recovery_point" if row["recovery_point"] is not None else "pre_recovery"
            if basis == "pre_recovery" and (
                expected_state is not ExternalBootActivationState.RECOVERY_CONFLICT
                or row["pre_recovery_evidence"] is None
            ):
                return CasResult(CasStatus.SUPERSEDED)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT COALESCE(max(attempt_number), 0) + 1 AS next_number "
                    "FROM external_boot_recovery_attempts WHERE activation_id = %s",
                    (activation_id,),
                )
                number_row = await cur.fetchone()
                if number_row is None:
                    raise RuntimeError("recovery attempt sequence returned no row")
                attempt_number = int(number_row["next_number"])
                await cur.execute(
                    "INSERT INTO external_boot_recovery_attempts "
                    "(activation_id, attempt_number, attempt_id, authority_generation, "
                    "recovery_basis, resolution_operation, resolution_identity, "
                    "acknowledged_composite_state, recovery_readiness_deadline, state) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'recovering')",
                    (
                        activation_id,
                        attempt_number,
                        attempt_id,
                        authority_generation,
                        basis,
                        resolution_operation,
                        resolution_identity,
                        acknowledged_composite_state,
                        recovery_readiness_deadline,
                    ),
                )
                await cur.execute(
                    "UPDATE external_boot_activations SET state = 'recovering', "
                    "current_attempt_id = %s WHERE id = %s AND system_id = %s "
                    "AND operation_owner_id = %s AND authority_generation = %s "
                    "AND state = %s AND NOT cleanup_complete RETURNING *",
                    (
                        attempt_id,
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                    ),
                )
                updated = await cur.fetchone()
        if updated is None:
            return CasResult(CasStatus.SUPERSEDED)
        return CasResult(CasStatus.APPLIED, _activation(updated))

    async def finish_recovery_attempt(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        attempt_id: UUID,
        new_state: ExternalBootActivationState,
        terminal_evidence: ExternalBootTerminalEvidenceV1 | None = None,
        conflict_evidence: ExternalBootConflictEvidenceV1 | None = None,
    ) -> CasResult:
        ensure_transition(expected_state, new_state)
        states = {
            ExternalBootActivationState.RECOVERY_CONFLICT: (
                ExternalBootRecoveryAttemptState.CONFLICT
            ),
            ExternalBootActivationState.RECOVERY_FAILED: ExternalBootRecoveryAttemptState.FAILED,
            ExternalBootActivationState.RECOVERED: ExternalBootRecoveryAttemptState.RECOVERED,
        }
        if new_state not in states:
            raise ValueError("recovery attempt has an invalid terminal state")
        if new_state is ExternalBootActivationState.RECOVERY_CONFLICT:
            if conflict_evidence is None or terminal_evidence is not None:
                raise ValueError("recovery conflict requires only conflict evidence")
        elif terminal_evidence is None or conflict_evidence is not None:
            raise ValueError("terminal recovery requires only terminal evidence")
        elif (
            terminal_evidence.outcome
            != {
                ExternalBootActivationState.RECOVERED: "recovered",
                ExternalBootActivationState.RECOVERY_FAILED: "recovery_failed",
            }[new_state]
        ):
            raise ValueError("terminal evidence outcome does not match recovery state")
        evidence = conflict_evidence or terminal_evidence
        if evidence is not None and (evidence.activation_id, evidence.system_id) != (
            activation_id,
            system_id,
        ):
            raise ValueError("attempt evidence ownership does not match the activation")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            current = await self.get_current_recovery_attempt(conn, activation_id)
            current_activation = await self.get(conn, activation_id)
            if (
                current is not None
                and current_activation is not None
                and current.attempt_id == attempt_id
                and current.state is states[new_state]
                and current.conflict_evidence == conflict_evidence
                and current.terminal_evidence == terminal_evidence
                and current_activation.state is new_state
                and current_activation.system_id == system_id
                and current_activation.operation_owner_id == operation_owner_id
                and current_activation.authority_generation == authority_generation
                and not current_activation.cleanup_complete
            ):
                return CasResult(CasStatus.APPLIED, current_activation)
            row = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if row is None or row["current_attempt_id"] != attempt_id:
                return await self._miss(conn, activation_id)
            if not await self._require_ready_reservation(conn, activation_id):
                return CasResult(CasStatus.SUPERSEDED)
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "UPDATE external_boot_recovery_attempts SET state = %s, "
                    "conflict_evidence = %s, terminal_evidence = %s "
                    "WHERE activation_id = %s AND attempt_id = %s "
                    "AND authority_generation = %s AND state = 'recovering' RETURNING attempt_id",
                    (
                        states[new_state],
                        _json(conflict_evidence) if conflict_evidence else None,
                        _json(terminal_evidence) if terminal_evidence else None,
                        activation_id,
                        attempt_id,
                        authority_generation,
                    ),
                )
                if await cur.fetchone() is None:
                    return CasResult(CasStatus.SUPERSEDED)
                await cur.execute(
                    "UPDATE external_boot_activations SET state = %s "
                    "WHERE id = %s AND system_id = %s AND operation_owner_id = %s "
                    "AND authority_generation = %s AND state = %s "
                    "AND current_attempt_id = %s AND NOT cleanup_complete RETURNING *",
                    (
                        new_state,
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                        attempt_id,
                    ),
                )
                updated = await cur.fetchone()
        if updated is None:
            return CasResult(CasStatus.SUPERSEDED)
        return CasResult(CasStatus.APPLIED, _activation(updated))

    async def mark_reservation_ready(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        recovery_max_bytes: int,
    ) -> CasResult:
        if recovery_max_bytes <= 0:
            raise ValueError("recovery_max_bytes must be positive")
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            activation_row = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if activation_row is None:
                return await self._miss(conn, activation_id)
            reservation = await self.get_reservation(conn, activation_id)
            if reservation is None:
                return CasResult(CasStatus.SUPERSEDED)
            if reservation.state.value == "ready":
                return CasResult(CasStatus.APPLIED, _activation(activation_row))
            async with advisory_xact_lock(
                conn, LockScope.RECOVERY_STORE, reservation.store_identity
            ):
                cur = await conn.execute(
                    "SELECT COALESCE(sum(reserved_bytes), 0) FROM external_boot_reservations "
                    "WHERE store_identity = %s AND state = 'ready'",
                    (reservation.store_identity,),
                )
                row = await cur.fetchone()
                used = 0 if row is None else int(row[0])
                if used + reservation.reserved_bytes > recovery_max_bytes:
                    return CasResult(CasStatus.CAPACITY_EXHAUSTED, _activation(activation_row))
                await conn.execute(
                    "UPDATE external_boot_reservations r SET state = 'ready', ready_at = now() "
                    "FROM external_boot_activations a WHERE r.activation_id = a.id "
                    "AND a.id = %s AND a.system_id = %s AND a.operation_owner_id = %s "
                    "AND a.authority_generation = %s AND a.state = %s "
                    "AND NOT a.cleanup_complete AND r.state = 'pending'",
                    (
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                    ),
                )
        return CasResult(CasStatus.APPLIED, _activation(activation_row))

    async def release_reservation(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        release_evidence: ExternalBootReleaseEvidenceV1,
        teardown_evidence: ExternalBootTeardownEvidenceV1 | None = None,
    ) -> CasResult:
        release_identity = release_evidence.identity
        if (release_evidence.activation_id, release_evidence.system_id) != (
            activation_id,
            system_id,
        ):
            raise ValueError("release evidence ownership does not match the activation")
        ordinary = {
            ExternalBootActivationState.RECOVERED,
            ExternalBootActivationState.ABANDONED,
        }
        teardown = {
            ExternalBootActivationState.RECOVERY_FAILED,
            ExternalBootActivationState.RECOVERY_CONFLICT,
        }
        if expected_state not in ordinary | teardown:
            return CasResult(CasStatus.RETAINED_CAPACITY)
        if expected_state in ordinary and teardown_evidence is not None:
            raise ValueError("ordinary cleanup forbids teardown evidence")
        if expected_state in teardown and (
            teardown_evidence is None or teardown_evidence.system_id != system_id
        ):
            return CasResult(CasStatus.RETAINED_CAPACITY)
        async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
            activation_row = await self._authorized_row(
                conn,
                system_id=system_id,
                activation_id=activation_id,
                operation_owner_id=operation_owner_id,
                authority_generation=authority_generation,
                expected_state=expected_state,
            )
            if activation_row is None:
                return await self._miss(conn, activation_id)
            if expected_state in teardown:
                state_cur = await conn.execute(
                    "SELECT state FROM systems WHERE id = %s", (system_id,)
                )
                state_row = await state_cur.fetchone()
                if state_row != ("torn_down",):
                    return CasResult(CasStatus.RETAINED_CAPACITY, _activation(activation_row))
            reservation = await self.get_reservation(conn, activation_id)
            if reservation is None:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT * FROM external_boot_reservation_releases WHERE activation_id = %s",
                        (activation_id,),
                    )
                    release_row = await cur.fetchone()
                if release_row is None:
                    return CasResult(CasStatus.SUPERSEDED)
                release = ExternalBootReservationRelease.model_validate(release_row)
                if (
                    release.release_evidence != release_evidence
                    or release.teardown_evidence != teardown_evidence
                ):
                    return CasResult(CasStatus.SUPERSEDED)
                return CasResult(CasStatus.APPLIED, _activation(activation_row), release)
            if (
                release_evidence.store_identity.ref,
                release_evidence.owner_key.ref,
                release_evidence.reserved_bytes,
            ) != (
                reservation.store_identity,
                reservation.owner_key,
                reservation.reserved_bytes,
            ):
                return CasResult(CasStatus.RETAINED_CAPACITY)
            known = self._known_objects(activation_row)
            absent = {item.object.ref for item in release_evidence.objects}
            if not known <= absent:
                return CasResult(CasStatus.RETAINED_CAPACITY)
            async with (
                advisory_xact_lock(conn, LockScope.RECOVERY_STORE, reservation.store_identity),
                conn.cursor(row_factory=dict_row) as cur,
            ):
                if teardown_evidence is not None:
                    await cur.execute(
                        "UPDATE external_boot_activations SET teardown_evidence = "
                        "COALESCE(teardown_evidence, %s) WHERE id = %s AND system_id = %s "
                        "AND operation_owner_id = %s AND authority_generation = %s "
                        "AND state = %s AND NOT cleanup_complete "
                        "AND (teardown_evidence IS NULL OR teardown_evidence = %s) RETURNING *",
                        (
                            _json(teardown_evidence),
                            activation_id,
                            system_id,
                            operation_owner_id,
                            authority_generation,
                            expected_state,
                            _json(teardown_evidence),
                        ),
                    )
                    activation_row = await cur.fetchone()
                    if activation_row is None:
                        return CasResult(CasStatus.SUPERSEDED)
                await cur.execute(
                    "DELETE FROM external_boot_reservations r USING external_boot_activations a "
                    "WHERE r.activation_id = a.id AND a.id = %s AND a.system_id = %s "
                    "AND a.operation_owner_id = %s AND a.authority_generation = %s "
                    "AND a.state = %s AND NOT a.cleanup_complete",
                    (
                        activation_id,
                        system_id,
                        operation_owner_id,
                        authority_generation,
                        expected_state,
                    ),
                )
                await cur.execute(
                    "INSERT INTO external_boot_reservation_releases "
                    "(activation_id, store_identity, owner_key, reserved_bytes, "
                    "release_identity, release_evidence, teardown_evidence) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                    "RETURNING *",
                    (
                        activation_id,
                        reservation.store_identity,
                        reservation.owner_key,
                        reservation.reserved_bytes,
                        release_identity,
                        _json(release_evidence),
                        _json(teardown_evidence) if teardown_evidence else None,
                    ),
                )
                release_row = await cur.fetchone()
        release = ExternalBootReservationRelease.model_validate(release_row)
        return CasResult(CasStatus.APPLIED, _activation(activation_row), release)

    @staticmethod
    def _known_objects(activation_row: dict[str, Any]) -> set[str]:
        known: set[str] = set()
        materialization = activation_row.get("materialization")
        if materialization:
            artifacts = materialization["artifacts"]
            known.update(
                value["ref"]
                for value in (artifacts["kernel"], artifacts["modules"], artifacts.get("initrd"))
                if value is not None
            )
        recovery_point = activation_row.get("recovery_point")
        if recovery_point:
            known.add(recovery_point["recovery_ref"]["ref"])
        pre_recovery = activation_row.get("pre_recovery_evidence")
        if pre_recovery:
            known.add(pre_recovery["recovery_object"]["ref"])
        return known

    async def mark_cleanup_complete(
        self,
        conn: AsyncConnection,
        *,
        system_id: UUID,
        activation_id: UUID,
        operation_owner_id: UUID,
        authority_generation: int,
        expected_state: ExternalBootActivationState,
        cleanup_evidence: ExternalBootCleanupEvidenceV1,
    ) -> CasResult:
        if (cleanup_evidence.activation_id, cleanup_evidence.system_id) != (
            activation_id,
            system_id,
        ):
            raise ValueError("cleanup evidence ownership does not match the activation")
        ordinary = expected_state in {
            ExternalBootActivationState.RECOVERED,
            ExternalBootActivationState.ABANDONED,
        }
        teardown = expected_state in {
            ExternalBootActivationState.RECOVERY_FAILED,
            ExternalBootActivationState.RECOVERY_CONFLICT,
        }
        if (cleanup_evidence.mode == "ordinary") != ordinary or not (ordinary or teardown):
            return CasResult(CasStatus.SUPERSEDED)
        async with (
            advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "SELECT * FROM external_boot_activations WHERE id = %s AND system_id = %s "
                "AND operation_owner_id = %s AND authority_generation = %s AND state = %s",
                (
                    activation_id,
                    system_id,
                    operation_owner_id,
                    authority_generation,
                    expected_state,
                ),
            )
            existing_row = await cur.fetchone()
            existing = _activation(existing_row)
            if (
                existing is not None
                and existing.cleanup_complete
                and existing.cleanup_evidence == cleanup_evidence
            ):
                return CasResult(CasStatus.APPLIED, existing)
            await cur.execute(
                "SELECT rel.* FROM external_boot_reservation_releases rel "
                "JOIN external_boot_activations a ON a.id = rel.activation_id "
                "WHERE a.id = %s AND a.system_id = %s AND a.operation_owner_id = %s "
                "AND a.authority_generation = %s AND a.state = %s "
                "AND NOT a.cleanup_complete AND rel.release_identity = %s "
                "AND NOT EXISTS (SELECT 1 FROM external_boot_reservations r "
                "WHERE r.activation_id = a.id)",
                (
                    activation_id,
                    system_id,
                    operation_owner_id,
                    authority_generation,
                    expected_state,
                    cleanup_evidence.release_identity,
                ),
            )
            release_row = await cur.fetchone()
            if release_row is None:
                return await self._miss(conn, activation_id)
            release = ExternalBootReservationRelease.model_validate(release_row)
            if release.release_evidence.system_id != system_id or (
                release.teardown_evidence is not None
                and release.teardown_evidence.system_id != system_id
            ):
                return CasResult(CasStatus.SUPERSEDED)
            if teardown:
                state_cur = await conn.execute(
                    "SELECT state FROM systems WHERE id = %s", (system_id,)
                )
                if await state_cur.fetchone() != ("torn_down",):
                    return CasResult(CasStatus.SUPERSEDED)
                if (
                    release.teardown_evidence is None
                    or cleanup_evidence.teardown_identity != release.teardown_evidence.identity
                ):
                    return CasResult(CasStatus.SUPERSEDED)
            await cur.execute(
                "UPDATE external_boot_activations a SET cleanup_complete = true, "
                "cleanup_evidence = %s WHERE a.id = %s AND a.system_id = %s "
                "AND a.operation_owner_id = %s AND a.authority_generation = %s "
                "AND a.state = %s AND NOT a.cleanup_complete "
                "AND NOT EXISTS (SELECT 1 FROM external_boot_reservations r "
                "WHERE r.activation_id = a.id) "
                "AND EXISTS (SELECT 1 FROM external_boot_reservation_releases rel "
                "WHERE rel.activation_id = a.id AND rel.release_identity = %s) RETURNING a.*",
                (
                    _json(cleanup_evidence),
                    activation_id,
                    system_id,
                    operation_owner_id,
                    authority_generation,
                    expected_state,
                    cleanup_evidence.release_identity,
                ),
            )
            row = await cur.fetchone()
        if row is None:
            return await self._miss(conn, activation_id)
        return CasResult(CasStatus.APPLIED, _activation(row))


__all__ = ["CasResult", "CasStatus", "ExternalBootActivationRepository"]

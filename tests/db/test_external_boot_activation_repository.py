"""Repository proofs for external-boot activation persistence (ADR-0583/0584)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TypedDict
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from kdive.db.external_boot_activations import (
    CasStatus,
    ExternalBootActivationRepository,
)
from kdive.db.locks import LockScope, advisory_xact_lock, try_advisory_xact_lock
from kdive.domain.external_boot_activation import (
    ExternalBootActivation,
    ExternalBootActivationState,
    ExternalBootCleanupEvidenceV1,
    ExternalBootConflictEvidenceV1,
    ExternalBootPreRecoveryEvidenceV1,
    ExternalBootReleaseEvidenceV1,
    ExternalBootReleaseObject,
    ExternalBootReservation,
    ExternalBootReservationRelease,
    ExternalBootReservationState,
    ExternalBootTeardownEvidenceV1,
    ExternalBootTerminalEvidenceV1,
)
from kdive.providers.ports.external_boot import (
    ExternalBootMaterialization,
    OpaqueProviderRef,
    RecoveryPoint,
)
from tests.db_waits import wait_until_backend_waiting

_AT = datetime(2026, 8, 28, tzinfo=UTC)
_PLAN = "sha256:" + "a" * 64
_DIGEST = "sha256:" + "b" * 64


async def _seed(conn: psycopg.AsyncConnection) -> tuple[UUID, UUID]:
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id, run_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
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
        "(%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return system_id, run_id


def _records(
    system_id: UUID, run_id: UUID
) -> tuple[ExternalBootActivation, ExternalBootReservation]:
    activation_id, owner_id = uuid4(), uuid4()
    activation = ExternalBootActivation(
        id=activation_id,
        system_id=system_id,
        run_id=run_id,
        plan_identity=_PLAN,
        operation_owner_id=owner_id,
        authority_generation=7,
        state=ExternalBootActivationState.PREPARING,
        created_at=_AT,
        updated_at=_AT,
    )
    reservation = ExternalBootReservation(
        activation_id=activation_id,
        store_identity="stores/main",
        owner_key=f"owners/{activation_id}",
        reserved_bytes=4096,
        state=ExternalBootReservationState.PENDING,
        created_at=_AT,
        updated_at=_AT,
    )
    return activation, reservation


def _materialization(activation: ExternalBootActivation) -> ExternalBootMaterialization:
    return ExternalBootMaterialization.model_validate(
        {
            "architecture": "x86_64",
            "provider_kind": "remote-libvirt",
            "ownership": {
                "system_id": str(activation.system_id),
                "run_id": str(activation.run_id),
            },
            "plan_identity": activation.plan_identity,
            "extracted_vmlinuz_sha256": _DIGEST,
            "source_module_manifest": _DIGEST,
            "installed_module_tree": _DIGEST,
            "verified_bundle_sha256": _DIGEST,
            "verified_initrd_sha256": None,
            "kernel_observation": {
                "architecture": "x86_64",
                "release": "6.12.0",
                "gnu_build_id": "deadbeef",
            },
            "artifacts": {
                "kernel": {"ref": "objects/kernel"},
                "modules": {"ref": "objects/modules"},
                "initrd": None,
            },
        }
    )


def _recovery_point(
    activation: ExternalBootActivation, materialization: ExternalBootMaterialization
) -> RecoveryPoint:
    return RecoveryPoint.model_validate(
        {
            "ownership": {
                "system_id": str(activation.system_id),
                "run_id": str(activation.run_id),
            },
            "plan_identity": activation.plan_identity,
            "materialization_identity": materialization.identity,
            "recovery_ref": {"ref": "objects/recovery"},
            "source_state": {
                "definition": _DIGEST,
                "modules": {"state": "absent"},
            },
            "target_state": {
                "definition": "sha256:" + "c" * 64,
                "modules": {"state": "present", "manifest": _DIGEST},
            },
        }
    )


class _AuthorityArgs(TypedDict):
    system_id: UUID
    activation_id: UUID
    operation_owner_id: UUID
    authority_generation: int


def _authority(activation: ExternalBootActivation) -> _AuthorityArgs:
    return {
        "system_id": activation.system_id,
        "activation_id": activation.id,
        "operation_owner_id": activation.operation_owner_id,
        "authority_generation": activation.authority_generation,
    }


async def _ledger_snapshot(conn: psycopg.AsyncConnection) -> tuple[str | None, ...]:
    snapshots: list[str | None] = []
    for table, order in (
        ("external_boot_activations", "id"),
        ("external_boot_reservations", "activation_id"),
        ("external_boot_reservation_releases", "activation_id"),
        ("external_boot_recovery_attempts", "activation_id, attempt_number"),
    ):
        cur = await conn.execute(
            f"SELECT jsonb_agg(to_jsonb(row_value) ORDER BY {order})::text "  # noqa: S608
            f"FROM {table} row_value"  # noqa: S608
        )
        row = await cur.fetchone()
        assert row is not None
        snapshots.append(row[0])
    return tuple(snapshots)


def test_create_reads_and_stale_generation_are_atomic(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            created = await repo.create(conn, activation, reservation)
            assert await repo.create(conn, activation, reservation) == created
            assert await repo.get(conn, activation.id) == created
            assert await repo.get_reservation(conn, activation.id) is not None

            materialization = _materialization(activation)
            stale_authority = _authority(activation)
            stale_authority["authority_generation"] = 6
            before = await _ledger_snapshot(conn)
            stale_capacity = await repo.mark_reservation_ready(
                conn,
                **stale_authority,
                expected_state=ExternalBootActivationState.PREPARING,
                recovery_max_bytes=reservation.reserved_bytes,
            )
            assert stale_capacity.status is CasStatus.SUPERSEDED
            assert await _ledger_snapshot(conn) == before
            before = await _ledger_snapshot(conn)
            stale = await repo.record_materialization(
                conn,
                **stale_authority,
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=materialization,
            )
            assert stale.status is CasStatus.SUPERSEDED
            assert await _ledger_snapshot(conn) == before
            unchanged = await repo.get(conn, activation.id)
            assert unchanged is not None and unchanged.materialization is None

            applied = await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=materialization,
            )
            assert applied.status is CasStatus.APPLIED
            assert applied.activation is not None
            assert applied.activation.materialization == materialization
            await conn.commit()

    asyncio.run(_run())


def test_database_rejects_cross_activation_evidence(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            evidence = ExternalBootPreRecoveryEvidenceV1(
                activation_id=uuid4(),
                system_id=system_id,
                run_id=run_id,
                plan_identity=_PLAN,
                recovery_object=OpaqueProviderRef(ref="objects/recovery"),
                source_composite_state=_DIGEST,
                observed_at=_AT,
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="evidence_ownership"):
                await conn.execute(
                    "UPDATE external_boot_activations SET pre_recovery_evidence = %s WHERE id = %s",
                    (Jsonb(evidence.model_dump(mode="json", by_alias=True)), activation.id),
                )

    asyncio.run(_run())


def test_release_row_load_rejects_mismatched_identity(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            evidence = ExternalBootReleaseEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                store_identity=OpaqueProviderRef(ref=reservation.store_identity),
                owner_key=OpaqueProviderRef(ref=reservation.owner_key),
                reserved_bytes=reservation.reserved_bytes,
                objects=(),
                verified_at=_AT,
            )
            await conn.execute(
                "INSERT INTO external_boot_reservation_releases "
                "(activation_id, store_identity, owner_key, reserved_bytes, release_identity, "
                "release_evidence) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    activation.id,
                    reservation.store_identity,
                    reservation.owner_key,
                    reservation.reserved_bytes,
                    "sha256:" + "f" * 64,
                    Jsonb(evidence.model_dump(mode="json", by_alias=True)),
                ),
            )
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT * FROM external_boot_reservation_releases WHERE activation_id = %s",
                    (activation.id,),
                )
                row = await cur.fetchone()
            assert row is not None
            with pytest.raises(ValidationError, match="release identity"):
                ExternalBootReservationRelease.model_validate(row)

    asyncio.run(_run())


def test_capacity_exact_cap_and_over_cap_across_systems(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            first_system, first_run = await _seed(conn)
            second_system, second_run = await _seed(conn)
            first, first_reservation = _records(first_system, first_run)
            second, second_reservation = _records(second_system, second_run)
            second_reservation = second_reservation.model_copy(
                update={"store_identity": first_reservation.store_identity}
            )
            await repo.create(conn, first, first_reservation)
            await repo.create(conn, second, second_reservation)
            assert (
                await repo.mark_reservation_ready(
                    conn,
                    **_authority(first),
                    expected_state=ExternalBootActivationState.PREPARING,
                    recovery_max_bytes=first_reservation.reserved_bytes,
                )
            ).status is CasStatus.APPLIED
            before = await _ledger_snapshot(conn)
            assert (
                await repo.mark_reservation_ready(
                    conn,
                    **_authority(second),
                    expected_state=ExternalBootActivationState.PREPARING,
                    recovery_max_bytes=first_reservation.reserved_bytes,
                )
            ).status is CasStatus.CAPACITY_EXHAUSTED
            assert await _ledger_snapshot(conn) == before
            pending = await repo.get_reservation(conn, second.id)
            assert pending is not None
            assert pending.state is ExternalBootReservationState.PENDING
            await conn.commit()

    asyncio.run(_run())


def test_capacity_write_takes_system_before_recovery_store_lock(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with (
            await psycopg.AsyncConnection.connect(migrated_url) as setup,
            await psycopg.AsyncConnection.connect(migrated_url) as holder,
            await psycopg.AsyncConnection.connect(migrated_url) as waiter,
            await psycopg.AsyncConnection.connect(migrated_url) as probe,
        ):
            system_id, run_id = await _seed(setup)
            activation, reservation = _records(system_id, run_id)
            await repo.create(setup, activation, reservation)
            await setup.commit()

            async def admit() -> CasStatus:
                async with waiter.transaction():
                    result = await repo.mark_reservation_ready(
                        waiter,
                        **_authority(activation),
                        expected_state=ExternalBootActivationState.PREPARING,
                        recovery_max_bytes=reservation.reserved_bytes,
                    )
                    return result.status

            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.RECOVERY_STORE, reservation.store_identity),
            ):
                task = asyncio.create_task(admit())
                await wait_until_backend_waiting(
                    holder, waiter.info.backend_pid, locktype="advisory"
                )
                async with probe.transaction():
                    assert not await try_advisory_xact_lock(probe, LockScope.SYSTEM, system_id)
                assert not task.done()
            assert await asyncio.wait_for(task, timeout=5) is CasStatus.APPLIED

    asyncio.run(_run())


def test_capacity_release_cleanup_and_post_cleanup_fence(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            ready = await repo.mark_reservation_ready(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                recovery_max_bytes=4096,
            )
            assert ready.status is CasStatus.APPLIED

            terminal = ExternalBootTerminalEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                outcome="abandoned",
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
            abandoned = await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                new_state=ExternalBootActivationState.ABANDONED,
                terminal_evidence=terminal,
            )
            assert abandoned.status is CasStatus.APPLIED

            release = ExternalBootReleaseEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                store_identity=OpaqueProviderRef(ref=reservation.store_identity),
                owner_key=OpaqueProviderRef(ref=reservation.owner_key),
                reserved_bytes=reservation.reserved_bytes,
                objects=(),
                verified_at=_AT,
            )
            released = await repo.release_reservation(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.ABANDONED,
                release_evidence=release,
            )
            assert released.status is CasStatus.APPLIED
            assert await repo.get_reservation(conn, activation.id) is None
            assert (await repo.create(conn, activation, reservation)).state is (
                ExternalBootActivationState.ABANDONED
            )
            assert await repo.get_reservation(conn, activation.id) is None
            mismatched_reservation = reservation.model_copy(update={"owner_key": "owners/other"})
            with pytest.raises(ValueError, match="release identity"):
                await repo.create(conn, activation, mismatched_reservation)

            cleanup = ExternalBootCleanupEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                release_identity=release.identity,
                mode="ordinary",
                completed_at=_AT + timedelta(seconds=1),
            )
            cleaned = await repo.mark_cleanup_complete(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.ABANDONED,
                cleanup_evidence=cleanup,
            )
            assert cleaned.status is CasStatus.APPLIED
            assert (await repo.create(conn, activation, reservation)).cleanup_complete
            assert await repo.get_reservation(conn, activation.id) is None
            await conn.commit()

    asyncio.run(_run())


def test_oversized_release_fails_before_live_debit_delete(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            objects = tuple(
                ExternalBootReleaseObject(
                    object=OpaqueProviderRef(ref=f"objects/{index:03d}-" + "x" * 1000)
                )
                for index in range(70)
            )
            oversized = ExternalBootReleaseEvidenceV1.model_construct(
                activation_id=activation.id,
                system_id=system_id,
                store_identity=OpaqueProviderRef(ref=reservation.store_identity),
                owner_key=OpaqueProviderRef(ref=reservation.owner_key),
                reserved_bytes=reservation.reserved_bytes,
                enumeration_complete=True,
                objects=objects,
                verified_at=_AT,
            )
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="65536"):
                await repo.release_reservation(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.ABANDONED,
                    release_evidence=oversized,
                )
            await conn.commit()
            assert await _ledger_snapshot(conn) == before

    asyncio.run(_run())


def test_cleanup_rejects_release_evidence_for_another_system(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            terminal = ExternalBootTerminalEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                outcome="abandoned",
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
            await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                new_state=ExternalBootActivationState.ABANDONED,
                terminal_evidence=terminal,
            )
            evidence = ExternalBootReleaseEvidenceV1(
                activation_id=activation.id,
                system_id=uuid4(),
                store_identity=OpaqueProviderRef(ref=reservation.store_identity),
                owner_key=OpaqueProviderRef(ref=reservation.owner_key),
                reserved_bytes=reservation.reserved_bytes,
                objects=(),
                verified_at=_AT,
            )
            await conn.execute(
                "DELETE FROM external_boot_reservations WHERE activation_id = %s",
                (activation.id,),
            )
            await conn.execute(
                "INSERT INTO external_boot_reservation_releases "
                "(activation_id, store_identity, owner_key, reserved_bytes, release_identity, "
                "release_evidence) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    activation.id,
                    reservation.store_identity,
                    reservation.owner_key,
                    reservation.reserved_bytes,
                    evidence.identity,
                    Jsonb(evidence.model_dump(mode="json", by_alias=True)),
                ),
            )
            cleanup = ExternalBootCleanupEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                release_identity=evidence.identity,
                mode="ordinary",
                completed_at=_AT,
            )
            before = await _ledger_snapshot(conn)
            result = await repo.mark_cleanup_complete(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.ABANDONED,
                cleanup_evidence=cleanup,
            )
            assert result.status is CasStatus.SUPERSEDED
            assert await _ledger_snapshot(conn) == before

    asyncio.run(_run())


def test_prepared_activation_deadline_and_terminal_evidence_round_trip(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            materialization = _materialization(activation)
            await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=materialization,
            )
            recovery_point = _recovery_point(activation, materialization)
            pending = await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                new_state=ExternalBootActivationState.PREPARED,
                recovery_point=recovery_point,
            )
            assert pending.status is CasStatus.SUPERSEDED
            await repo.mark_reservation_ready(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                recovery_max_bytes=reservation.reserved_bytes,
            )
            prepared = await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                new_state=ExternalBootActivationState.PREPARED,
                recovery_point=recovery_point,
            )
            assert prepared.status is CasStatus.APPLIED
            deadline = _AT + timedelta(minutes=5)
            activating = await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARED,
                new_state=ExternalBootActivationState.ACTIVATING,
                activation_readiness_deadline=deadline,
            )
            assert activating.activation is not None
            assert activating.activation.activation_readiness_deadline == deadline
            terminal = ExternalBootTerminalEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                outcome="active",
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
            active = await repo.transition(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.ACTIVATING,
                new_state=ExternalBootActivationState.ACTIVE,
                terminal_evidence=terminal,
            )
            assert active.activation is not None
            assert active.activation.recovery_point == recovery_point
            assert active.activation.activation_readiness_deadline == deadline
            assert active.activation.terminal_evidence == terminal
            await conn.commit()

    asyncio.run(_run())


def test_pre_recovery_evidence_requires_materialization(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            evidence = ExternalBootPreRecoveryEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                run_id=run_id,
                plan_identity=_PLAN,
                recovery_object=OpaqueProviderRef(ref="objects/recovery"),
                source_composite_state=_DIGEST,
                observed_at=_AT,
            )
            rejected = await repo.record_pre_recovery_evidence(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                evidence=evidence,
            )
            assert rejected.status is CasStatus.SUPERSEDED
            await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=_materialization(activation),
            )
            accepted = await repo.record_pre_recovery_evidence(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                evidence=evidence,
            )
            assert accepted.status is CasStatus.APPLIED
            await conn.commit()

    asyncio.run(_run())


def test_materialization_and_pre_recovery_bind_the_run_and_plan(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            materialization_values = _materialization(activation).model_dump(by_alias=True)
            materialization_values["ownership"]["run_id"] = str(uuid4())
            wrong_run = ExternalBootMaterialization.model_validate(materialization_values)
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="ownership"):
                await repo.record_materialization(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.PREPARING,
                    materialization=wrong_run,
                )
            assert await _ledger_snapshot(conn) == before

            await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=_materialization(activation),
            )
            wrong_plan = ExternalBootPreRecoveryEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                run_id=run_id,
                plan_identity="sha256:" + "f" * 64,
                recovery_object=OpaqueProviderRef(ref="objects/recovery"),
                source_composite_state=_DIGEST,
                observed_at=_AT,
            )
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="Run and plan"):
                await repo.record_pre_recovery_evidence(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.PREPARING,
                    evidence=wrong_plan,
                )
            assert await _ledger_snapshot(conn) == before
            await conn.commit()

    asyncio.run(_run())


def test_pre_recovery_conflict_resolution_retains_attempt_history(migrated_url: str) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            await repo.mark_reservation_ready(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                recovery_max_bytes=reservation.reserved_bytes,
            )
            await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=_materialization(activation),
            )
            pre_recovery = ExternalBootPreRecoveryEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                run_id=run_id,
                plan_identity=_PLAN,
                recovery_object=OpaqueProviderRef(ref="objects/recovery"),
                source_composite_state=_DIGEST,
                observed_at=_AT,
            )
            await repo.record_pre_recovery_evidence(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                evidence=pre_recovery,
            )
            conflict = ExternalBootConflictEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                observation_id=uuid4(),
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
            first_attempt = uuid4()
            observed = await repo.record_conflict(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                attempt_id=first_attempt,
                evidence=conflict,
            )
            assert observed.status is CasStatus.APPLIED

            second_attempt = uuid4()
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="current conflict evidence"):
                await repo.begin_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                    attempt_id=second_attempt,
                    recovery_readiness_deadline=_AT + timedelta(minutes=10),
                    resolution_operation="accept-observed-state",
                    resolution_identity="sha256:" + "c" * 64,
                    acknowledged_composite_state="sha256:" + "d" * 64,
                )
            assert await _ledger_snapshot(conn) == before
            for invalid_operation in ("", "x" * 256):
                with pytest.raises(ValueError, match="1 through 255"):
                    await repo.begin_recovery_attempt(
                        conn,
                        **_authority(activation),
                        expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                        attempt_id=second_attempt,
                        recovery_readiness_deadline=_AT + timedelta(minutes=10),
                        resolution_operation=invalid_operation,
                        resolution_identity="sha256:" + "c" * 64,
                        acknowledged_composite_state=_DIGEST,
                    )
                assert await _ledger_snapshot(conn) == before
            begun = await repo.begin_recovery_attempt(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                attempt_id=second_attempt,
                recovery_readiness_deadline=_AT + timedelta(minutes=10),
                resolution_operation="accept-observed-state",
                resolution_identity="sha256:" + "c" * 64,
                acknowledged_composite_state=_DIGEST,
            )
            assert begun.status is CasStatus.APPLIED
            assert (
                await repo.begin_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                    attempt_id=second_attempt,
                    recovery_readiness_deadline=_AT + timedelta(minutes=10),
                    resolution_operation="accept-observed-state",
                    resolution_identity="sha256:" + "c" * 64,
                    acknowledged_composite_state=_DIGEST,
                )
            ).status is CasStatus.APPLIED
            current_attempt = await repo.get_current_recovery_attempt(conn, activation.id)
            assert current_attempt is not None
            assert current_attempt.attempt_id == second_attempt
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="direct-conflict"):
                await repo.record_conflict(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERING,
                    attempt_id=uuid4(),
                    evidence=conflict,
                )
            assert await _ledger_snapshot(conn) == before

            terminal = ExternalBootTerminalEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                outcome="recovered",
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT + timedelta(minutes=1),
            )
            wrong_terminal = terminal.model_copy(update={"outcome": "recovery_failed"})
            before = await _ledger_snapshot(conn)
            with pytest.raises(ValueError, match="outcome"):
                await repo.finish_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERING,
                    attempt_id=second_attempt,
                    new_state=ExternalBootActivationState.RECOVERED,
                    terminal_evidence=wrong_terminal,
                )
            assert await _ledger_snapshot(conn) == before
            finished = await repo.finish_recovery_attempt(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.RECOVERING,
                attempt_id=second_attempt,
                new_state=ExternalBootActivationState.RECOVERED,
                terminal_evidence=terminal,
            )
            assert finished.status is CasStatus.APPLIED
            assert (
                await repo.finish_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERING,
                    attempt_id=second_attempt,
                    new_state=ExternalBootActivationState.RECOVERED,
                    terminal_evidence=terminal,
                )
            ).status is CasStatus.APPLIED
            attempts = await repo.list_recovery_attempts(conn, activation.id, limit=10)
            assert [item.attempt_id for item in attempts] == [second_attempt, first_attempt]
            assert attempts[0].recovery_basis == "pre_recovery"
            recovered = await repo.get(conn, activation.id)
            assert recovered is not None and recovered.recovery_point is None
            await conn.commit()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "cleanup_state",
    [
        ExternalBootActivationState.RECOVERY_CONFLICT,
        ExternalBootActivationState.RECOVERY_FAILED,
    ],
)
def test_teardown_cleanup_releases_capacity_and_fences_terminal_state(
    migrated_url: str, cleanup_state: ExternalBootActivationState
) -> None:
    async def _run() -> None:
        repo = ExternalBootActivationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            activation, reservation = _records(system_id, run_id)
            await repo.create(conn, activation, reservation)
            await repo.mark_reservation_ready(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                recovery_max_bytes=reservation.reserved_bytes,
            )
            await repo.record_materialization(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                materialization=_materialization(activation),
            )
            pre_recovery = ExternalBootPreRecoveryEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                run_id=run_id,
                plan_identity=_PLAN,
                recovery_object=OpaqueProviderRef(ref="objects/recovery"),
                source_composite_state=_DIGEST,
                observed_at=_AT,
            )
            await repo.record_pre_recovery_evidence(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                evidence=pre_recovery,
            )
            conflict = ExternalBootConflictEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                observation_id=uuid4(),
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
            await repo.record_conflict(
                conn,
                **_authority(activation),
                expected_state=ExternalBootActivationState.PREPARING,
                attempt_id=uuid4(),
                evidence=conflict,
            )
            if cleanup_state is ExternalBootActivationState.RECOVERY_FAILED:
                attempt_id = uuid4()
                await repo.begin_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                    attempt_id=attempt_id,
                    recovery_readiness_deadline=_AT + timedelta(minutes=10),
                    resolution_operation="accept-observed-state",
                    resolution_identity="sha256:" + "c" * 64,
                    acknowledged_composite_state=_DIGEST,
                )
                failure = ExternalBootTerminalEvidenceV1(
                    activation_id=activation.id,
                    system_id=system_id,
                    outcome="recovery_failed",
                    composite_state=_DIGEST,
                    objects=(),
                    observed_at=_AT,
                )
                await repo.finish_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERING,
                    attempt_id=attempt_id,
                    new_state=ExternalBootActivationState.RECOVERY_FAILED,
                    terminal_evidence=failure,
                )
            await conn.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            refs = tuple(
                ExternalBootReleaseObject(object=OpaqueProviderRef(ref=value))
                for value in sorted(("objects/kernel", "objects/modules", "objects/recovery"))
            )
            release = ExternalBootReleaseEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                store_identity=OpaqueProviderRef(ref=reservation.store_identity),
                owner_key=OpaqueProviderRef(ref=reservation.owner_key),
                reserved_bytes=reservation.reserved_bytes,
                objects=refs,
                verified_at=_AT,
            )
            teardown = ExternalBootTeardownEvidenceV1(system_id=system_id, observed_at=_AT)
            released = await repo.release_reservation(
                conn,
                **_authority(activation),
                expected_state=cleanup_state,
                release_evidence=release,
                teardown_evidence=teardown,
            )
            assert released.status is CasStatus.APPLIED
            cleanup = ExternalBootCleanupEvidenceV1(
                activation_id=activation.id,
                system_id=system_id,
                release_identity=release.identity,
                mode="system_teardown",
                teardown_identity=teardown.identity,
                completed_at=_AT + timedelta(seconds=1),
            )
            cleaned = await repo.mark_cleanup_complete(
                conn,
                **_authority(activation),
                expected_state=cleanup_state,
                cleanup_evidence=cleanup,
            )
            assert cleaned.status is CasStatus.APPLIED
            assert cleaned.activation is not None and cleaned.activation.cleanup_complete
            assert cleaned.activation.pre_recovery_evidence == pre_recovery
            assert cleaned.activation.teardown_evidence == teardown
            assert cleaned.activation.current_attempt_id is not None
            if cleanup_state is ExternalBootActivationState.RECOVERY_CONFLICT:
                fenced = await repo.begin_recovery_attempt(
                    conn,
                    **_authority(activation),
                    expected_state=ExternalBootActivationState.RECOVERY_CONFLICT,
                    attempt_id=uuid4(),
                    recovery_readiness_deadline=_AT + timedelta(minutes=20),
                    resolution_operation="accept-observed-state",
                    resolution_identity="sha256:" + "d" * 64,
                    acknowledged_composite_state=_DIGEST,
                )
                assert fenced.status is CasStatus.SUPERSEDED
            await conn.commit()

    asyncio.run(_run())

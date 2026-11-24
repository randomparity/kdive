"""Real-Postgres admission budgets for automatic external-boot repairs."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from kdive.reconciler.repairs import external_boot
from tests.db.external_boot_authority_support import _RoleDsns
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import SeededCase, seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle

_OPERATIONS: dict[str, tuple[external_boot.RepairLane, str, str]] = {
    "activate": ("activation", "activate", "prepared"),
    "recover": ("recovery", "recover", "recovering"),
    "resolve-conflict": ("recovery", "resolve-conflict", "recovering"),
    "release": ("release", "release", "recovered"),
    "cleanup": ("cleanup", "release", "recovered"),
    "teardown": ("cleanup", "teardown", "recovery_failed"),
}


@dataclass
class _Case:
    admin: psycopg.AsyncConnection
    conn: psycopg.AsyncConnection
    seeded: SeededCase
    lane: external_boot.RepairLane
    cursors: external_boot.RepairCursors = field(default_factory=dict)

    async def repair(self, authority: str = "authority-current") -> int:
        count = await external_boot.repair_external_boot_lane(
            self.conn,
            lane=self.lane,
            resolver=resolver_for(self.seeded.vehicle),
            authority_instance=authority,
            cursors=self.cursors,
        )
        assert self.conn.info.transaction_status is TransactionStatus.IDLE
        return count

    async def ids(self) -> set[UUID]:
        rows = await (
            await self.admin.execute(
                "SELECT id FROM jobs WHERE dedup_key LIKE %s",
                ("external-boot:repair:%",),
            )
        ).fetchall()
        return {row[0] for row in rows}

    async def exhaust(self, job_id: UUID, *, state: str = "running") -> None:
        await self.admin.execute(
            "UPDATE jobs SET state=%s, attempt=max_attempts, worker_id=%s, "
            "lease_expires_at=clock_timestamp()-interval '1 second', "
            "heartbeat_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (state, self.seeded.worker_incarnation, job_id),
        )

    async def finite_slot(self, authority: str = "authority-current") -> UUID:
        before = await self.ids()
        assert await self.repair(authority) == 1
        added = await self.ids() - before
        assert len(added) == 1
        job_id = added.pop()
        await self.exhaust(job_id)
        assert await self.repair(authority) == 0
        assert await self.ids() == before | {job_id}
        return job_id

    async def copy_job(self, source_id: UUID, *, key: str, operation_identity: str) -> UUID:
        job_id = uuid4()
        await self.admin.execute(
            "INSERT INTO jobs (id,kind,payload,state,attempt,max_attempts,authorizing,dedup_key, "
            "created_at) SELECT %s,kind,jsonb_set(payload, "
            "'{external_boot_authority_v1,operation_identity}',%s), 'failed',max_attempts, "
            "max_attempts,authorizing,%s,clock_timestamp() FROM jobs WHERE id=%s",
            (job_id, Jsonb(operation_identity), key, source_id),
        )
        return job_id


@asynccontextmanager
async def _case(url: str, roles: _RoleDsns, operation: str = "activate") -> AsyncGenerator[_Case]:
    lane, purpose, state = _OPERATIONS[operation]
    async with (
        await psycopg.AsyncConnection.connect(url, autocommit=True) as admin,
        await psycopg.AsyncConnection.connect(roles("kdive_reconciler"), autocommit=True) as conn,
    ):
        seeded = await seed_case(
            admin,
            build_vehicle(),
            purpose=purpose,
            operation=operation,
            activation_state=state,
            attempt_state="failed" if operation == "teardown" else "recovering",
            with_reservation=operation == "release",
            with_release=operation in {"cleanup", "teardown"},
            with_pre_recovery=operation == "resolve-conflict",
        )
        case = _Case(admin, conn, seeded, lane)
        await case.exhaust(seeded.job_id)
        if lane == "recovery":
            await admin.execute(
                "UPDATE external_boot_recovery_attempts SET recovery_readiness_deadline="
                "clock_timestamp()-interval '1 second', recovery_basis=%s WHERE attempt_id=%s",
                (
                    "pre_recovery" if operation == "resolve-conflict" else "recovery_point",
                    seeded.attempt_id,
                ),
            )
        yield case


@pytest.mark.parametrize("operation", list(_OPERATIONS))
@pytest.mark.parametrize("terminal", ["running", "failed"])
def test_unchanged_phase_does_not_replenish_automatic_attempts(
    migrated_url: str, authority_role_dsns: _RoleDsns, operation: str, terminal: str
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns, operation) as case:
            before = await (
                await case.admin.execute(
                    "SELECT state,current_attempt_id,activation_readiness_deadline "
                    "FROM external_boot_activations WHERE id=%s",
                    (case.seeded.vehicle.activation_id,),
                )
            ).fetchone()
            assert await case.repair() == 1
            [job_id] = await case.ids()
            await case.exhaust(job_id, state=terminal)
            counts = []
            for _ in range(3):
                prior = await case.ids()
                counts.append(await case.repair())
                for replacement in await case.ids() - prior:
                    await case.exhaust(replacement, state=terminal)
            assert counts == [0, 0, 0]
            assert await case.ids() == {job_id}
            row = await (
                await case.admin.execute(
                    "SELECT state,attempt,max_attempts,authorizing,payload FROM jobs WHERE id=%s",
                    (job_id,),
                )
            ).fetchone()
            assert row is not None and row[:3] == (terminal, 3, 3)
            assert row[3] == {"principal": "p", "agent_session": None, "project": "proj"}
            marker = row[4]["external_boot_authority_v1"]
            assert str(case.seeded.job_id) in marker["operation_identity"]
            assert len(marker["operation_identity"].encode()) <= 255
            assert (
                before
                == await (
                    await case.admin.execute(
                        "SELECT state,current_attempt_id,activation_readiness_deadline "
                        "FROM external_boot_activations WHERE id=%s",
                        (case.seeded.vehicle.activation_id,),
                    )
                ).fetchone()
            )

    asyncio.run(run())


@pytest.mark.parametrize("keep_origin", [True, False])
def test_historical_chain_cannot_supply_or_hide_the_source(
    migrated_url: str, authority_role_dsns: _RoleDsns, keep_origin: bool
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            source_id = case.seeded.job_id
            for _ in range(101):
                identity = f"repair:{case.seeded.vehicle.activation_id}:activate:{source_id}"
                source_id = await case.copy_job(
                    source_id, key=f"external-boot:{identity}", operation_identity=identity
                )
            if not keep_origin:
                await case.admin.execute("DELETE FROM jobs WHERE id=%s", (case.seeded.job_id,))
            before = await case.ids()
            assert await case.repair() == int(keep_origin)
            added = await case.ids() - before
            if keep_origin:
                [job_id] = added
                row = await (
                    await case.admin.execute(
                        "SELECT payload #>> '{external_boot_authority_v1,operation_identity}' "
                        "FROM jobs WHERE id=%s",
                        (job_id,),
                    )
                ).fetchone()
                assert row is not None and str(case.seeded.job_id) in row[0]
                assert str(source_id) not in row[0]
            else:
                assert added == set()

    asyncio.run(run())


def test_live_automatic_job_suppresses_a_new_explicit_source(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            assert await case.repair() == 1
            before = await case.ids()
            await case.copy_job(
                case.seeded.job_id,
                key=f"external-boot-request:{uuid4()}",
                operation_identity=f"request:{uuid4()}",
            )
            assert await case.repair() == 0
            assert await case.ids() == before

    asyncio.run(run())


def test_a_new_explicit_request_gets_one_new_finite_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            await case.finite_slot()
            request = await case.copy_job(
                case.seeded.job_id,
                key=f"external-boot-request:{uuid4()}",
                operation_identity=f"request:{uuid4()}",
            )
            job_id = await case.finite_slot()
            row = await (
                await case.admin.execute(
                    "SELECT payload #>> '{external_boot_authority_v1,operation_identity}' "
                    "FROM jobs WHERE id=%s",
                    (job_id,),
                )
            ).fetchone()
            assert row is not None and str(request) in row[0]

    asyncio.run(run())


def test_committed_activation_phase_permits_another_finite_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            await case.finite_slot()
            await case.admin.execute(
                "UPDATE external_boot_activations SET state='activating', "
                "activation_readiness_deadline=clock_timestamp()-interval '1 second' WHERE id=%s",
                (case.seeded.vehicle.activation_id,),
            )
            await case.finite_slot()

    asyncio.run(run())


def test_authority_route_change_permits_another_finite_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            await case.finite_slot()
            await case.finite_slot("authority-replacement")

    asyncio.run(run())


def test_bookkeeping_changes_cannot_renew_a_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            job_id = await case.finite_slot()
            await case.admin.execute(
                "UPDATE external_boot_activations SET updated_at=clock_timestamp(), "
                "authority_generation=authority_generation+1,operation_owner_id=%s WHERE id=%s",
                (uuid4(), case.seeded.vehicle.activation_id),
            )
            await case.admin.execute(
                "UPDATE jobs SET updated_at=clock_timestamp(), "
                "lease_expires_at=clock_timestamp()-interval '2 seconds', "
                "heartbeat_at=clock_timestamp()-interval '2 seconds' WHERE id=ANY(%s)",
                ([job_id, case.seeded.job_id],),
            )
            assert await case.repair() == 0
            assert await case.ids() == {job_id}
            assert not any("candidate repair failed" in r.getMessage() for r in caplog.records)

    asyncio.run(run())


def test_timezone_changes_preserve_the_same_deadline_budget(
    migrated_url: str, authority_role_dsns: _RoleDsns, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            await case.admin.execute(
                "UPDATE external_boot_activations SET state='activating', "
                "activation_readiness_deadline=clock_timestamp()-interval '1 second' WHERE id=%s",
                (case.seeded.vehicle.activation_id,),
            )
            await case.conn.execute("SET TIME ZONE 'UTC'")
            job_id = await case.finite_slot()
            await case.conn.execute("SET TIME ZONE 'Pacific/Honolulu'")
            [candidate] = await external_boot._candidates(case.conn, "activation")
            assert candidate.readiness_deadline is not None
            assert candidate.readiness_deadline.utcoffset() != timedelta(0)
            # Payload repositories retain their existing UTC-only connection contract.
            await case.conn.execute("SET TIME ZONE 'UTC'")
            sources = await external_boot._candidate_jobs(
                case.conn, (candidate,), live_only=False, authority_instance="authority-current"
            )
            live = await external_boot._candidate_jobs(
                case.conn, (candidate,), live_only=True, authority_instance="authority-current"
            )
            async with case.conn.transaction():
                assert not await external_boot._enqueue_candidate(
                    case.conn,
                    candidate,
                    resolver=resolver_for(case.seeded.vehicle),
                    authority_instance="authority-current",
                    source_jobs=sources,
                    live_jobs=live,
                )
            assert await case.ids() == {job_id}
            assert not any("candidate repair failed" in r.getMessage() for r in caplog.records)

    asyncio.run(run())


def test_concurrent_sweeps_admit_only_one_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_reconciler"), autocommit=True
            ) as peer:
                counts = await asyncio.gather(
                    case.repair(),
                    external_boot.repair_external_boot_lane(
                        peer,
                        lane=case.lane,
                        resolver=resolver_for(case.seeded.vehicle),
                        authority_instance="authority-current",
                        cursors={},
                    ),
                )
            assert sum(counts) == 1
            assert len(await case.ids()) == 1

    asyncio.run(run())


def test_changed_persisted_deadline_permits_one_new_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            await case.admin.execute(
                "UPDATE external_boot_activations SET state='activating', "
                "activation_readiness_deadline=clock_timestamp()-interval '1 second' WHERE id=%s",
                (case.seeded.vehicle.activation_id,),
            )
            await case.finite_slot()
            await case.admin.execute(
                "UPDATE external_boot_activations SET activation_readiness_deadline="
                "activation_readiness_deadline-interval '1 second' WHERE id=%s",
                (case.seeded.vehicle.activation_id,),
            )
            await case.finite_slot()

    asyncio.run(run())


def test_new_current_attempt_permits_one_new_slot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns, "recover") as case:
            await case.finite_slot()
            next_attempt = uuid4()
            await case.admin.execute(
                "INSERT INTO external_boot_recovery_attempts "
                "(activation_id,attempt_number,attempt_id,authority_generation,recovery_basis, "
                "state,recovery_readiness_deadline) SELECT activation_id,attempt_number+1,%s, "
                "authority_generation,recovery_basis,state,recovery_readiness_deadline "
                "FROM external_boot_recovery_attempts WHERE attempt_id=%s",
                (next_attempt, case.seeded.attempt_id),
            )
            await case.admin.execute(
                "UPDATE external_boot_activations SET current_attempt_id=%s WHERE id=%s",
                (next_attempt, case.seeded.vehicle.activation_id),
            )
            await case.finite_slot()

    asyncio.run(run())


def test_malformed_new_source_does_not_displace_a_valid_origin(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        async with _case(migrated_url, authority_role_dsns) as case:
            job_id = await case.finite_slot()
            malformed = await case.copy_job(
                case.seeded.job_id,
                key=f"external-boot-request:{uuid4()}",
                operation_identity=f"request:{uuid4()}",
            )
            await case.admin.execute(
                "UPDATE jobs SET payload=jsonb_set(payload, "
                "'{external_boot_authority_v1,purpose}', %s) WHERE id=%s",
                (Jsonb("unsupported"), malformed),
            )
            assert await case.repair() == 0
            assert await case.ids() == {job_id}

    asyncio.run(run())

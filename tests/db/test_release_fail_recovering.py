"""A release attempt that fails while recovering commits, and the next attempt adopts (#3396)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job
from kdive.jobs import queue
from kdive.jobs.models import ExternalBootAuthorityFailureV1
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from tests.db.release_phase_support import (
    SourceAuthorityAdapter,
    begin_release_recovery,
    release_takeover,
)
from tests.jobs.handlers.external_boot.seeding import (
    AUTHORITY_INSTANCE,
    SeededCase,
    seed_case,
)
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


class _Lane:
    """One release job on a seeded activation, driven attempt by attempt through real SQL."""

    def __init__(self, migrated_url: str, dsns: Callable[[str], str], tmp_path: Path) -> None:
        self.migrated_url = migrated_url
        self.dsns = dsns
        self.service = ExternalBootAuthorityService(
            repository=DatabaseAuthorityRepository(self._authority_connection),
            journal_factory=lambda system_id: FileAuthorityJournal(
                tmp_path, f"{system_id}.journal"
            ),
            adapter=SourceAuthorityAdapter(),
        )
        self.admin: AsyncConnection
        self.case: SeededCase
        self.incarnation = ""
        self.credential = ""

    @asynccontextmanager
    async def _authority_connection(self) -> AsyncIterator[AsyncConnection]:
        connection = await psycopg.AsyncConnection.connect(
            self.dsns("kdive_provider_authority"), autocommit=True
        )
        try:
            yield connection
        finally:
            await connection.close()

    @asynccontextmanager
    async def worker(self) -> AsyncIterator[AsyncConnection]:
        async with await psycopg.AsyncConnection.connect(
            self.dsns("kdive_worker"), autocommit=True
        ) as connection:
            yield connection

    async def seed(self, admin: AsyncConnection, *, max_attempts: int = 3) -> None:
        self.admin = admin
        self.case = await seed_case(
            admin,
            build_vehicle(),
            purpose="release",
            operation="release",
            activation_state="active",
            with_reservation=True,
        )
        self.incarnation = self.case.worker_incarnation
        self.credential = self.case.credential
        await admin.execute(
            "UPDATE jobs SET max_attempts = %s WHERE id = %s", (max_attempts, self.case.job_id)
        )

    async def allocate(self, attempt: int) -> tuple[str, dict[str, Any]]:
        async with self.worker() as conn, conn.cursor(row_factory=dict_row) as cur:
            case = self.case
            await cur.execute(
                "SELECT * FROM allocate_external_boot_authority("
                "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    self.credential,
                    case.job_id,
                    attempt,
                    case.vehicle.activation_id,
                    case.vehicle.run_id,
                    case.vehicle.system_id,
                    case.vehicle.plan_identity,
                    "release",
                    "local-libvirt",
                    AUTHORITY_INSTANCE,
                    case.marker["operation_identity"],
                ),
            )
            row = await cur.fetchone()
        assert row is not None
        return str(row["status"]), dict(row)

    async def acknowledge(self, authority: dict[str, Any]) -> None:
        await self.service.acknowledge_takeover(
            AuthenticatedPeer(self.incarnation), release_takeover(self.case, authority)
        )

    async def begin(self, attempt: int, authority: dict[str, Any]) -> str:
        async with self.worker() as conn:
            return await begin_release_recovery(
                conn,
                credential=self.credential,
                job_id=self.case.job_id,
                attempt=attempt,
                authority_id=authority["authority_id"],
                generation=authority["generation"],
                attempt_id=uuid4(),
            )

    async def fail(self, authority: dict[str, Any]) -> Job | queue.ExternalBootCommitStatus:
        case = self.case
        head = await (
            await self.admin.execute(
                "SELECT sequence, digest FROM external_boot_authority_journal_heads "
                "WHERE system_id = %s",
                (case.vehicle.system_id,),
            )
        ).fetchone()
        assert head is not None
        result = ExternalBootAuthorityFailureV1.model_validate(
            {
                "schema": "external-boot-authority-result-v1",
                "authority_id": authority["authority_id"],
                "generation": authority["generation"],
                "activation_id": case.vehicle.activation_id,
                "run_id": case.vehicle.run_id,
                "system_id": case.vehicle.system_id,
                "plan_identity": case.vehicle.plan_identity,
                "purpose": "release",
                "provider_kind": "local-libvirt",
                "authority_instance": AUTHORITY_INSTANCE,
                "admitted_operation": "release",
                "operation_identity": case.marker["operation_identity"],
                "operation_digest": authority["operation_digest"],
                "journal_sequence": head[0],
                "journal_digest": head[1],
                "result": {
                    "schema": "external-boot-authority-result-v1",
                    "operation": "fail",
                    "error_category": "infrastructure_failure",
                    "failure_context": {"phase": "commit"},
                    "terminal": False,
                },
            }
        )
        async with self.worker() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT * FROM jobs WHERE id = %s", (case.job_id,))
                job = Job.model_validate(await cur.fetchone())
            return await queue.fail_external_boot(
                conn, job, result, incarnation_credential=SecretStr(self.credential)
            )

    async def reclaim(self) -> int:
        """Claim the requeued job as a new incarnation, as the next attempt's worker does."""
        self.incarnation = f"docker:release-retry-{uuid4()}"
        self.credential = f"release-retry-credential-{uuid4()}"
        await self.admin.execute(
            "INSERT INTO worker_incarnations (incarnation, authority_kind, authority_binding, "
            "credential_hash, fence_protocol) VALUES "
            "(%s, 'docker', '{}'::jsonb, sha256(convert_to(%s, 'UTF8')), 4)",
            (self.incarnation, self.credential),
        )
        async with self.worker() as conn:
            claimed = await queue.dequeue(
                conn, self.incarnation, incarnation_credential=SecretStr(self.credential)
            )
        assert claimed is not None and claimed.id == self.case.job_id
        return claimed.attempt

    async def states(self) -> dict[str, Any]:
        activation = self.case.vehicle.activation_id
        authorities = await (
            await self.admin.execute(
                "SELECT generation, state FROM external_boot_authorities "
                "WHERE activation_id = %s ORDER BY generation",
                (activation,),
            )
        ).fetchall()
        attempts = await (
            await self.admin.execute(
                "SELECT attempt_number, authority_generation, state "
                "FROM external_boot_recovery_attempts WHERE activation_id = %s "
                "ORDER BY attempt_number",
                (activation,),
            )
        ).fetchall()
        row = await (
            await self.admin.execute(
                "SELECT a.state, j.state, j.attempt FROM external_boot_activations AS a, "
                "jobs AS j WHERE a.id = %s AND j.id = %s",
                (activation, self.case.job_id),
            )
        ).fetchone()
        assert row is not None
        return {
            "authorities": authorities,
            "attempts": attempts,
            "activation": row[0],
            "job": row[1],
            "job_attempt": row[2],
        }


def _drive(
    migrated_url: str,
    dsns: Callable[[str], str],
    tmp_path: Path,
    body: Callable[[_Lane], Any],
    *,
    max_attempts: int = 3,
) -> None:
    async def run() -> None:
        lane = _Lane(migrated_url, dsns, tmp_path)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as admin:
            await lane.seed(admin, max_attempts=max_attempts)
            await body(lane)

    asyncio.run(run())


async def _first_attempt_begun(lane: _Lane) -> dict[str, Any]:
    first = (await lane.allocate(1))[1]
    await lane.acknowledge(first)
    assert await lane.begin(1, first) == "applied"
    return first


def test_a_failed_attempt_commits_and_the_retry_adopts_its_recovery(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def body(lane: _Lane) -> None:
        first = await _first_attempt_begun(lane)
        failed = await lane.fail(first)
        assert isinstance(failed, Job) and failed.state.value == "queued"
        assert await lane.states() == {
            "authorities": [(first["generation"], "retired")],
            "attempts": [(1, first["generation"], "recovering")],
            "activation": "recovering",
            "job": "queued",
            "job_attempt": 1,
        }
        assert await lane.reclaim() == 2
        status, second = await lane.allocate(2)
        assert status == "allocated"
        await lane.acknowledge(second)
        assert await lane.begin(2, second) == "applied"
        states = await lane.states()
        assert states["attempts"] == [
            (1, first["generation"], "recovering"),
            (2, second["generation"], "recovering"),
        ]
        assert states["authorities"] == [
            (first["generation"], "retired"),
            (second["generation"], "current"),
        ]

    _drive(migrated_url, authority_role_dsns, tmp_path, body)


def test_two_consecutive_failed_attempts_each_adopt_the_previous_recovery(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def body(lane: _Lane) -> None:
        first = await _first_attempt_begun(lane)
        assert isinstance(await lane.fail(first), Job)
        await lane.reclaim()
        second = (await lane.allocate(2))[1]
        await lane.acknowledge(second)
        assert await lane.begin(2, second) == "applied"
        assert isinstance(await lane.fail(second), Job)
        assert await lane.reclaim() == 3
        status, third = await lane.allocate(3)
        assert status == "allocated"
        await lane.acknowledge(third)
        assert await lane.begin(3, third) == "applied"

    _drive(migrated_url, authority_role_dsns, tmp_path, body)


def test_a_failure_of_an_authority_that_never_began_the_recovery_is_refused(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    """The recovering attempt belongs to the first authority, so a later root cannot fail it."""

    async def body(lane: _Lane) -> None:
        first = await _first_attempt_begun(lane)
        assert isinstance(await lane.fail(first), Job)
        await lane.reclaim()
        second = (await lane.allocate(2))[1]
        await lane.acknowledge(second)
        before = await lane.states()
        assert await lane.fail(second) is queue.ExternalBootCommitStatus.SUPERSEDED
        assert await lane.states() == before

    _drive(migrated_url, authority_role_dsns, tmp_path, body)


def test_another_jobs_retired_authority_is_not_adopted_by_allocation(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def body(lane: _Lane) -> None:
        first = await _first_attempt_begun(lane)
        assert isinstance(await lane.fail(first), Job)
        other = uuid4()
        await lane.admin.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) "
            "SELECT %s, kind, payload, 'running', 1, max_attempts, %s, "
            "now() + interval '5 minutes', now(), authorizing, %s FROM jobs WHERE id = %s",
            (other, lane.incarnation, f"external-boot-{other}", lane.case.job_id),
        )
        async with lane.worker() as conn, conn.cursor(row_factory=dict_row) as cur:
            case = lane.case
            await cur.execute(
                "SELECT status FROM allocate_external_boot_authority("
                "sha256(convert_to(%s,'UTF8')),%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    lane.credential,
                    other,
                    1,
                    case.vehicle.activation_id,
                    case.vehicle.run_id,
                    case.vehicle.system_id,
                    case.vehicle.plan_identity,
                    "release",
                    "local-libvirt",
                    AUTHORITY_INSTANCE,
                    case.marker["operation_identity"],
                ),
            )
            row = await cur.fetchone()
        assert row is not None and row["status"] == "superseded"

    _drive(migrated_url, authority_role_dsns, tmp_path, body)


def test_the_final_failed_attempt_ends_the_job_and_leaves_the_activation_recovering(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def body(lane: _Lane) -> None:
        first = await _first_attempt_begun(lane)
        failed = await lane.fail(first)
        assert isinstance(failed, Job) and failed.state.value == "failed"
        assert await lane.states() == {
            "authorities": [(first["generation"], "retired")],
            "attempts": [(1, first["generation"], "recovering")],
            "activation": "recovering",
            "job": "failed",
            "job_attempt": 1,
        }

    _drive(migrated_url, authority_role_dsns, tmp_path, body, max_attempts=1)

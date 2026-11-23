"""Ordinary teardown recovery consumes one durable request allowance (ADR-0843)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import DEFAULT_JOB_DISPATCH_LANE, Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.systems import teardown_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import PayloadValidationError, TeardownPayload, dump_payload
from kdive.jobs.worker import Worker
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.reconciler import loop
from kdive.reconciler.repairs.systems import (
    TeardownWarningHistory,
    repair_stalled_tearing_down_systems,
)
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.worker_lifecycle.authority_store import CURRENT_WORKER_FENCE_PROTOCOL
from tests.jobs.test_worker import (
    _registered_worker,  # noqa: PLC2701 - reuse credential-bound worker registration
)
from tests.mcp.systems_support import FakeProvisioning, ctx, provider_resolver
from tests.reconcile_helpers import make_reconcile_config
from tests.reconciler.conftest import FakeReaper, connect, seed_system
from tests.support.object_store import INERT_OBJECT_STORE

_MARKER = "automatic_teardown_replay_v1"
_REASON = "teardown_recovery_reason"
_AVAILABLE = "teardown_automatic_replay_available"
_EXHAUSTED = "teardown_automatic_replay_exhausted"
_TERMINAL = "teardown_terminal_failure"


class FailingProvisioner(FakeProvisioning):
    """Inject failure at the provider boundary, leaving all core execution real."""

    def __init__(
        self,
        category: ErrorCategory = ErrorCategory.CONTROL_FAILURE,
        *,
        terminal_on_call: int | None = None,
    ) -> None:
        super().__init__()
        self.category = category
        self.terminal_on_call = terminal_on_call
        self.available = False

    def teardown(self, domain_name: str) -> None:
        super().teardown(domain_name)
        if self.available:
            return
        raise CategorizedError(
            "provider teardown unavailable",
            category=self.category,
            terminal=len(self.torn_down) == self.terminal_on_call,
        )


@dataclass
class Scenario:
    pool: AsyncConnectionPool
    worker: Worker
    system_id: UUID
    provider: FailingProvisioner

    async def current(self) -> Job:
        async with self.pool.connection() as conn:
            job = await queue.get_by_dedup_key(conn, f"{self.system_id}:teardown")
        assert job is not None
        return job

    async def fail_current(self) -> Job:
        job = await self.current()
        for _ in range(job.max_attempts):
            if job.state is JobState.FAILED:
                break
            assert await self.worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is not None
            job = await self.current()
        assert job.state is JobState.FAILED
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute("SELECT state FROM systems WHERE id=%s", (self.system_id,))
            ).fetchone()
        assert row == ("tearing_down",)
        return job

    async def repair(self) -> int:
        async with self.pool.connection() as conn:
            return await repair_stalled_tearing_down_systems(conn)


@asynccontextmanager
async def scenario(
    url: str, *, provider: FailingProvisioner | None = None, max_attempts: int = 1
) -> AsyncGenerator[Scenario]:
    async with await connect(url) as conn:
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
    failing = provider if provider is not None else FailingProvisioner()
    registry = HandlerRegistry()
    registry.register(
        JobKind.TEARDOWN,
        partial(
            teardown_handler,
            resolver=provider_resolver(provisioner=failing),
            artifact_store=INERT_OBJECT_STORE,
        ),
    )
    async with AsyncConnectionPool(url, min_size=2, max_size=8) as pool:
        response = await teardown_system(
            pool, ctx(Role.ADMIN), str(system_id), idempotency_key="initial-request"
        )
        assert response.status == "queued"
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE jobs SET max_attempts=%s WHERE id=%s", (max_attempts, response.object_id)
            )
        worker = await _registered_worker(pool, registry, worker_id="bounded-replay-worker")
        yield Scenario(pool, worker, system_id, failing)


def test_persistent_provider_failure_stops_after_one_automatic_replay(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            initial = await case.fail_current()
            assert initial.failure_context[_REASON] == _AVAILABLE
            assert await case.repair() == 1
            recycled = await case.current()
            assert recycled.id == initial.id
            assert recycled.payload[_MARKER] == "consumed"
            assert recycled.attempt == 0 and recycled.authorizing == initial.authorizing
            failed = await case.fail_current()
            assert failed.failure_context[_REASON] == _EXHAUSTED
            assert ToolResponse.from_job(failed).data[_REASON] == _EXHAUSTED
            assert [await case.repair() for _ in range(3)] == [0, 0, 0]
            assert await case.current() == failed
            assert len(case.provider.torn_down) == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    ("category", "terminal_on_call", "max_attempts", "expected_calls"),
    [
        (ErrorCategory.CONFIGURATION_ERROR, None, 3, 1),
        (ErrorCategory.CONTROL_FAILURE, 1, 3, 1),
        (ErrorCategory.CONTROL_FAILURE, 3, 3, 3),
    ],
    ids=["permanent-category", "terminal-escalation", "terminal-on-exhaustion"],
)
def test_actual_terminal_decision_prevents_automatic_replay(
    migrated_url: str,
    category: ErrorCategory,
    terminal_on_call: int | None,
    max_attempts: int,
    expected_calls: int,
) -> None:
    async def run() -> None:
        provider = FailingProvisioner(category, terminal_on_call=terminal_on_call)
        async with scenario(migrated_url, provider=provider, max_attempts=max_attempts) as case:
            failed = await case.fail_current()
            assert failed.error_category is category
            assert failed.failure_context[_REASON] == _TERMINAL
            assert await case.repair() == 0
            assert await case.current() == failed
            assert len(provider.torn_down) == expected_calls

    asyncio.run(run())


def test_missing_job_consumes_its_only_automatic_admission(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            original = await case.current()
            async with case.pool.connection() as conn:
                await conn.execute("DELETE FROM jobs WHERE id=%s", (original.id,))
            assert await case.repair() == 1
            admitted = await case.current()
            assert admitted.id != original.id
            assert admitted.payload[_MARKER] == "consumed"
            failed = await case.fail_current()
            assert failed.failure_context[_REASON] == _EXHAUSTED
            assert await case.repair() == 0
            assert await case.current() == failed

    asyncio.run(run())


def test_fresh_reconciler_and_worker_keep_the_consumed_allowance(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            await case.fail_current()
            runner = loop.Reconciler(case.pool, FakeReaper(), config=make_reconcile_config())
            assert (await runner.run_once()).repair_counts["stalled_tearing_down_systems"] == 1
            registry = HandlerRegistry()
            registry.register(
                JobKind.TEARDOWN,
                partial(
                    teardown_handler,
                    resolver=provider_resolver(provisioner=case.provider),
                    artifact_store=INERT_OBJECT_STORE,
                ),
            )
            async with AsyncConnectionPool(migrated_url, min_size=2, max_size=8) as fresh_pool:
                credential = SecretStr("replacement-worker-test-credential")
                async with fresh_pool.connection() as conn:
                    await conn.execute(
                        "INSERT INTO worker_incarnations (incarnation, authority_kind, "
                        "authority_binding, fence_protocol, credential_hash) VALUES "
                        "('replacement-worker', 'local', '{}'::jsonb, %s, "
                        "sha256(convert_to(%s, 'UTF8')))",
                        (CURRENT_WORKER_FENCE_PROTOCOL, credential.get_secret_value()),
                    )
                case.worker = Worker(
                    fresh_pool,
                    registry,
                    worker_id="replacement-worker",
                    incarnation_credential=credential,
                    secret_registry=SecretRegistry(),
                )
                failed = await case.fail_current()
                assert failed.failure_context[_REASON] == _EXHAUSTED
                fresh = loop.Reconciler(fresh_pool, FakeReaper(), config=make_reconcile_config())
                assert (await fresh.run_once()).repair_counts["stalled_tearing_down_systems"] == 0
            assert await case.current() == failed

    asyncio.run(run())


async def _wait_for_locks(conn: psycopg.AsyncConnection, pids: list[int]) -> None:
    async with asyncio.timeout(10):
        while True:
            row = await (
                await conn.execute(
                    "SELECT count(DISTINCT pid) FROM pg_locks WHERE pid=ANY(%s) AND NOT granted",
                    (pids,),
                )
            ).fetchone()
            if row == (len(pids),):
                return


def test_concurrent_repairs_consume_the_allowance_exactly_once(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            await case.fail_current()
            tasks: list[asyncio.Task[int]] = []
            async with (
                case.pool.connection() as first,
                case.pool.connection() as second,
                await connect(migrated_url) as blocker,
            ):
                try:
                    async with (
                        blocker.transaction(),
                        advisory_xact_lock(blocker, LockScope.SYSTEM, case.system_id),
                    ):
                        tasks = [
                            asyncio.create_task(repair_stalled_tearing_down_systems(conn))
                            for conn in (first, second)
                        ]
                        await _wait_for_locks(
                            blocker, [first.info.backend_pid, second.info.backend_pid]
                        )
                    assert sorted(await asyncio.gather(*tasks)) == [0, 1]
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            current = await case.current()
            assert current.state is JobState.QUEUED
            assert current.payload[_MARKER] == "consumed"
            await case.fail_current()
            assert await case.repair() == 0

    asyncio.run(run())


def test_outer_rollback_restores_both_failed_job_and_unused_allowance(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            before = await case.fail_current()
            async with case.pool.connection() as conn:
                with pytest.raises(RuntimeError, match="abort admission"):
                    async with conn.transaction():
                        assert await repair_stalled_tearing_down_systems(conn) == 1
                        admitted = await queue.get_by_dedup_key(conn, f"{case.system_id}:teardown")
                        assert admitted is not None and admitted.payload[_MARKER] == "consumed"
                        raise RuntimeError("abort admission")
                assert conn.info.transaction_status == TransactionStatus.IDLE
            assert await case.current() == before
            assert await case.repair() == 1
            assert (await case.current()).payload[_MARKER] == "consumed"

    asyncio.run(run())


def test_cancelled_queue_write_retains_the_unused_allowance(migrated_url: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            before = await case.fail_current()
            task: asyncio.Task[int] | None = None
            async with case.pool.connection() as conn, await connect(migrated_url) as blocker:
                try:
                    async with blocker.transaction():
                        await blocker.execute(
                            "SELECT id FROM jobs WHERE id=%s FOR UPDATE", (before.id,)
                        )
                        task = asyncio.create_task(repair_stalled_tearing_down_systems(conn))
                        await _wait_for_locks(blocker, [conn.info.backend_pid])
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(task, timeout=10)
                finally:
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                assert conn.info.transaction_status == TransactionStatus.IDLE
            assert await case.current() == before
            assert await case.repair() == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "variant",
    [
        "legacy",
        "terminal-evidence",
        "consumed",
        "permanent-category",
        "not-exhausted",
        "succeeded",
        "canceled",
        "wrong-kind",
        "wrong-system",
        "invalid-system",
        "invalid-marker",
        "authority_system_v1",
        "external_boot_authority_v1",
    ],
)
def test_unproven_or_owned_work_is_unchanged(migrated_url: str, variant: str) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            failed = await case.fail_current()
            async with case.pool.connection() as conn:
                if variant in {"legacy", "terminal-evidence"}:
                    context = {} if variant == "legacy" else {_REASON: _TERMINAL}
                    await conn.execute(
                        "UPDATE jobs SET failure_context=%s WHERE id=%s",
                        (Jsonb(context), failed.id),
                    )
                elif variant in {"consumed", "invalid-marker", "wrong-system", "invalid-system"}:
                    payload = dict(failed.payload)
                    if variant in {"consumed", "invalid-marker"}:
                        payload[_MARKER] = "consumed" if variant == "consumed" else "unused"
                    else:
                        payload["system_id"] = str(uuid4()) if variant == "wrong-system" else "bad"
                    await conn.execute(
                        "UPDATE jobs SET payload=%s WHERE id=%s", (Jsonb(payload), failed.id)
                    )
                elif variant in {"authority_system_v1", "external_boot_authority_v1"}:
                    await conn.execute(
                        "UPDATE jobs SET payload=payload || %s WHERE id=%s",
                        (Jsonb({variant: {"marker": 1}}), failed.id),
                    )
                elif variant == "permanent-category":
                    await conn.execute(
                        "UPDATE jobs SET error_category='configuration_error' WHERE id=%s",
                        (failed.id,),
                    )
                elif variant == "not-exhausted":
                    await conn.execute("UPDATE jobs SET attempt=0 WHERE id=%s", (failed.id,))
                elif variant == "wrong-kind":
                    await conn.execute("UPDATE jobs SET kind='power' WHERE id=%s", (failed.id,))
                else:
                    await conn.execute("UPDATE jobs SET state=%s WHERE id=%s", (variant, failed.id))
            before = await case.current()
            history = TeardownWarningHistory()
            async with case.pool.connection() as conn:
                for _ in range(2):
                    assert await repair_stalled_tearing_down_systems(conn, warnings=history) == 0
            assert await case.current() == before

    asyncio.run(run())


@pytest.mark.parametrize("new_key", [None, "fresh-request"])
def test_only_actual_explicit_recycle_resets_allowance_and_audits_requester(
    migrated_url: str, new_key: str | None
) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            initial = await case.fail_current()
            assert await case.repair() == 1
            live = await case.current()
            replayed_live = await teardown_system(
                case.pool, ctx(Role.ADMIN), str(case.system_id), idempotency_key="live-replay"
            )
            assert replayed_live.object_id == str(initial.id)
            assert await case.current() == live
            failed = await case.fail_current()
            replayed_key = await teardown_system(
                case.pool, ctx(Role.ADMIN), str(case.system_id), idempotency_key="initial-request"
            )
            assert replayed_key.object_id == str(initial.id)
            assert await case.current() == failed
            requester = replace(ctx(Role.ADMIN), principal="second-requester")
            retried = await teardown_system(
                case.pool, requester, str(case.system_id), idempotency_key=new_key
            )
            assert retried.status == "queued" and retried.object_id == str(initial.id)
            fresh = await case.current()
            assert _MARKER not in fresh.payload and fresh.attempt == 0
            assert fresh.authorizing == initial.authorizing
            async with case.pool.connection() as conn:
                rows = await (
                    await conn.execute(
                        "SELECT principal FROM audit_log WHERE object_id=%s "
                        "AND transition='teardown:recycled'",
                        (case.system_id,),
                    )
                ).fetchall()
            assert rows == [("second-requester",)]
            assert (await case.fail_current()).failure_context[_REASON] == _AVAILABLE
            assert await case.repair() == 1
            assert (await case.fail_current()).failure_context[_REASON] == _EXHAUSTED
            assert await case.repair() == 0

    asyncio.run(run())


@pytest.mark.parametrize("invalid", [True, False, 0, 1, "unused", {}, []])
def test_consumption_marker_is_closed_and_absent_payload_bytes_are_preserved(
    invalid: object,
) -> None:
    system_id = str(uuid4())
    assert dump_payload(JobKind.TEARDOWN, TeardownPayload(system_id=system_id)) == {
        "system_id": system_id
    }
    with pytest.raises(PayloadValidationError, match=_MARKER):
        dump_payload(JobKind.TEARDOWN, {"system_id": system_id, _MARKER: invalid})


@pytest.mark.parametrize("explicit", [False, True], ids=["automatic", "explicit-after-exhaustion"])
def test_recovery_finishes_actual_provider_and_core_cleanup(
    migrated_url: str, explicit: bool
) -> None:
    async def run() -> None:
        async with scenario(migrated_url) as case:
            await case.fail_current()
            assert await case.repair() == 1
            if explicit:
                await case.fail_current()
                assert await case.repair() == 0
                response = await teardown_system(case.pool, ctx(Role.ADMIN), str(case.system_id))
                assert response.status == "queued"
            case.provider.available = True
            assert await case.worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is not None
            complete = await case.current()
            assert complete.state is JobState.SUCCEEDED
            assert complete.failure_context == {}
            assert "allocations.release" in ToolResponse.from_job(complete).suggested_next_actions
            async with case.pool.connection() as conn:
                row = await (
                    await conn.execute("SELECT state FROM systems WHERE id=%s", (case.system_id,))
                ).fetchone()
            assert row == ("torn_down",)
            assert await case.repair() == 0

    asyncio.run(run())


def test_worker_retries_retain_consumption_but_clear_intermediate_failure_context(
    migrated_url: str,
) -> None:
    async def run() -> None:
        async with scenario(migrated_url, max_attempts=3) as case:
            for attempt in (1, 2):
                assert await case.worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is not None
                queued = await case.current()
                assert queued.state is JobState.QUEUED and queued.attempt == attempt
                assert queued.failure_context == {} and _MARKER not in queued.payload
            assert (await case.fail_current()).failure_context[_REASON] == _AVAILABLE
            assert await case.repair() == 1
            for attempt in (1, 2):
                assert await case.worker.run_once(DEFAULT_JOB_DISPATCH_LANE) is not None
                queued = await case.current()
                assert queued.state is JobState.QUEUED and queued.attempt == attempt
                assert queued.failure_context == {} and queued.payload[_MARKER] == "consumed"
            assert (await case.fail_current()).failure_context[_REASON] == _EXHAUSTED
            assert await case.repair() == 0

    asyncio.run(run())

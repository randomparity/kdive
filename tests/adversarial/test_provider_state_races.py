"""Adversarial: provision and teardown racing on one System must not leak a domain.

`provision_handler` runs the slow `provision()` **without** the SYSTEM advisory lock,
then re-reads the state under the lock and *compensates* — reaping the domain it created
— if a concurrent teardown drove the System terminal first (systems.py, ADR-0025 §8).
`teardown_handler` commits `torn_down` under the lock, then destroys unlocked.

The invariant under attack: for every interleaving of a concurrent provision + teardown
of the same System, the System ends `torn_down` and **no provisioned domain is left
live** (every domain `provision()` created was `teardown()`-reaped). The existing suite
simulates the race by flipping DB state inside the fake provider on one connection; this
test runs the two handlers as genuinely concurrent tasks on separate pooled connections.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, try_advisory_xact_lock
from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory, retryable_category
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.handlers.control import control as control_plane
from kdive.jobs.payloads import PowerPayload, SystemPayload, TeardownPayload
from kdive.jobs.provider_context import take_provider_kind
from tests.adversarial.conftest import seed_allocation, seed_resource
from tests.mcp.systems_support import provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

_DT = datetime(2026, 1, 1, tzinfo=UTC)

_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 2,
    "memory_mb": 2048,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {
                "kind": "local",
                "path": "/var/lib/kdive/rootfs/fedora-40.qcow2",
            },
            "crashkernel": "256M",
        }
    },
}


class _TrackingProvisioner:
    """A fake provider that models the host's live-domain set.

    ``provision`` adds the domain; ``teardown`` removes it (idempotent discard);
    ``reprovision`` re-applies in place. A non-empty ``live`` after the race means a
    domain leaked.
    """

    def __init__(self) -> None:
        self.live: set[str] = set()
        self.provisioned: list[UUID] = []
        self.reprovisioned: list[UUID] = []
        self.torn_down: list[str] = []

    def provision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: Any = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        del overlay_customizers, bootstrap_pubkey, job_id
        name = f"kdive-{system_id}"
        self.provisioned.append(system_id)
        self.live.add(name)
        return name

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)
        self.live.discard(domain_name)

    def read_resolved_cpu(self, system_id: object) -> None:
        del system_id
        return None

    def reprovision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: Any = (),
        bootstrap_pubkey: str | None = None,
        job_id: UUID | None = None,
    ) -> str:
        del overlay_customizers, bootstrap_pubkey, job_id
        name = f"kdive-{system_id}"
        self.reprovisioned.append(system_id)
        self.live.add(name)
        return name


class _TeardownBeforeProvisionCommitProvisioner(_TrackingProvisioner):
    """Hold the first provider reap while a concurrent provision attempts admission."""

    def __init__(self) -> None:
        super().__init__()
        self.teardown_started = threading.Event()
        self.allow_teardown_commit = threading.Event()
        self._teardown_calls = 0

    def teardown(self, domain_name: str) -> None:
        self.torn_down.append(domain_name)
        self.live.discard(domain_name)
        self._teardown_calls += 1
        if self._teardown_calls == 1:
            self.teardown_started.set()
            assert self.allow_teardown_commit.wait(timeout=2), "test did not release teardown"


class _RecordingController:
    """Records control ops; force_crash/power never raise (the live host would)."""

    def __init__(self) -> None:
        self.crashed: list[str] = []
        self.powered: list[tuple[str, PowerAction]] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.powered.append((domain_name, action))

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)


class _RaisingCrashController(_RecordingController):
    """force_crash records the attempt then raises like a degraded provider (transport error)."""

    def force_crash(self, domain_name: str) -> None:
        self.crashed.append(domain_name)
        raise CategorizedError("inject-nmi failed", category=ErrorCategory.INFRASTRUCTURE_FAILURE)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=2, max_size=4, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(
    pool: AsyncConnectionPool, state: SystemState, *, domain_name: str | None = None
) -> str:
    async with pool.connection() as conn:
        resource = await seed_resource(conn, cap=4)
        allocation = await seed_allocation(conn, resource.id, AllocationState.ACTIVE)
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=state,
                provisioning_profile=_PROFILE,
                domain_name=domain_name,
            ),
        )
    return str(system.id)


async def _enqueue(pool: AsyncConnectionPool, kind: JobKind, system_id: str, dedup: str) -> Job:
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            kind,
            TeardownPayload(system_id=system_id)
            if kind is JobKind.TEARDOWN
            else SystemPayload(system_id=system_id),
            {"principal": "alice", "agent_session": "s", "project": "proj"},
            dedup,
        )


async def _system_state(pool: AsyncConnectionPool, system_id: str) -> str:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    assert row is not None
    return row["state"]


async def _set_state(pool: AsyncConnectionPool, system_id: str, state: str) -> None:
    """Direct state UPDATE bypassing can_transition — scaffolds a mid-window state."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("UPDATE systems SET state = %s WHERE id = %s", (state, system_id))


async def _enqueue_power(
    pool: AsyncConnectionPool,
    system_id: str,
    dedup: str,
    action: PowerAction = PowerAction.RESET,
) -> Job:
    """Enqueue a POWER job with a valid PowerPayload (power_handler loads PowerPayload)."""
    async with pool.connection() as conn:
        return await queue.enqueue(
            conn,
            JobKind.POWER,
            PowerPayload(system_id=system_id, action=action),
            {"principal": "alice", "agent_session": "s", "project": "proj"},
            dedup,
        )


async def _race_once(pool: AsyncConnectionPool, *, provision_first: bool) -> tuple[str, set[str]]:
    system_id = await _seed_system(pool, SystemState.PROVISIONING)
    prov = _TrackingProvisioner()
    resolver = provider_resolver(provisioner=prov)
    pjob = await _enqueue(pool, JobKind.PROVISION, system_id, f"{system_id}:provision")
    tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

    async def run_provision() -> None:
        async with pool.connection() as conn:
            await conn.set_autocommit(True)  # the worker runs handlers in autocommit
            await systems_handlers.provision_handler(conn, pjob, resolver=resolver)

    async def run_teardown() -> None:
        async with pool.connection() as conn:
            await conn.set_autocommit(True)  # the worker runs handlers in autocommit
            await systems_handlers.teardown_handler(
                conn, tjob, resolver=resolver, artifact_store=INERT_OBJECT_STORE
            )

    tasks = (
        [run_provision(), run_teardown()] if provision_first else [run_teardown(), run_provision()]
    )
    await asyncio.gather(*tasks)
    return await _system_state(pool, system_id), prov.live


def test_provision_failure_marks_system_failed_and_raises_terminally(migrated_url: str) -> None:
    # A provision failure drives the System to a terminal `failed` state, so the job must NOT be
    # retryable: a retry re-enters a terminal System and would report success, masking the failure
    # (job `succeeded` while the System is `failed`). The handler re-raises the categorized error
    # marked `terminal` so the worker dead-letters at once and the real reason reaches the agent.
    class _FailingProvisioner(_TrackingProvisioner):
        def provision(
            self,
            system_id: UUID,
            profile: Any,
            *,
            overlay_customizers: Any = (),
            bootstrap_pubkey: str | None = None,
            job_id: UUID | None = None,
        ) -> str:
            del overlay_customizers, bootstrap_pubkey, job_id
            raise CategorizedError(
                "base image volume not staged",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.PROVISIONING)
            resolver = provider_resolver(provisioner=_FailingProvisioner())
            pjob = await _enqueue(pool, JobKind.PROVISION, system_id, f"{system_id}:provision")
            async with pool.connection() as conn:
                await conn.set_autocommit(True)  # the worker runs handlers in autocommit
                with pytest.raises(CategorizedError) as excinfo:
                    await systems_handlers.provision_handler(conn, pjob, resolver=resolver)
            assert excinfo.value.terminal is True
            assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert await _system_state(pool, system_id) == SystemState.FAILED.value

    asyncio.run(_run())


def test_concurrent_provision_teardown_never_leaks_a_domain(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):  # sample interleavings, both start orders
                state, live = await _race_once(pool, provision_first=(i % 2 == 0))
                assert state == "torn_down", (
                    f"iteration {i}: System ended {state!r}, want torn_down"
                )
                assert live == set(), f"iteration {i}: leaked domain(s): {live}"

    asyncio.run(_run())


def test_teardown_terminal_admission_fences_concurrent_provision(
    migrated_url: str,
) -> None:
    """The terminal state fences a provision that starts during provider teardown."""

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.PROVISIONING)
            prov = _TeardownBeforeProvisionCommitProvisioner()
            resolver = provider_resolver(provisioner=prov)
            pjob = await _enqueue(pool, JobKind.PROVISION, system_id, f"{system_id}:provision")
            tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

            async def run_provision() -> None:
                async with pool.connection() as conn:
                    await conn.set_autocommit(True)
                    await systems_handlers.provision_handler(conn, pjob, resolver=resolver)

            async def run_teardown() -> None:
                async with pool.connection() as conn:
                    await conn.set_autocommit(True)
                    await systems_handlers.teardown_handler(
                        conn, tjob, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                    )

            teardown = asyncio.create_task(run_teardown())
            started = await asyncio.wait_for(
                asyncio.to_thread(prov.teardown_started.wait), timeout=2
            )
            assert started
            await run_provision()
            prov.allow_teardown_commit.set()
            await teardown

            assert await _system_state(pool, system_id) == SystemState.TORN_DOWN.value
            assert prov.provisioned == []
            assert prov.live == set()

    asyncio.run(_run())


def test_concurrent_force_crash_and_teardown_end_torn_down_no_stale_nmi(migrated_url: str) -> None:
    # force_crash (ready->crashing->crashed) and teardown (->torn_down) both hold the SYSTEM lock
    # their whole transition. Whatever the order: the System ends torn_down (teardown is the
    # terminal sink), the domain is reaped, and the NMI fires at most once and never against a
    # System the lock already shows terminal (force_crash's terminal-state early return).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):
                system_id = await _seed_system(pool, SystemState.READY)
                prov = _TrackingProvisioner()
                prov.live.add(f"kdive-{system_id}")  # the live System's domain
                ctrl = _RecordingController()
                resolver = provider_resolver(provisioner=prov, controller=ctrl)
                cjob = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:crash")
                tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

                async def run_crash(
                    job: Job = cjob,
                    ctrl: _RecordingController = ctrl,
                    resolver=resolver,
                ) -> None:
                    del ctrl
                    async with pool.connection() as conn:
                        await control_plane.force_crash_handler(conn, job, resolver=resolver)

                async def run_teardown(
                    job: Job = tjob,
                    prov: _TrackingProvisioner = prov,
                    resolver=resolver,
                ) -> None:
                    del prov
                    async with pool.connection() as conn:
                        await systems_handlers.teardown_handler(
                            conn, job, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                        )

                order = [run_crash(), run_teardown()] if i % 2 else [run_teardown(), run_crash()]
                await asyncio.gather(*order)

                assert await _system_state(pool, system_id) == "torn_down"
                assert prov.live == set(), f"iteration {i}: leaked domain {prov.live}"
                assert len(ctrl.crashed) <= 1

    asyncio.run(_run())


def test_concurrent_double_teardown_is_idempotent(migrated_url: str) -> None:
    # A single teardown job double-dispatched (lease lapse -> two handler runs) must converge:
    # both runs succeed, the System ends torn_down exactly once, and the domain is reaped.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(10):
                system_id = await _seed_system(pool, SystemState.READY)
                prov = _TrackingProvisioner()
                prov.live.add(f"kdive-{system_id}")
                resolver = provider_resolver(provisioner=prov)
                job = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")

                async def run(
                    j: Job = job,
                    prov: _TrackingProvisioner = prov,
                    resolver=resolver,
                ) -> str | None:
                    del prov
                    async with pool.connection() as conn:
                        return await systems_handlers.teardown_handler(
                            conn, j, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                        )

                results = await asyncio.gather(run(), run())
                assert all(r == system_id for r in results)
                assert await _system_state(pool, system_id) == "torn_down"
                assert prov.live == set(), f"iteration {i}: leaked domain {prov.live}"
                # Exactly one terminal transition despite two teardown runs.
                async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT count(*) AS n FROM audit_log "
                        "WHERE object_id = %s AND transition = 'tearing_down->torn_down'",
                        (UUID(system_id),),
                    )
                    row = await cur.fetchone()
                assert row is not None and row["n"] == 1

    asyncio.run(_run())


class _RecordingSnapshotter:
    """Records ``delete_all`` so a test can prove teardown asked the provider for snapshots."""

    def __init__(self) -> None:
        self.deleted_all: list[str] = []

    def delete_all(self, domain_name: str) -> None:
        self.deleted_all.append(domain_name)


async def _tearing_down_audit_count(pool: AsyncConnectionPool, system_id: str) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT count(*) AS n FROM audit_log WHERE object_id = %s AND transition LIKE %s",
            (UUID(system_id), "%tearing_down%"),
        )
        row = await cur.fetchone()
    assert row is not None
    return row["n"]


def test_teardown_queued_behind_failed_provision_reclaims_and_stays_failed(
    migrated_url: str,
) -> None:
    # #2908: a teardown queued behind a provision that then fails meets a terminal `failed` System.
    # It must reclaim what the failed provision left on the host and succeed, without the illegal
    # failed -> tearing_down move that burned every attempt as infrastructure_failure.
    class _LeakingFailingProvisioner(_TrackingProvisioner):
        def provision(
            self,
            system_id: UUID,
            profile: Any,
            *,
            overlay_customizers: Any = (),
            bootstrap_pubkey: str | None = None,
            job_id: UUID | None = None,
        ) -> str:
            del profile, overlay_customizers, bootstrap_pubkey, job_id
            self.live.add(f"kdive-{system_id}")
            raise CategorizedError(
                "readiness marker never appeared", category=ErrorCategory.PROVISIONING_FAILURE
            )

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.PROVISIONING)
            prov = _LeakingFailingProvisioner()
            snapshotter = _RecordingSnapshotter()
            resolver = provider_resolver(provisioner=prov, snapshotter=snapshotter)
            pjob = await _enqueue(pool, JobKind.PROVISION, system_id, f"{system_id}:provision")
            tjob = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                with pytest.raises(CategorizedError):
                    await systems_handlers.provision_handler(conn, pjob, resolver=resolver)
                assert await _system_state(pool, system_id) == SystemState.FAILED.value

                result = await systems_handlers.teardown_handler(
                    conn, tjob, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                )

            assert result == system_id
            assert await _system_state(pool, system_id) == SystemState.FAILED.value
            assert prov.live == set()
            assert snapshotter.deleted_all == [f"kdive-{system_id}"]
            assert await _tearing_down_audit_count(pool, system_id) == 0

    asyncio.run(_run())


def test_failed_system_provider_teardown_fault_stays_retryable(migrated_url: str) -> None:
    # A provider fault while reclaiming a `failed` System surfaces as the provider's own retryable
    # error, and the next attempt asks the provider again and completes the reclaim.
    class _FlakyTeardownProvisioner(_TrackingProvisioner):
        def teardown(self, domain_name: str) -> None:
            if not self.torn_down:
                self.torn_down.append(domain_name)
                raise CategorizedError(
                    "virDomainDestroy timed out", category=ErrorCategory.INFRASTRUCTURE_FAILURE
                )
            super().teardown(domain_name)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.FAILED)
            domain = f"kdive-{system_id}"
            prov = _FlakyTeardownProvisioner()
            prov.live.add(domain)
            resolver = provider_resolver(provisioner=prov)
            job = await _enqueue(pool, JobKind.TEARDOWN, system_id, f"{system_id}:teardown")
            async with pool.connection() as conn:
                await conn.set_autocommit(True)
                with pytest.raises(CategorizedError) as excinfo:
                    await systems_handlers.teardown_handler(
                        conn, job, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                    )
                assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
                assert excinfo.value.terminal is False
                assert retryable_category(excinfo.value.category)
                assert await _system_state(pool, system_id) == SystemState.FAILED.value
                assert prov.live == {domain}

                result = await systems_handlers.teardown_handler(
                    conn, job, resolver=resolver, artifact_store=INERT_OBJECT_STORE
                )

            assert result == system_id
            assert prov.torn_down == [domain, domain]
            assert prov.live == set()
            assert await _system_state(pool, system_id) == SystemState.FAILED.value

    asyncio.run(_run())


def test_force_crash_drives_ready_crashing_crashed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(conn, job, resolver=resolver)
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value
            assert ctrl.crashed == ["kdive-x"]  # NMI fired exactly once
            # Non-skippable CI guard on the system-transition audit literal (also used by the
            # gated live_stack proof and the reconciler recovery):
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM audit_log "
                    "WHERE object_kind = 'systems' AND transition = 'crashing->crashed'"
                )
                row = await cur.fetchone()
            assert row is not None and row["n"] == 1

    asyncio.run(_run())


def test_force_crash_retry_after_crashing_finalizes_without_second_nmi(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            # Model a handler that marked CRASHING and fired the NMI but died before finalize:
            # set CRASHING directly (bypassing can_transition), then run the handler as a retry.
            await _set_state(pool, system_id, SystemState.CRASHING.value)
            async with pool.connection() as conn:
                await control_plane.force_crash_handler(conn, job, resolver=resolver)
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value
            assert ctrl.crashed == []  # finalize-only retry: NMI NOT re-fired

    asyncio.run(_run())


def test_force_crash_nmi_raise_propagates_and_leaves_crashing(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = _RaisingCrashController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash")
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await control_plane.force_crash_handler(conn, job, resolver=resolver)
            # AC4b: the raise is NON-terminal, so the worker requeues (not dead-letters) — and the
            # marker is left set (System stays CRASHING), NOT marked FAILED by the handler.
            assert excinfo.value.terminal is False
            assert await _system_state(pool, system_id) == SystemState.CRASHING.value

    asyncio.run(_run())


@pytest.mark.parametrize("action", [PowerAction.RESET, PowerAction.OFF])
def test_power_refused_on_crashing_no_physical_reset(
    migrated_url: str, action: PowerAction
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            await _set_state(pool, system_id, SystemState.CRASHING.value)
            ctrl = _RecordingController()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            pjob = await _enqueue_power(pool, system_id, f"{system_id}:power", action)
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as excinfo:
                    await control_plane.power_handler(conn, pjob, resolver=resolver)
            assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert excinfo.value.terminal is True
            assert ctrl.powered == []  # the load-bearing property: no physical reset

    asyncio.run(_run())


def test_force_crash_marker_refuses_racing_power(migrated_url: str) -> None:
    # Interleaving A: force_crash commits CRASHING before the power op's re-check -> power refused.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(12):
                system_id = await _seed_system(pool, SystemState.READY, domain_name=f"kdive-{i}")
                ctrl = _RecordingController()
                resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
                cjob = await _enqueue(
                    pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:force_crash"
                )
                pjob = await _enqueue_power(pool, system_id, f"{system_id}:power")

                async def run_crash(job: Job = cjob, resolver=resolver) -> None:
                    async with pool.connection() as conn:
                        await control_plane.force_crash_handler(conn, job, resolver=resolver)

                async def run_power(job: Job = pjob, resolver=resolver) -> None:
                    async with pool.connection() as conn:
                        # refused (CategorizedError) when it saw CRASHING/CRASHED — the safe outcome
                        with contextlib.suppress(CategorizedError):
                            await control_plane.power_handler(conn, job, resolver=resolver)

                await asyncio.gather(run_crash(), run_power())
                assert await _system_state(pool, system_id) in {
                    SystemState.CRASHED.value,
                    SystemState.CRASHING.value,
                }
                assert len(ctrl.powered) <= 1  # at most the pre-marker READY power op

    asyncio.run(_run())


@pytest.mark.parametrize(
    "expected_action", [PowerAction.OFF, PowerAction.ON, PowerAction.CYCLE, PowerAction.RESET]
)
def test_power_holds_force_crash_marker_until_provider_finishes(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, expected_action: PowerAction
) -> None:
    class BlockedPower(_RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()
            self.events: list[str] = []

        def power(self, domain_name: str, action: PowerAction) -> None:
            assert action is expected_action
            self.events.append("power-start")
            self.started.set()
            assert self.release.wait(timeout=5)
            self.events.append("power-done")

        def force_crash(self, domain_name: str) -> None:
            self.events.append("crash")

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = BlockedPower()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            power_job = await _enqueue_power(pool, system_id, f"{system_id}:power", expected_action)
            crash_job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:crash")

            async def run_power() -> None:
                async with pool.connection() as conn:
                    await control_plane.power_handler(conn, power_job, resolver=resolver)

            async def run_crash() -> None:
                async with pool.connection() as conn:
                    await control_plane.force_crash_handler(conn, crash_job, resolver=resolver)

            power_task = asyncio.create_task(run_power())
            assert await asyncio.to_thread(ctrl.started.wait, 2)
            # Probe before crash can acquire the same lock, so a refusal proves power owns it.
            try:
                async with pool.connection() as probe, probe.transaction():
                    assert not await try_advisory_xact_lock(
                        probe, LockScope.SYSTEM, UUID(system_id)
                    )
            except BaseException:
                ctrl.release.set()
                await power_task
                raise
            crash_waiting = asyncio.Event()
            real_lock = control_plane.advisory_xact_lock

            @asynccontextmanager
            async def observed_lock(
                conn: AsyncConnection, scope: LockScope, key: UUID | str
            ) -> AsyncIterator[None]:
                if asyncio.current_task() is crash_task:
                    crash_waiting.set()
                async with real_lock(conn, scope, key):
                    yield

            monkeypatch.setattr(control_plane, "advisory_xact_lock", observed_lock)
            crash_task = asyncio.create_task(run_crash())
            await asyncio.wait_for(crash_waiting.wait(), 2)
            try:
                async with pool.connection() as probe, probe.transaction():
                    assert not await try_advisory_xact_lock(
                        probe, LockScope.SYSTEM, UUID(system_id)
                    )
                assert await _system_state(pool, system_id) == SystemState.READY.value
            finally:
                ctrl.release.set()
            await asyncio.gather(power_task, crash_task)
            assert ctrl.events == ["power-start", "power-done", "crash"]
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value

    asyncio.run(_run())


@pytest.mark.parametrize("expected_action", [PowerAction.OFF, PowerAction.RESET])
def test_cancelled_power_keeps_fence_until_provider_thread_finishes(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, expected_action: PowerAction
) -> None:
    class BlockedPower(_RecordingController):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()
            self.release = threading.Event()

        def power(self, domain_name: str, action: PowerAction) -> None:
            assert action is expected_action
            self.started.set()
            assert self.release.wait(timeout=5)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            ctrl = BlockedPower()
            resolver = provider_resolver(provisioner=_TrackingProvisioner(), controller=ctrl)
            power_job = await _enqueue_power(pool, system_id, f"{system_id}:power", expected_action)
            crash_job = await _enqueue(pool, JobKind.FORCE_CRASH, system_id, f"{system_id}:crash")
            connection_modes: list[tuple[bool, bool]] = []
            provider_kinds: list[str | None] = []

            async def run_power() -> None:
                async with pool.connection() as conn:
                    previous = conn.autocommit
                    try:
                        await control_plane.power_handler(conn, power_job, resolver=resolver)
                    finally:
                        connection_modes.append((previous, conn.autocommit))
                        provider_kinds.append(take_provider_kind())

            async def run_crash() -> None:
                async with pool.connection() as conn:
                    await control_plane.force_crash_handler(conn, crash_job, resolver=resolver)

            power_task = asyncio.create_task(run_power())
            assert await asyncio.to_thread(ctrl.started.wait, 2)
            async with pool.connection() as probe, probe.transaction():
                assert not await try_advisory_xact_lock(probe, LockScope.SYSTEM, UUID(system_id))
            power_task.cancel()
            crash_waiting = asyncio.Event()
            real_lock = control_plane.advisory_xact_lock

            @asynccontextmanager
            async def observed_lock(
                conn: AsyncConnection, scope: LockScope, key: UUID | str
            ) -> AsyncIterator[None]:
                if asyncio.current_task() is crash_task:
                    crash_waiting.set()
                async with real_lock(conn, scope, key):
                    yield

            monkeypatch.setattr(control_plane, "advisory_xact_lock", observed_lock)
            crash_task = asyncio.create_task(run_crash())
            await asyncio.wait_for(crash_waiting.wait(), 2)
            power_task.cancel()  # repeated cancellation must not interrupt fenced cleanup
            try:
                assert not power_task.done()
                async with pool.connection() as probe, probe.transaction():
                    assert not await try_advisory_xact_lock(
                        probe, LockScope.SYSTEM, UUID(system_id)
                    )
                assert await _system_state(pool, system_id) == SystemState.READY.value
            finally:
                ctrl.release.set()
            with pytest.raises(asyncio.CancelledError):
                await power_task
            await crash_task
            assert connection_modes == [(False, False)]
            assert provider_kinds == ["local-libvirt"]
            assert await _system_state(pool, system_id) == SystemState.CRASHED.value

    asyncio.run(_run())


@pytest.mark.parametrize("action", [PowerAction.OFF, PowerAction.RESET])
def test_failed_power_releases_system_fence_and_restores_autocommit(
    migrated_url: str, action: PowerAction
) -> None:
    class FailingPower(_RecordingController):
        def power(self, domain_name: str, action: PowerAction) -> None:
            raise CategorizedError("provider failed", category=ErrorCategory.CONTROL_FAILURE)

    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(pool, SystemState.READY, domain_name="kdive-x")
            resolver = provider_resolver(controller=FailingPower())
            power_job = await _enqueue_power(pool, system_id, f"{system_id}:power", action)
            async with pool.connection() as conn:
                assert conn.autocommit is False
                with pytest.raises(CategorizedError):
                    await control_plane.power_handler(conn, power_job, resolver=resolver)
                assert conn.autocommit is False
                assert take_provider_kind() == "local-libvirt"
                async with pool.connection() as probe, probe.transaction():
                    assert await try_advisory_xact_lock(probe, LockScope.SYSTEM, UUID(system_id))

    asyncio.run(_run())

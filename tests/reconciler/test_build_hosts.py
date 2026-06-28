"""Tests for reconciler build-host lease reclaim (Task 12, ADR-0099).

A build_host_leases row whose owning BUILD job is terminal or gone is deleted so
the capacity slot frees — the backstop for a worker that died mid-build.  Keyed on
job liveness (queued/running), never on elapsed time.

Seeding uses autocommit connections; repair runs through a real non-autocommit pool
to exercise the transaction-nesting path (mirrors test_loop.py conventions).
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.providers.infra.reaping import NullReaper
from kdive.reconciler import loop
from kdive.reconciler.loop import reconcile_once
from kdive.reconciler.repairs.build_hosts import reclaim_orphan_build_host_leases
from tests.reconciler.conftest import connect, run_repair, seed_run, seed_system

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_ssh_build_host(conn: psycopg.AsyncConnection) -> UUID:
    """Insert a minimal ssh build_host; return its id."""
    host_id = uuid4()
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
        "    workspace_root, max_concurrent) "
        "VALUES (%s, %s, 'ssh', %s, %s, %s, %s)",
        (host_id, f"host-{host_id}", "10.0.0.1", "cred-ref", "/build", 2),
    )
    return host_id


async def _seed_lease(conn: psycopg.AsyncConnection, run_id: UUID, build_host_id: UUID) -> None:
    """Insert a build_host_leases row for (run_id, build_host_id)."""
    await conn.execute(
        "INSERT INTO build_host_leases (run_id, build_host_id) VALUES (%s, %s)",
        (run_id, build_host_id),
    )


async def _seed_build_job(
    conn: psycopg.AsyncConnection, run_id: UUID, *, state: str, kind: str = "build"
) -> UUID:
    """Insert a build-bearing job for run_id with the given state; return its id."""
    job_id = uuid4()
    await conn.execute(
        "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, "
        "    authorizing, dedup_key) "
        "VALUES (%s, %s, %s, %s, 1, 3, %s, %s)",
        (
            job_id,
            kind,
            Jsonb({"run_id": str(run_id)}),
            state,
            Jsonb({"principal": "test", "agent_session": None, "project": "p"}),
            f"{kind}:{run_id}",
        ),
    )
    return job_id


async def _lease_exists(conn: psycopg.AsyncConnection, run_id: UUID) -> bool:
    cur = await conn.execute("SELECT 1 FROM build_host_leases WHERE run_id = %s", (run_id,))
    return await cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_queued_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is queued (still live) must NOT be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="queued")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_running_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is running (still live) must NOT be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_composite_queued_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease held by a queued build_install_boot job must NOT be reclaimed (C1 regression)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="queued", kind="build_install_boot")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_composite_running_job_lease_not_reclaimed(migrated_url: str) -> None:
    """A lease held by a running build_install_boot job must NOT be reclaimed (C1 regression)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running", kind="build_install_boot")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_failed_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is failed (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_succeeded_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job succeeded (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="succeeded")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_canceled_job_lease_reclaimed(migrated_url: str) -> None:
    """A lease whose build job is canceled (terminal) must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="canceled")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_no_job_row_lease_reclaimed(migrated_url: str) -> None:
    """A lease with no matching BUILD job row at all must be reclaimed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            # intentionally no job row inserted
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert count == 1
        async with await connect(migrated_url) as check:
            assert not await _lease_exists(check, run_id)

    asyncio.run(_run())


def test_reclaim_is_idempotent(migrated_url: str) -> None:
    """Running the repair twice is safe; the second pass returns 0."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            first = await run_repair(pool, reclaim_orphan_build_host_leases)
            second = await run_repair(pool, reclaim_orphan_build_host_leases)

        assert first == 1
        assert second == 0

    asyncio.run(_run())


def test_reconcile_once_reports_reclaimed_build_host_leases(migrated_url: str) -> None:
    """reconcile_once includes the reclaim count in its report."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            host_id = await _seed_ssh_build_host(seed)
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
            await _seed_lease(seed, run_id, host_id)

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(pool, NullReaper())

        assert report.reclaimed_build_host_leases == 1

    asyncio.run(_run())


def test_reclaim_spec_registered_in_loop() -> None:
    """_reclaim_build_host_leases alias is present in the loop module's __all__."""
    assert "_reclaim_build_host_leases" in loop.__all__


# ---------------------------------------------------------------------------
# Ephemeral build-VM reaping (ADR-0100)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

from kdive.providers.infra.reaping import BuildVm  # noqa: E402
from kdive.reconciler.repairs.build_hosts import reap_orphan_build_vms  # noqa: E402


class _FakeBuildVmReaper:
    """Records delete_build_vm calls; returns a canned list_build_vms result."""

    def __init__(self, vms: list[BuildVm]) -> None:
        self._vms = vms
        self.deleted: list[str] = []

    async def list_build_vms(self) -> list[BuildVm]:
        return list(self._vms)

    async def delete_build_vm(self, domain_name: str) -> None:
        self.deleted.append(domain_name)


def _build_vm(run_id: UUID) -> BuildVm:
    return BuildVm(domain_name=f"kdive-build-{run_id}", run_id=run_id)


def test_build_vm_reaped_when_build_job_terminal(migrated_url: str) -> None:
    """A build VM whose BUILD job is terminal (failed) is reaped."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="failed")
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 1
        assert reaper.deleted == [f"kdive-build-{run_id}"]

    asyncio.run(_run())


def test_build_vm_not_reaped_when_build_job_live(migrated_url: str) -> None:
    """A build VM whose BUILD job is still running is NOT reaped (no age-based reap)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running")
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 0
        assert reaper.deleted == []

    asyncio.run(_run())


def test_build_vm_not_reaped_when_composite_job_live(migrated_url: str) -> None:
    """A build VM whose build_install_boot job is running is NOT reaped (C1 regression)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            system_id = await seed_system(seed)
            run_id = await seed_run(seed, system_id)
            await _seed_build_job(seed, run_id, state="running", kind="build_install_boot")
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 0
        assert reaper.deleted == []

    asyncio.run(_run())


def test_build_vm_reaped_when_no_job_row(migrated_url: str) -> None:
    """A build VM with no matching BUILD job row at all is reaped (orphan)."""

    async def _run() -> None:
        run_id = uuid4()  # no run, no job
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 1
        assert reaper.deleted == [f"kdive-build-{run_id}"]

    asyncio.run(_run())


async def _seed_ephemeral_host(conn: psycopg.AsyncConnection) -> UUID:
    host_id = uuid4()
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent, "
        "base_image_volume) VALUES (%s, %s, 'ephemeral_libvirt', %s, %s, %s)",
        (host_id, f"eph-{host_id}", "/build", 1, "base.qcow2"),
    )
    return host_id


def test_build_vm_not_reaped_when_doctor_probe_heartbeat_is_live(migrated_url: str) -> None:
    """A kdive-build-<run_id> with a fresh doctor-probe heartbeat is live and is NOT reaped."""

    async def _run() -> None:
        from kdive.db import buildhost_agent_probes as probes

        run_id = uuid4()  # no BUILD job, but a live probe marker holds it
        async with await connect(migrated_url) as seed, seed.transaction():
            host_id = await _seed_ephemeral_host(seed)
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await probes.register(pool, build_host_id=host_id, run_id=run_id)
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 0
        assert reaper.deleted == []

    asyncio.run(_run())


def test_build_vm_reaped_when_doctor_probe_heartbeat_released(migrated_url: str) -> None:
    """A kdive-build-<run_id> with no live job and a released probe marker is reaped."""

    async def _run() -> None:
        from kdive.db import buildhost_agent_probes as probes

        run_id = uuid4()
        async with await connect(migrated_url) as seed, seed.transaction():
            host_id = await _seed_ephemeral_host(seed)
        reaper = _FakeBuildVmReaper([_build_vm(run_id)])

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            probe_id = await probes.register(pool, build_host_id=host_id, run_id=run_id)
            await probes.release(pool, probe_id)  # released → not live
            count = await run_repair(pool, lambda conn: reap_orphan_build_vms(conn, reaper))

        assert count == 1
        assert reaper.deleted == [f"kdive-build-{run_id}"]

    asyncio.run(_run())


def test_build_vm_reap_runs_before_lease_reclaim_in_repair_plan() -> None:
    """The reaped_build_vms repair must precede reclaimed_build_host_leases (reap before reclaim).

    Freeing a lease slot before reaping the leaked VM would let a new build over-admit the host
    past max_concurrent while the leaked VM still runs (ADR-0100 §4.6).
    """
    plan = loop._repair_plan(
        reaper=NullReaper(),
        config=loop.ReconcileConfig(),
        image_publish_grace=timedelta(minutes=5),
    )
    names = [spec.name for spec in plan]
    assert "reaped_build_vms" in names
    assert "reclaimed_build_host_leases" in names
    assert names.index("reaped_build_vms") < names.index("reclaimed_build_host_leases")
    assert callable(loop._reclaim_build_host_leases)


# ---------------------------------------------------------------------------
# Reachability probe (ADR-0103)
# ---------------------------------------------------------------------------

import pytest  # noqa: E402

from kdive.db.build_hosts import BuildHost  # noqa: E402
from kdive.reconciler.repairs.build_hosts import probe_build_host_reachability  # noqa: E402


class _FakeProber:
    """A BuildHostProber stand-in: maps host name -> reachable bool; can raise per host.

    Records every probed host name so a test can prove a disabled/local host is never
    probed and that one host's failure does not stop the others.
    """

    def __init__(
        self, results: dict[str, bool], *, raise_for: frozenset[str] = frozenset()
    ) -> None:
        self._results = results
        self._raise_for = raise_for
        self.probed: list[str] = []

    async def probe(self, host: BuildHost) -> bool:
        self.probed.append(host.name)
        if host.name in self._raise_for:
            raise RuntimeError("probe boom")
        return self._results[host.name]


async def _seed_named_host(
    conn: psycopg.AsyncConnection,
    name: str,
    *,
    kind: str = "ssh",
    state: str = "ready",
    enabled: bool = True,
) -> None:
    """Insert a build host with explicit name/kind/state/enabled."""
    address = "10.0.0.1" if kind == "ssh" else None
    cred = "cred-ref" if kind == "ssh" else None
    await conn.execute(
        "INSERT INTO build_hosts (id, name, kind, address, ssh_credential_ref, "
        "    workspace_root, max_concurrent, enabled, state) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (uuid4(), name, kind, address, cred, "/build", 2, enabled, state),
    )


async def _state_of(conn: psycopg.AsyncConnection, name: str) -> str:
    cur = await conn.execute("SELECT state FROM build_hosts WHERE name = %s", (name,))
    row = await cur.fetchone()
    assert row is not None
    return str(row[0])


def test_probe_ready_reachable_is_noop(migrated_url: str) -> None:
    """A ready host that probes reachable → no transition, count 0."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "b1", state="ready")
        prober = _FakeProber({"b1": True})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 0
        async with await connect(migrated_url) as check:
            assert await _state_of(check, "b1") == "ready"

    asyncio.run(_run())


def test_probe_ready_unreachable_flips(migrated_url: str) -> None:
    """A ready host that probes unreachable → flips to unreachable, count 1."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "b1", state="ready")
        prober = _FakeProber({"b1": False})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _state_of(check, "b1") == "unreachable"

    asyncio.run(_run())


def test_probe_unreachable_reachable_flips(migrated_url: str) -> None:
    """An unreachable host that probes reachable → flips back to ready, count 1."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "b1", state="unreachable")
        prober = _FakeProber({"b1": True})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 1
        async with await connect(migrated_url) as check:
            assert await _state_of(check, "b1") == "ready"

    asyncio.run(_run())


def test_probe_skips_disabled_ssh_host(migrated_url: str) -> None:
    """A disabled ssh host is never probed and its state is untouched."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "enabled-1", state="ready", enabled=True)
            await _seed_named_host(seed, "disabled-1", state="ready", enabled=False)
        prober = _FakeProber({"enabled-1": True})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert prober.probed == ["enabled-1"]
        assert "disabled-1" not in prober.probed
        async with await connect(migrated_url) as check:
            assert await _state_of(check, "disabled-1") == "ready"

    asyncio.run(_run())


def test_probe_skips_local_host(migrated_url: str) -> None:
    """The seeded local worker-local host is never probed."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "ssh-1", state="ready")
        prober = _FakeProber({"ssh-1": True})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert "worker-local" not in prober.probed
        assert prober.probed == ["ssh-1"]

    asyncio.run(_run())


def test_probe_one_host_failure_does_not_stop_others(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A prober raising for one host must not stop a second host from flipping."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "a-boom", state="ready")
            await _seed_named_host(seed, "b-flip", state="ready")
        # a-boom raises; b-flip probes unreachable and must still flip.
        prober = _FakeProber({"b-flip": False}, raise_for=frozenset({"a-boom"}))

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            caplog.set_level("WARNING", logger="kdive.reconciler.repairs.build_hosts")
            count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 1
        assert set(prober.probed) == {"a-boom", "b-flip"}
        warnings = [
            record for record in caplog.records if "probing build host" in record.getMessage()
        ]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None
        assert isinstance(warnings[0].exc_info[1], RuntimeError)
        async with await connect(migrated_url) as check:
            assert await _state_of(check, "a-boom") == "ready"  # untouched (probe raised)
            assert await _state_of(check, "b-flip") == "unreachable"

    asyncio.run(_run())


def test_probe_logs_probed_and_changed_counts(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A non-empty pass logs the probed count and the changed count at INFO (ADR-0103 §3.3)."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "ok-host", state="ready")
            await _seed_named_host(seed, "down-host", state="ready")
        prober = _FakeProber({"ok-host": True, "down-host": False})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            with caplog.at_level("INFO", logger="kdive.reconciler.repairs.build_hosts"):
                count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 1
        summary = [r for r in caplog.records if "probed" in r.getMessage()]
        assert summary, "a non-empty probe pass must log a probed/changed summary"
        # probed=2, changed=1 — assert on the structured args, not the message string.
        assert summary[-1].args == (2, 1)

    asyncio.run(_run())


def test_probe_empty_set_is_noop(migrated_url: str) -> None:
    """With no probeable ssh hosts the repair returns 0 and probes nothing."""

    async def _run() -> None:
        prober = _FakeProber({})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            count = await run_repair(pool, lambda c: probe_build_host_reachability(c, prober))

        assert count == 0
        assert prober.probed == []

    asyncio.run(_run())


def test_probe_spec_registered_in_loop() -> None:
    """The _probe_build_host_reachability alias is exported from the loop module."""
    assert "_probe_build_host_reachability" in loop.__all__
    assert callable(loop._probe_build_host_reachability)


def test_probe_repair_absent_without_prober_present_with_one() -> None:
    """The probe repair is in the plan only when a build_host_prober is configured."""
    without = [
        spec.name
        for spec in loop._repair_plan(
            reaper=NullReaper(),
            config=loop.ReconcileConfig(),
            image_publish_grace=timedelta(minutes=5),
        )
    ]
    assert "build_host_states_changed" not in without

    with_prober = [
        spec.name
        for spec in loop._repair_plan(
            reaper=NullReaper(),
            config=loop.ReconcileConfig(build_host_prober=_FakeProber({})),
            image_publish_grace=timedelta(minutes=5),
        )
    ]
    assert "build_host_states_changed" in with_prober


def test_reconcile_once_reports_build_host_states_changed(migrated_url: str) -> None:
    """reconcile_once surfaces the probe's transition count when a prober is configured."""

    async def _run() -> None:
        async with await connect(migrated_url) as seed:
            await _seed_named_host(seed, "b1", state="ready")
        prober = _FakeProber({"b1": False})

        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=4) as pool:
            report = await reconcile_once(
                pool, NullReaper(), config=loop.ReconcileConfig(build_host_prober=prober)
            )

        assert report.build_host_states_changed == 1

    asyncio.run(_run())

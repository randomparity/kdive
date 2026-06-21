"""Coverage anchor for the split build run handler module."""

from __future__ import annotations

import asyncio
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.build_artifacts.results import BuildOutput
from kdive.db.build_hosts import get_by_name
from kdive.domain.capacity.state import SystemState
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import runs, runs_build
from kdive.jobs.payloads import BuildPayload
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.runs.steps import existing_build_result
from tests.integration._seed import seed_granted_allocation, seed_running_run, seed_system
from tests.mcp.systems_support import provider_resolver


def test_build_handler_is_exported_through_runs_facade() -> None:
    assert runs.build_handler is runs_build.build_handler
    assert runs._run_build is runs_build._run_build


def test_build_handler_persists_modules_ref(
    migrated_url: str, monkeypatch: object, tmp_path: object
) -> None:
    """build_handler threads modules_ref from BuildOutput into the run_steps ledger."""
    import pytest as _pytest

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    monkeypatch.setenv("KDIVE_KERNEL_SRC", str(tmp_path))

    class _ModulesBuilder:
        def build(self, run_id: UUID, profile: object, **_: object) -> BuildOutput:
            return BuildOutput(
                kernel_ref=f"proj/runs/{run_id}/kernel",
                debuginfo_ref=f"proj/runs/{run_id}/vmlinux",
                build_id="abcdef0123456789",
                modules_ref=f"proj/runs/{run_id}/modules",
            )

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            allocation_id = await seed_granted_allocation(pool)
            system_id = await seed_system(pool, allocation_id, SystemState.READY)
            run_id = await seed_running_run(pool, system_id)
            async with pool.connection() as conn:
                host = await get_by_name(conn, "worker-local")
            assert host is not None
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.BUILD,
                    BuildPayload(run_id=run_id, build_host_id=str(host.id)),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{run_id}:build",
                )
            builder = _ModulesBuilder()
            async with pool.connection() as conn:
                await runs.build_handler(
                    conn,
                    job,
                    resolver=provider_resolver(builder=builder),
                    secret_registry=SecretRegistry(),
                )
            async with pool.connection() as conn:
                result = await existing_build_result(conn, UUID(run_id))
            assert result is not None
            assert result.modules_ref == f"proj/runs/{run_id}/modules"

    asyncio.run(_run())

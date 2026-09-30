"""A second activation on one System after the first activation's completed release (#2968)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr

from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.jobs.external_boot_authority_client import ExternalBootAuthorityClient
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.handlers.external_boot.router import route_marked
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.worker import Worker
from kdive.mcp.tools.external_boot.recovery_requests import request_release
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.transport import _dispatch
from kdive.providers.local_libvirt.external_boot_authority import LocalExternalBootAuthorityAdapter
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.integration.test_external_boot_job_lifecycle import (
    CREDENTIAL,
    _configure_external_boot,
    _one,
    _PreparingProvider,
    _register_incarnation,
    _seed_public_external_boot,
)
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver


class _ReleasingProvider(_PreparingProvider):
    """Add the recover and cleanup ports a root release reaches; the journal is what is proved."""

    def recover(self, recovery: Any, authority: Any, *, local_timing: Any = None) -> None:
        super().recover(recovery, authority)
        self._active.discard(recovery.binding.activation_id)

    def cleanup_is_accounted(self, recovery: Any, authority: Any) -> bool:
        del recovery, authority
        return True

    def record_cleanup_quarantine(self, recovery: Any, proof: Any, authority: Any) -> None:
        del recovery, proof, authority

    def cleanup_receipt(self, binding: Any, authority: Any) -> Any:
        del authority
        return self._points.get(binding.activation_id)

    def finalize_cleanup_tombstone(self, recovery: Any, proof: Any, authority: Any) -> None:
        del recovery, proof, authority

    def recovery_is_absent(self, binding: Any, authority: Any) -> bool:
        del binding, authority
        return True


async def _second_run(pool: AsyncConnectionPool, first_run: str) -> str:
    run_id = str(uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, "
            "build_profile, build_ref, principal, project) "
            "SELECT %s, investigation_id, system_id, target_kind, state, build_profile, "
            "build_ref, principal, project FROM runs WHERE id = %s",
            (run_id, first_run),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s,'install','succeeded','{}'::jsonb)",
            (run_id,),
        )
    return run_id


def test_second_activation_after_completed_release(
    migrated_url: str, authority_role_dsns: Callable[[str], str], tmp_path: Path
) -> None:
    async def body() -> None:
        _configure_external_boot()
        provider = _ReleasingProvider()
        resolver = provider_resolver(external_boot=provider)
        authority_dsn = authority_role_dsns("kdive_provider_authority")

        @asynccontextmanager
        async def authority_connection() -> AsyncIterator[AsyncConnection]:
            async with await psycopg.AsyncConnection.connect(
                authority_dsn, autocommit=True
            ) as connection:
                yield connection

        service = ExternalBootAuthorityService(
            repository=DatabaseAuthorityRepository(authority_connection),
            journal_factory=lambda owned: FileAuthorityJournal(tmp_path, f"{owned}.journal"),
            adapter=LocalExternalBootAuthorityAdapter(cast(Any, provider)),
        )
        worker_id = "local:system-reuse"
        peer = AuthenticatedPeer(worker_id)

        async def authenticate(_credential: SecretStr) -> AuthenticatedPeer:
            return peer

        class Backend:
            async def _request_frame(self, envelope: bytes, *, deadline: float) -> bytes:
                return await _dispatch(envelope, authenticate, service)

        sender = AuthorityRequestSender(Backend, lambda: CREDENTIAL)

        async def must_not_run(_conn: AsyncConnection, job: Job) -> str:
            raise AssertionError(f"a marked {job.kind.value} job reached the ordinary handler")

        operations = build_operations(
            ExternalBootHandlerPorts(
                resolver=resolver,
                incarnation_credential=CREDENTIAL,
                secret_registry=SecretRegistry(),
                authority_client_factory=lambda binding, marker, deadline: (
                    ExternalBootAuthorityClient(sender, marker, deadline)
                ),
            )
        )
        registry = HandlerRegistry()
        registry.register(JobKind.BOOT, route_marked(operations, must_not_run))
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool:
            await _register_incarnation(pool, worker_id)
            worker = Worker(
                pool,
                registry,
                worker_id=worker_id,
                incarnation_credential=CREDENTIAL,
                secret_registry=SecretRegistry(),
            )

            async def drain(job_id: UUID) -> None:
                async with pool.connection() as conn:
                    lane = (
                        await _one(conn, "SELECT dispatch_lane FROM jobs WHERE id=%s", (job_id,))
                    )["dispatch_lane"]
                    assert await worker.run_once(lane) is not None
                    job = await _one(conn, "SELECT state FROM jobs WHERE id=%s", (job_id,))
                assert job["state"] == "succeeded", job

            async def activate_then_release(run_id: str) -> None:
                booted = await boot_run(pool, runs_support.ctx(), run_id, resolver=resolver)
                assert booted.status == "queued", booted
                await drain(UUID(booted.object_id))
                released = await request_release(
                    pool, runs_support.ctx(), run_id=run_id, resolver=resolver
                )
                assert released.status == "queued", released.data
                await drain(UUID(released.object_id))

            try:
                first_run, system_id = await _seed_public_external_boot(pool)
                await activate_then_release(first_run)
                await activate_then_release(await _second_run(pool, first_run))
            finally:
                await service.close()
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT state, cleanup_complete FROM external_boot_activations "
                    "WHERE system_id = %s ORDER BY created_at",
                    (system_id,),
                )
                assert await cur.fetchall() == [("recovered", True), ("recovered", True)]
                await cur.execute(
                    "SELECT purpose, state FROM external_boot_authorities "
                    "WHERE system_id = %s ORDER BY generation",
                    (system_id,),
                )
                assert (
                    await cur.fetchall()
                    == [
                        ("activate", "retired"),
                        ("release", "retired"),
                    ]
                    * 2
                )

    asyncio.run(body())

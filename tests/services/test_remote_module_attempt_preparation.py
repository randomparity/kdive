"""Server/worker module-attempt preparation boundary (ADR-0605)."""

from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.remote_module_attempt_preparation import (
    ModuleAttemptObligationReceiptV1,
    ModuleAttemptPreparationRequestV1,
)
from kdive.services.remote_module_attempt_preparation import (
    ModuleAttemptObligationVerificationError,
    run_verified_module_attempt_preparation,
)
from tests.db.external_boot_authority_support import (
    _RoleDsns,
)
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.db.remote_module_attempt_obligations_support import _evidence


async def _seed(conn: psycopg.AsyncConnection) -> ModuleAttempt:
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    investigation_id, run_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'remote-libvirt', 'default', 'standard', 'available', "
        "'qemu+tls://example.invalid/system')",
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
        "(%s, %s, %s, 'remote-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return ModuleAttempt(system_id, run_id, "a" * 32)


def _request(attempt: ModuleAttempt) -> ModuleAttemptPreparationRequestV1:
    return ModuleAttemptPreparationRequestV1(
        module_attempt_obligation=ModuleAttemptObligationReceiptV1(
            system_id=attempt.system_id,
            run_id=attempt.run_id,
            operation_nonce=attempt.operation_nonce,
        )
    )


async def _open_pool(dsn: str, *, size: int = 2) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(dsn, min_size=1, max_size=size, open=False)
    await pool.open()
    return pool


@pytest.mark.parametrize("case", ["missing", "mismatch", "discharged"])
def test_worker_fails_closed_before_consumer(
    migrated_url: str, authority_role_dsns: _RoleDsns, case: str
) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            if case != "missing":
                await repo.open_mutation_obligation(admin, attempt)
                if case == "discharged":
                    await repo.discharge_mutation_obligation(admin, attempt, reason="restored")
        expected = attempt
        if case == "mismatch":
            expected = ModuleAttempt(attempt.system_id, attempt.run_id, "b" * 32)
        called = False

        async def consumer(_: ModuleAttempt) -> None:
            nonlocal called
            called = True

        async with await _open_pool(authority_role_dsns("kdive_worker")) as worker:
            with pytest.raises(ModuleAttemptObligationVerificationError) as caught:
                await run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), expected, consumer
                )
        assert str(caught.value) == "module-attempt obligation verification failed"
        assert called is False

    asyncio.run(_run())


def test_verification_holds_lock_through_consumer_and_returns_only_result(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
        entered, release = asyncio.Event(), asyncio.Event()
        retained: list[ModuleAttempt] = []

        async def consumer(value: ModuleAttempt) -> str:
            retained.append(value)
            entered.set()
            await release.wait()
            return "created-two"

        async with (
            await _open_pool(authority_role_dsns("kdive_worker")) as worker,
            await _open_pool(authority_role_dsns("kdive_server")) as server,
        ):
            verification = asyncio.create_task(
                run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), attempt, consumer
                )
            )
            await entered.wait()
            async with server.connection() as conn:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '100ms'")
                        await repo.discharge_mutation_obligation(conn, attempt, reason="restored")
            release.set()
            assert await verification == "created-two"
            async with server.connection() as conn, conn.transaction():
                assert await repo.discharge_mutation_obligation(conn, attempt, reason="restored")
        assert retained == [attempt]
        assert not hasattr(retained[0], "authorize")
        post_return_called = False

        async def post_return_consumer(_: ModuleAttempt) -> None:
            nonlocal post_return_called
            post_return_called = True

        async with await _open_pool(authority_role_dsns("kdive_worker")) as worker:
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), retained[0], post_return_consumer
                )
        assert post_return_called is False

    asyncio.run(_run())


def test_terminal_commit_failure_rolls_back_evidence_and_reap_retention(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
            await admin.commit()

        async def consumer(_: ModuleAttempt) -> str:
            return "remote-terminal"

        async def fail_after_write(
            conn: psycopg.AsyncConnection, value: ModuleAttempt, result: str
        ) -> None:
            assert result == "remote-terminal"
            await repo.record_terminal_evidence(conn, value, _evidence(value))
            await repo.open_reap_obligation(conn, value)
            raise RuntimeError("terminal evidence commit failed")

        async with await _open_pool(migrated_url) as worker:
            with pytest.raises(RuntimeError, match="terminal evidence commit failed"):
                await run_verified_module_attempt_preparation(
                    worker,
                    repo,
                    _request(attempt),
                    attempt,
                    consumer,
                    commit_result=fail_after_write,
                )

        async with await psycopg.AsyncConnection.connect(migrated_url) as observer:
            assert await repo.read_terminal_evidence(observer, attempt) is None
            assert await repo.reap_obligation_is_open(observer, attempt) is False
            assert await repo.mutation_obligation_is_open(observer, attempt) is True

    asyncio.run(_run())


def test_retained_terminal_evidence_allows_only_explicit_replay(migrated_url: str) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
            await repo.record_terminal_evidence(admin, attempt, _evidence(attempt))
            await repo.open_reap_obligation(admin, attempt)
            await admin.commit()

        calls = 0

        async def consumer(_: ModuleAttempt) -> str:
            nonlocal calls
            calls += 1
            return "replayed-terminal"

        async with await _open_pool(migrated_url) as worker:
            with pytest.raises(ModuleAttemptObligationVerificationError):
                await run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), attempt, consumer
                )
            assert (
                await run_verified_module_attempt_preparation(
                    worker,
                    repo,
                    _request(attempt),
                    attempt,
                    consumer,
                    commit_result=lambda *_args: asyncio.sleep(0),
                    allow_terminal_replay=True,
                )
                == "replayed-terminal"
            )
        assert calls == 1

    asyncio.run(_run())


def test_verification_blocks_terminal_escape_until_its_consumer_finishes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """The bulk terminal path uses the same System lock as the ADR-0605 verifier."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
        entered, release = asyncio.Event(), asyncio.Event()

        async def consumer(_: ModuleAttempt) -> None:
            entered.set()
            await release.wait()

        async with (
            await _open_pool(authority_role_dsns("kdive_worker")) as worker,
            await _open_pool(authority_role_dsns("kdive_server")) as server,
        ):
            verification = asyncio.create_task(
                run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), attempt, consumer
                )
            )
            await entered.wait()
            async with server.connection() as conn:
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    async with conn.transaction():
                        await conn.execute("SET LOCAL lock_timeout = '100ms'")
                        await repo.discharge_system_mutation_obligations(conn, attempt.system_id)
            release.set()
            await verification
            async with server.connection() as conn, conn.transaction():
                assert (
                    await repo.discharge_system_mutation_obligations(conn, attempt.system_id) == 1
                )

    asyncio.run(_run())


@pytest.mark.parametrize("exit_kind", ["exception", "cancellation"])
def test_consumer_failure_releases_lock_without_detached_work(
    migrated_url: str, authority_role_dsns: _RoleDsns, exit_kind: str
) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
        entered, release = asyncio.Event(), asyncio.Event()
        finished = False

        async def consumer(_: ModuleAttempt) -> None:
            nonlocal finished
            entered.set()
            try:
                await release.wait()
                raise RuntimeError("consumer failed")
            finally:
                finished = True

        async with (
            await _open_pool(authority_role_dsns("kdive_worker")) as worker,
            await _open_pool(authority_role_dsns("kdive_server")) as server,
        ):
            task = asyncio.create_task(
                run_verified_module_attempt_preparation(
                    worker, repo, _request(attempt), attempt, consumer
                )
            )
            await entered.wait()
            if exit_kind == "cancellation":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                with pytest.raises(RuntimeError, match="consumer failed"):
                    await task
            assert finished is True
            async with server.connection() as conn, conn.transaction():
                assert await asyncio.wait_for(
                    repo.discharge_mutation_obligation(conn, attempt, reason="restored"), 1
                )

    asyncio.run(_run())


def test_worker_role_cannot_mutate_obligation(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)
            await repo.open_mutation_obligation(admin, attempt)
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                await repo.discharge_mutation_obligation(worker, attempt, reason="restored")

    asyncio.run(_run())


def test_worker_database_failure_is_redacted(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    class UnreadableRepository(RemoteModuleAttemptObligationRepository):
        async def mutation_obligation_is_open(
            self, conn: psycopg.AsyncConnection, attempt: ModuleAttempt
        ) -> bool:
            del conn, attempt
            raise psycopg.OperationalError("traceable-database-marker")

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            attempt = await _seed(admin)

        async def consumer(_: ModuleAttempt) -> None:
            raise AssertionError("consumer must not run")

        async with await _open_pool(authority_role_dsns("kdive_worker")) as worker:
            with pytest.raises(ModuleAttemptObligationVerificationError) as caught:
                await run_verified_module_attempt_preparation(
                    worker, UnreadableRepository(), _request(attempt), attempt, consumer
                )
        assert "traceable-database-marker" not in str(caught.value)
        assert caught.value.__cause__ is None

    asyncio.run(_run())


def test_production_mutation_discharge_sql_is_repository_mediated() -> None:
    root = Path(__file__).parents[2] / "src" / "kdive"
    update = re.compile(r"\bupdate\s+(?:[a-z_][a-z0-9_]*\.)?remote_module_attempt_obligations\b")
    hits: list[Path] = []
    for path in root.rglob("*.py"):
        constants = (
            node.value
            for node in ast.walk(ast.parse(path.read_text()))
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        for value in constants:
            normalized = " ".join(value.lower().split())
            if update.search(normalized) and "mutation_discharged_at" in normalized:
                hits.append(path)
                break

    assert [path.relative_to(root).as_posix() for path in hits] == [
        "db/remote_module_attempt_obligations.py"
    ]

"""Authorized production recovery for a dead worker's reusable-build pin."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools.ops import build_uses
from kdive.security.authz.rbac import PlatformRole
from kdive.services.runs.worker_incarnations import register_worker_incarnation


def _ctx(*, operator: bool) -> RequestContext:
    return RequestContext(
        principal="operator-1",
        agent_session="session-1",
        projects=(),
        roles={},
        platform_roles=(frozenset({PlatformRole.PLATFORM_OPERATOR}) if operator else frozenset()),
    )


@asynccontextmanager
async def _pool(url: str):
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed(pool: AsyncConnectionPool) -> tuple[UUID, str]:
    investigation_id, generation, job_id, use_id = uuid4(), uuid4(), uuid4(), uuid4()
    holder = "host-a:42:boot-123:987"
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO investigations (id, principal, project, title, state) "
            "VALUES (%s, 'p', 'proj', 't', 'active')",
            (investigation_id,),
        )
        await conn.execute(
            "INSERT INTO investigation_builds (investigation_id, generation, build_ref, "
            "content_digest, canonical_document, build_result, artifacts, target_kind, "
            "build_profile, expires_at) VALUES (%s, %s, %s, %s, '{}'::jsonb, '{}'::jsonb, "
            "'{}'::jsonb, 'local-libvirt', '{}'::jsonb, now() - interval '1 sec')",
            (investigation_id, generation, f"{'a' * 64}.{generation}", "a" * 64),
        )
        await conn.execute(
            "INSERT INTO jobs (id, kind, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, authorizing, dedup_key) VALUES "
            "(%s, 'install', 'running', 1, 3, %s, now() - interval '1 sec', "
            "'{}'::jsonb, %s)",
            (job_id, holder, f"recovery-{job_id}"),
        )
        await conn.execute(
            "INSERT INTO investigation_build_uses (use_id, investigation_id, generation, "
            "job_id, attempt, holder_worker_id, lease_expires_at) "
            "VALUES (%s, %s, %s, %s, 1, %s, now() - interval '1 sec')",
            (use_id, investigation_id, generation, job_id, holder),
        )
    async with pool.connection() as conn:
        await register_worker_incarnation(
            conn,
            holder,
            "local",
            {"host": "host-a", "pid": 42, "boot_id": "boot-123", "start_ticks": "987"},
        )
    return use_id, holder


class _Verifier:
    def __init__(self, evidence: str | None) -> None:
        self.evidence = evidence
        self.seen: list[str] = []

    def verify_dead(self, worker_incarnation: str) -> str | None:
        self.seen.append(worker_incarnation)
        return self.evidence


class _FailingVerifier:
    def verify_dead(self, worker_incarnation: str) -> str | None:
        raise RuntimeError("authority unavailable with secret material")


def test_recover_build_use_requires_operator_and_independent_death_proof(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            verifier = _Verifier("local-proc: exact worker incarnation absent")
            denied = await build_uses.recover_build_use(
                pool,
                _ctx(operator=False),
                verifier,
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert denied.error_category == "authorization_denied"
            assert verifier.seen == []

            no_proof = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                _Verifier(None),
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert no_proof.error_category == "configuration_error"

            recovered = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                verifier,
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert recovered.status == "recovered"
            async with pool.connection() as conn:
                use_count = (
                    await (
                        await conn.execute(
                            "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s",
                            (use_id,),
                        )
                    ).fetchone()
                )[0]
                ledger = await (
                    await conn.execute(
                        "SELECT holder_worker_id, recovered_by, evidence, reason "
                        "FROM investigation_build_use_recoveries WHERE use_id = %s",
                        (use_id,),
                    )
                ).fetchone()
                audit_count = (
                    await (
                        await conn.execute(
                            "SELECT count(*) FROM platform_audit_log "
                            "WHERE tool = 'ops.recover_build_use'"
                        )
                    ).fetchone()
                )[0]
            assert use_count == 0
            assert ledger == (
                holder,
                "operator-1",
                "local: durable exact-incarnation termination (failed)",
                "worker host was replaced",
            )
            assert audit_count == 2  # authorized refusal and atomic successful recovery

    asyncio.run(_run())


def test_recover_build_use_audits_every_authorized_refusal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            attempts = (
                ("", holder, _Verifier("proof"), "invalid_input"),
                ("dead", holder, _Verifier(None), "death_not_proven"),
                ("dead", holder, _FailingVerifier(), "verifier_error"),
                ("dead", holder, _Verifier("x" * 1025), "evidence_oversized"),
                ("dead", holder + "-wrong", _Verifier("proof"), "use_or_holder_mismatch"),
            )
            expected_scopes: set[str] = set()
            for reason, attempted_holder, verifier, outcome in attempts:
                response = await build_uses.recover_build_use(
                    pool,
                    _ctx(operator=True),
                    verifier,
                    use_id=use_id,
                    holder=attempted_holder,
                    reason=reason,
                )
                assert response.error_category == "configuration_error"
                async with pool.connection() as conn:
                    rows = await (
                        await conn.execute(
                            "SELECT scope FROM platform_audit_log "
                            "WHERE tool = 'ops.recover_build_use'"
                        )
                    ).fetchall()
                expected_scopes.add(f"build-use-recovery:{outcome}")
                assert {row[0] for row in rows} == expected_scopes

            async with pool.connection() as conn:
                assert (
                    await (
                        await conn.execute(
                            "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s",
                            (use_id,),
                        )
                    ).fetchone()
                )[0] == 1

    asyncio.run(_run())


def test_recovery_refusal_fails_closed_when_audit_write_fails(
    migrated_url: str, monkeypatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)

            async def _fail_audit(*args, **kwargs):
                raise RuntimeError("audit unavailable")

            monkeypatch.setattr(build_uses.audit, "record_platform", _fail_audit)
            try:
                await build_uses.recover_build_use(
                    pool,
                    _ctx(operator=True),
                    _Verifier(None),
                    use_id=use_id,
                    holder=holder,
                    reason="dead",
                )
            except RuntimeError as exc:
                assert str(exc) == "audit unavailable"
            else:
                raise AssertionError("audit failure must fail the recovery request")
            async with pool.connection() as conn:
                assert (
                    await (
                        await conn.execute(
                            "SELECT count(*) FROM investigation_build_uses WHERE use_id = %s",
                            (use_id,),
                        )
                    ).fetchone()
                )[0] == 1

    asyncio.run(_run())


def test_recover_build_use_refuses_mismatch_and_bounds_reason(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            mismatch = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                _Verifier("proof"),
                use_id=use_id,
                holder=holder + "-wrong",
                reason="dead",
            )
            assert mismatch.error_category == "configuration_error"
            too_long = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                _Verifier("proof"),
                use_id=use_id,
                holder=holder,
                reason="x" * 513,
            )
            assert too_long.error_category == "configuration_error"

    asyncio.run(_run())


def test_list_build_uses_is_operator_only_and_bounded(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            denied = await build_uses.list_build_uses(pool, _ctx(operator=False), limit=1)
            assert denied.error_category == "authorization_denied"
            listed = await build_uses.list_build_uses(pool, _ctx(operator=True), limit=1)
            assert listed.status == "ok"
            assert len(listed.items) == 1
            assert listed.items[0].object_id == str(use_id)
            assert listed.items[0].data["holder"] == holder
            assert listed.items[0].data["attempt"] == "1"

    asyncio.run(_run())

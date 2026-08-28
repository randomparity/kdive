"""Authorized production recovery for a dead worker's reusable-build pin."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.mcp.auth import RequestContext
from kdive.mcp.tools._common import encode_ts_uuid_cursor
from kdive.mcp.tools.ops import build_uses
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.worker_lifecycle.authority_store import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    LocalAuthorityBinding,
    register_worker_incarnation,
    terminate_worker_incarnation,
)

_LOCAL_BINDING = LocalAuthorityBinding(
    unit="kdive-worker@1.service",
    generation="generation-1",
    boot_id="boot-123",
    invocation_id="invocation-987",
    host="host-a",
)


def _ctx(*, operator: bool) -> RequestContext:
    return RequestContext(
        principal="operator-1",
        agent_session="session-1",
        projects=("proj",),
        roles={"proj": Role.VIEWER},
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


async def _seed(pool: AsyncConnectionPool, *, terminated: bool = False) -> tuple[UUID, str]:
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
            _LOCAL_BINDING,
            hashlib.sha256(holder.encode()).digest(),
            CURRENT_WORKER_FENCE_PROTOCOL,
        )
    if terminated:
        async with pool.connection() as conn:
            await terminate_worker_incarnation(
                conn,
                holder,
                "local",
                _LOCAL_BINDING,
                "failed",
            )
    return use_id, holder


def test_recover_build_use_requires_operator_and_independent_death_proof(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            denied = await build_uses.recover_build_use(
                pool,
                _ctx(operator=False),
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert denied.error_category == "authorization_denied"

            no_proof = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert no_proof.error_category == "configuration_error"

            async with pool.connection() as conn:
                await terminate_worker_incarnation(
                    conn,
                    holder,
                    "local",
                    _LOCAL_BINDING,
                    "failed",
                )

            async with pool.connection() as conn:
                expected_use = await (
                    await conn.execute(
                        "SELECT investigation_id, generation, job_id, attempt, holder_worker_id "
                        "FROM investigation_build_uses WHERE use_id = %s",
                        (use_id,),
                    )
                ).fetchone()
                expected_termination = await (
                    await conn.execute(
                        "SELECT authority_kind, authority_binding, outcome, terminated_at "
                        "FROM worker_incarnations WHERE incarnation = %s",
                        (holder,),
                    )
                ).fetchone()
                assert expected_use is not None
                assert expected_termination is not None

            recovered = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                use_id=use_id,
                holder=holder,
                reason="worker host was replaced",
            )
            assert recovered.status == "recovered"
            assert recovered.suggested_next_actions == ["ops.build_uses_list"]
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
                        "SELECT use_id, project, investigation_id, generation, job_id, attempt, "
                        "holder_worker_id, recovered_by, evidence, reason, authority_kind, "
                        "authority_binding, termination_outcome, terminated_at, recovered_at "
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
            assert ledger is not None
            assert ledger[:-1] == (
                use_id,
                "proj",
                *expected_use,
                "operator-1",
                "local: durable exact-incarnation termination (failed)",
                "worker host was replaced",
                *expected_termination,
            )
            assert ledger[-1] >= expected_termination[-1]
            assert audit_count == 2  # missing durable evidence and atomic successful recovery

    asyncio.run(_run())


def test_recover_build_use_audits_every_authorized_refusal(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            use_id, holder = await _seed(pool)
            attempts = (
                ("", holder, "invalid_input"),
                ("dead", holder, "use_or_holder_mismatch"),
                ("dead", holder + "-wrong", "use_or_holder_mismatch"),
            )
            expected_scopes: set[str] = set()
            for reason, attempted_holder, outcome in attempts:
                response = await build_uses.recover_build_use(
                    pool,
                    _ctx(operator=True),
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
                use_id=use_id,
                holder=holder + "-wrong",
                reason="dead",
            )
            assert mismatch.error_category == "configuration_error"
            too_long = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                use_id=use_id,
                holder=holder,
                reason="x" * 513,
            )
            assert too_long.error_category == "configuration_error"
            multibyte_holder = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                use_id=use_id,
                holder="é" * 300,
                reason="dead",
            )
            assert multibyte_holder.error_category == "configuration_error"
            multibyte_reason = await build_uses.recover_build_use(
                pool,
                _ctx(operator=True),
                use_id=use_id,
                holder=holder,
                reason="é" * 300,
            )
            assert multibyte_reason.error_category == "configuration_error"

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


def test_list_build_uses_rejects_invalid_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            cursors = (
                "not-a-cursor",
                "",
                encode_ts_uuid_cursor("another.list", datetime.now(UTC), uuid4()),
            )
            for cursor in cursors:
                invalid = await build_uses.list_build_uses(
                    pool, _ctx(operator=True), limit=1, cursor=cursor
                )
                assert invalid.status == "error"
                assert invalid.error_category == "configuration_error"
                assert invalid.data["reason"] == "invalid_cursor"

    asyncio.run(_run())

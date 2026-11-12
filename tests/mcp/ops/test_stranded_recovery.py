"""`ops.recover_stranded_activation`: observe, then settle, audited (ADR-0816 amendment, #3551)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.external_boot_activations import SettlementStatus
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops.security import breakglass
from kdive.security.authz.rbac import PlatformRole
from kdive.services.external_boot import settlement
from tests.mcp import systems_support
from tests.services.external_boot.conftest import seed_activation


def _ctx(role: PlatformRole) -> RequestContext:
    return RequestContext(
        principal="ops-admin",
        agent_session="sess-admin",
        projects=(),
        roles={},
        platform_roles=frozenset({role}),
    )


_ADMIN = _ctx(PlatformRole.PLATFORM_ADMIN)


async def _preparing(pool: AsyncConnectionPool) -> tuple[UUID, UUID]:
    async with pool.connection() as conn:
        seeded = await seed_activation(conn, state=ExternalBootActivationState.PREPARING)
    return seeded.activation.id, seeded.system_id


async def _recover(
    pool: AsyncConnectionPool,
    activation_id: UUID | str,
    step: str,
    *,
    ctx: RequestContext = _ADMIN,
    reason: str = "stranded by #3138",
) -> ToolResponse:
    return await breakglass.recover_stranded_activation(
        pool, ctx, activation_id=str(activation_id), step=step, reason=reason
    )


async def _scopes(pool: AsyncConnectionPool, activation_id: UUID) -> list[str]:
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT scope FROM platform_audit_log WHERE scope LIKE %s ORDER BY ts",
            (f"%{activation_id}",),
        )
        return [row[0] for row in await cursor.fetchall()]


async def _finish_observation(
    pool: AsyncConnectionPool, activation_id: UUID, state: str, context: dict[str, Any]
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE jobs SET state = %s, failure_context = %s WHERE dedup_key = %s",
            (state, Jsonb(context), f"stranded-observation:{activation_id}"),
        )


def test_a_lesser_platform_role_is_denied_and_audited(migrated_url: str) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, _ = await _preparing(pool)
            response = await _recover(
                pool, activation_id, "observe", ctx=_ctx(PlatformRole.PLATFORM_OPERATOR)
            )
            assert response.error_category == "authorization_denied"
            assert await _scopes(pool, activation_id) == [f"denied:{activation_id}"]

    asyncio.run(body())


@pytest.mark.parametrize(
    ("activation_id", "step", "reason"),
    [
        ("not-a-uuid", "observe", "r"),
        ("00000000-0000-0000-0000-000000000001", "observe", "  "),
        ("00000000-0000-0000-0000-000000000001", "settle", "x" * 1025),
        ("00000000-0000-0000-0000-000000000001", "teardown", "r"),
    ],
)
def test_malformed_input_is_a_configuration_error(
    migrated_url: str, activation_id: str, step: str, reason: str
) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            response = await _recover(pool, activation_id, step, reason=reason)
        assert response.error_category == "configuration_error"

    asyncio.run(body())


def test_observe_is_audited_and_settle_reports_the_pending_job(migrated_url: str) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, _ = await _preparing(pool)
            observed = await _recover(pool, activation_id, "observe")
            pending = await _recover(pool, activation_id, "settle")
            scopes = await _scopes(pool, activation_id)
        assert observed.status == "observation_requested"
        assert observed.data["job_state"] == "queued"
        assert pending.status == "error"
        assert pending.data["reason"] == "observation_pending"
        assert pending.data["job_id"] == observed.data["job_id"]
        assert scopes == [
            f"observe:{activation_id}",
            f"refused:observation_pending:{activation_id}",
        ]

    asyncio.run(body())


def test_settle_before_any_observation_refuses_and_audits(migrated_url: str) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, _ = await _preparing(pool)
            response = await _recover(pool, activation_id, "settle")
            scopes = await _scopes(pool, activation_id)
        assert response.data["reason"] == "observation_missing"
        assert scopes == [f"refused:observation_missing:{activation_id}"]

    asyncio.run(body())


@pytest.mark.parametrize(
    ("context", "reason"),
    [
        (
            {
                "failure_detail_reason": "authority_objects_present",
                "failure_message": "the authority still holds held/kernel",
            },
            "authority_objects_present",
        ),
        (
            {"failure_detail_reason": "stranded_observation_unsupported"},
            "stranded_observation_unsupported",
        ),
        ({}, "observation_failed"),
    ],
)
def test_a_failed_observation_is_returned_unchanged_and_never_settles(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch, context: dict[str, Any], reason: str
) -> None:
    calls: list[UUID] = []

    async def settle(_conn: object, _ctx: object, *, activation_id: UUID, **_: object) -> None:
        calls.append(activation_id)

    monkeypatch.setattr(settlement, "settle_stranded_activation", settle)

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, _ = await _preparing(pool)
            await _recover(pool, activation_id, "observe")
            await _finish_observation(pool, activation_id, "failed", context)
            response = await _recover(pool, activation_id, "settle")
            scopes = await _scopes(pool, activation_id)
        assert response.data["reason"] == reason
        assert context.get("failure_message", "no reason recorded") in str(response.detail)
        assert scopes[-1] == f"refused:{reason}:{activation_id}"
        assert calls == []

    asyncio.run(body())


def test_a_primitive_refusal_is_returned_unchanged_and_audited(migrated_url: str) -> None:
    """The real settlement function refuses: neither owner recorded any evidence."""

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, _ = await _preparing(pool)
            await _recover(pool, activation_id, "observe")
            await _finish_observation(pool, activation_id, "succeeded", {})
            response = await _recover(pool, activation_id, "settle")
            scopes = await _scopes(pool, activation_id)
        assert response.data["reason"] == "authority_evidence_missing"
        assert scopes[-1] == f"refused:authority_evidence_missing:{activation_id}"

    asyncio.run(body())


def test_a_settlement_names_the_system_and_adds_no_tool_audit_row(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []

    async def settle(_conn: object, _ctx: object, **values: object) -> SettlementStatus:
        calls.append(values)
        return SettlementStatus.APPLIED

    monkeypatch.setattr(settlement, "settle_stranded_activation", settle)

    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            activation_id, system_id = await _preparing(pool)
            await _recover(pool, activation_id, "observe")
            await _finish_observation(pool, activation_id, "succeeded", {})
            response = await _recover(pool, activation_id, "settle", reason="  stranded  ")
            scopes = await _scopes(pool, activation_id)
        assert response.status == "settled"
        assert response.data["system_id"] == str(system_id)
        assert response.suggested_next_actions[0] == "ops.force_teardown"
        assert calls == [
            {"activation_id": activation_id, "actor": calls[0]["actor"], "reason": "stranded"}
        ]
        assert scopes == [f"observe:{activation_id}"]

    asyncio.run(body())


def test_an_observe_refusal_is_audited_with_its_reason(migrated_url: str) -> None:
    async def body() -> None:
        async with systems_support.pool(migrated_url) as pool:
            async with pool.connection() as conn:
                seeded = await seed_activation(conn, state=ExternalBootActivationState.ACTIVE)
            activation_id = seeded.activation.id
            response = await _recover(pool, activation_id, "observe")
            scopes = await _scopes(pool, activation_id)
        assert response.data["reason"] == "not_preparing"
        assert scopes == [
            f"observe:{activation_id}",
            f"refused:not_preparing:{activation_id}",
        ]

    asyncio.run(body())

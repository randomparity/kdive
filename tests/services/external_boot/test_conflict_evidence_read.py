"""``systems.get`` exposes the recorded conflict evidence to an admin (ADR-0807, #3331).

The block carries the composite state ``systems.resolve_external_boot_conflict`` requires as
``observed_identity``. It is present only while the current recovery attempt is in conflict and
only for a caller holding admin on the System's project; a lower role sees the unchanged read.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.systems.conflict_evidence import MAX_CONFLICT_OBJECTS
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.reconciler.conftest import connect, seed_run, seed_system
from tests.services.external_boot.conftest import seed_activation

_STATE = ExternalBootActivationState
_BLOCK = "external_boot_conflict"
_RESOLVER = provider_resolver()
_SEEDED_DIGEST = "sha256:" + "b" * 64


def _ctx(role: Role = Role.ADMIN, *, projects: tuple[str, ...] = ("proj",)) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session="s",
        projects=projects,
        roles={project: role for project in projects},
    )


async def _seed(
    conn: psycopg.AsyncConnection, state: ExternalBootActivationState | None
) -> tuple[UUID, UUID | None]:
    """A System with a Run, restricted by an activation in ``state`` unless it is None."""
    system_id = await seed_system(conn)
    run_id = await seed_run(conn, system_id)
    if state is None:
        return system_id, None
    async with conn.transaction():
        seeded = await seed_activation(conn, state=state, system_id=system_id, run_id=run_id)
    return system_id, seeded.activation.id


async def _record_objects(conn: psycopg.AsyncConnection, activation_id: UUID, count: int) -> None:
    refs = [{"ref": f"objects/o{index:03d}"} for index in range(count)]
    await conn.execute(
        "UPDATE external_boot_recovery_attempts "
        "SET conflict_evidence = jsonb_set(conflict_evidence, '{objects}', %s) "
        "WHERE activation_id = %s",
        (Jsonb(refs), activation_id),
    )


def _read(
    url: str,
    state: ExternalBootActivationState | None,
    ctx: RequestContext,
    *,
    objects: int = 0,
) -> ToolResponse:
    async def _run() -> ToolResponse:
        conn = await connect(url)
        try:
            system_id, activation_id = await _seed(conn, state)
            if activation_id is not None and objects:
                await _record_objects(conn, activation_id, objects)
            async with runs_support.pool(url) as pool:
                return await get_system(pool, ctx, str(system_id), resolver=_RESOLVER)
        finally:
            await conn.close()

    return asyncio.run(_run())


def _run_with[T](url: str, body: Callable[[psycopg.AsyncConnection], Awaitable[T]]) -> T:
    async def _run() -> T:
        conn = await connect(url)
        try:
            return await body(conn)
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_admin_reads_the_recorded_conflict_evidence(migrated_url: str) -> None:
    response = _read(migrated_url, _STATE.RECOVERY_CONFLICT, _ctx(), objects=2)

    block = response.data[_BLOCK]
    assert isinstance(block, dict)
    assert block["composite_state"] == _SEEDED_DIGEST
    assert block["objects"] == ["objects/o000", "objects/o001"]
    assert block["objects_truncated"] is False
    assert UUID(str(block["observation_id"]))
    assert UUID(str(block["activation_id"]))
    assert UUID(str(block["attempt_id"]))
    assert str(block["observed_at"]).startswith("2026-08-28")
    assert "systems.resolve_external_boot_conflict" in response.suggested_next_actions


def test_objects_are_capped_and_flagged(migrated_url: str) -> None:
    response = _read(
        migrated_url, _STATE.RECOVERY_CONFLICT, _ctx(), objects=MAX_CONFLICT_OBJECTS + 6
    )

    block = response.data[_BLOCK]
    assert isinstance(block, dict)
    assert block["objects"] == [f"objects/o{index:03d}" for index in range(MAX_CONFLICT_OBJECTS)]
    assert block["objects_truncated"] is True


def test_lower_roles_see_no_block_and_no_resolve_hint(migrated_url: str) -> None:
    for role in (Role.VIEWER, Role.CONTRIBUTOR, Role.OPERATOR):
        response = _read(migrated_url, _STATE.RECOVERY_CONFLICT, _ctx(role), objects=1)

        assert _BLOCK not in response.data, role
        assert "systems.resolve_external_boot_conflict" not in response.suggested_next_actions


def test_a_foreign_project_caller_gets_not_found(migrated_url: str) -> None:
    response = _read(migrated_url, _STATE.RECOVERY_CONFLICT, _ctx(projects=("other",)))

    assert response.error_category == "not_found"
    assert _BLOCK not in response.data


def test_no_block_unless_the_current_attempt_is_in_conflict(migrated_url: str) -> None:
    for state in (
        None,
        _STATE.ACTIVE,
        _STATE.RECOVERING,
        _STATE.RECOVERY_FAILED,
        _STATE.RECOVERED,
    ):
        response = _read(migrated_url, state, _ctx())

        assert _BLOCK not in response.data, state
        assert "systems.resolve_external_boot_conflict" not in response.suggested_next_actions


def test_a_conflict_that_is_no_longer_current_is_not_reported(migrated_url: str) -> None:
    async def _body(conn: psycopg.AsyncConnection) -> ToolResponse:
        system_id, activation_id = await _seed(conn, _STATE.RECOVERY_CONFLICT)
        assert activation_id is not None
        terminal = {
            "schema": "external-boot-terminal-evidence-v1",
            "activation_id": str(activation_id),
            "system_id": str(system_id),
            "outcome": "recovery_failed",
            "composite_state": _SEEDED_DIGEST,
            "objects": [],
            "observed_at": "2026-08-28T00:00:00Z",
        }
        await conn.execute(
            "UPDATE external_boot_recovery_attempts "
            "SET state = 'failed', conflict_evidence = NULL, terminal_evidence = %s "
            "WHERE activation_id = %s",
            (Jsonb(terminal), activation_id),
        )
        async with runs_support.pool(migrated_url) as pool:
            return await get_system(pool, _ctx(), str(system_id), resolver=_RESOLVER)

    assert _BLOCK not in _run_with(migrated_url, _body).data

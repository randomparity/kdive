"""The three external-boot recovery contract services (ADR-0583, #2117).

Admission rejects invalid inputs and missing provider configuration without changing activation
state. Configured paths enqueue durable recovery jobs; connected worker/authority execution is
covered in the sibling release, conflict, and orphan suites.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, LiteralString
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.domain.capacity.state import DebugSessionState, ExternalBootActivationState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.external_boot import recovery_requests
from kdive.mcp.tools.external_boot.recovery_idempotency import recovery_request
from kdive.mcp.tools.external_boot.recovery_requests import (
    request_release,
    resolve_conflict,
    resolve_recovery_orphan,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role, RoleDenied
from kdive.serialization import _MAX_ERROR_ENTRIES
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import provider_resolver
from tests.reconciler.conftest import connect, seed_debug_session, seed_run, seed_system
from tests.services.external_boot.conftest import seed_activation

_STATE = ExternalBootActivationState
_DIGEST = "sha256:" + "b" * 64
_RESOLUTION = "restore-recorded-source"
_UNAVAILABLE = "recovery_provider_not_configured"
_ACTIVE_ACTIONS = ["runs.get", "runs.release_external_boot", "systems.teardown"]
_CONFLICT_ACTIONS = ["runs.get", "systems.teardown"]
_AUTHORIZING = {"principal": "alice", "agent_session": None, "project": "proj"}
_CAP = _MAX_ERROR_ENTRIES
_RESOLVER = provider_resolver(external_boot=object())

# Every name that begins, advances, or finishes an external-boot activation transition, plus the
# authority marker and the job enqueue such a transition would need. The amendment's hard rule is
# that this module reaches none of them; `test_no_activation_writing_name_is_reachable` is the
# static half of that, beside the behavioral one below.
_ACTIVATION_WRITING_NAMES = frozenset(
    {
        "ExternalBootAuthorityMarkerV1",
        "begin_recovery_attempt",
        "create",
        "finish_recovery_attempt",
        "mark_cleanup_complete",
        "record_conflict",
        "release_reservation",
        "transition",
    }
)


def _ctx(
    role: Role = Role.ADMIN,
    *,
    projects: tuple[str, ...] = ("proj",),
    platform: frozenset[PlatformRole] = frozenset(),
) -> RequestContext:
    return RequestContext(
        principal="alice",
        agent_session="s",
        projects=projects,
        roles={project: role for project in projects},
        platform_roles=platform,
    )


def _platform_ctx() -> RequestContext:
    return _ctx(platform=frozenset({PlatformRole.PLATFORM_ADMIN}))


@dataclass(frozen=True, slots=True)
class _Seeded:
    """A System, a Run bound to it, and the activation restricting it."""

    system_id: UUID
    run_id: UUID
    activation_id: UUID | None


@dataclass(frozen=True, slots=True)
class _Fixture:
    """One migrated database reachable both as a seeding connection and as a service pool."""

    conn: psycopg.AsyncConnection
    pool: AsyncConnectionPool


@asynccontextmanager
async def _fixture(url: str) -> AsyncIterator[_Fixture]:
    conn = await connect(url)
    try:
        async with runs_support.pool(url) as conn_pool:
            yield _Fixture(conn=conn, pool=conn_pool)
    finally:
        await conn.close()


async def _seed(
    conn: psycopg.AsyncConnection,
    *,
    state: ExternalBootActivationState | None = _STATE.ACTIVE,
) -> _Seeded:
    """Seed a System with a bound Run, restricted by an activation unless ``state`` is None.

    The activation and its recovery attempt are inserted in one transaction: the
    ``external_boot_activation_current_attempt_fk`` back-reference is ``INITIALLY DEFERRED``,
    so on this autocommit connection the two inserts must share a transaction to satisfy it.
    """
    system_id = await seed_system(conn)
    run_id = await seed_run(conn, system_id)
    if state is None:
        return _Seeded(system_id=system_id, run_id=run_id, activation_id=None)
    async with conn.transaction():
        seeded = await seed_activation(conn, state=state, system_id=system_id, run_id=run_id)
    return _Seeded(system_id=system_id, run_id=run_id, activation_id=seeded.activation.id)


async def _seed_queued_job(
    conn: psycopg.AsyncConnection, *, kind: str, payload: dict[str, str]
) -> UUID:
    cur = await conn.execute(
        "INSERT INTO jobs (kind, payload, state, max_attempts, authorizing, dedup_key) "
        "VALUES (%s, %s, 'queued', 3, %s, %s) RETURNING id",
        (kind, Jsonb(payload), Jsonb(_AUTHORIZING), str(uuid4())),
    )
    row = await cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


async def _seed_retired_conflict_authority(
    conn: psycopg.AsyncConnection, seeded: _Seeded, *, authority_instance: str = "authority-a"
) -> None:
    """Persist the server-owned binding from which conflict admission must dispatch."""
    assert seeded.activation_id is not None
    job_id = await _seed_queued_job(conn, kind="boot", payload={"run_id": str(seeded.run_id)})
    worker = f"worker-{uuid4()}"
    await conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
        "VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
        (worker, b"1" * 32),
    )
    cur = await conn.execute(
        "SELECT s.allocation_id, e.plan_identity FROM systems s "
        "JOIN allocations a ON a.id = s.allocation_id "
        "JOIN external_boot_activations e ON e.system_id = s.id "
        "WHERE s.id = %s AND e.id = %s",
        (seeded.system_id, seeded.activation_id),
    )
    row = await cur.fetchone()
    assert row is not None
    await conn.execute(
        "INSERT INTO external_boot_authorities "
        "(system_id, allocation_id, activation_id, run_id, plan_identity, job_id, job_attempt, "
        "purpose, provider_kind, authority_instance, worker_incarnation, operation, "
        "operation_identity, operation_digest, generation, state, acknowledged_at, retired_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, 1, 'recover', 'local-libvirt', %s, %s, "
        "'recover', %s, %s, 1, 'retired', now(), now())",
        (
            seeded.system_id,
            row[0],
            seeded.activation_id,
            seeded.run_id,
            row[1],
            job_id,
            authority_instance,
            worker,
            "prior-recovery",
            "sha256:" + "2" * 64,
        ),
    )


async def _activation_row(
    conn: psycopg.AsyncConnection, activation_id: UUID
) -> dict[str, Any] | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM external_boot_activations WHERE id = %s", (activation_id,))
        return await cur.fetchone()


_ATTEMPTS_SQL: LiteralString = (
    "SELECT count(*) FROM external_boot_recovery_attempts WHERE activation_id = %s"
)
_SYSTEM_JOBS_SQL: LiteralString = (
    "SELECT count(*) FROM jobs j LEFT JOIN runs r ON r.id::text = j.payload->>'run_id' "
    "WHERE j.payload->>'system_id' = %s OR r.system_id = %s"
)
_PLATFORM_DENIALS_SQL: LiteralString = (
    "SELECT count(*) FROM platform_audit_log WHERE tool = 'ops.resolve_recovery_orphan' "
    "AND scope = %s"
)


async def _count(conn: psycopg.AsyncConnection, query: LiteralString, *params: object) -> int:
    cur = await conn.execute(query, params)
    row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _drive[T](url: str, body: Callable[[_Fixture], Awaitable[T]]) -> T:
    async def _run() -> T:
        async with _fixture(url) as fixture:
            return await body(fixture)

    return asyncio.run(_run())


def _assert_unavailable(response: ToolResponse, object_id: str | None = None) -> None:
    dumped = response.model_dump()
    assert response.error_category == "configuration_error", dumped
    assert response.data["reason"] == _UNAVAILABLE, dumped
    assert response.suggested_next_actions == ["systems.get"], dumped
    if object_id is not None:
        assert response.object_id == object_id, dumped


def _assert_reason(response: ToolResponse, category: str, reason: str) -> None:
    dumped = response.model_dump()
    assert response.error_category == category, dumped
    assert response.data["reason"] == reason, dumped


# --- authorization --------------------------------------------------------------------------


def test_release_denies_a_viewer(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn)
        with pytest.raises(RoleDenied):
            await request_release(fixture.pool, _ctx(Role.VIEWER), run_id=str(seeded.run_id))

    _drive(migrated_url, _body)


def test_conflict_resolution_denies_a_contributor(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        with pytest.raises(RoleDenied):
            await resolve_conflict(
                fixture.pool,
                _ctx(Role.CONTRIBUTOR),
                resolver=_RESOLVER,
                system_id=str(seeded.system_id),
                operation=_RESOLUTION,
                observed_identity=_DIGEST,
            )

    _drive(migrated_url, _body)


def test_orphan_repair_denies_a_caller_without_the_platform_role(migrated_url: str) -> None:
    """`require_platform_role` raises the bare error no middleware envelopes, so it is caught."""

    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        return await resolve_recovery_orphan(
            fixture.pool,
            _ctx(Role.ADMIN),
            system_id=str(seeded.system_id),
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )

    response = _drive(migrated_url, _body)
    assert response.error_category == "authorization_denied", response.model_dump()
    assert response.data["missing_roles"] == [PlatformRole.PLATFORM_ADMIN.value]


def test_orphan_repair_denies_a_platform_operator_and_records_the_overreach(
    migrated_url: str,
) -> None:
    """A held-but-insufficient platform role leaves the separation-of-duties audit row."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, int]:
        seeded = await _seed(fixture.conn)
        response = await resolve_recovery_orphan(
            fixture.pool,
            _ctx(platform=frozenset({PlatformRole.PLATFORM_OPERATOR})),
            system_id=str(seeded.system_id),
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )
        return response, await _count(
            fixture.conn, _PLATFORM_DENIALS_SQL, f"denied:{seeded.system_id}"
        )

    response, audited = _drive(migrated_url, _body)
    assert response.error_category == "authorization_denied", response.model_dump()
    assert audited == 1


# --- object resolution ----------------------------------------------------------------------


def test_release_rejects_a_malformed_run_id(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        return await request_release(fixture.pool, _ctx(), run_id="not-a-uuid")

    _assert_reason(_drive(migrated_url, _body), "configuration_error", "invalid_uuid")


def test_release_reports_a_missing_and_a_foreign_run_identically(migrated_url: str) -> None:
    """Neither envelope discloses whether the Run exists or which project holds it."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, ToolResponse]:
        seeded = await _seed(fixture.conn)
        missing = await request_release(fixture.pool, _ctx(), run_id=str(uuid4()))
        foreign = await request_release(
            fixture.pool, _ctx(projects=("other",)), run_id=str(seeded.run_id)
        )
        return missing, foreign

    missing, foreign = _drive(migrated_url, _body)
    # `not_found`, not `configuration_error`: domain/errors.py reserves the latter for a
    # malformed id. Both cases stay byte-identical, which is what the envelope is for.
    _assert_reason(missing, "not_found", "unresolved_run")
    _assert_reason(foreign, "not_found", "unresolved_run")
    assert missing.detail == foreign.detail
    assert missing.data == foreign.data


def test_release_rejects_an_unbound_run(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        system_id = await seed_system(fixture.conn)
        run_id = await seed_run(fixture.conn, system_id)
        await fixture.conn.execute("UPDATE runs SET system_id = NULL WHERE id = %s", (run_id,))
        return await request_release(fixture.pool, _ctx(), run_id=str(run_id))

    _assert_reason(_drive(migrated_url, _body), "configuration_error", "run_not_bound")


def test_conflict_resolution_rejects_a_malformed_and_a_missing_system(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, ToolResponse]:
        malformed = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id="not-a-uuid",
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )
        missing = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(uuid4()),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )
        return malformed, missing

    malformed, missing = _drive(migrated_url, _body)
    # The two halves of the errors.py rule, in one test: a malformed id is a caller mistake to
    # fix, an id that resolves to nothing is `not_found` and no retry changes it.
    _assert_reason(malformed, "configuration_error", "invalid_uuid")
    _assert_reason(missing, "not_found", "unresolved_system")


def test_orphan_repair_rejects_a_missing_system(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        return await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(uuid4()),
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )

    _assert_reason(_drive(migrated_url, _body), "not_found", "unresolved_system")


# --- admission ------------------------------------------------------------------------------


def test_release_conflicts_when_no_activation_restricts_the_system(migrated_url: str) -> None:
    """An absent activation is a denial here, which the shared guard cannot express."""

    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=None)
        return await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))

    response = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "no_active_activation")
    assert response.suggested_next_actions == ["runs.get"]


def test_conflict_resolution_conflicts_when_no_activation_restricts_the_system(
    migrated_url: str,
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=None)
        return await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )

    response = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "no_recovery_conflict")
    assert response.suggested_next_actions == ["runs.get"]


@pytest.mark.parametrize(
    ("state", "actions"),
    [
        (_STATE.RECOVERY_CONFLICT, _CONFLICT_ACTIONS),
        (_STATE.RECOVERY_FAILED, _CONFLICT_ACTIONS),
        (_STATE.PREPARING, ["runs.get"]),
    ],
)
def test_release_is_denied_outside_the_active_state(
    migrated_url: str, state: ExternalBootActivationState, actions: list[str]
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=state)
        return await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))

    response = _drive(migrated_url, _body)
    assert response.error_category == "conflict", response.model_dump()
    assert response.data["activation_state"] == state.value
    assert response.suggested_next_actions == actions


def test_release_is_denied_for_a_run_that_does_not_own_the_activation(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        other_run = await seed_run(fixture.conn, seeded.system_id)
        return await request_release(fixture.pool, _ctx(), run_id=str(other_run))

    response = _drive(migrated_url, _body)
    assert response.error_category == "conflict", response.model_dump()
    assert response.suggested_next_actions == _ACTIVE_ACTIONS


def test_conflict_resolution_is_denied_outside_recovery_conflict(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        return await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )

    response = _drive(migrated_url, _body)
    assert response.error_category == "conflict", response.model_dump()
    assert response.data["activation_state"] == _STATE.ACTIVE.value


# --- the release-blocking refusals ADR-0583 names --------------------------------------------


def test_release_refuses_while_a_system_scoped_job_is_active(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, UUID]:
        seeded = await _seed(fixture.conn)
        job_id = await _seed_queued_job(
            fixture.conn, kind="power", payload={"system_id": str(seeded.system_id)}
        )
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, job_id

    response, job_id = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "system_job_active")
    assert response.data["job_ids"] == [str(job_id)]
    # A complete list carries no `truncated` key, which is what makes the capped case below
    # distinguishable from this one.
    assert "truncated" not in response.data


def test_release_caps_the_blocking_job_ids_it_returns(migrated_url: str) -> None:
    """A System can hold more blockers than the envelope should carry."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, set[str]]:
        seeded = await _seed(fixture.conn)
        job_ids = {
            str(
                await _seed_queued_job(
                    fixture.conn, kind="power", payload={"system_id": str(seeded.system_id)}
                )
            )
            for _ in range(_CAP + 1)
        }
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, job_ids

    response, job_ids = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "system_job_active")
    returned = response.data["job_ids"]
    assert isinstance(returned, list)
    assert len(returned) == _CAP
    assert set(returned) < job_ids
    assert response.data["truncated"] is True


def test_release_caps_the_blocking_session_ids_it_returns(migrated_url: str) -> None:
    """The session refusal is bounded by the same helper, on its own list key."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, set[str]]:
        seeded = await _seed(fixture.conn)
        session_ids = set()
        for _ in range(_CAP + 1):
            run_id = await seed_run(fixture.conn, seeded.system_id)
            session_ids.add(
                str(await seed_debug_session(fixture.conn, run_id, state=DebugSessionState.LIVE))
            )
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, session_ids

    response, session_ids = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "debug_session_active")
    returned = response.data["session_ids"]
    assert isinstance(returned, list)
    assert len(returned) == _CAP
    assert set(returned) < session_ids
    assert response.data["truncated"] is True


def test_release_refuses_while_a_job_for_another_run_on_the_system_is_active(
    migrated_url: str,
) -> None:
    """Step 5 is System-scoped: a job owned by a different Run still blocks the release."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, UUID]:
        seeded = await _seed(fixture.conn)
        other_run = await seed_run(fixture.conn, seeded.system_id)
        job_id = await _seed_queued_job(
            fixture.conn, kind="boot", payload={"run_id": str(other_run)}
        )
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, job_id

    response, job_id = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "system_job_active")
    assert response.data["job_ids"] == [str(job_id)]


def test_release_ignores_a_terminal_job_for_the_system(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        job_id = await _seed_queued_job(
            fixture.conn, kind="power", payload={"system_id": str(seeded.system_id)}
        )
        await fixture.conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (job_id,))
        return await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))

    _assert_unavailable(_drive(migrated_url, _body))


def test_release_refuses_while_a_debug_session_is_active(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, UUID]:
        seeded = await _seed(fixture.conn)
        session_id = await seed_debug_session(
            fixture.conn, seeded.run_id, state=DebugSessionState.LIVE
        )
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, session_id

    response, session_id = _drive(migrated_url, _body)
    _assert_reason(response, "conflict", "debug_session_active")
    assert response.data["session_ids"] == [str(session_id)]
    assert "truncated" not in response.data


def test_release_ignores_a_detached_debug_session(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        await seed_debug_session(fixture.conn, seeded.run_id, state=DebugSessionState.DETACHED)
        return await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))

    _assert_unavailable(_drive(migrated_url, _body))


# --- bounded input --------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["", "restore_recorded_source", "adopt-observed-source"])
def test_conflict_resolution_rejects_an_unsupported_operation(
    migrated_url: str, operation: str
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        return await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=operation,
            observed_identity=_DIGEST,
        )

    _assert_reason(
        _drive(migrated_url, _body), "configuration_error", "unsupported_resolution_operation"
    )


@pytest.mark.parametrize(
    "observed_identity", ["", "sha256:" + "z" * 64, "x" * 4096, _DIGEST.upper()]
)
def test_conflict_resolution_rejects_an_out_of_shape_observed_identity(
    migrated_url: str, observed_identity: str
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        return await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity=observed_identity,
        )

    _assert_reason(_drive(migrated_url, _body), "configuration_error", "invalid_observed_identity")


def test_conflict_resolution_enqueues_the_exact_observed_identity_idempotently(
    migrated_url: str,
) -> None:
    """The worker, not MCP admission, compares the exact observation with fresh provider state."""

    async def _body(fixture: _Fixture) -> tuple[ToolResponse, ToolResponse, dict[str, Any]]:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        await _seed_retired_conflict_authority(fixture.conn, seeded)
        first = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity="sha256:" + "f" * 64,
        )
        second = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity="sha256:" + "f" * 64,
        )
        cur = await fixture.conn.execute(
            "SELECT payload FROM jobs WHERE id = %s", (first.object_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return first, second, row[0]

    first, second, payload = _drive(migrated_url, _body)
    assert first.status == second.status == "queued"
    assert first.object_id == second.object_id
    assert payload["external_boot_authority_v1"]["expected_observed_composite"] == (
        "sha256:" + "f" * 64
    )


@pytest.mark.parametrize("disposition", ["", "purge", "Delete"])
def test_orphan_repair_rejects_an_unsupported_disposition(
    migrated_url: str, disposition: str
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        return await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(seeded.system_id),
            object_identities=["objects/orphan-1"],
            disposition=disposition,
        )

    _assert_reason(_drive(migrated_url, _body), "configuration_error", "unsupported_disposition")


@pytest.mark.parametrize(
    "object_identities", [[], ["objects/orphan-1"] * 65, ["x" * 1025], [""], ["ok", ""]]
)
def test_orphan_repair_rejects_out_of_bound_object_identities(
    migrated_url: str, object_identities: list[str]
) -> None:
    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn)
        return await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(seeded.system_id),
            object_identities=object_identities,
            disposition="delete",
        )

    _assert_reason(_drive(migrated_url, _body), "configuration_error", "invalid_object_identities")


# --- the terminal report --------------------------------------------------------------------


def test_release_refuses_without_a_configured_provider(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, str]:
        seeded = await _seed(fixture.conn)
        response = await request_release(fixture.pool, _ctx(), run_id=str(seeded.run_id))
        return response, str(seeded.run_id)

    response, run_id = _drive(migrated_url, _body)
    _assert_unavailable(response, run_id)
    assert "runs.release_external_boot" in (response.detail or "")


def test_conflict_resolution_without_durable_authority_fails_closed(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, str]:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        response = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )
        return response, str(seeded.system_id)

    response, system_id = _drive(migrated_url, _body)
    _assert_reason(response, "configuration_error", "conflict_authority_unresolved")
    assert response.object_id == system_id


@pytest.mark.parametrize("disposition", ["delete", "adopt"])
def test_orphan_repair_refuses_without_a_configured_provider(
    migrated_url: str, disposition: str
) -> None:
    async def _body(fixture: _Fixture) -> tuple[ToolResponse, str]:
        seeded = await _seed(fixture.conn)
        response = await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(seeded.system_id),
            object_identities=["objects/orphan-1", "objects/orphan-2"],
            disposition=disposition,
        )
        return response, str(seeded.system_id)

    response, system_id = _drive(migrated_url, _body)
    _assert_unavailable(response, system_id)
    assert "ops.resolve_recovery_orphan" in (response.detail or "")


def test_orphan_repair_needs_no_activation_to_report(migrated_url: str) -> None:
    """ADR-0583 scopes the orphan tool to quarantined objects, not to the activation."""

    async def _body(fixture: _Fixture) -> ToolResponse:
        seeded = await _seed(fixture.conn, state=None)
        return await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(seeded.system_id),
            object_identities=["objects/orphan-1"],
            disposition="adopt",
        )

    _assert_unavailable(_drive(migrated_url, _body))


# --- the amendment's hard rule ----------------------------------------------------------------


def _reachable_names(source: str, bound: set[str]) -> set[str]:
    """Every name this source could call, plus the names already bound in its namespace.

    Both halves are needed: an activation write can arrive as a bare call (``enqueue(...)``),
    as a method on an object the module holds (``_REPOSITORY.begin_recovery_attempt(...)``), or
    as an import that a later edit only has to call. Attribute names are collected unqualified,
    so no walk of the transitive import graph — which reaches every writer through the
    repository module — is needed or wanted.
    """
    tree = ast.parse(source)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    return names | attributes | bound


def test_no_activation_writing_name_is_reachable() -> None:
    """MCP admission may enqueue but cannot start the worker-owned activation transition.

    A later edit that reintroduces a write fails here at import time rather than needing a
    seeded database to expose it.
    """
    reachable = _reachable_names(inspect.getsource(recovery_requests), set(vars(recovery_requests)))
    assert sorted(reachable & _ACTIVATION_WRITING_NAMES) == []


def test_the_name_gate_bites() -> None:
    """Canary: the gate runs over a clean module, so prove it still detects each name."""
    for name in sorted(_ACTIVATION_WRITING_NAMES):
        source = f"async def f(conn):\n    await _REPOSITORY.{name}(conn)\n"
        called = _reachable_names(source, set())
        assert called & _ACTIVATION_WRITING_NAMES == {name}
        imported = _reachable_names("", {name})
        assert imported & _ACTIVATION_WRITING_NAMES == {name}


def _kdive_imports(source: str) -> set[str]:
    """Every first-party name ``source`` imports, as ``module:name`` (``module:*`` for ``import``).

    Names, not modules: ``kdive.services.external_boot`` is already imported, so a module-level
    allow-list would wave through ``from kdive.services.external_boot import executor`` and the
    delegate could then reach any writer without the writing name ever appearing here.
    """
    imports: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imports.update(
                f"{alias.name}:*" for alias in node.names if alias.name.split(".")[0] == "kdive"
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".")[0] == "kdive":
                imports.update(f"{module}:{alias.name}" for alias in node.names)
    return imports


# The reviewed first-party import closure of `recovery_requests`. Every entry is a name the
# module reaches today; a new one has to be added here, which is where a reviewer sees that a
# transition writer has come within reach.
_REVIEWED_FIRST_PARTY_IMPORTS = frozenset(
    {
        "kdive.db.external_boot_activations:ExternalBootActivationRepository",
        "kdive.db.locks:LockScope",
        "kdive.db.locks:advisory_xact_lock",
        "kdive.db.repositories:RUNS",
        "kdive.db.repositories:SYSTEMS",
        "kdive.domain.capacity.state:JobState",
        "kdive.domain.errors:CategorizedError",
        "kdive.domain.errors:ErrorCategory",
        "kdive.domain.external_boot_activation:Digest",
        "kdive.domain.lifecycle.records:Run",
        "kdive.jobs.handlers.external_boot.admission:build_external_boot_payload",
        "kdive.jobs.payloads:ResolveRecoveryOrphanPayload",
        "kdive.domain.operations.jobs:JobKind",
        "kdive.jobs:queue",
        "kdive.log:bind_context",
        "kdive.mcp.platform_auth:audit_platform_denial",
        "kdive.mcp.responses:ToolResponse",
        "kdive.mcp.tools._common:as_uuid",
        "kdive.mcp.tools._common:authorizing",
        "kdive.mcp.tools._common:external_boot_denial",
        "kdive.mcp.tools._common:invalid_uuid_error",
        "kdive.mcp.tools.external_boot.recovery_idempotency:recovery_request",
        "kdive.mcp.tools.external_boot.recovery_idempotency:recovery_response",
        "kdive.security.authz.context:RequestContext",
        "kdive.security.authz.rbac:AuthorizationError",
        "kdive.security.authz.rbac:PlatformRole",
        "kdive.security.authz.rbac:Role",
        "kdive.security.authz.rbac:require_platform_role",
        "kdive.security.authz.rbac:require_role",
        "kdive.serialization:JsonValue",
        "kdive.serialization:_MAX_ERROR_ENTRIES",
        "kdive.services.debug.sessions:active_session_ids_for_system",
        "kdive.services.external_boot:ExternalBootDenied",
        "kdive.services.external_boot:ExternalBootOperation",
        "kdive.services.external_boot:check_external_boot_admission",
        "kdive.providers.core.resolver:ProviderResolver",
    }
)


def test_the_first_party_import_closure_is_the_reviewed_set() -> None:
    """No tool commits a transition it cannot complete — held as a closure, not a name scan.

    The name scan above is one level deep by construction, so it cannot see a write routed
    through a first-party helper. Pinning the import set is the closure property that can: any
    new ``kdive.*`` name this module reaches fails here until it is reviewed and listed.
    """
    assert _kdive_imports(inspect.getsource(recovery_requests)) == _REVIEWED_FIRST_PARTY_IMPORTS


def test_the_import_closure_gate_bites() -> None:
    """Canary: prove the closure detects the delegate-module escape the name scan misses."""
    escape = "from kdive.services.external_boot import executor\n"
    reached = _kdive_imports(escape)
    assert reached - _REVIEWED_FIRST_PARTY_IMPORTS == {"kdive.services.external_boot:executor"}
    assert _reachable_names(escape, set()) & _ACTIVATION_WRITING_NAMES == set()
    assert _kdive_imports("import kdive.jobs.queue\n") - _REVIEWED_FIRST_PARTY_IMPORTS == {
        "kdive.jobs.queue:*"
    }
    assert _kdive_imports("import os\nfrom collections import abc\n") == set()


def test_conflict_enqueue_leaves_activation_unchanged(migrated_url: str) -> None:
    """Conflict admission durably enqueues without starting the worker-owned transition.

    Both admissible activations are read whole before and after all three services run, so a
    changed ``state``, ``current_attempt_id``, or ``updated_at`` fails here regardless of which
    column a future write touched. Attempt and job rows are compared by count for the two
    Systems, because seeding a ``recovery_conflict`` activation legitimately inserts one attempt.
    """

    async def _body(fixture: _Fixture) -> None:
        conn = fixture.conn
        active = await _seed(conn, state=_STATE.ACTIVE)
        conflicted = await _seed(conn, state=_STATE.RECOVERY_CONFLICT)
        assert active.activation_id is not None and conflicted.activation_id is not None
        await _seed_retired_conflict_authority(conn, conflicted)

        before = (
            await _activation_row(conn, active.activation_id),
            await _activation_row(conn, conflicted.activation_id),
        )
        attempts_before = (
            await _count(conn, _ATTEMPTS_SQL, active.activation_id),
            await _count(conn, _ATTEMPTS_SQL, conflicted.activation_id),
        )
        jobs_before = (
            await _count(conn, _SYSTEM_JOBS_SQL, str(active.system_id), active.system_id),
            await _count(conn, _SYSTEM_JOBS_SQL, str(conflicted.system_id), conflicted.system_id),
        )

        released = await request_release(fixture.pool, _ctx(), run_id=str(active.run_id))
        resolved = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(conflicted.system_id),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
        )
        repaired = await resolve_recovery_orphan(
            fixture.pool,
            _platform_ctx(),
            system_id=str(active.system_id),
            object_identities=["objects/orphan-1"],
            disposition="delete",
        )
        for response in (released, repaired):
            _assert_unavailable(response, response.object_id)
        assert resolved.status == "queued"

        assert (
            await _activation_row(conn, active.activation_id),
            await _activation_row(conn, conflicted.activation_id),
        ) == before
        assert (
            await _count(conn, _ATTEMPTS_SQL, active.activation_id),
            await _count(conn, _ATTEMPTS_SQL, conflicted.activation_id),
        ) == attempts_before
        assert (
            await _count(conn, _SYSTEM_JOBS_SQL, str(active.system_id), active.system_id),
            await _count(conn, _SYSTEM_JOBS_SQL, str(conflicted.system_id), conflicted.system_id),
        ) == (jobs_before[0], jobs_before[1] + 1)

    _drive(migrated_url, _body)


def test_release_enqueues_once_from_retired_exact_authority(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn, state=_STATE.ACTIVE)
        await _seed_retired_conflict_authority(fixture.conn, seeded)
        await fixture.conn.execute("UPDATE jobs SET state = 'succeeded'")

        first = await request_release(
            fixture.pool, _ctx(), run_id=str(seeded.run_id), resolver=_RESOLVER
        )
        second = await request_release(
            fixture.pool, _ctx(), run_id=str(seeded.run_id), resolver=_RESOLVER
        )

        assert first.status == second.status == "queued"
        assert first.object_id == second.object_id
        async with fixture.conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT kind, payload FROM jobs WHERE id = %s", (first.object_id,))
            job = await cur.fetchone()
        assert job is not None
        assert job["kind"] == "boot"
        marker = job["payload"]["external_boot_authority_v1"]
        assert marker["purpose"] == marker["operation"] == "release"
        assert marker["activation_id"] == str(seeded.activation_id)

    _drive(migrated_url, _body)


def test_release_key_replays_after_activation_state_changes(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn, state=_STATE.ACTIVE)
        await _seed_retired_conflict_authority(fixture.conn, seeded)
        await fixture.conn.execute("UPDATE jobs SET state = 'succeeded'")
        first = await request_release(
            fixture.pool,
            _ctx(),
            run_id=str(seeded.run_id),
            resolver=_RESOLVER,
            idempotency_key="release-once",
        )
        assert first.status == "queued"
        deadline = first.data["recovery_readiness_deadline"]
        await fixture.conn.execute(
            "UPDATE external_boot_activations SET state = 'abandoned', "
            "terminal_evidence = jsonb_set(terminal_evidence, '{outcome}', '\"abandoned\"') "
            "WHERE id = %s",
            (seeded.activation_id,),
        )
        second = await request_release(
            fixture.pool,
            _ctx(),
            run_id=str(seeded.run_id),
            resolver=_RESOLVER,
            idempotency_key="release-once",
        )
        assert second.object_id == first.object_id
        assert second.data["recovery_readiness_deadline"] == deadline
        row = await (
            await fixture.conn.execute(
                "SELECT payload->'recovery_request_v1' FROM jobs WHERE id = %s", (first.object_id,)
            )
        ).fetchone()
        assert row is not None and row[0]["readiness_deadline"] == deadline

    _drive(migrated_url, _body)


def test_release_distinct_keys_bind_distinct_authority_operations(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn, state=_STATE.ACTIVE)
        await _seed_retired_conflict_authority(fixture.conn, seeded)
        await fixture.conn.execute("UPDATE jobs SET state = 'succeeded'")
        first = await request_release(
            fixture.pool,
            _ctx(),
            run_id=str(seeded.run_id),
            resolver=_RESOLVER,
            idempotency_key="release-first",
        )
        await fixture.conn.execute(
            "UPDATE jobs SET state = 'succeeded' WHERE id = %s", (first.object_id,)
        )
        second = await request_release(
            fixture.pool,
            _ctx(),
            run_id=str(seeded.run_id),
            resolver=_RESOLVER,
            idempotency_key="release-second",
        )
        rows = await (
            await fixture.conn.execute(
                "SELECT payload->'external_boot_authority_v1'->>'operation_identity' "
                "FROM jobs WHERE id = ANY(%s)",
                ([first.object_id, second.object_id],),
            )
        ).fetchall()
        assert first.object_id != second.object_id
        assert len({row[0] for row in rows}) == 2

    _drive(migrated_url, _body)


def test_omitted_key_is_scoped_to_the_selected_activation(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        first, _, _ = await recovery_request(
            fixture.conn,
            tool="runs.release_external_boot",
            object_key="run_id",
            object_id="run",
            arguments=(),
            idempotency_key=None,
            scope_identity="activation-a",
        )
        second, _, _ = await recovery_request(
            fixture.conn,
            tool="runs.release_external_boot",
            object_key="run_id",
            object_id="run",
            arguments=(),
            idempotency_key=None,
            scope_identity="activation-b",
        )
        assert first != second

    _drive(migrated_url, _body)


def test_conflict_key_refuses_different_observation(migrated_url: str) -> None:
    async def _body(fixture: _Fixture) -> None:
        seeded = await _seed(fixture.conn, state=_STATE.RECOVERY_CONFLICT)
        await _seed_retired_conflict_authority(fixture.conn, seeded)
        first = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity=_DIGEST,
            idempotency_key="resolve-once",
        )
        assert first.status == "queued"
        second = await resolve_conflict(
            fixture.pool,
            _ctx(),
            resolver=_RESOLVER,
            system_id=str(seeded.system_id),
            operation=_RESOLUTION,
            observed_identity="sha256:" + "c" * 64,
            idempotency_key="resolve-once",
        )
        assert second.error_category == "conflict"
        assert second.data["reason"] == "idempotency_key_conflict"

    _drive(migrated_url, _body)

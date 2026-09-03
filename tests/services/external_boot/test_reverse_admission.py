"""Every enforced call site, against a restricting external-boot activation (ADR-0583, #2117).

The matrix itself is covered by ``test_admission.py``; this module proves the guard is actually
reached, through the real handlers, on the connection and inside the lock each site holds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, _lock_key
from kdive.db.repositories import RUNS, SNAPSHOTS, SYSTEMS
from kdive.domain.capacity.state import (
    ExternalBootActivationState,
    RunState,
    SnapshotState,
    SystemState,
)
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Run, Snapshot
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.control.registrar import (
    capture_traffic_system,
    diagnostic_sysrq_system,
    force_crash_system,
    power_system,
    watch_for_crash_system,
)
from kdive.mcp.tools.lifecycle.runs.bind import RunBindRequest, bind_run
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run
from kdive.mcp.tools.lifecycle.runs.steps import boot_run
from kdive.mcp.tools.lifecycle.support._runtime_resolution import with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.lifecycle.systems.snapshot import delete_snapshot, snapshot_system
from kdive.mcp.tools.lifecycle.systems.ssh_access import authorize_ssh_key
from kdive.mcp.tools.lifecycle.vmcore.handlers import VmcoreHandlers
from kdive.providers.ports.handles import SystemHandle, TransportHandle
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.services.debug import lifecycle as debug_lifecycle
from kdive.services.external_boot import ExternalBootDenied
from tests.mcp._seed import seed_run_on_system
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import (
    SYSTEM_ADMIN_HANDLERS,
    provider_resolver,
    provisioning_profile,
)
from tests.services.external_boot.conftest import SeedActivation

_STATE = ExternalBootActivationState
_DT = datetime(2026, 1, 1, tzinfo=UTC)
_ACTIVE_ACTIONS = ["runs.get", "runs.release_external_boot", "systems.teardown"]
_CONFLICT_ACTIONS = ["runs.get", "systems.teardown"]
_GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 agent@kdive"


class _FakeConnector:
    """A Connector whose SSH endpoint is recorded and whose transports are handed out inert."""

    def __init__(self) -> None:
        self.closed: list[str] = []

    def open_transport(self, system: SystemHandle, kind: str) -> TransportHandle:
        del system, kind
        return TransportHandle("handle-1")

    def close_transport(self, handle: TransportHandle) -> None:
        self.closed.append(str(handle))

    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        del system
        return ("127.0.0.1", 22022)


def _ctx(role: Role = Role.ADMIN) -> RequestContext:
    return RequestContext(
        principal="user-1", agent_session="s", projects=("proj",), roles={"proj": role}
    )


def _resolver() -> Any:
    return provider_resolver(connector=_FakeConnector())


def _assert_denied(response: ToolResponse, actions: list[str]) -> None:
    assert response.error_category == "conflict", response.model_dump()
    assert response.suggested_next_actions == actions
    assert response.data["activation_state"] is not None


@dataclass(frozen=True, slots=True)
class _Restricted:
    """A System restricted by an activation its own Run owns."""

    pool: AsyncConnectionPool
    system_id: str
    owning_run_id: str


async def _restrict(
    pool: AsyncConnectionPool,
    seed: SeedActivation,
    system_id: str,
    run_id: str,
    state: ExternalBootActivationState,
) -> None:
    async with pool.connection() as conn:
        await seed(conn, state=state, system_id=UUID(system_id), run_id=UUID(run_id))


def _profile() -> dict[str, Any]:
    """The standard local-libvirt profile, opted in to ``force_crash``.

    ``control.force_crash`` runs the ADR-0028 destructive gate before the admission guard, so
    without the opt-in the gate would answer first and the guard would never be reached.
    """
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["destructive_ops"] = ["force_crash"]
    return profile


async def _ready_system_with_run(pool: AsyncConnectionPool) -> tuple[str, str]:
    system_id = await runs_support.seed_system(pool, provisioning_profile=_profile())
    run_id = await seed_run_on_system(pool, system_id, debuginfo_ref=None, build_id="b")
    return system_id, run_id


async def _restricted_ready_system(
    pool: AsyncConnectionPool,
    seed: SeedActivation,
    *,
    state: ExternalBootActivationState = _STATE.ACTIVE,
) -> _Restricted:
    system_id, run_id = await _ready_system_with_run(pool)
    await _restrict(pool, seed, system_id, run_id, state)
    return _Restricted(pool=pool, system_id=system_id, owning_run_id=run_id)


async def _insert_run(
    pool: AsyncConnectionPool,
    *,
    investigation_id: UUID,
    system_id: str | None,
    state: RunState,
) -> str:
    async with pool.connection() as conn:
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                investigation_id=investigation_id,
                system_id=None if system_id is None else UUID(system_id),
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=state,
                build_profile={},
            ),
        )
    return str(run.id)


async def _mark_installed(pool: AsyncConnectionPool, run_id: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, 'install', 'succeeded')",
            (UUID(run_id),),
        )


async def _set_system_state(pool: AsyncConnectionPool, system_id: str, state: SystemState) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE systems SET state = %s WHERE id = %s", (state.value, UUID(system_id))
        )


async def _snapshot(restricted: _Restricted) -> ToolResponse:
    resolver = _resolver()
    return await with_runtime_for_system(
        restricted.pool,
        resolver,
        _ctx(),
        restricted.system_id,
        lambda runtime: snapshot_system(
            restricted.pool,
            _ctx(),
            runtime,
            system_id=restricted.system_id,
            name="snap-1",
            include_memory=False,
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _delete_snapshot(restricted: _Restricted) -> ToolResponse:
    """Its own case because three tools share `SYSTEM_SNAPSHOT`.

    The forward gate is satisfied by any one of them, so without this the guard at
    `snapshot.py` could be deleted and every closure gate in this change would stay green.
    """
    resolver = _resolver()
    return await with_runtime_for_system(
        restricted.pool,
        resolver,
        _ctx(),
        restricted.system_id,
        lambda runtime: delete_snapshot(
            restricted.pool,
            _ctx(),
            runtime,
            system_id=restricted.system_id,
            name="snap-1",
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _create_other_run(restricted: _Restricted) -> ToolResponse:
    investigation_id = await runs_support.seed_investigation(restricted.pool)
    return await runs_support.create(
        restricted.pool, _ctx(), investigation_id, restricted.system_id
    )


async def _bind_other_run(restricted: _Restricted) -> ToolResponse:
    investigation_id = await runs_support.seed_investigation(restricted.pool)
    run_id = await _insert_run(
        restricted.pool,
        investigation_id=UUID(investigation_id),
        system_id=None,
        state=RunState.CREATED,
    )
    return await bind_run(
        restricted.pool,
        _ctx(),
        RunBindRequest(run_id=run_id, system_id=restricted.system_id),
    )


async def _cancel_other_run(restricted: _Restricted) -> ToolResponse:
    investigation_id = await runs_support.seed_investigation(restricted.pool)
    run_id = await _insert_run(
        restricted.pool,
        investigation_id=UUID(investigation_id),
        system_id=restricted.system_id,
        state=RunState.CREATED,
    )
    return await cancel_run(restricted.pool, _ctx(), run_id)


async def _install_owning_run(restricted: _Restricted) -> ToolResponse:
    return await runs_support.install(restricted.pool, _ctx(), restricted.owning_run_id)


async def _boot_owning_run(restricted: _Restricted) -> ToolResponse:
    await _mark_installed(restricted.pool, restricted.owning_run_id)
    return await boot_run(restricted.pool, _ctx(), restricted.owning_run_id)


async def _power(restricted: _Restricted) -> ToolResponse:
    return await power_system(restricted.pool, _ctx(), system_id=restricted.system_id, action="off")


async def _sysrq(restricted: _Restricted) -> ToolResponse:
    return await diagnostic_sysrq_system(
        restricted.pool,
        _ctx(),
        system_id=restricted.system_id,
        command="show_memory",
        resolver=_resolver(),
    )


async def _watch_for_crash(restricted: _Restricted) -> ToolResponse:
    return await watch_for_crash_system(
        restricted.pool,
        _ctx(),
        system_id=restricted.system_id,
        deadline_s=5.0,
        resolver=_resolver(),
    )


async def _reprovision(restricted: _Restricted) -> ToolResponse:
    return await SYSTEM_ADMIN_HANDLERS.reprovision_system(
        restricted.pool,
        _ctx(),
        system_id=restricted.system_id,
        profile=provisioning_profile(),
    )


async def _authorize_ssh_key(restricted: _Restricted) -> ToolResponse:
    return await authorize_ssh_key(
        restricted.pool, _ctx(), restricted.system_id, _GOOD_KEY, resolver=_resolver()
    )


async def _capture_traffic_other_run(restricted: _Restricted) -> ToolResponse:
    investigation_id = await runs_support.seed_investigation(restricted.pool)
    run_id = await _insert_run(
        restricted.pool,
        investigation_id=UUID(investigation_id),
        system_id=restricted.system_id,
        state=RunState.SUCCEEDED,
    )
    return await capture_traffic_system(
        restricted.pool,
        _ctx(),
        resolver=_resolver(),
        run_id=run_id,
        duration_s=5,
        max_bytes=1048576,
        snaplen=128,
        capture_filter=None,
    )


async def _capture_traffic_owning_run(restricted: _Restricted) -> ToolResponse:
    return await capture_traffic_system(
        restricted.pool,
        _ctx(),
        resolver=_resolver(),
        run_id=restricted.owning_run_id,
        duration_s=5,
        max_bytes=1048576,
        snaplen=128,
        capture_filter=None,
    )


_READY_DENIED: dict[str, Callable[[_Restricted], Awaitable[ToolResponse]]] = {
    "runs.create": _create_other_run,
    "runs.bind": _bind_other_run,
    "runs.cancel": _cancel_other_run,
    "runs.install": _install_owning_run,
    "runs.boot": _boot_owning_run,
    "control.power": _power,
    "control.diagnostic_sysrq": _sysrq,
    "systems.snapshot": _snapshot,
    "systems.delete_snapshot": _delete_snapshot,
    "systems.reprovision": _reprovision,
    "systems.authorize_ssh_key": _authorize_ssh_key,
    "control.capture_traffic": _capture_traffic_other_run,
}


async def _power_keyed(
    pool: AsyncConnectionPool, system_id: str, _run: str, key: str
) -> ToolResponse:
    return await power_system(pool, _ctx(), system_id=system_id, action="off", idempotency_key=key)


async def _force_crash_keyed(
    pool: AsyncConnectionPool, system_id: str, _run: str, key: str
) -> ToolResponse:
    return await force_crash_system(
        pool, _ctx(), system_id=system_id, resolver=_resolver(), idempotency_key=key
    )


async def _sysrq_keyed(
    pool: AsyncConnectionPool, system_id: str, _run: str, key: str
) -> ToolResponse:
    return await diagnostic_sysrq_system(
        pool,
        _ctx(),
        system_id=system_id,
        command="show_memory",
        resolver=_resolver(),
        idempotency_key=key,
    )


async def _watch_keyed(
    pool: AsyncConnectionPool, system_id: str, _run: str, key: str
) -> ToolResponse:
    return await watch_for_crash_system(
        pool,
        _ctx(),
        system_id=system_id,
        deadline_s=5.0,
        resolver=_resolver(),
        idempotency_key=key,
    )


async def _capture_traffic_keyed(
    pool: AsyncConnectionPool, _system: str, run_id: str, key: str
) -> ToolResponse:
    return await capture_traffic_system(
        pool,
        _ctx(),
        resolver=_resolver(),
        run_id=run_id,
        duration_s=5,
        max_bytes=1048576,
        snaplen=128,
        capture_filter=None,
        idempotency_key=key,
    )


async def _vmcore_keyed(
    pool: AsyncConnectionPool, _system: str, run_id: str, key: str
) -> ToolResponse:
    handlers = VmcoreHandlers(_resolver(), SecretRegistry())
    return await handlers.fetch_vmcore(
        pool, _ctx(), run_id=run_id, method="host_dump", idempotency_key=key
    )


async def _crash_the_system(pool: AsyncConnectionPool, system_id: str) -> None:
    await _set_system_state(pool, system_id, SystemState.CRASHED)


@dataclass(frozen=True, slots=True)
class _ReplayCase:
    """A ``keyed_mutation`` tool, a state that denies it, and how to invoke it with a key."""

    state: ExternalBootActivationState
    invoke: Callable[[AsyncConnectionPool, str, str, str], Awaitable[ToolResponse]]
    prepare: Callable[[AsyncConnectionPool, str], Awaitable[None]] | None = None


# `force_crash`, `watch_for_crash`, `capture_traffic` and `vmcore.fetch` are admitted in
# `active`, so their denial needs a state that is not — `recovery_conflict` denies all six.
_REPLAY_CASES: dict[str, _ReplayCase] = {
    "control.capture_traffic": _ReplayCase(_STATE.RECOVERY_CONFLICT, _capture_traffic_keyed),
    "control.diagnostic_sysrq": _ReplayCase(_STATE.ACTIVE, _sysrq_keyed),
    "control.force_crash": _ReplayCase(_STATE.RECOVERY_CONFLICT, _force_crash_keyed),
    "control.power": _ReplayCase(_STATE.ACTIVE, _power_keyed),
    "control.watch_for_crash": _ReplayCase(_STATE.RECOVERY_CONFLICT, _watch_keyed),
    "vmcore.fetch": _ReplayCase(_STATE.RECOVERY_CONFLICT, _vmcore_keyed, _crash_the_system),
}


@pytest.mark.parametrize("tool", sorted(_REPLAY_CASES))
def test_a_keyed_mutation_admitted_before_the_activation_still_replays(
    migrated_url: str, seeded_activation: SeedActivation, tool: str
) -> None:
    """The guard sits inside ``do_work``, behind ``keyed_mutation``'s replay lookup.

    A repeated idempotency key must return the stored envelope of work that was already
    committed — the caller stored the key precisely so it could recover the job id it lost —
    while a *fresh* key on the same restricted System is still denied. The install and boot
    steps carry the same rule; these six are the ``keyed_mutation`` sites.
    """
    case = _REPLAY_CASES[tool]

    async def _run() -> tuple[ToolResponse, ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            if case.prepare is not None:
                await case.prepare(conn_pool, system_id)
            key = f"replay-{uuid4()}"
            first = await case.invoke(conn_pool, system_id, run_id, key)
            await _restrict(conn_pool, seeded_activation, system_id, run_id, case.state)
            replay = await case.invoke(conn_pool, system_id, run_id, key)
            fresh = await case.invoke(conn_pool, system_id, run_id, f"replay-{uuid4()}")
            return first, replay, fresh

    first, replay, fresh = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert replay.error_category is None, replay.model_dump()
    assert replay.model_dump() == first.model_dump()
    denied_actions = (
        _CONFLICT_ACTIONS if case.state is _STATE.RECOVERY_CONFLICT else _ACTIVE_ACTIONS
    )
    _assert_denied(fresh, denied_actions)


@pytest.mark.parametrize("operation", sorted(_READY_DENIED))
def test_a_restricting_activation_denies_every_reverse_operation(
    migrated_url: str, seeded_activation: SeedActivation, operation: str
) -> None:
    async def _run() -> None:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            response = await _READY_DENIED[operation](restricted)
        _assert_denied(response, _ACTIVE_ACTIONS)

    asyncio.run(_run())


def test_traffic_capture_is_admitted_for_the_owning_run(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> ToolResponse:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            return await _capture_traffic_owning_run(restricted)

    capture = asyncio.run(_run())
    assert capture.status == "queued", capture.model_dump()
    assert capture.error_category is None


def test_watch_for_crash_is_admitted_while_active_and_denied_once_recovery_conflicts(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """``SYSTEM_WATCH_CRASH`` is admitted in ``active`` for any caller (it carries no Run)."""

    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            active = await _restricted_ready_system(conn_pool, seeded_activation)
            admitted = await _watch_for_crash(active)
            conflicted = await _restricted_ready_system(
                conn_pool, seeded_activation, state=_STATE.RECOVERY_CONFLICT
            )
            return admitted, await _watch_for_crash(conflicted)

    admitted, denied = asyncio.run(_run())
    assert admitted.status == "queued", admitted.model_dump()
    _assert_denied(denied, _CONFLICT_ACTIONS)


def test_force_crash_is_admitted_while_active_and_denied_once_recovery_conflicts(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        async with runs_support.pool(migrated_url) as conn_pool:
            active = await _restricted_ready_system(conn_pool, seeded_activation)
            admitted = await force_crash_system(
                conn_pool,
                _ctx(),
                system_id=active.system_id,
                resolver=_resolver(),
            )
            conflicted = await _restricted_ready_system(
                conn_pool, seeded_activation, state=_STATE.RECOVERY_CONFLICT
            )
            denied = await force_crash_system(
                conn_pool,
                _ctx(),
                system_id=conflicted.system_id,
                resolver=_resolver(),
            )
        assert admitted.status == "queued", admitted.model_dump()
        _assert_denied(denied, _CONFLICT_ACTIONS)

    asyncio.run(_run())


def test_teardown_is_admitted_in_every_restricted_state(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(
                conn_pool, seeded_activation, state=_STATE.RECOVERY_FAILED
            )
            response = await teardown_system(conn_pool, _ctx(), restricted.system_id)
        assert response.status == "queued", response.model_dump()

    asyncio.run(_run())


def test_vmcore_capture_is_admitted_for_the_owning_run_and_denied_for_another(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            await _set_system_state(conn_pool, restricted.system_id, SystemState.CRASHED)
            investigation_id = await runs_support.seed_investigation(conn_pool)
            other_run = await _insert_run(
                conn_pool,
                investigation_id=UUID(investigation_id),
                system_id=restricted.system_id,
                state=RunState.SUCCEEDED,
            )
            handlers = VmcoreHandlers(_resolver(), SecretRegistry())
            owning = await handlers.fetch_vmcore(
                conn_pool, _ctx(), run_id=restricted.owning_run_id, method="host_dump"
            )
            foreign = await handlers.fetch_vmcore(
                conn_pool, _ctx(), run_id=other_run, method="host_dump"
            )
            return owning, foreign

    owning, foreign = asyncio.run(_run())
    assert owning.status == "queued", owning.model_dump()
    _assert_denied(foreign, _ACTIVE_ACTIONS)


def test_debug_attach_is_denied_for_a_run_that_does_not_own_the_activation(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            investigation_id = await runs_support.seed_investigation(conn_pool)
            other_run = await _insert_run(
                conn_pool,
                investigation_id=UUID(investigation_id),
                system_id=restricted.system_id,
                state=RunState.SUCCEEDED,
            )
            connector = _FakeConnector()
            async with conn_pool.connection() as conn:
                run = await RUNS.get(conn, UUID(other_run))
                system = await SYSTEMS.get(conn, UUID(restricted.system_id))
                assert run is not None and system is not None
                request = debug_lifecycle.AttachRequest(
                    run=run,
                    system=system,
                    session_id=uuid4(),
                    transport="gdbstub",
                    connector=cast(Any, connector),
                )
                with pytest.raises(ExternalBootDenied) as denied:
                    await debug_lifecycle.insert_session_locked(
                        conn, _ctx(), request, TransportHandle("handle-1")
                    )
        assert denied.value.next_actions == _ACTIVE_ACTIONS

    asyncio.run(_run())


def test_debug_attach_is_admitted_for_the_run_that_owns_the_activation(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    async def _run() -> None:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            connector = _FakeConnector()
            async with conn_pool.connection() as conn:
                run = await RUNS.get(conn, UUID(restricted.owning_run_id))
                system = await SYSTEMS.get(conn, UUID(restricted.system_id))
                assert run is not None and system is not None
                request = debug_lifecycle.AttachRequest(
                    run=run,
                    system=system,
                    session_id=uuid4(),
                    transport="gdbstub",
                    connector=cast(Any, connector),
                )
                admitted = await debug_lifecycle.insert_session_locked(
                    conn, _ctx(), request, TransportHandle("handle-1")
                )
                assert isinstance(admitted, debug_lifecycle.AttachAdmitted)
                detached = await debug_lifecycle.detach_locked(
                    conn,
                    _ctx(),
                    admitted.session_id,
                    UUID(restricted.system_id),
                    cast(Any, connector),
                )
                assert isinstance(detached, debug_lifecycle.DetachedSession)

    asyncio.run(_run())


def test_a_non_owning_run_may_detach_in_a_restricting_state(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """Detach is admitted whichever Run owns the session.

    See docs/debt/0006-external-boot-detach-departs-from-adr-0583.md.

    ``runs.release_external_boot`` refuses on any live DebugSession of the System, regardless of
    owning Run. Fencing the detach to the activation's Run would leave a session owned by another
    Run of that System blocking the release with no caller able to clear it.
    """

    async def _run() -> object:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, owning_run = await _ready_system_with_run(conn_pool)
            investigation_id = await runs_support.seed_investigation(conn_pool)
            other_run = await _insert_run(
                conn_pool,
                investigation_id=UUID(investigation_id),
                system_id=system_id,
                state=RunState.SUCCEEDED,
            )
            connector = _FakeConnector()
            async with conn_pool.connection() as conn:
                run = await RUNS.get(conn, UUID(other_run))
                system = await SYSTEMS.get(conn, UUID(system_id))
                assert run is not None and system is not None
                admitted = await debug_lifecycle.insert_session_locked(
                    conn,
                    _ctx(),
                    debug_lifecycle.AttachRequest(
                        run=run,
                        system=system,
                        session_id=uuid4(),
                        transport="gdbstub",
                        connector=cast(Any, connector),
                    ),
                    TransportHandle("handle-1"),
                )
                assert isinstance(admitted, debug_lifecycle.AttachAdmitted)
            # Owned by a different Run than the session, and in a state that admits nothing but
            # teardown and detach.
            await _restrict(
                conn_pool, seeded_activation, system_id, owning_run, _STATE.RECOVERY_CONFLICT
            )
            async with conn_pool.connection() as conn:
                return await debug_lifecycle.detach_locked(
                    conn, _ctx(), admitted.session_id, UUID(system_id), cast(Any, connector)
                )

    assert isinstance(asyncio.run(_run()), debug_lifecycle.DetachedSession)


def test_an_already_terminal_cancel_keeps_its_envelope_under_a_restricting_activation(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """``runs.cancel`` runs the guard only when the cancel has something to transition.

    A retried cancel of an already-``canceled`` Run keeps its idempotent success and a
    ``succeeded`` Run keeps its ``conflict``: neither frees the System, so denying them protects
    nothing. A cancel that would actually transition is still denied.
    """

    async def _run() -> tuple[ToolResponse, ToolResponse, ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, owning_run = await _ready_system_with_run(conn_pool)
            investigation_id = UUID(await runs_support.seed_investigation(conn_pool))
            canceled = await _insert_run(
                conn_pool,
                investigation_id=investigation_id,
                system_id=system_id,
                state=RunState.CREATED,
            )
            first = await cancel_run(conn_pool, _ctx(), canceled)
            succeeded = await _insert_run(
                conn_pool,
                investigation_id=investigation_id,
                system_id=system_id,
                state=RunState.SUCCEEDED,
            )
            await _restrict(conn_pool, seeded_activation, system_id, owning_run, _STATE.ACTIVE)
            live = await _insert_run(
                conn_pool,
                investigation_id=investigation_id,
                system_id=system_id,
                state=RunState.CREATED,
            )
            return (
                first,
                await cancel_run(conn_pool, _ctx(), canceled),
                await cancel_run(conn_pool, _ctx(), succeeded),
                await cancel_run(conn_pool, _ctx(), live),
            )

    first, retry, terminal, denied = asyncio.run(_run())
    assert first.status == "canceled", first.model_dump()
    assert retry.error_category is None, retry.model_dump()
    assert retry.status == "canceled"
    assert terminal.error_category == "conflict", terminal.model_dump()
    assert terminal.data["current_status"] == "succeeded"
    _assert_denied(denied, _ACTIVE_ACTIONS)


async def _boot_jobs(pool: AsyncConnectionPool, run_id: str) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) FROM jobs WHERE dedup_key = %s", (f"{run_id}:boot",)
        )
        row = await cur.fetchone()
    return 0 if row is None else int(row[0])


def test_a_boot_does_not_cross_a_restriction_committed_mid_flight(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """Exactly one side of the commit/admit race proceeds.

    The ordering is forced by a test-only ``pg_advisory_xact_lock`` rather than by timing: the
    writer takes the key, commits the activation that restricts the System, and releases the key
    at commit; the boot blocks on the same key and only then runs. A sleep-based interleave would
    make this a flake generator, so the barrier is the whole ordering mechanism.
    """
    race_key = 0x2117_0583

    async def _run() -> tuple[ToolResponse, int]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            await _mark_installed(conn_pool, run_id)
            barrier_taken = asyncio.Event()

            async def _commit_restriction() -> None:
                writer = await psycopg.AsyncConnection.connect(migrated_url)
                async with writer, writer.transaction():
                    await writer.execute("SELECT pg_advisory_xact_lock(%s)", (race_key,))
                    barrier_taken.set()
                    await seeded_activation(
                        writer,
                        state=_STATE.ACTIVE,
                        system_id=UUID(system_id),
                        run_id=UUID(run_id),
                    )

            async def _boot() -> ToolResponse:
                await barrier_taken.wait()
                async with conn_pool.connection() as conn:
                    # Blocks until the writer's transaction commits, so the guard below reads a
                    # restriction that landed after this request began.
                    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (race_key,))
                return await boot_run(conn_pool, _ctx(), run_id)

            _, response = await asyncio.gather(_commit_restriction(), _boot())
            return response, await _boot_jobs(conn_pool, run_id)

    response, enqueued = asyncio.run(_run())
    _assert_denied(response, _ACTIVE_ACTIONS)
    assert enqueued == 0


_REPLAY_SNAP = "replay-snap"


async def _snapshot_unkeyed(pool: AsyncConnectionPool, system_id: str, _run: str) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        _resolver(),
        _ctx(),
        system_id,
        lambda runtime: snapshot_system(
            pool, _ctx(), runtime, system_id=system_id, name=_REPLAY_SNAP, include_memory=False
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _delete_snapshot_unkeyed(
    pool: AsyncConnectionPool, system_id: str, _run: str
) -> ToolResponse:
    return await with_runtime_for_system(
        pool,
        _resolver(),
        _ctx(),
        system_id,
        lambda runtime: delete_snapshot(
            pool, _ctx(), runtime, system_id=system_id, name=_REPLAY_SNAP
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _reprovision_unkeyed(
    pool: AsyncConnectionPool, system_id: str, _run: str
) -> ToolResponse:
    return await SYSTEM_ADMIN_HANDLERS.reprovision_system(
        pool, _ctx(), system_id=system_id, profile=provisioning_profile()
    )


async def _vmcore_unkeyed(pool: AsyncConnectionPool, _system: str, run_id: str) -> ToolResponse:
    handlers = VmcoreHandlers(_resolver(), SecretRegistry())
    return await handlers.fetch_vmcore(
        pool, _ctx(), run_id=run_id, method="host_dump", idempotency_key=None
    )


async def _seed_available_snapshot(pool: AsyncConnectionPool, system_id: str) -> None:
    async with pool.connection() as conn:
        await SNAPSHOTS.insert(
            conn,
            Snapshot(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                system_id=UUID(system_id),
                name=_REPLAY_SNAP,
                include_memory=False,
                state=SnapshotState.AVAILABLE,
            ),
        )


@dataclass(frozen=True, slots=True)
class _UnkeyedReplayCase:
    """A tool whose replay is its fixed dedup key rather than a stored envelope."""

    state: ExternalBootActivationState
    invoke: Callable[[AsyncConnectionPool, str, str], Awaitable[ToolResponse]]
    prepare: Callable[[AsyncConnectionPool, str], Awaitable[None]] | None = None


# `capture_vmcore` is admitted in `active` for the owning Run, so its denial needs
# `recovery_conflict`; the three `system_snapshot`/`system_reprovision` tools are denied in
# `active`.
_UNKEYED_REPLAY_CASES: dict[str, _UnkeyedReplayCase] = {
    "systems.delete_snapshot": _UnkeyedReplayCase(
        _STATE.ACTIVE, _delete_snapshot_unkeyed, _seed_available_snapshot
    ),
    "systems.reprovision": _UnkeyedReplayCase(_STATE.ACTIVE, _reprovision_unkeyed),
    "systems.snapshot": _UnkeyedReplayCase(_STATE.ACTIVE, _snapshot_unkeyed),
    "vmcore.fetch": _UnkeyedReplayCase(
        _STATE.RECOVERY_CONFLICT, _vmcore_unkeyed, _crash_the_system
    ),
}


@pytest.mark.parametrize("tool", sorted(_UNKEYED_REPLAY_CASES))
def test_an_unkeyed_dedup_replay_survives_an_activation(
    migrated_url: str, seeded_activation: SeedActivation, tool: str
) -> None:
    """The same rule as the keyed sites, on the path that has no stored envelope.

    ``keyed_mutation`` calls ``do_work()`` straight through when ``idempotency_key is None``, so
    on the unkeyed path — the default for every tool here — the replay is the fixed dedup key,
    not a recorded envelope. Putting the guard inside the closure therefore protects nothing
    here; each of these sites has to probe its own dedup key ahead of the guard. Without that, an
    agent polling work it already enqueued is told the operation was denied while its job stays
    queued against the same System and runs.
    """
    case = _UNKEYED_REPLAY_CASES[tool]

    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            if case.prepare is not None:
                await case.prepare(conn_pool, system_id)
            first = await case.invoke(conn_pool, system_id, run_id)
            await _restrict(conn_pool, seeded_activation, system_id, run_id, case.state)
            return first, await case.invoke(conn_pool, system_id, run_id)

    first, replay = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert replay.error_category is None, replay.model_dump()
    assert replay.model_dump() == first.model_dump()


def test_a_cancel_refuses_a_run_bound_after_its_pre_lock_read(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """`runs.cancel` decides its SYSTEM lock and its guard from a read taken before any lock.

    A Run that was unbound at that read and is bound by the time the RUN lock is acquired would
    otherwise take neither the lock nor the guard, and cancel against a restricted System. The
    barrier is a test-only ``pg_advisory_xact_lock`` on the RUN key, not a sleep: the writer
    holds the key, commits the binding, and releases it at commit, so the cancel is guaranteed
    to have read the Run unbound and to see it bound once it gets the lock.
    """

    async def _run() -> tuple[ToolResponse, str | None]:
        async with runs_support.pool(migrated_url) as conn_pool:
            restricted = await _restricted_ready_system(conn_pool, seeded_activation)
            investigation_id = await runs_support.seed_investigation(conn_pool)
            run_id = await _insert_run(
                conn_pool,
                investigation_id=UUID(investigation_id),
                system_id=None,
                state=RunState.CREATED,
            )
            run_key = _lock_key(LockScope.RUN, UUID(run_id))
            barrier_taken = asyncio.Event()

            async def _bind_under_the_barrier() -> None:
                writer = await psycopg.AsyncConnection.connect(migrated_url)
                async with writer, writer.transaction():
                    await writer.execute("SELECT pg_advisory_xact_lock(%s)", (run_key,))
                    barrier_taken.set()
                    # Let the cancel take its pre-lock read of the still-unbound Run, then
                    # commit exactly what `runs.bind` commits.
                    await asyncio.sleep(0)
                    await writer.execute(
                        "UPDATE runs SET system_id = %s WHERE id = %s",
                        (UUID(restricted.system_id), UUID(run_id)),
                    )

            async def _cancel() -> ToolResponse:
                await barrier_taken.wait()
                return await cancel_run(conn_pool, _ctx(), run_id)

            _, response = await asyncio.gather(_bind_under_the_barrier(), _cancel())
            async with conn_pool.connection() as conn:
                cur = await conn.execute("SELECT state FROM runs WHERE id = %s", (UUID(run_id),))
                row = await cur.fetchone()
            return response, None if row is None else str(row[0])

    response, state = asyncio.run(_run())
    assert response.error_category == "conflict", response.model_dump()
    assert response.data["reason"] == "run_binding_changed"
    # The whole point: the Run is not canceled behind the guard's back.
    assert state == RunState.CREATED.value


def test_an_install_admitted_before_the_activation_still_replays(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """Idempotent replay is how an agent polls its own in-flight step job.

    The guard sits behind the ``get_by_dedup_key`` replay check, so a repeat ``runs.install``
    for a job enqueued before the activation appeared gets that job back rather than a
    ``conflict`` it can do nothing about.
    """

    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            first = await runs_support.install(conn_pool, _ctx(), run_id)
            await _restrict(conn_pool, seeded_activation, system_id, run_id, _STATE.ACTIVE)
            return first, await runs_support.install(conn_pool, _ctx(), run_id)

    first, replay = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert replay.error_category is None, replay.model_dump()
    assert replay.object_id == first.object_id


def test_a_boot_admitted_before_the_activation_still_replays(
    migrated_url: str, seeded_activation: SeedActivation
) -> None:
    """The boot path carries the same replay fence, and marks the envelope ``replayed``."""

    async def _run() -> tuple[ToolResponse, ToolResponse, int]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            await _mark_installed(conn_pool, run_id)
            first = await boot_run(conn_pool, _ctx(), run_id)
            await _restrict(conn_pool, seeded_activation, system_id, run_id, _STATE.ACTIVE)
            replay = await boot_run(conn_pool, _ctx(), run_id)
            return first, replay, await _boot_jobs(conn_pool, run_id)

    first, replay, enqueued = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert replay.error_category is None, replay.model_dump()
    assert replay.object_id == first.object_id
    assert replay.data["replayed"] is True
    assert enqueued == 1

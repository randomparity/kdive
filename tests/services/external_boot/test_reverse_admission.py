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
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    delete_snapshot,
    restore_system,
    snapshot_system,
)
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
from tests.services.external_boot.admission_support import GUARDED_TOOLS
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
    committed — the caller stored the key precisely so it could recover the job id it lost.

    What a *fresh* key means then depends on whether the tool's dedup key varies with it, and
    the test decides that from an unrestricted control arm rather than a hand-written list.
    Where the key varies (`control.power`, `control.diagnostic_sysrq`,
    `control.capture_traffic`), a fresh key really does mint a new job, so the matrix must
    decide it. Where the key is fixed (`control.force_crash`, `control.watch_for_crash`,
    `vmcore.fetch`), `queue.enqueue` returns the prior row whatever key was supplied, so a
    denial would refuse a call that commits nothing while its job stays queued and runs — the
    same divergence as denying an unkeyed repeat (#2117 review). An earlier version of this
    test asserted the denial for all six and so pinned the wrong contract at three of them.
    """
    case = _REPLAY_CASES[tool]

    async def _run() -> tuple[ToolResponse, ToolResponse, ToolResponse, bool]:
        async with runs_support.pool(migrated_url) as conn_pool:
            # Control arm: no activation, so a fresh key here shows what the dedup key does.
            plain_system, plain_run = await _ready_system_with_run(conn_pool)
            if case.prepare is not None:
                await case.prepare(conn_pool, plain_system)
            plain_first = await case.invoke(conn_pool, plain_system, plain_run, f"replay-{uuid4()}")
            plain_fresh = await case.invoke(conn_pool, plain_system, plain_run, f"replay-{uuid4()}")
            # `job_envelope` keys the envelope on the job id, so a different `object_id` is a
            # different job — i.e. the fresh key minted one.
            key_varies = plain_fresh.object_id != plain_first.object_id

            system_id, run_id = await _ready_system_with_run(conn_pool)
            if case.prepare is not None:
                await case.prepare(conn_pool, system_id)
            key = f"replay-{uuid4()}"
            first = await case.invoke(conn_pool, system_id, run_id, key)
            await _restrict(conn_pool, seeded_activation, system_id, run_id, case.state)
            replay = await case.invoke(conn_pool, system_id, run_id, key)
            fresh = await case.invoke(conn_pool, system_id, run_id, f"replay-{uuid4()}")
            return first, replay, fresh, key_varies

    first, replay, fresh, key_varies = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert replay.error_category is None, replay.model_dump()
    assert replay.model_dump() == first.model_dump()
    if key_varies:
        denied_actions = (
            _CONFLICT_ACTIONS if case.state is _STATE.RECOVERY_CONFLICT else _ACTIVE_ACTIONS
        )
        _assert_denied(fresh, denied_actions)
    else:
        assert fresh.error_category is None, fresh.model_dump()
        assert fresh.model_dump() == first.model_dump()


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


async def _force_crash(restricted: _Restricted) -> ToolResponse:
    return await force_crash_system(
        restricted.pool, _ctx(), system_id=restricted.system_id, resolver=_resolver()
    )


async def _teardown(restricted: _Restricted) -> ToolResponse:
    return await teardown_system(restricted.pool, _ctx(), restricted.system_id)


async def _vmcore_owning_run(restricted: _Restricted) -> ToolResponse:
    handlers = VmcoreHandlers(_resolver(), SecretRegistry())
    return await handlers.fetch_vmcore(
        restricted.pool, _ctx(), run_id=restricted.owning_run_id, method="host_dump"
    )


async def _restore(restricted: _Restricted) -> ToolResponse:
    return await with_runtime_for_system(
        restricted.pool,
        _resolver(),
        _ctx(),
        restricted.system_id,
        lambda runtime: restore_system(
            restricted.pool,
            _ctx(),
            runtime,
            system_id=restricted.system_id,
            name=_REPLAY_SNAP,
            start_paused=False,
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _snapshot_replay_name(restricted: _Restricted) -> ToolResponse:
    return await with_runtime_for_system(
        restricted.pool,
        _resolver(),
        _ctx(),
        restricted.system_id,
        lambda runtime: snapshot_system(
            restricted.pool,
            _ctx(),
            runtime,
            system_id=restricted.system_id,
            name=_REPLAY_SNAP,
            include_memory=False,
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _delete_snapshot_replay_name(restricted: _Restricted) -> ToolResponse:
    return await with_runtime_for_system(
        restricted.pool,
        _resolver(),
        _ctx(),
        restricted.system_id,
        lambda runtime: delete_snapshot(
            restricted.pool, _ctx(), runtime, system_id=restricted.system_id, name=_REPLAY_SNAP
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _prepare_crashed(restricted: _Restricted) -> None:
    await _crash_the_system(restricted.pool, restricted.system_id)


async def _prepare_installed(restricted: _Restricted) -> None:
    await _mark_installed(restricted.pool, restricted.owning_run_id)


async def _boot_only(restricted: _Restricted) -> ToolResponse:
    """`_boot_owning_run` marks installed itself, which a repeat call cannot do twice."""
    return await boot_run(restricted.pool, _ctx(), restricted.owning_run_id)


async def _prepare_available_snapshot(restricted: _Restricted) -> None:
    await _seed_available_snapshot(restricted.pool, restricted.system_id)


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
class _JobTool:
    """How to invoke a guarded job-enqueuing tool unkeyed, and a state that denies it."""

    state: ExternalBootActivationState
    invoke: Callable[[_Restricted], Awaitable[ToolResponse]]
    prepare: Callable[[_Restricted], Awaitable[None]] | None = None


# Guarded tools that enqueue a job. `force_crash`, `watch_for_crash`, `capture_traffic` and
# `capture_vmcore` are admitted in `active`, so their denial needs `recovery_conflict`.
_JOB_TOOLS: dict[str, _JobTool] = {
    "control.capture_traffic": _JobTool(_STATE.RECOVERY_CONFLICT, _capture_traffic_owning_run),
    "control.diagnostic_sysrq": _JobTool(_STATE.ACTIVE, _sysrq),
    "control.force_crash": _JobTool(_STATE.RECOVERY_CONFLICT, _force_crash),
    "control.power": _JobTool(_STATE.ACTIVE, _power),
    "control.watch_for_crash": _JobTool(_STATE.RECOVERY_CONFLICT, _watch_for_crash),
    "runs.boot": _JobTool(_STATE.ACTIVE, _boot_only, _prepare_installed),
    "runs.install": _JobTool(_STATE.ACTIVE, _install_owning_run),
    "systems.authorize_ssh_key": _JobTool(_STATE.ACTIVE, _authorize_ssh_key),
    "systems.delete_snapshot": _JobTool(
        _STATE.ACTIVE, _delete_snapshot_replay_name, _prepare_available_snapshot
    ),
    "systems.reprovision": _JobTool(_STATE.ACTIVE, _reprovision),
    "systems.restore": _JobTool(_STATE.ACTIVE, _restore, _prepare_available_snapshot),
    "systems.snapshot": _JobTool(_STATE.ACTIVE, _snapshot_replay_name),
    "systems.teardown": _JobTool(_STATE.ACTIVE, _teardown),
    "vmcore.fetch": _JobTool(_STATE.RECOVERY_CONFLICT, _vmcore_owning_run, _prepare_crashed),
}

# Guarded tools that enqueue nothing, so there is no dedup replay for a guard to preempt. Each
# reason is the mechanism, not a restatement: a tool that grows a job path belongs above.
_NO_JOB_PATH: dict[str, str] = {
    "allocations.release": "transitions Allocation state and enqueues no job",
    "debug.end_session": "transitions a DebugSession row; enqueues no job",
    "debug.start_session": "inserts a DebugSession row; enqueues no job",
    "runs.bind": "writes runs.system_id; enqueues no job",
    "runs.cancel": "transitions the Run and cancels an existing job; enqueues none",
    "runs.create": "inserts a Run; the build job is runs.install's",
    "runs.release_external_boot": (
        "returns configuration_error/recovery_executor_unavailable until #2118; enqueues nothing"
    ),
    "systems.resolve_external_boot_conflict": (
        "returns configuration_error/recovery_executor_unavailable until #2118; enqueues nothing"
    ),
}


def test_every_guarded_tool_is_classified_for_the_replay_gate() -> None:
    """Derived from `GUARDED_TOOLS`, so a newly guarded tool cannot skip the gate below.

    The hand-written table this replaced enumerated four tools and silently omitted three that
    had the same defect — `control.force_crash`, `control.watch_for_crash` and
    `systems.authorize_ssh_key` — because a list only covers what someone remembered. This is
    the same inverted-gate technique `test_admission.py` uses to prove the matrix is closed:
    walk the decided set and require every member to be classified, so the omission fails here
    rather than shipping.
    """
    classified = _JOB_TOOLS.keys() | _NO_JOB_PATH.keys()
    assert GUARDED_TOOLS.keys() - classified == set()
    assert classified - GUARDED_TOOLS.keys() == set()
    assert _JOB_TOOLS.keys().isdisjoint(_NO_JOB_PATH)
    assert all(reason.strip() for reason in _NO_JOB_PATH.values())
    # Every name with a recipe must be exercisable by the falsification test below, so a tool
    # cannot be parked in `_NO_JOB_PATH` and also left unexercised.
    assert _NO_JOB_PATH_INVOCATIONS.keys() <= _NO_JOB_PATH.keys()


# The `_NO_JOB_PATH` entries this module can already invoke. The remaining four
# (`debug.start_session`, `debug.end_session`, and the two recovery contracts) rest on their
# stated reason alone: they are exercised elsewhere and reaching them here would need fixtures
# this module does not otherwise build. Partial coverage stated as partial, not as closed.
_NO_JOB_PATH_INVOCATIONS: dict[str, Callable[[_Restricted], Awaitable[ToolResponse]]] = {
    "runs.bind": _bind_other_run,
    "runs.cancel": _cancel_other_run,
    "runs.create": _create_other_run,
}


@pytest.mark.parametrize("tool", sorted(_NO_JOB_PATH_INVOCATIONS))
def test_a_no_job_path_tool_really_enqueues_nothing(migrated_url: str, tool: str) -> None:
    """`_NO_JOB_PATH` is prose, and prose is what the replaced hand-written table was.

    Without this a guarded tool that does enqueue a job can be parked there with any reason and
    escape the replay gate entirely — the same "covers what someone remembered" failure moved one
    level up (#2117 review).
    """

    async def _run() -> tuple[int, int]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            restricted = _Restricted(pool=conn_pool, system_id=system_id, owning_run_id=run_id)
            before = await _job_count(conn_pool)
            await _NO_JOB_PATH_INVOCATIONS[tool](restricted)
            return before, await _job_count(conn_pool)

    before, after = asyncio.run(_run())
    assert after == before


async def _job_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT count(*) FROM jobs")
        row = await cur.fetchone()
    return 0 if row is None else int(row[0])


async def _unkeyed_repeat(
    pool: AsyncConnectionPool,
    case: _JobTool,
    seed: SeedActivation | None,
) -> tuple[ToolResponse, ToolResponse, bool]:
    """Call the tool unkeyed twice, restricting between the calls when ``seed`` is given.

    Also reports whether the *repeat* minted a job row. Envelope inequality is not the same
    question: `runs.boot` replays the same job while flipping `data.replayed`, so comparing
    envelopes classified a real replay as fresh work and skipped its assertion (#2117 review).
    """
    system_id, run_id = await _ready_system_with_run(pool)
    restricted = _Restricted(pool=pool, system_id=system_id, owning_run_id=run_id)
    if case.prepare is not None:
        await case.prepare(restricted)
    first = await case.invoke(restricted)
    if seed is not None:
        await _restrict(pool, seed, system_id, run_id, case.state)
    before = await _job_count(pool)
    second = await case.invoke(restricted)
    return first, second, await _job_count(pool) > before


@pytest.mark.parametrize("tool", sorted(_JOB_TOOLS))
def test_an_unkeyed_repeat_that_replays_still_replays_under_an_activation(
    migrated_url: str, seeded_activation: SeedActivation, tool: str
) -> None:
    """Whatever an unkeyed repeat does without an activation, it must do with one.

    The property is differential rather than declared: the unrestricted arm decides whether this
    tool's repeat is a replay, so nothing here hand-asserts which dedup keys are stable. A tool
    that mixes a `uuid4()` into its key (`control.power`, `control.diagnostic_sysrq`,
    `control.capture_traffic`) enqueues fresh work on the repeat, is correctly denied, and this
    gate says nothing about it. A tool whose key is stable replays, and that replay must survive
    the activation — `keyed_mutation` short-circuits to `do_work()` when `idempotency_key is
    None`, so on that path the dedup key is the only replay there is and a guard ahead of it
    turns an agent's poll into a refusal while the job it is polling stays queued and runs.
    """
    case = _JOB_TOOLS[tool]

    async def _run() -> tuple[ToolResponse, ToolResponse, bool, ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            plain_first, plain_second, plain_minted = await _unkeyed_repeat(conn_pool, case, None)
            held_first, held_second, _ = await _unkeyed_repeat(conn_pool, case, seeded_activation)
            return plain_first, plain_second, plain_minted, held_first, held_second

    plain_first, plain_second, plain_minted, held_first, held_second = asyncio.run(_run())
    assert plain_first.status == "queued", plain_first.model_dump()
    if plain_minted:
        # The repeat enqueued fresh work, which the matrix is entitled to deny; the denial
        # itself is `test_a_restricting_activation_denies_every_reverse_operation`'s.
        return
    if plain_second.error_category is not None:
        # The repeat is refused for a reason of the tool's own (`systems.restore` leaves the
        # System `RESTORING`, which its own precondition rejects). Nothing was replayed, so
        # there is no replay for the guard to preempt.
        return
    assert held_first.status == "queued", held_first.model_dump()
    assert held_second.error_category is None, held_second.model_dump()
    # The same job, not the same envelope: a replay may annotate itself (`data.replayed`).
    assert held_second.object_id == held_first.object_id


async def _force_job_state(pool: AsyncConnectionPool, dedup_key: str, state: str) -> None:
    async with pool.connection() as conn:
        await conn.execute("UPDATE jobs SET state = %s WHERE dedup_key = %s", (state, dedup_key))


async def _mark_step(pool: AsyncConnectionPool, run_id: str, step: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state) VALUES (%s, %s, 'succeeded')",
            (UUID(run_id), step),
        )


# (step, prior job state, whether the step's `run_steps` row exists). Both rows are replays the
# hand-listed `{QUEUED, RUNNING}` probe reported as absent: under `NEVER` (row present) a
# `succeeded` job is returned unchanged, and under `TERMINAL` (no row) a `canceled` one is.
_SETTLED_STEP_ARMS = [
    ("install", "succeeded", True),
    ("install", "canceled", False),
    ("boot", "succeeded", True),
    ("boot", "canceled", False),
]


@pytest.mark.parametrize(("step", "prior_state", "has_row"), _SETTLED_STEP_ARMS)
def test_a_settled_step_repeat_still_replays_under_an_activation(
    migrated_url: str,
    seeded_activation: SeedActivation,
    step: str,
    prior_state: str,
    has_row: bool,
) -> None:
    """The step sites' replay is wider than a live job, and the guard must sit behind all of it.

    `_locked_enqueue` passes `NEVER` when the step's `run_steps` row is present and `TERMINAL`
    when it is absent, so a `succeeded` job under the first and a `canceled` job under the second
    are both returned unchanged — the repeat commits nothing. The differential gate above only
    ever reaches a `queued` prior, which is why these arms are their own test (#2117 review).
    """

    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with runs_support.pool(migrated_url) as conn_pool:
            system_id, run_id = await _ready_system_with_run(conn_pool)
            await _mark_installed(conn_pool, run_id)
            if step == "install":
                first = await runs_support.install(conn_pool, _ctx(), run_id)
            else:
                first = await boot_run(conn_pool, _ctx(), run_id)
            await _force_job_state(conn_pool, f"{run_id}:{step}", prior_state)
            if has_row and step == "boot":
                await _mark_step(conn_pool, run_id, "boot")
            if not has_row:
                async with conn_pool.connection() as conn:
                    await conn.execute(
                        "DELETE FROM run_steps WHERE run_id = %s AND step = %s",
                        (UUID(run_id), step),
                    )
            await _restrict(conn_pool, seeded_activation, system_id, run_id, _STATE.ACTIVE)
            if step == "install":
                return first, await runs_support.install(conn_pool, _ctx(), run_id)
            return first, await boot_run(conn_pool, _ctx(), run_id)

    first, repeat = asyncio.run(_run())
    assert first.status == "queued", first.model_dump()
    assert repeat.error_category is None, repeat.model_dump()
    assert repeat.object_id == first.object_id


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

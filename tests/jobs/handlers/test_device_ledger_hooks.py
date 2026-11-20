"""Teardown, reprovision, reclaim, power and restore keep the device ledger truthful (ADR-0823).

Each hook runs over the kdive_worker login, as in production, so the grants migration 0201 gives the
worker are exercised along with the behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEM_DEVICES, SYSTEMS, devices_for_system
from kdive.domain.capacity.state import JobState, SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import Job, JobKind, PowerAction
from kdive.jobs.handlers.control.control import power_handler
from kdive.jobs.handlers.system_reclaim import reclaim_system_core_after_provider_teardown
from kdive.jobs.handlers.systems import reprovision_handler, restore_handler, teardown_handler
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.shared.runtime_paths import domain_name_for
from tests.conftest import _RoleDsns
from tests.db.system_ledger_support import connect, seed_system
from tests.mcp.systems_support import PROVISIONING_PROFILE, FakeProvisioning, provider_resolver
from tests.support.object_store import INERT_OBJECT_STORE

_DT = datetime(2026, 11, 19, tzinfo=UTC)
_STATES = list(SystemDeviceState)


class _Hotplug:
    """Records ``release_all`` calls in a shared event log; optionally fails them."""

    def __init__(self, events: list[str], *, fail: bool = False) -> None:
        self._events = events
        self._fail = fail

    def release_all(self, domain_name: str) -> None:
        self._events.append(f"release_all:{domain_name}")
        if self._fail:
            raise CategorizedError("provider fault", category=ErrorCategory.INFRASTRUCTURE_FAILURE)


class _Provisioning(FakeProvisioning):
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def reprovision(self, system_id: UUID, profile: Any, **kwargs: Any) -> str:
        self._events.append("reprovision")
        return super().reprovision(system_id, profile, **kwargs)


class _Control:
    def __init__(self) -> None:
        self.calls: list[PowerAction] = []

    def power(self, domain_name: str, action: PowerAction) -> None:
        self.calls.append(action)


class _Snapshotter:
    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
        return None

    def delete_all(self, domain_name: str) -> None:
        return None


def _job(kind: JobKind, system_id: UUID, **payload: object) -> Job:
    return Job(
        id=uuid4(),
        created_at=_DT,
        updated_at=_DT,
        kind=kind,
        payload={"system_id": str(system_id), **payload},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "proj"},
        dedup_key=f"{kind.value}:{system_id}",
    )


async def _seed_rows(
    owner: psycopg.AsyncConnection, system_id: UUID
) -> dict[SystemDeviceState, UUID]:
    rows: dict[SystemDeviceState, UUID] = {}
    for state in _STATES:
        rows[state] = (
            await SYSTEM_DEVICES.insert(
                owner,
                SystemDevice.model_validate(
                    dict(
                        id=uuid4(),
                        created_at=_DT,
                        updated_at=_DT,
                        system_id=system_id,
                        kind="nic",
                        state=state,
                    )
                ),
            )
        ).id
    return rows


async def _states(owner: psycopg.AsyncConnection, system_id: UUID) -> dict[UUID, SystemDeviceState]:
    return {row.id: row.state for row in await devices_for_system(owner, system_id)}


def _run(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    scenario: Callable[[psycopg.AsyncConnection, AsyncConnectionPool, UUID], Awaitable[None]],
    *,
    system_state: SystemState = SystemState.READY,
    profile: dict[str, object] | None = None,
) -> None:
    async def main() -> None:
        worker_pool = AsyncConnectionPool(
            authority_role_dsns("kdive_worker"), min_size=1, max_size=2, open=False
        )
        await worker_pool.open()
        try:
            async with await connect(migrated_url) as owner:
                system_id = await seed_system(owner)
                await owner.execute(
                    "UPDATE systems SET state = %s, provisioning_profile = %s WHERE id = %s",
                    (
                        system_state.value,
                        Jsonb(profile or {"k": "v"}),
                        system_id,
                    ),
                )
                await scenario(owner, worker_pool, system_id)
        finally:
            await worker_pool.close()

    asyncio.run(main())


# --- power ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "moves"),
    [
        (PowerAction.OFF, True),
        (PowerAction.CYCLE, True),
        (PowerAction.ON, False),
        (PowerAction.RESET, False),
    ],
)
def test_power_off_and_cycle_move_attached_rows_to_lost_and_nothing_else(
    migrated_url: str, authority_role_dsns: _RoleDsns, action: PowerAction, moves: bool
) -> None:
    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        rows = await _seed_rows(owner, sid)
        control = _Control()
        async with pool.connection() as conn:
            await power_handler(
                conn,
                _job(JobKind.POWER, sid, action=action.value),
                resolver=provider_resolver(controller=control),
            )

        expected = {state: state for state in _STATES}
        if moves:
            expected[SystemDeviceState.ATTACHED] = SystemDeviceState.LOST
        assert await _states(owner, sid) == {rows[s]: expected[s] for s in _STATES}
        assert control.calls == [action]

    _run(migrated_url, authority_role_dsns, scenario)


def test_a_failed_power_call_leaves_the_rows_attached(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    class _Broken(_Control):
        def power(self, domain_name: str, action: PowerAction) -> None:
            raise CategorizedError("no", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        rows = await _seed_rows(owner, sid)
        async with pool.connection() as conn:
            with pytest.raises(CategorizedError):
                await power_handler(
                    conn,
                    _job(JobKind.POWER, sid, action="off"),
                    resolver=provider_resolver(controller=_Broken()),
                )

        assert (await _states(owner, sid))[rows[SystemDeviceState.ATTACHED]] is (
            SystemDeviceState.ATTACHED
        )

    _run(migrated_url, authority_role_dsns, scenario)


# --- restore -------------------------------------------------------------------------------


@pytest.mark.parametrize("start_paused", [False, True])
def test_restore_moves_attached_rows_to_lost_and_nothing_else(
    migrated_url: str, authority_role_dsns: _RoleDsns, start_paused: bool
) -> None:
    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        rows = await _seed_rows(owner, sid)
        async with pool.connection() as conn:
            await restore_handler(
                conn,
                _job(JobKind.RESTORE, sid, name="cp", start_paused=start_paused),
                resolver=provider_resolver(snapshotter=_Snapshotter()),
            )

        expected = {state: state for state in _STATES}
        expected[SystemDeviceState.ATTACHED] = SystemDeviceState.LOST
        assert await _states(owner, sid) == {rows[s]: expected[s] for s in _STATES}

    _run(migrated_url, authority_role_dsns, scenario, system_state=SystemState.RESTORING)


def test_a_failed_restore_leaves_the_rows_alone(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    class _Broken(_Snapshotter):
        def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None:
            raise CategorizedError("no", category=ErrorCategory.INFRASTRUCTURE_FAILURE)

    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        rows = await _seed_rows(owner, sid)
        async with pool.connection() as conn:
            with pytest.raises(CategorizedError):
                await restore_handler(
                    conn,
                    _job(JobKind.RESTORE, sid, name="cp", start_paused=False),
                    resolver=provider_resolver(snapshotter=_Broken()),
                )

        assert (await _states(owner, sid))[rows[SystemDeviceState.ATTACHED]] is (
            SystemDeviceState.ATTACHED
        )

    _run(migrated_url, authority_role_dsns, scenario, system_state=SystemState.RESTORING)


# --- teardown ------------------------------------------------------------------------------


@pytest.mark.parametrize("fail", [False, True], ids=["release-ok", "release-fails"])
def test_teardown_releases_devices_and_deletes_every_row(
    migrated_url: str, authority_role_dsns: _RoleDsns, fail: bool
) -> None:
    events: list[str] = []

    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        await _seed_rows(owner, sid)
        async with pool.connection() as conn:
            await teardown_handler(
                conn,
                _job(JobKind.TEARDOWN, sid),
                resolver=provider_resolver(
                    provisioner=FakeProvisioning(), hotplugger=_Hotplug(events, fail=fail)
                ),
                artifact_store=INERT_OBJECT_STORE,
            )

        row = await SYSTEMS.get(owner, sid)
        assert row is not None and row.state is SystemState.TORN_DOWN
        assert await devices_for_system(owner, sid) == []
        assert events == [f"release_all:{domain_name_for(sid)}"]

    _run(migrated_url, authority_role_dsns, scenario)


def test_teardown_of_a_provider_without_hot_plug_still_clears_the_rows(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        await _seed_rows(owner, sid)
        async with pool.connection() as conn:
            await teardown_handler(
                conn,
                _job(JobKind.TEARDOWN, sid),
                resolver=provider_resolver(provisioner=FakeProvisioning()),
                artifact_store=INERT_OBJECT_STORE,
            )

        assert await devices_for_system(owner, sid) == []

    _run(migrated_url, authority_role_dsns, scenario)


# --- reprovision ---------------------------------------------------------------------------


@pytest.mark.parametrize("fail", [False, True], ids=["release-ok", "release-fails"])
def test_reprovision_releases_devices_first_and_deletes_every_row(
    migrated_url: str, authority_role_dsns: _RoleDsns, fail: bool
) -> None:
    events: list[str] = []

    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        await _seed_rows(owner, sid)
        digest = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))
        async with pool.connection() as conn:
            await reprovision_handler(
                conn,
                _job(JobKind.REPROVISION, sid, profile_digest=digest),
                resolver=provider_resolver(
                    provisioner=_Provisioning(events), hotplugger=_Hotplug(events, fail=fail)
                ),
            )

        row = await SYSTEMS.get(owner, sid)
        assert row is not None and row.state is SystemState.READY
        assert await devices_for_system(owner, sid) == []
        assert events == [f"release_all:{domain_name_for(sid)}", "reprovision"]

    _run(
        migrated_url,
        authority_role_dsns,
        scenario,
        system_state=SystemState.REPROVISIONING,
        profile=PROVISIONING_PROFILE,
    )


def test_a_failed_reprovision_marks_attached_rows_lost_and_keeps_every_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        rows = await _seed_rows(owner, sid)
        digest = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))
        async with pool.connection() as conn:
            with pytest.raises(CategorizedError):
                await reprovision_handler(
                    conn,
                    _job(JobKind.REPROVISION, sid, profile_digest=digest),
                    resolver=provider_resolver(
                        provisioner=FakeProvisioning(reprovision_error=True),
                        hotplugger=_Hotplug([]),
                    ),
                )

        after = await _states(owner, sid)
        assert after == {
            row_id: SystemDeviceState.LOST if state is SystemDeviceState.ATTACHED else state
            for state, row_id in rows.items()
        }
        system = await SYSTEMS.get(owner, sid)
        assert system is not None and system.state is SystemState.FAILED

    _run(
        migrated_url,
        authority_role_dsns,
        scenario,
        system_state=SystemState.REPROVISIONING,
        profile=PROVISIONING_PROFILE,
    )


# --- reclaim -------------------------------------------------------------------------------


class _NoStore:
    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


@pytest.mark.parametrize("role", ["kdive_worker", "kdive_reconciler"])
@pytest.mark.parametrize("flag", [True, False])
def test_system_reclaim_deletes_device_rows_only_under_its_own_flag(
    migrated_url: str, authority_role_dsns: _RoleDsns, role: str, flag: bool
) -> None:
    async def scenario(
        owner: psycopg.AsyncConnection, pool: AsyncConnectionPool, sid: UUID
    ) -> None:
        await _seed_rows(owner, sid)
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns(role), autocommit=True
        ) as conn:
            await reclaim_system_core_after_provider_teardown(
                conn,
                _NoStore(),
                sid,
                reclaim_snapshot_ledger=False,
                reclaim_device_ledger=flag,
                discharge_mutation_obligations=False,
            )

        assert (await devices_for_system(owner, sid) == []) is flag

    _run(migrated_url, authority_role_dsns, scenario)

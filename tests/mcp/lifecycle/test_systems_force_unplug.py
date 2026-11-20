"""Admission and end-to-end tests for ``systems.force_unplug_device`` (#3762, ADR-0828).

The tool runs through ``with_runtime_for_system`` with the registrar's ``Role.VIEWER`` wiring, so
membership, the destructive-operation gate and the admission under the System lock are the real
ones, over the ``kdive_server`` login. The worker side is the real un-plug handler over the
fault-inject hot-plug port, so a forced removal and a refusal are seen as an agent would see them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS, SYSTEM_DEVICES
from kdive.domain.capacity.state import SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.devices import unplug_device_handler
from kdive.jobs.payloads import UnplugDevicePayload
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.support._runtime_resolution import with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.devices import force_unplug_device, unplug_device
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.hotplug import FaultInjectHotplug, HotplugBehaviour
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.audit import args_digest
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.conftest import _RoleDsns
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import provider_resolver, provisioning_profile

_ATTACHED, _FAILED, _LOST = (
    SystemDeviceState.ATTACHED,
    SystemDeviceState.FAILED,
    SystemDeviceState.LOST,
)
_ALL_KINDS = frozenset(HotplugDeviceKind)


def _profile(*, opt_in: bool) -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["destructive_ops"] = (
        ["force_unplug_device"] if opt_in else []
    )
    return profile


@dataclass
class _Env:
    owner: AsyncConnectionPool
    server: AsyncConnectionPool
    worker: psycopg.AsyncConnection
    inventory: FaultInjectInventory
    system_id: str
    forced: frozenset[HotplugDeviceKind]

    def resolver(self, port: object | None = None) -> ProviderResolver:
        return provider_resolver(
            hotplugger=port or FaultInjectHotplug(self.inventory),
            forced_unplug_devices=self.forced,
        )

    async def force(
        self,
        device_id: str | UUID,
        role: Role | None = Role.ADMIN,
        *,
        system_id: str | None = None,
        idempotency_key: str | None = None,
        projects: tuple[str, ...] = ("proj",),
    ) -> ToolResponse:
        """Invoke the tool the way the registrar does: viewer membership, then the gate."""
        target = system_id or self.system_id
        ctx = _ctx(role, projects=projects)
        resolver = self.resolver()
        return await with_runtime_for_system(
            self.server,
            resolver,
            ctx,
            target,
            lambda runtime: force_unplug_device(
                self.server,
                ctx,
                runtime,
                resolver,
                system_id=target,
                device_id=str(device_id),
                idempotency_key=idempotency_key,
            ),
            required_role=Role.VIEWER,
        )

    async def graceful(self, device_id: str | UUID, *, idempotency_key: str | None) -> ToolResponse:
        ctx = _ctx(Role.ADMIN)
        return await with_runtime_for_system(
            self.server,
            self.resolver(),
            ctx,
            self.system_id,
            lambda runtime: unplug_device(
                self.server,
                ctx,
                runtime,
                system_id=self.system_id,
                device_id=str(device_id),
                idempotency_key=idempotency_key,
            ),
            required_role=Role.CONTRIBUTOR,
        )

    async def add_row(
        self,
        state: SystemDeviceState = _ATTACHED,
        *,
        kind: str = "nic",
        system_id: str | None = None,
    ) -> UUID:
        async with self.owner.connection() as conn:
            row = await SYSTEM_DEVICES.insert(
                conn,
                SystemDevice.model_validate(
                    dict(
                        id=uuid4(),
                        created_at=runs_support.TEST_DT,
                        updated_at=runs_support.TEST_DT,
                        system_id=UUID(system_id or self.system_id),
                        kind=kind,
                        state=state,
                    )
                ),
            )
        return row.id

    async def row(self, device_id: str | UUID) -> SystemDevice | None:
        async with self.owner.connection() as conn:
            return await SYSTEM_DEVICES.get(conn, UUID(str(device_id)))

    async def jobs(self, kind: JobKind) -> list[dict[str, Any]]:
        async with self.owner.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM jobs WHERE kind = %s AND payload->>'system_id' = %s ORDER BY id",
                (kind.value, self.system_id),
            )
            return await cur.fetchall()

    async def audit(self) -> list[tuple[str, str, str]]:
        async with self.owner.connection() as conn:
            cur = await conn.execute(
                "SELECT tool, transition, args_digest FROM audit_log WHERE object_id = %s "
                "ORDER BY ts, id",
                (UUID(self.system_id),),
            )
            return [(tool, transition, digest) for tool, transition, digest in await cur.fetchall()]

    async def run_worker(self, job_id: str, port: object | None = None) -> None:
        async with self.owner.connection() as conn:
            job = await JOBS.get(conn, UUID(job_id))
        assert job is not None
        await unplug_device_handler(
            self.worker, job, resolver=self.resolver(port), secret_registry=SecretRegistry()
        )

    def attach_on_port(self, port: FaultInjectHotplug, device_id: UUID, kind: str) -> str:
        """Make the port hold the device, as a settled plug would; return its handle."""
        spec = DeviceSpec(
            device_id=str(device_id), kind=HotplugDeviceKind(kind), network=None, size_gb=None
        )
        return port.plug(domain_name_for(UUID(self.system_id)), spec).provider_handle


def _scenario(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    body: Callable[[_Env], Awaitable[None]],
    *,
    state: SystemState = SystemState.READY,
    opt_in: bool = True,
    forced: frozenset[HotplugDeviceKind] = _ALL_KINDS,
) -> None:
    @asynccontextmanager
    async def env() -> AsyncGenerator[_Env]:
        async with (
            runs_support.pool(migrated_url) as owner,
            runs_support.pool(authority_role_dsns("kdive_server")) as server,
            await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker"), autocommit=True
            ) as worker,
        ):
            system_id = await runs_support.seed_system(
                owner, system_state=state, provisioning_profile=_profile(opt_in=opt_in)
            )
            inventory = FaultInjectInventory()
            inventory.record(UUID(system_id), domain_name_for(UUID(system_id)))
            yield _Env(owner, server, worker, inventory, system_id, forced)

    async def main() -> None:
        async with env() as e:
            await body(e)

    asyncio.run(main())


def _reason(response: ToolResponse) -> Any:
    return response.data.get("reason")


# --- admission order, one test per step ---------------------------------------------------------


def test_a_malformed_id_is_a_configuration_error(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        bad_system = await env.force(uuid4(), system_id="nope")
        bad_device = await env.force("nope")
        for response in (bad_system, bad_device):
            assert response.error_category == "configuration_error", response.model_dump()
        assert await env.audit() == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_non_member_gets_not_found_before_the_gate_and_leaves_no_audit(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()

        response = await env.force(device_id, Role.ADMIN, projects=("elsewhere",))

        assert response.error_category in {"not_found", "configuration_error"}
        assert response.error_category != "authorization_denied"
        assert await env.audit() == []
        assert await env.jobs(JobKind.FORCE_UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_provider_with_no_forced_kind_refuses_before_the_gate(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()

        for role in (Role.ADMIN, Role.VIEWER):
            response = await env.force(device_id, role)
            assert response.error_category == "configuration_error", response.model_dump()
            assert _reason(response) == "capability_unsupported"
            assert response.data["capability"] == "forced_unplug"
            assert response.data["supported"] == []
        assert await env.audit() == []

    _scenario(migrated_url, authority_role_dsns, body, forced=frozenset())


@pytest.mark.parametrize(
    ("role", "opt_in", "missing"),
    [
        (Role.ADMIN, False, ["profile_opt_in"]),
        (Role.CONTRIBUTOR, True, ["admin_role"]),
        (Role.VIEWER, True, ["admin_role"]),
        (Role.CONTRIBUTOR, False, ["admin_role", "profile_opt_in"]),
    ],
)
def test_the_gate_denies_and_audits_every_missing_factor(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    role: Role,
    opt_in: bool,
    missing: list[str],
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()

        response = await env.force(device_id, role)

        assert response.error_category == "authorization_denied", response.model_dump()
        assert sorted(map(str, cast(list[Any], response.data["missing_checks"]))) == sorted(missing)
        ((tool, transition, digest),) = await env.audit()
        assert (tool, transition) == ("systems.force_unplug_device", "force_unplug_device:denied")
        denied = {"system_id": env.system_id, "missing": response.data["missing_checks"]}
        assert digest == args_digest(denied)
        assert await env.jobs(JobKind.FORCE_UNPLUG_DEVICE) == []
        row = await env.row(device_id)
        assert row is not None and row.state is _ATTACHED

    _scenario(migrated_url, authority_role_dsns, body, opt_in=opt_in)


def test_a_non_admin_is_denied_before_any_device_or_state_check(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        for device_id in (uuid4(), await env.add_row(_LOST)):
            response = await env.force(device_id, Role.CONTRIBUTOR)
            assert response.error_category == "authorization_denied", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body, state=SystemState.PAUSED)


def test_an_admin_with_opt_in_is_refused_a_system_that_is_not_ready(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        response = await env.force(await env.add_row())

        assert response.error_category == "conflict" and _reason(response) == "system_not_ready"
        assert await env.audit() == []

    _scenario(migrated_url, authority_role_dsns, body, state=SystemState.PAUSED)


def test_an_unknown_or_foreign_device_is_not_found(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        other = await runs_support.seed_system(env.owner)
        foreign = await env.add_row(system_id=other)
        for device_id in (uuid4(), foreign):
            response = await env.force(device_id)
            assert response.error_category == "not_found", response.model_dump()
        assert await env.audit() == []

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize("state", [SystemDeviceState.PLUGGING, SystemDeviceState.UNPLUGGING])
def test_a_transient_row_is_a_device_op_in_flight(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemDeviceState
) -> None:
    async def body(env: _Env) -> None:
        response = await env.force(await env.add_row(state))

        assert response.error_category == "conflict" and _reason(response) == "device_op_in_flight"
        assert await env.jobs(JobKind.FORCE_UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_another_transient_row_or_active_job_blocks_the_target(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        target = await env.add_row()
        busy = await env.add_row(SystemDeviceState.UNPLUGGING)

        assert _reason(await env.force(target)) == "device_op_in_flight"

        async with env.owner.connection() as conn:
            await conn.execute("DELETE FROM system_devices WHERE id = %s", (busy,))
        first = await env.force(target)
        assert first.status == "running", first.model_dump()
        # The first forced job is still queued, so a second target is refused too.
        second = await env.add_row()
        assert _reason(await env.force(second)) == "device_op_in_flight"

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_lost_row_is_not_forceable_and_points_at_the_graceful_tool(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        response = await env.force(await env.add_row(_LOST))

        assert response.error_category == "conflict" and _reason(response) == "device_not_forceable"
        assert response.suggested_next_actions == ["systems.unplug_device"]
        assert await env.jobs(JobKind.FORCE_UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_kind_the_provider_cannot_force_is_refused_before_enqueue(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        nic = await env.add_row(kind="nic")
        disk = await env.add_row(kind="disk")

        refused = await env.force(nic)

        assert refused.error_category == "configuration_error", refused.model_dump()
        assert _reason(refused) == "capability_unsupported"
        assert refused.data["capability"] == "forced_unplug:nic"
        assert refused.data["supported"] == ["disk"]
        assert await env.jobs(JobKind.FORCE_UNPLUG_DEVICE) == []
        row = await env.row(nic)
        assert row is not None and row.state is _ATTACHED
        assert (await env.force(disk)).status == "running"

    _scenario(migrated_url, authority_role_dsns, body, forced=frozenset({HotplugDeviceKind.DISK}))


# --- the admitted call --------------------------------------------------------------------------


@pytest.mark.parametrize("state", [_ATTACHED, _FAILED])
def test_admission_moves_the_row_enqueues_one_job_and_audits_it(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemDeviceState
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row(state, kind="disk")

        response = await env.force(device_id)

        assert response.status == "running" and response.error_category is None
        assert response.suggested_next_actions == ["jobs.wait", "systems.list_devices"]
        data = response.data
        assert set(data) == {
            "job_id",
            "device_id",
            "kind",
            "state",
            "settle_timeout_s",
            "forced",
        }
        assert (data["kind"], data["state"], data["settle_timeout_s"], data["forced"]) == (
            "disk",
            "unplugging",
            30,
            True,
        )
        row = await env.row(device_id)
        assert row is not None and row.state is SystemDeviceState.UNPLUGGING
        (job,) = await env.jobs(JobKind.FORCE_UNPLUG_DEVICE)
        assert str(job["id"]) == data["job_id"]
        assert job["max_attempts"] == 1 and job["dispatch_lane"] == "state-fenced"
        assert job["dedup_key"].startswith(f"force_unplug:{device_id}:")
        payload = UnplugDevicePayload.model_validate(job["payload"])
        assert (payload.device_id, payload.from_state) == (str(device_id), state)
        assert await env.jobs(JobKind.UNPLUG_DEVICE) == []
        ((tool, transition, digest),) = await env.audit()
        assert (tool, transition) == ("systems.force_unplug_device", "force_unplug_device")
        assert digest == args_digest(
            {
                "system_id": env.system_id,
                "device_id": str(device_id),
                "kind": "disk",
                "from_state": state.value,
            }
        )

    _scenario(migrated_url, authority_role_dsns, body)


# --- idempotency --------------------------------------------------------------------------------


def test_a_repeated_key_replays_while_the_caller_still_passes_the_gate(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()
        first = await env.force(device_id, idempotency_key="f-1")
        replay = await env.force(device_id, idempotency_key="f-1")

        assert replay.model_dump() == first.model_dump()
        assert len(await env.jobs(JobKind.FORCE_UNPLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_keyed_repeat_after_the_caller_lost_the_role_is_denied_not_replayed(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()
        first = await env.force(device_id, Role.ADMIN, idempotency_key="f-1")
        assert first.status == "running"

        repeat = await env.force(device_id, Role.CONTRIBUTOR, idempotency_key="f-1")

        assert repeat.error_category == "authorization_denied", repeat.model_dump()
        assert len(await env.jobs(JobKind.FORCE_UNPLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_keyed_repeat_after_the_profile_stopped_opting_in_is_denied(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()
        assert (await env.force(device_id, idempotency_key="f-1")).status == "running"
        async with env.owner.connection() as conn:
            await conn.execute(
                "UPDATE systems SET provisioning_profile = %s::jsonb WHERE id = %s",
                (Jsonb(_profile(opt_in=False)), UUID(env.system_id)),
            )

        repeat = await env.force(device_id, idempotency_key="f-1")

        assert repeat.error_category == "authorization_denied", repeat.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize("graceful_first", [True, False])
def test_one_key_does_not_replay_across_the_graceful_and_forced_tools(
    migrated_url: str, authority_role_dsns: _RoleDsns, graceful_first: bool
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row()
        if graceful_first:
            first = await env.graceful(device_id, idempotency_key="shared")
            second = await env.force(device_id, idempotency_key="shared")
        else:
            first = await env.force(device_id, idempotency_key="shared")
            second = await env.graceful(device_id, idempotency_key="shared")

        assert first.status == "running", first.model_dump()
        assert second.status != "running" and second.data.get("job_id") != first.data["job_id"]
        total = len(await env.jobs(JobKind.UNPLUG_DEVICE)) + len(
            await env.jobs(JobKind.FORCE_UNPLUG_DEVICE)
        )
        assert total == 1

    _scenario(migrated_url, authority_role_dsns, body)


# --- end to end against fault-inject ------------------------------------------------------------


def test_a_forced_removal_clears_a_device_the_graceful_path_cannot(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row(kind="nic")
        port = FaultInjectHotplug(env.inventory)
        env.attach_on_port(port, device_id, "nic")
        hung = port.with_behaviour(HotplugBehaviour.HANG)

        response = await env.force(device_id)
        await env.run_worker(str(response.data["job_id"]), hung)

        assert await env.row(device_id) is None
        domain = domain_name_for(UUID(env.system_id))
        assert not hung.is_attached(domain, str(device_id), None, kind=HotplugDeviceKind.NIC)

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_provider_refusal_returns_the_row_to_where_it_started(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row(_FAILED, kind="disk")
        port = FaultInjectHotplug(env.inventory)
        env.attach_on_port(port, device_id, "disk")
        refusing = port.with_behaviour(HotplugBehaviour.NO_FORCE)

        response = await env.force(device_id)
        with pytest.raises(CategorizedError) as refused:
            await env.run_worker(str(response.data["job_id"]), refusing)

        assert refused.value.details["reason"] == "unplug_refused"
        row = await env.row(device_id)
        assert row is not None and row.state is _FAILED

    _scenario(migrated_url, authority_role_dsns, body)


# --- discovery ----------------------------------------------------------------------------------


def test_systems_get_reports_the_forced_kinds_sorted(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        response = await get_system(env.server, _ctx(), env.system_id, resolver=env.resolver())
        assert response.data["forced_unplug_devices"] == sorted(k.value for k in env.forced)
        assert response.data["hotplug_devices"] == ["disk", "nic"]

    for forced in (_ALL_KINDS, frozenset({HotplugDeviceKind.DISK}), frozenset()):
        _scenario(migrated_url, authority_role_dsns, body, forced=forced)

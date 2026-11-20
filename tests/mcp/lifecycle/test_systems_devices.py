"""Admission and end-to-end tests for the device hot-plug `systems.*` tools (#3700, ADR-0823).

Tools run through ``with_runtime_for_system`` (the registrar wiring, so role, membership and
capability are exercised) over the ``kdive_server`` login, so the migration's grants are part of
the proof. The worker side is the real handlers over the fault-inject port, run on the
``kdive_worker`` login, so plug, list and un-plug are exercised as an agent would see them.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import JOBS, SNAPSHOTS, SYSTEM_DEVICES, SYSTEMS, devices_for_system
from kdive.domain.capacity.state import SnapshotState, SystemDeviceState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Snapshot, SystemDevice
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.devices import plug_device_handler, unplug_device_handler
from kdive.jobs.payloads import PlugDevicePayload, UnplugDevicePayload
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.lifecycle.control.registrar import power_system
from kdive.mcp.tools.lifecycle.support._device_ops import device_job_active, device_op_in_flight
from kdive.mcp.tools.lifecycle.support._runtime_resolution import with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.mcp.tools.lifecycle.systems.devices import (
    DEVICE_LIMIT,
    list_devices,
    plug_device,
    unplug_device,
)
from kdive.mcp.tools.lifecycle.systems.snapshot import restore_system, snapshot_system
from kdive.mcp.tools.lifecycle.systems.view import get_system
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.fault_inject.faulting.engine import FaultEngine
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.faulted import FaultedHotplug
from kdive.providers.fault_inject.lifecycle.hotplug import FaultInjectHotplug
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.authz.rbac import Role, RoleDenied
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.conftest import _RoleDsns
from tests.db_waits import wait_until_any_backend_waiting
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import SYSTEM_ADMIN_HANDLERS, provider_resolver, provisioning_profile
from tests.mcp.systems_support import ctx as _ctx
from tests.support.worker_fence import register_worker

_PLUGGING, _ATTACHED = SystemDeviceState.PLUGGING, SystemDeviceState.ATTACHED
_FAILED, _LOST = SystemDeviceState.FAILED, SystemDeviceState.LOST


@dataclass
class _Env:
    owner: AsyncConnectionPool
    server: AsyncConnectionPool
    worker: psycopg.AsyncConnection
    inventory: FaultInjectInventory
    system_id: str

    def resolver(self, port: object | None = None, *, capable: bool = True) -> ProviderResolver:
        hotplugger = (port or FaultInjectHotplug(self.inventory)) if capable else None
        return provider_resolver(hotplugger=hotplugger)

    async def call(
        self,
        tool: str,
        role: Role | None = Role.CONTRIBUTOR,
        *,
        capable: bool = True,
        **kwargs: Any,
    ) -> ToolResponse:
        """Invoke ``tool`` the way the registrar does."""
        handlers = {"plug": plug_device, "unplug": unplug_device, "list": list_devices}
        handler = handlers[tool]
        system_id = kwargs.pop("system_id", self.system_id)
        ctx = _ctx(role)
        required = Role.VIEWER if tool == "list" else Role.CONTRIBUTOR
        return await with_runtime_for_system(
            self.server,
            self.resolver(capable=capable),
            ctx,
            system_id,
            lambda runtime: handler(self.server, ctx, runtime, system_id=system_id, **kwargs),
            required_role=required,
        )

    async def plug(self, kind: str = "nic", **kwargs: Any) -> ToolResponse:
        params: dict[str, Any] = {"network": None, "size_gb": None, "idempotency_key": None}
        return await self.call("plug", kind=kind, **{**params, **kwargs})

    async def unplug(self, device_id: str, **kwargs: Any) -> ToolResponse:
        params: dict[str, Any] = {"idempotency_key": None}
        return await self.call("unplug", device_id=device_id, **{**params, **kwargs})

    async def listing(self, **kwargs: Any) -> dict[str, Any]:
        response = await self.call("list", **kwargs)
        assert response.error_category is None, response.model_dump()
        return dict(response.data)

    async def add_row(self, state: SystemDeviceState, system_id: str | None = None) -> UUID:
        async with self.owner.connection() as conn:
            row = await SYSTEM_DEVICES.insert(
                conn,
                SystemDevice.model_validate(
                    dict(
                        id=uuid4(),
                        created_at=runs_support.TEST_DT,
                        updated_at=runs_support.TEST_DT,
                        system_id=UUID(system_id or self.system_id),
                        kind="nic",
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

    async def settle_job(self, job_id: str, state: str = "succeeded") -> None:
        async with self.owner.connection() as conn, conn.transaction():
            if state == "running":  # the claim trigger requires a registered worker
                await register_worker(conn, "devices-test-worker")
                await conn.execute(
                    "UPDATE jobs SET state = 'running', worker_id = 'devices-test-worker' "
                    "WHERE id = %s",
                    (UUID(job_id),),
                )
            else:
                await conn.execute(
                    "UPDATE jobs SET state = %s WHERE id = %s", (state, UUID(job_id))
                )

    async def run_worker(self, job_id: str, port: object | None = None) -> None:
        """Run the queued job's real handler on the worker login, as the worker would."""
        async with self.owner.connection() as conn:
            job = await JOBS.get(conn, UUID(job_id))
        assert job is not None
        resolver = self.resolver(port)
        run = plug_device_handler if job.kind is JobKind.PLUG_DEVICE else unplug_device_handler
        try:
            await run(self.worker, job, resolver=resolver, secret_registry=SecretRegistry())
        finally:
            await self.settle_job(job_id)


def _scenario(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    body: Callable[[_Env], Awaitable[None]],
    *,
    state: SystemState = SystemState.READY,
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
            system_id = await runs_support.seed_system(owner, system_state=state)
            inventory = FaultInjectInventory()
            inventory.record(UUID(system_id), domain_name_for(UUID(system_id)))
            yield _Env(owner, server, worker, inventory, system_id)

    async def main() -> None:
        async with env() as e:
            await body(e)

    asyncio.run(main())


def _reason(response: ToolResponse) -> Any:
    return response.data.get("reason")


# --- plug admission ---------------------------------------------------------------------------


def test_plug_inserts_a_plugging_row_enqueues_one_job_and_audits(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        response = await env.plug("disk", size_gb=4)

        assert response.status == "running" and response.error_category is None
        assert response.suggested_next_actions == ["jobs.wait", "systems.list_devices"]
        data = response.data
        assert set(data) == {"job_id", "device_id", "kind", "state", "settle_timeout_s"}
        assert (data["kind"], data["state"], data["settle_timeout_s"]) == ("disk", "plugging", 60)
        row = await env.row(str(data["device_id"]))
        assert row is not None and row.state is _PLUGGING and row.size_gb == 4
        (job,) = await env.jobs(JobKind.PLUG_DEVICE)
        assert str(job["id"]) == data["job_id"]
        assert (job["max_attempts"], job["dedup_key"]) == (1, f"plug:{data['device_id']}")
        assert job["dispatch_lane"] == "state-fenced"
        assert PlugDevicePayload.model_validate(job["payload"]).device_id == data["device_id"]
        async with env.owner.connection() as conn:
            cur = await conn.execute(
                "SELECT tool, transition FROM audit_log WHERE object_id = %s",
                (UUID(env.system_id),),
            )
            assert await cur.fetchall() == [("systems.plug_device", "plug_device")]

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize(
    ("kind", "network", "size_gb", "reason"),
    [
        ("usb", None, None, "invalid_kind"),
        ("nic", "bad name", None, "invalid_network"),
        ("nic", "br0\n", None, "invalid_network"),
        ("nic", "n" * 65, None, "invalid_network"),
        ("nic", "", None, "invalid_network"),
        ("nic", None, 4, "parameter_mismatch"),
        ("disk", "net0", 4, "parameter_mismatch"),
        ("disk", None, None, "invalid_size"),
        ("disk", None, 0, "invalid_size"),
        ("disk", None, 33, "invalid_size"),
        ("disk", None, True, "invalid_size"),
    ],
)
def test_plug_parameter_errors_name_their_reason_and_change_nothing(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    kind: str,
    network: str | None,
    size_gb: int | None,
    reason: str,
) -> None:
    async def body(env: _Env) -> None:
        response = await env.plug(kind, network=network, size_gb=size_gb)

        assert response.error_category == "configuration_error", response.model_dump()
        assert _reason(response) == reason
        assert await env.jobs(JobKind.PLUG_DEVICE) == []
        async with env.owner.connection() as conn:
            assert await devices_for_system(conn, UUID(env.system_id)) == []

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize(("kind", "size_gb"), [("nic", None), ("disk", 32), ("disk", 1)])
def test_plug_accepts_the_documented_extremes(
    migrated_url: str, authority_role_dsns: _RoleDsns, kind: str, size_gb: int | None
) -> None:
    async def body(env: _Env) -> None:
        network = "a" * 64 if kind == "nic" else None
        response = await env.plug(kind, network=network, size_gb=size_gb)
        assert response.status == "running", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_malformed_system_id_is_a_configuration_error(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        for response in (
            await env.call(
                "plug",
                system_id="nope",
                kind="nic",
                network=None,
                size_gb=None,
                idempotency_key=None,
            ),
            await env.call("list", system_id="nope"),
        ):
            assert response.error_category == "configuration_error", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize("state", [SystemState.PAUSED, SystemState.RESTORING, SystemState.FAILED])
def test_plug_and_unplug_refuse_a_system_that_is_not_ready(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemState
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row(_ATTACHED)
        for response in (await env.plug(), await env.unplug(str(device_id))):
            assert response.error_category == "conflict", response.model_dump()
            assert _reason(response) == "system_not_ready"
            assert response.data["current_status"] == state.value
        assert await env.jobs(JobKind.PLUG_DEVICE) == await env.jobs(JobKind.UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body, state=state)


def test_plug_refuses_while_a_device_job_is_queued_or_a_row_is_transient(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug()
        assert first.status == "running"

        again = await env.plug()
        assert again.error_category == "conflict" and _reason(again) == "device_op_in_flight"
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1

        await env.settle_job(str(first.data["job_id"]), "failed")  # a dead job leaves its row
        stalled = await env.plug()
        assert _reason(stalled) == "device_op_in_flight"

    _scenario(migrated_url, authority_role_dsns, body)


def test_plug_refuses_a_ninth_device_with_the_limit_in_data(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        for _ in range(DEVICE_LIMIT):
            await env.add_row(_FAILED)

        response = await env.plug()

        assert response.error_category == "conflict" and _reason(response) == "device_limit"
        assert response.data["limit"] == 8
        assert await env.jobs(JobKind.PLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_the_device_limit_counts_rows_of_this_system_only(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        other = await runs_support.seed_system(env.owner)
        for _ in range(DEVICE_LIMIT):
            await env.add_row(_FAILED, other)

        assert (await env.plug()).status == "running"

    _scenario(migrated_url, authority_role_dsns, body)


# --- idempotency ------------------------------------------------------------------------------


def test_a_repeated_idempotency_key_replays_the_same_device(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug(idempotency_key="k-1")
        replay = await env.plug(idempotency_key="k-1")
        fresh = await env.plug(idempotency_key="k-2")

        assert replay.model_dump() == first.model_dump()
        assert replay.data["device_id"] == first.data["device_id"]
        # The unkeyed/fresh-key call is a new device, refused only because the first is in flight.
        assert _reason(fresh) == "device_op_in_flight"
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_unkeyed_plugs_each_mint_a_new_device(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug()
        await env.run_worker(str(first.data["job_id"]))
        second = await env.plug()

        assert second.data["device_id"] != first.data["device_id"]

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_repeated_unplug_key_replays_and_a_fresh_retry_enqueues_new_work(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = str(await env.add_row(_FAILED))
        first = await env.unplug(device_id, idempotency_key="u-1")
        assert (
            await env.unplug(device_id, idempotency_key="u-1")
        ).model_dump() == first.model_dump()

        # The job ends without clearing the row (a timeout): a retry must enqueue fresh work.
        await env.settle_job(str(first.data["job_id"]), "failed")
        async with env.owner.connection() as conn:
            await conn.execute(
                "UPDATE system_devices SET state = 'failed' WHERE id = %s", (UUID(device_id),)
            )
        retry = await env.unplug(device_id)

        assert retry.status == "running", retry.model_dump()
        assert retry.data["job_id"] != first.data["job_id"]
        assert len(await env.jobs(JobKind.UNPLUG_DEVICE)) == 2

    _scenario(migrated_url, authority_role_dsns, body)


# --- un-plug admission ------------------------------------------------------------------------


@pytest.mark.parametrize("state", [_ATTACHED, _FAILED, _LOST])
def test_unplug_admits_a_settled_row_and_records_where_it_started(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: SystemDeviceState
) -> None:
    async def body(env: _Env) -> None:
        device_id = str(await env.add_row(state))

        response = await env.unplug(device_id)

        assert response.status == "running" and response.error_category is None
        assert response.data["state"] == "unplugging" and response.data["settle_timeout_s"] == 30
        assert response.data["device_id"] == device_id
        row = await env.row(device_id)
        assert row is not None and row.state is SystemDeviceState.UNPLUGGING
        (job,) = await env.jobs(JobKind.UNPLUG_DEVICE)
        payload = UnplugDevicePayload.model_validate(job["payload"])
        assert payload.from_state is state
        assert job["max_attempts"] == 1 and job["dedup_key"].startswith(f"unplug:{device_id}:")

    _scenario(migrated_url, authority_role_dsns, body)


def test_unplug_of_an_unknown_or_foreign_device_is_not_found(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        other = await runs_support.seed_system(env.owner)
        foreign = str(await env.add_row(_ATTACHED, other))

        for device_id in (str(uuid4()), foreign):
            response = await env.unplug(device_id)
            assert response.error_category == "not_found", response.model_dump()
        bad = await env.unplug("not-a-uuid")
        assert bad.error_category == "configuration_error"
        assert await env.jobs(JobKind.UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_unplug_refuses_a_row_that_is_still_transient(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        for state in (_PLUGGING, SystemDeviceState.UNPLUGGING):
            device_id = str(await env.add_row(state))
            response = await env.unplug(device_id)
            assert response.error_category == "conflict", response.model_dump()
            assert _reason(response) == "device_op_in_flight"
        assert await env.jobs(JobKind.UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_unplug_refuses_while_another_device_job_is_active(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        attached = str(await env.add_row(_ATTACHED))
        assert (await env.plug()).status == "running"

        response = await env.unplug(attached)

        assert response.error_category == "conflict" and _reason(response) == "device_op_in_flight"

    _scenario(migrated_url, authority_role_dsns, body)


# --- capability and RBAC ----------------------------------------------------------------------


def test_a_provider_without_hotplug_refuses_before_judging_the_kind(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        response = await env.plug("usb", capable=False)

        assert _reason(response) == "capability_unsupported", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_non_canonical_system_id_cannot_hide_a_device_job_from_the_probe(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        plugged = await env.plug(system_id=env.system_id.upper())
        assert plugged.status == "running", plugged.model_dump()

        response = await _power(env)

        assert _reason(response) == "device_op_in_flight", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize("tool", ["plug", "unplug", "list"])
def test_a_provider_without_hotplug_refuses_every_tool(
    migrated_url: str, authority_role_dsns: _RoleDsns, tool: str
) -> None:
    async def body(env: _Env) -> None:
        kwargs: dict[str, Any] = {
            "plug": {"kind": "nic", "network": None, "size_gb": None, "idempotency_key": None},
            "unplug": {"device_id": str(uuid4()), "idempotency_key": None},
            "list": {},
        }[tool]

        response = await env.call(tool, capable=False, **kwargs)

        assert response.error_category == "configuration_error", response.model_dump()
        assert _reason(response) == "capability_unsupported"
        assert response.data["capability"] == ("hotplug:nic" if tool == "plug" else "hotplug")
        assert response.data["supported"] == []
        assert await env.jobs(JobKind.PLUG_DEVICE) == await env.jobs(JobKind.UNPLUG_DEVICE) == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_roles_gate_the_tools(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    async def body(env: _Env) -> None:
        for role in (Role.VIEWER, None):
            with pytest.raises(RoleDenied):
                await env.plug(role=role)
            with pytest.raises(RoleDenied):
                await env.unplug(str(uuid4()), role=role)
        with pytest.raises(RoleDenied):
            await env.call("list", role=None)
        assert (await env.call("list", Role.VIEWER)).error_category is None

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_caller_outside_the_project_cannot_see_the_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        outsider = _ctx(Role.CONTRIBUTOR, projects=("other",))
        response = await with_runtime_for_system(
            env.server,
            env.resolver(),
            outsider,
            env.system_id,
            lambda runtime: list_devices(env.server, outsider, runtime, system_id=env.system_id),
            required_role=Role.VIEWER,
        )
        assert response.error_category in {
            "not_found",
            "configuration_error",
            "authorization_denied",
        }
        assert "devices" not in response.data

    _scenario(migrated_url, authority_role_dsns, body)


# --- end to end against fault-inject ----------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "extra", "identity_key"),
    [("nic", {"network": "net0"}, "mac"), ("disk", {"size_gb": 4}, "serial")],
)
def test_plug_list_and_unplug_work_end_to_end(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    kind: str,
    extra: dict[str, Any],
    identity_key: str,
) -> None:
    async def body(env: _Env) -> None:
        plugged = await env.plug(kind, **extra)
        device_id = str(plugged.data["device_id"])

        before = await env.listing()
        assert before["device_count"] == 1 and before["device_limit"] == 8
        (pending,) = before["devices"]
        assert pending["state"] == "plugging" and pending["identity"] == {}
        assert pending["settle_deadline"] is None  # the job has not started

        await env.run_worker(str(plugged.data["job_id"]))
        after = await env.listing()
        (device,) = after["devices"]
        assert device["device_id"] == device_id and device["state"] == "attached"
        assert device["kind"] == kind and set(device["identity"]) == {identity_key}
        assert device["settle_deadline"] is None and device["detail"] is None
        assert device["network"] == extra.get("network") and device["size_gb"] == extra.get(
            "size_gb"
        )
        assert after["server_time"]

        unplugged = await env.unplug(device_id)
        assert unplugged.status == "running", unplugged.model_dump()
        await env.run_worker(str(unplugged.data["job_id"]))
        assert (await env.listing())["devices"] == []

    _scenario(migrated_url, authority_role_dsns, body)


def test_list_shows_the_deadline_only_while_a_started_job_is_in_flight(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        device_id = await env.add_row(_PLUGGING)
        async with env.owner.connection() as conn:
            await conn.execute(
                "UPDATE system_devices SET settle_deadline = now() + interval '60 seconds' "
                "WHERE id = %s",
                (device_id,),
            )

        (device,) = (await env.listing())["devices"]

        assert device["state"] == "plugging" and device["settle_deadline"] is not None

    _scenario(migrated_url, authority_role_dsns, body)


def test_an_injected_fault_fails_the_job_and_the_row_carries_the_detail(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        engine = FaultEngine(seed=1, fault_rate={"hotplug": 1.0}, max_latency_s={})
        port = FaultedHotplug(FaultInjectHotplug(env.inventory), engine, env.inventory)
        plugged = await env.plug()

        with pytest.raises(CategorizedError) as excinfo:
            await env.run_worker(str(plugged.data["job_id"]), port)

        assert excinfo.value.category is ErrorCategory.INFRASTRUCTURE_FAILURE
        (device,) = (await env.listing())["devices"]
        assert device["state"] == "failed" and device["detail"]
        # A failed row is cleared by un-plug, which is the documented recovery.
        assert (await env.unplug(device["device_id"])).status == "running"

    _scenario(migrated_url, authority_role_dsns, body)


def test_systems_get_reports_the_hotplug_kinds(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        for capable, expected in ((True, ["disk", "nic"]), (False, [])):
            response = await get_system(
                env.server, _ctx(), env.system_id, resolver=env.resolver(capable=capable)
            )
            assert response.data["hotplug_devices"] == expected, response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


# --- reverse exclusion ------------------------------------------------------------------------


async def _power(env: _Env) -> ToolResponse:
    return await power_system(env.server, _ctx(), system_id=env.system_id, action="off")


async def _snapshot(env: _Env) -> ToolResponse:
    ctx = _ctx()
    return await with_runtime_for_system(
        env.server,
        provider_resolver(),
        ctx,
        env.system_id,
        lambda runtime: snapshot_system(
            env.server, ctx, runtime, system_id=env.system_id, name="cp", include_memory=False
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _restore(env: _Env) -> ToolResponse:
    ctx = _ctx()
    return await with_runtime_for_system(
        env.server,
        provider_resolver(),
        ctx,
        env.system_id,
        lambda runtime: restore_system(
            env.server, ctx, runtime, system_id=env.system_id, name="cp", start_paused=False
        ),
        required_role=Role.CONTRIBUTOR,
    )


async def _reprovision(env: _Env) -> ToolResponse:
    return await SYSTEM_ADMIN_HANDLERS.reprovision_system(
        env.server, _ctx(), system_id=env.system_id, profile=provisioning_profile()
    )


@pytest.mark.parametrize("job_state", ["queued", "running"])
@pytest.mark.parametrize("op", [_power, _snapshot, _restore, _reprovision])
def test_the_four_lifecycle_tools_refuse_while_a_device_job_is_active(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    op: Callable[[_Env], Awaitable[ToolResponse]],
    job_state: str,
) -> None:
    async def body(env: _Env) -> None:
        plugged = await env.plug()
        await env.settle_job(str(plugged.data["job_id"]), job_state)
        async with env.owner.connection() as conn:
            await SNAPSHOTS.insert(
                conn,
                Snapshot(
                    id=uuid4(),
                    created_at=runs_support.TEST_DT,
                    updated_at=runs_support.TEST_DT,
                    principal="user-1",
                    project="proj",
                    system_id=UUID(env.system_id),
                    name="cp",
                    include_memory=False,
                    state=SnapshotState.AVAILABLE,
                ),
            )

        response = await op(env)

        assert response.error_category == "conflict", response.model_dump()
        assert _reason(response) == "device_op_in_flight"
        assert response.suggested_next_actions == ["systems.list_devices", "systems.get"]
        for kind in (JobKind.POWER, JobKind.SNAPSHOT, JobKind.RESTORE, JobKind.REPROVISION):
            assert await env.jobs(kind) == []
        async with env.owner.connection() as conn:
            system = await SYSTEMS.get(conn, UUID(env.system_id))
        assert system is not None and system.state is SystemState.READY

    _scenario(migrated_url, authority_role_dsns, body)


@pytest.mark.parametrize("job_state", ["succeeded", "failed", "canceled"])
def test_the_lifecycle_tools_resume_once_the_device_job_is_terminal(
    migrated_url: str, authority_role_dsns: _RoleDsns, job_state: str
) -> None:
    async def body(env: _Env) -> None:
        plugged = await env.plug()
        await env.settle_job(str(plugged.data["job_id"]), job_state)

        response = await _power(env)

        assert response.status == "queued", response.model_dump()

    _scenario(migrated_url, authority_role_dsns, body)


def test_teardown_proceeds_while_a_device_job_is_active(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        assert (await env.plug()).status == "running"

        response = await teardown_system(env.server, _ctx(Role.ADMIN), env.system_id)

        assert response.status == "queued" and response.error_category is None, (
            response.model_dump()
        )
        assert len(await env.jobs(JobKind.TEARDOWN)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_device_job_probe_sees_only_active_device_jobs_of_this_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        other = await runs_support.seed_system(env.owner)
        async with env.server.connection() as conn:
            assert not await device_job_active(conn, UUID(env.system_id))
        plugged = await env.plug()
        async with env.server.connection() as conn:
            assert await device_job_active(conn, UUID(env.system_id))
            assert not await device_job_active(conn, UUID(other))
        await env.settle_job(str(plugged.data["job_id"]))
        async with env.server.connection() as conn:
            assert not await device_job_active(conn, UUID(env.system_id))
        refusal = device_op_in_flight(env.system_id)
        assert refusal.error_category == "conflict" and _reason(refusal) == "device_op_in_flight"

    _scenario(migrated_url, authority_role_dsns, body)


def test_concurrent_plugs_serialize_on_the_system_lock(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as holder:
            async with (
                holder.transaction(),
                advisory_xact_lock(holder, LockScope.SYSTEM, UUID(env.system_id)),
            ):
                racers = [asyncio.create_task(env.plug()) for _ in range(2)]
                await wait_until_any_backend_waiting(holder)
            results = await asyncio.gather(*racers)

        assert sorted(r.status for r in results) == ["error", "running"], [
            r.model_dump() for r in results
        ]
        (loser,) = (r for r in results if r.status == "error")
        assert _reason(loser) == "device_op_in_flight"
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1
        async with env.owner.connection() as conn:
            assert len(await devices_for_system(conn, UUID(env.system_id))) == 1

    _scenario(migrated_url, authority_role_dsns, body)


# --- idempotency key scope: one key, one System (#3713, ADR-0193 amendment) ------------------


@pytest.mark.parametrize("tool", ["plug", "unplug"])
def test_a_key_reused_on_a_second_system_conflicts_and_changes_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns, tool: str
) -> None:
    async def body(env: _Env) -> None:
        other_system = await runs_support.seed_system(env.owner)
        env.inventory.record(UUID(other_system), domain_name_for(UUID(other_system)))
        mine, theirs = await env.add_row(_ATTACHED), await env.add_row(_ATTACHED, other_system)

        async def call(system_id: str, device_id: UUID) -> ToolResponse:
            if tool == "plug":
                return await env.plug(system_id=system_id, idempotency_key="shared-key")
            return await env.unplug(
                str(device_id), system_id=system_id, idempotency_key="shared-key"
            )

        first = await call(env.system_id, mine)
        assert first.status == "running", first.model_dump()
        assert (await call(env.system_id, mine)).model_dump() == first.model_dump()

        other = await call(other_system, theirs)

        assert other.error_category == "conflict", other.model_dump()
        assert _reason(other) == "idempotency_key_in_use"
        job_kind = JobKind.PLUG_DEVICE if tool == "plug" else JobKind.UNPLUG_DEVICE
        assert len(await env.jobs(job_kind)) == 1
        async with env.owner.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) FROM jobs WHERE kind = %s AND payload->>'system_id' = %s",
                (job_kind.value, other_system),
            )
            assert await cur.fetchone() == (0,)

    _scenario(migrated_url, authority_role_dsns, body)


def test_an_unplug_key_reused_on_a_second_device_conflicts_and_changes_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first_device, second_device = await env.add_row(_ATTACHED), await env.add_row(_ATTACHED)
        first = await env.unplug(str(first_device), idempotency_key="shared-key")
        assert first.status == "running", first.model_dump()
        await env.settle_job(str(first.data["job_id"]))
        async with env.owner.connection() as conn:  # the first un-plug settled back to attached
            await SYSTEM_DEVICES.update_state(conn, first_device, _ATTACHED)

        other = await env.unplug(str(second_device), idempotency_key="shared-key")

        assert other.error_category == "conflict", other.model_dump()
        assert _reason(other) == "idempotency_key_in_use"
        assert len(await env.jobs(JobKind.UNPLUG_DEVICE)) == 1
        row = await env.row(second_device)
        assert row is not None and row.state is _ATTACHED

    _scenario(migrated_url, authority_role_dsns, body)


# --- idempotency key binds its arguments (#3735, ADR-0193 amendment) ------------------------


@pytest.mark.parametrize(
    "changed",
    [
        {"kind": "disk", "size_gb": 5, "network": None},
        {"network": "other-net"},
    ],
    ids=["kind", "network"],
)
def test_a_plug_key_reused_with_changed_arguments_conflicts_and_runs_no_work(
    migrated_url: str, authority_role_dsns: _RoleDsns, changed: dict[str, Any]
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug(idempotency_key="k", network="net-a")
        assert first.status == "running", first.model_dump()
        assert (await env.plug(idempotency_key="k", network="net-a")).model_dump() == (
            first.model_dump()
        )
        await env.settle_job(str(first.data["job_id"]))
        async with env.owner.connection() as conn:  # the first plug settled to attached
            await SYSTEM_DEVICES.update_state(conn, UUID(str(first.data["device_id"])), _ATTACHED)

        other = await env.plug(**{"idempotency_key": "k", "network": "net-a", **changed})

        assert other.error_category == "conflict", other.model_dump()
        assert _reason(other) == "idempotency_key_in_use"
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_plug_key_reused_with_a_changed_size_conflicts_and_runs_no_work(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug("disk", size_gb=4, idempotency_key="k")
        assert first.status == "running", first.model_dump()
        await env.settle_job(str(first.data["job_id"]))
        async with env.owner.connection() as conn:
            await SYSTEM_DEVICES.update_state(conn, UUID(str(first.data["device_id"])), _ATTACHED)

        other = await env.plug("disk", size_gb=5, idempotency_key="k")

        assert other.error_category == "conflict", other.model_dump()
        assert _reason(other) == "idempotency_key_in_use"
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)


def test_a_plug_key_reused_with_changed_arguments_mid_flight_is_refused_without_work(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def body(env: _Env) -> None:
        first = await env.plug(idempotency_key="k", network="net-a")
        assert first.status == "running", first.model_dump()

        other = await env.plug(idempotency_key="k", network="net-b")

        assert _reason(other) == "device_op_in_flight", other.model_dump()
        assert len(await env.jobs(JobKind.PLUG_DEVICE)) == 1

    _scenario(migrated_url, authority_role_dsns, body)

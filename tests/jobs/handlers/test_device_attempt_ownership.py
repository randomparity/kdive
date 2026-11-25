"""A device attempt owns its provider threads and ledger writes (ADR-0831)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from pydantic import SecretStr

from kdive.db.locks import LockScope, advisory_xact_lock, try_advisory_xact_lock
from kdive.db.repositories import JOBS, SYSTEM_DEVICES
from kdive.domain.capacity.state import SystemDeviceState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.records import SystemDevice
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.devices import plug_device_handler, unplug_device_handler
from kdive.jobs.payloads import Authorizing, PlugDevicePayload, UnplugDevicePayload
from kdive.jobs.provider_context import clear_provider_kind, take_provider_kind
from kdive.providers.ports.lifecycle import DeviceSpec, HotplugDeviceKind, PlugOutcome
from kdive.providers.shared.libvirt_hotplug import LibvirtDeviceHotplug, device_alias
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.conftest import _RoleDsns
from tests.db.system_ledger_support import connect, seed_system
from tests.db_waits import wait_until_backend_waiting
from tests.mcp.systems_support import provider_resolver
from tests.support.worker_fence import dequeue_as_current_worker, incarnation_credential

_PHASES = [
    ("plug", JobKind.PLUG_DEVICE, HotplugDeviceKind.NIC),
    ("unplug", JobKind.UNPLUG_DEVICE, HotplugDeviceKind.NIC),
    ("force", JobKind.FORCE_UNPLUG_DEVICE, HotplugDeviceKind.NIC),
    ("cleanup", JobKind.UNPLUG_DEVICE, HotplugDeviceKind.DISK),
]
_IDENTITY = {"mac": "52:54:00:00:00:02"}


class _Gate:
    def __init__(self, action: Callable[[], None] | None = None) -> None:
        self.entered = asyncio.Event()
        self.finished = asyncio.Event()
        self.release = threading.Event()
        self.loop = asyncio.get_running_loop()
        self.action = action

    def call[T](self, result: T) -> T:
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.release.wait(5):
                raise TimeoutError("test did not release its provider callback")
            if self.action is not None:
                self.action()
            return result
        finally:
            self.loop.call_soon_threadsafe(self.finished.set)

    async def finish(self, task: asyncio.Task) -> None:
        self.release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
        if self.entered.is_set():
            await asyncio.wait_for(self.finished.wait(), 5)


class _AbsentDomain:
    def XMLDesc(self, flags: int = 0) -> str:  # noqa: N802
        return "<domain><devices/></domain>"


class _AbsentConnection:
    def lookupByName(self, name: str) -> _AbsentDomain:  # noqa: N802
        return _AbsentDomain()


class _CleanupVolumes:
    def __init__(self, gate: _Gate) -> None:
        self.gate = gate

    def remove(self, conn: Any, domain: str, device_id: str) -> None:
        self.gate.call(None)

    def create(self, *args: Any) -> str:
        raise AssertionError("absence observation must not create storage")

    def remove_all(self, *args: Any) -> None:
        raise AssertionError("absence observation must not remove other storage")


class _Port:
    def __init__(self, phase: str, gate: _Gate, *, pending: bool = False) -> None:
        self.phase = phase
        self.gate = gate
        self.pending = pending
        self.cleanup: LibvirtDeviceHotplug | None = None
        if phase == "cleanup":

            @contextmanager
            def connection():
                yield cast(Any, _AbsentConnection())

            self.cleanup = LibvirtDeviceHotplug(
                connection=connection,
                volumes=_CleanupVolumes(gate),
                nic_source=lambda _: ("user", None),
                kinds=frozenset({HotplugDeviceKind.DISK}),
            )

    def plug(self, domain: str, spec: DeviceSpec) -> PlugOutcome:
        outcome = PlugOutcome("owned-handle", _IDENTITY, settled=not self.pending)
        if self.phase == "plug-poll":
            return PlugOutcome("owned-handle", _IDENTITY, settled=False)
        assert self.phase == "plug", "stale attempt reached the provider"
        return self.gate.call(outcome)

    def unplug(
        self,
        domain: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        if self.phase == "cleanup":
            return False
        assert self.phase in {"unplug", "readonly"}
        return False if self.phase == "readonly" else self.gate.call(not self.pending)

    def force_unplug(
        self,
        domain: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        assert self.phase == "force"
        return self.gate.call(not self.pending)

    def is_attached(
        self,
        domain: str,
        device_id: str,
        provider_handle: str | None,
        *,
        kind: HotplugDeviceKind,
    ) -> bool:
        if self.cleanup is not None:
            return self.cleanup.is_attached(domain, device_id, provider_handle, kind=kind)
        if self.phase in {"plug-poll", "readonly"}:
            return self.gate.call(True)
        return self.phase != "plug"

    def release_all(self, domain: str) -> None:
        raise AssertionError("device operation must not release its System")


@dataclass
class _Attempt:
    owner: psycopg.AsyncConnection
    worker: psycopg.AsyncConnection
    system_id: UUID
    device_id: UUID
    job: Job
    credential: SecretStr

    async def invoke(self, port: _Port, *, window_s: float = 0.02) -> str | None:
        handler = (
            plug_device_handler if self.job.kind is JobKind.PLUG_DEVICE else unplug_device_handler
        )
        return await handler(
            self.worker,
            self.job,
            resolver=provider_resolver(hotplugger=port),
            secret_registry=SecretRegistry(),
            incarnation_credential=self.credential,
            window_s=window_s,
            poll_interval_s=0.001,
        )

    async def row(self) -> SystemDevice | None:
        return await SYSTEM_DEVICES.get(self.owner, self.device_id)

    async def replace_claim(self) -> Job:
        await self.owner.execute(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 minute' "
            "WHERE id = %s",
            (self.job.id,),
        )
        claimed = await dequeue_as_current_worker(self.owner, "replacement-device-worker")
        assert claimed is not None and claimed.id == self.job.id
        assert claimed.attempt == self.job.attempt + 1
        return claimed

    async def lock_is_available(self) -> bool:
        async with self.owner.transaction():
            return await try_advisory_xact_lock(self.owner, LockScope.SYSTEM, self.system_id)


@asynccontextmanager
async def _attempt(
    migrated_url: str,
    role_dsns: _RoleDsns,
    kind: JobKind,
    device_kind: HotplugDeviceKind = HotplugDeviceKind.NIC,
) -> AsyncGenerator[_Attempt]:
    async with (
        await connect(migrated_url) as owner,
        await connect(role_dsns("kdive_worker")) as worker,
    ):
        system_id = await seed_system(owner)
        device_id = uuid4()
        now = datetime.now(UTC)
        await SYSTEM_DEVICES.insert(
            owner,
            SystemDevice(
                id=device_id,
                created_at=now,
                updated_at=now,
                system_id=system_id,
                kind=device_kind.value,
                state=(
                    SystemDeviceState.PLUGGING
                    if kind is JobKind.PLUG_DEVICE
                    else SystemDeviceState.UNPLUGGING
                ),
                size_gb=1 if device_kind is HotplugDeviceKind.DISK else None,
                provider_handle=None
                if kind is JobKind.PLUG_DEVICE
                else device_alias(str(device_id)),
            ),
        )
        payload = (
            PlugDevicePayload(system_id=str(system_id), device_id=str(device_id))
            if kind is JobKind.PLUG_DEVICE
            else UnplugDevicePayload(
                system_id=str(system_id),
                device_id=str(device_id),
                from_state=SystemDeviceState.FAILED,
            )
        )
        queued = await queue.enqueue(
            owner,
            kind,
            payload,
            Authorizing(principal="p", agent_session=None, project="proj"),
            str(uuid4()),
            max_attempts=2,
        )
        job = await dequeue_as_current_worker(owner, "owning-device-worker")
        assert job is not None and job.id == queued.id
        yield _Attempt(
            owner, worker, system_id, device_id, job, incarnation_credential("owning-device-worker")
        )


@pytest.mark.anyio
@pytest.mark.parametrize(("phase", "kind", "device_kind"), _PHASES)
async def test_started_mutation_retains_system_lock_past_observation_expiry(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    tmp_path: Path,
    phase: str,
    kind: JobKind,
    device_kind: HotplugDeviceKind,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, kind, device_kind) as env:
        backing = tmp_path / "owned-scratch"
        backing.touch()
        gate = _Gate(backing.unlink if phase == "cleanup" else None)
        task = asyncio.create_task(env.invoke(_Port(phase, gate)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            done, _pending = await asyncio.wait({task}, timeout=0.06)
            assert not done, "terminal result escaped while the provider mutation still runs"
            assert not await env.lock_is_available(), "the running mutation lost its System lock"
            gate.release.set()
            assert await asyncio.wait_for(task, 5) == str(env.device_id)
            assert gate.finished.is_set()
            row = await env.row()
            if phase == "plug":
                assert row is not None and row.state is SystemDeviceState.ATTACHED
                assert row.provider_handle == "owned-handle" and row.identity == _IDENTITY
            else:
                assert row is None
            if phase == "cleanup":
                assert not backing.exists()
            assert await env.lock_is_available()
        finally:
            await gate.finish(task)


@pytest.mark.anyio
@pytest.mark.parametrize(("phase", "kind", "device_kind"), _PHASES)
async def test_repeated_cancellation_drains_mutation_before_releasing_system(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    tmp_path: Path,
    phase: str,
    kind: JobKind,
    device_kind: HotplugDeviceKind,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, kind, device_kind) as env:
        backing = tmp_path / "owned-scratch"
        backing.touch()
        gate = _Gate(backing.unlink if phase == "cleanup" else None)
        seen_kind = []
        clear_provider_kind()

        async def invoke() -> str | None:
            try:
                return await env.invoke(_Port(phase, gate))
            finally:
                seen_kind.append(take_provider_kind())

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            done, _pending = await asyncio.wait({task}, timeout=0.06)
            assert not done, "cancelled handler returned before its mutation drained"
            assert not await env.lock_is_available()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert gate.finished.is_set() and await env.lock_is_available()
            assert seen_kind == [ResourceKind.LOCAL_LIBVIRT.value]
        finally:
            await gate.finish(task)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("phase", "kind"), [("plug", JobKind.PLUG_DEVICE), ("unplug", JobKind.UNPLUG_DEVICE)]
)
async def test_late_pending_mutation_preserves_recovery_evidence(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    phase: str,
    kind: JobKind,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, kind) as env:
        gate = _Gate()
        task = asyncio.create_task(env.invoke(_Port(phase, gate, pending=True)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            done, _pending = await asyncio.wait({task}, timeout=0.06)
            assert not done
            gate.release.set()
            with pytest.raises(CategorizedError) as caught:
                await asyncio.wait_for(task, 5)
            assert caught.value.details["reason"] == "settle_timeout"
            row = await env.row()
            assert row is not None and row.state is SystemDeviceState.FAILED
            expected = "owned-handle" if phase == "plug" else device_alias(str(env.device_id))
            assert row.provider_handle == expected
            if phase == "plug":
                assert row.identity == _IDENTITY
        finally:
            await gate.finish(task)


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ["plug", "plug-poll", "unplug"])
async def test_replaced_claim_cannot_change_the_device_ledger(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    phase: str,
) -> None:
    kind = JobKind.UNPLUG_DEVICE if phase == "unplug" else JobKind.PLUG_DEVICE
    async with _attempt(migrated_url, authority_role_dsns, kind) as env:
        gate = _Gate()
        task = asyncio.create_task(env.invoke(_Port(phase, gate), window_s=5))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            replacement = await env.replace_claim()
            current = await env.row()
            gate.release.set()
            assert await asyncio.wait_for(task, 5) == str(env.device_id)
            assert await env.row() == current
            assert await JOBS.get(env.owner, replacement.id) == replacement
        finally:
            await gate.finish(task)


@pytest.mark.anyio
async def test_claim_is_rechecked_after_waiting_for_the_system_lock(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env:
        gate = _Gate()
        task = None
        try:
            async with (
                env.owner.transaction(),
                advisory_xact_lock(env.owner, LockScope.SYSTEM, env.system_id),
            ):
                task = asyncio.create_task(env.invoke(_Port("forbidden", gate), window_s=5))
                await wait_until_backend_waiting(
                    env.owner, env.worker.info.backend_pid, locktype="advisory"
                )
                replacement = await env.replace_claim()
                current = await env.row()
            assert await asyncio.wait_for(task, 5) == str(env.device_id)
            assert not gate.entered.is_set()
            assert await env.row() == current
            assert await JOBS.get(env.owner, replacement.id) == replacement
        finally:
            if task is not None:
                await gate.finish(task)


@pytest.mark.anyio
async def test_ledger_write_rechecks_the_claim_after_its_row_lock_wait(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env:
        gate = _Gate()
        task = asyncio.create_task(env.invoke(_Port("plug", gate), window_s=5))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            async with env.owner.transaction():
                await env.owner.execute(
                    "SELECT id FROM jobs WHERE id = %s FOR UPDATE", (env.job.id,)
                )
                gate.release.set()
                await wait_until_backend_waiting(
                    env.owner, env.worker.info.backend_pid, locktype="transactionid"
                )
                replacement = await env.replace_claim()
                current = await env.row()
            assert await asyncio.wait_for(task, 5) == str(env.device_id)
            assert await env.row() == current
            assert await JOBS.get(env.owner, replacement.id) == replacement
        finally:
            await gate.finish(task)


@pytest.mark.anyio
async def test_readonly_nic_poll_keeps_its_deadline(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.UNPLUG_DEVICE) as env:
        gate = _Gate()
        task = asyncio.create_task(env.invoke(_Port("readonly", gate)))
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            with pytest.raises(CategorizedError) as caught:
                await asyncio.wait_for(asyncio.shield(task), 1)
            assert caught.value.details["reason"] == "settle_timeout"
            assert not gate.finished.is_set()
            assert await env.lock_is_available()
        finally:
            await gate.finish(task)


@pytest.mark.anyio
async def test_success_restores_connection_mode_and_provider_kind(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env:
        await env.worker.set_autocommit(False)
        clear_provider_kind()
        gate = _Gate()
        gate.release.set()
        assert await env.invoke(_Port("plug", gate), window_s=5) == str(env.device_id)
        assert not env.worker.autocommit
        assert env.worker.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
        assert take_provider_kind() == ResourceKind.LOCAL_LIBVIRT.value


@pytest.mark.anyio
async def test_active_transaction_is_rejected_before_provider_io(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env:
        gate = _Gate()
        gate.release.set()
        async with env.worker.transaction():
            with pytest.raises(RuntimeError, match="transaction"):
                await env.invoke(_Port("plug", gate), window_s=5)
        assert not gate.entered.is_set()


@pytest.mark.anyio
async def test_cancellation_stays_primary_when_the_drained_provider_fails(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env:
        await env.worker.set_autocommit(False)

        def fail() -> None:
            raise ValueError("provider failed during drain")

        gate = _Gate(fail)
        seen_kind = []
        clear_provider_kind()

        async def invoke() -> str | None:
            try:
                return await env.invoke(_Port("plug", gate), window_s=5)
            finally:
                seen_kind.append(take_provider_kind())

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            task.cancel("caller cancelled the operation")
            await asyncio.sleep(0)
            assert not task.done() and not await env.lock_is_available()
            gate.release.set()
            with pytest.raises(asyncio.CancelledError, match="caller cancelled"):
                await asyncio.wait_for(task, 5)
            row = await env.row()
            assert row is not None and row.state is SystemDeviceState.FAILED
            assert not env.worker.autocommit
            assert env.worker.info.transaction_status is psycopg.pq.TransactionStatus.IDLE
            assert seen_kind == [ResourceKind.LOCAL_LIBVIRT.value]
            assert await env.lock_is_available()
        finally:
            await gate.finish(task)


@pytest.mark.anyio
async def test_claim_row_stays_locked_until_its_ledger_write_commits(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    async with (
        _attempt(migrated_url, authority_role_dsns, JobKind.PLUG_DEVICE) as env,
        await connect(authority_role_dsns("kdive_reconciler")) as writer,
    ):
        gate = _Gate()
        task = asyncio.create_task(env.invoke(_Port("plug", gate), window_s=5))
        write = None
        try:
            await asyncio.wait_for(gate.entered.wait(), 5)
            async with env.owner.transaction():
                await env.owner.execute(
                    "SELECT id FROM system_devices WHERE id = %s FOR UPDATE", (env.device_id,)
                )
                gate.release.set()
                await wait_until_backend_waiting(
                    env.owner, env.worker.info.backend_pid, locktype="transactionid"
                )
                write = asyncio.create_task(
                    writer.execute(
                        "UPDATE jobs SET state = 'failed', error_category = 'control_failure' "
                        "WHERE id = %s RETURNING id",
                        (env.job.id,),
                    )
                )
                await wait_until_backend_waiting(
                    env.owner, writer.info.backend_pid, locktype="transactionid"
                )
                cur = await env.owner.execute(
                    "SELECT pg_blocking_pids(%s)", (writer.info.backend_pid,)
                )
                blockers = await cur.fetchone()
                assert blockers is not None and env.worker.info.backend_pid in blockers[0]
                assert not task.done() and not write.done()
            assert await asyncio.wait_for(task, 5) == str(env.device_id)
            cur = await asyncio.wait_for(write, 5)
            assert await cur.fetchone() == (env.job.id,)
            row = await env.row()
            assert row is not None and row.provider_handle == "owned-handle"
        finally:
            await gate.finish(task)
            if write is not None:
                if not write.done():
                    write.cancel()
                await asyncio.wait_for(asyncio.gather(write, return_exceptions=True), 5)

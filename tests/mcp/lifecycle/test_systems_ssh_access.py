"""Tests for systems.ssh_info / systems.authorize_ssh_key (ADR-0271, #782)."""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.mcp.tools.lifecycle.systems.ssh_access import (
    authorize_ssh_key,
    check_ssh_reachable,
    ssh_info,
)
from kdive.security.authz.rbac import AuthorizationError, Role
from tests.mcp.systems_support import TEST_DT as _DT
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import granted_allocation as _granted_allocation
from tests.mcp.systems_support import pool as _pool
from tests.mcp.systems_support import provider_resolver as _provider_resolver
from tests.mcp.systems_support import provisioning_profile as _profile

_GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 agent@host"


class _FakeConnector:
    def __init__(self, endpoint: tuple[str, int] | None) -> None:
        self._endpoint = endpoint
        self.seen_handles: list[str] = []

    def recorded_ssh_endpoint(self, system: object) -> tuple[str, int] | None:
        # Capture the handle: the connector resolves the libvirt domain by name, so the caller
        # must pass the System's `kdive-<id>` domain name, not the bare id (regression for the
        # live-proof bug where the bare id raised VIR_ERR_NO_DOMAIN -> spurious unprovisioned).
        self.seen_handles.append(str(system))
        return self._endpoint


async def _seed_system(
    pool, alloc_id: str, state: SystemState, *, domain_name: str | None = None
) -> str:
    sid = uuid4()
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=sid,
                created_at=_DT,
                updated_at=_DT,
                principal="user-1",
                project="proj",
                allocation_id=UUID(alloc_id),
                state=state,
                provisioning_profile=_profile(),
                domain_name=domain_name if domain_name is not None else f"kdive-{sid}",
            ),
        )
    return str(system.id)


def test_ssh_info_ready_returns_descriptor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.status == "ok"
        ssh = resp.data["ssh"]
        assert ssh == {
            "user": "root",
            "host": "127.0.0.1",
            "port": 22022,
            "jump_host": None,
            "host_scope": "worker_loopback",
        }
        assert isinstance(ssh, dict)
        assert isinstance(ssh["port"], int)  # native JSON int, not float (ADR-0263)
        assert "systems.authorize_ssh_key" in resp.suggested_next_actions

    asyncio.run(_run())


def test_ssh_info_resolves_endpoint_by_domain_name(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(
                pool, alloc_id, SystemState.READY, domain_name="kdive-vm-xyz"
            )
            connector = _FakeConnector(("127.0.0.1", 22022))
            resolver = _provider_resolver(connector=connector)
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.status == "ok"
        # The connector looks up the live libvirt domain by name, so it must receive the System's
        # domain name (`kdive-…`), not the bare system_id.
        assert connector.seen_handles == ["kdive-vm-xyz"]

    asyncio.run(_run())


def test_ssh_info_viewer_omits_operator_next_action(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await ssh_info(pool, _ctx(role=Role.VIEWER), sys_id, resolver=resolver)
        assert "systems.authorize_ssh_key" not in resp.suggested_next_actions

    asyncio.run(_run())


def test_ssh_info_not_ready_is_readiness_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.READINESS_FAILURE.value

    asyncio.run(_run())


def test_ssh_info_unprovisioned_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(None))
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "ssh_not_provisioned"
        # The detail describes the provider-capability gap (local-libvirt only), not a reprovision:
        # the local forward is always rendered now (ADR-0281), so a None endpoint means the
        # provider exposes no loopback SSH forward, not a missing per-profile credential.
        assert resp.detail is not None
        assert "local-libvirt" in resp.detail
        assert "reprovision" not in resp.detail.lower()

    asyncio.run(_run())


def test_authorize_ssh_key_malformed_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await authorize_ssh_key(pool, _ctx(), sys_id, "not-a-key", resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_authorize_ssh_key_not_ready_is_readiness_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await authorize_ssh_key(pool, _ctx(), sys_id, _GOOD_KEY, resolver=resolver)
        assert resp.error_category == ErrorCategory.READINESS_FAILURE.value

    asyncio.run(_run())


def test_authorize_ssh_key_viewer_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            with pytest.raises(AuthorizationError):
                await authorize_ssh_key(
                    pool, _ctx(role=Role.VIEWER), sys_id, _GOOD_KEY, resolver=resolver
                )

    asyncio.run(_run())


def test_authorize_ssh_key_happy_path_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await authorize_ssh_key(
                pool, _ctx(), sys_id, f"  {_GOOD_KEY}\n", resolver=resolver
            )
        assert resp.status == "queued"
        assert resp.data["kind"] == "authorize_ssh_key"

    asyncio.run(_run())


def test_authorize_ssh_key_distinct_keys_enqueue_distinct_jobs(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            key_a = "ssh-ed25519 AAAAaaaa agent-a@host"
            key_b = "ssh-ed25519 AAAAbbbb agent-b@host"
            first = await authorize_ssh_key(pool, _ctx(), sys_id, key_a, resolver=resolver)
            second = await authorize_ssh_key(pool, _ctx(), sys_id, key_b, resolver=resolver)
            # re-authorizing key_a is idempotent: same dedup_key returns the first job
            replay = await authorize_ssh_key(pool, _ctx(), sys_id, key_a, resolver=resolver)
        assert first.object_id != second.object_id  # distinct keys -> distinct jobs
        assert replay.object_id == first.object_id  # same key -> same job (idempotent)

    asyncio.run(_run())


def test_check_ssh_reachable_ready_enqueues_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.status == "queued"
        assert resp.data["kind"] == "check_ssh_reachable"

    asyncio.run(_run())


def test_check_ssh_reachable_viewer_can_enqueue(migrated_url: str) -> None:
    # Unlike authorize_ssh_key (OPERATOR), the probe is read-only observability gated at VIEWER.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await check_ssh_reachable(
                pool, _ctx(role=Role.VIEWER), sys_id, resolver=resolver
            )
        assert resp.status == "queued"

    asyncio.run(_run())


def test_check_ssh_reachable_fresh_job_each_call(migrated_url: str) -> None:
    # A liveness probe is a fresh measurement each call: the nonce dedup_key mints a distinct job,
    # so a re-issue is never pinned to a prior (terminal) job's stale verdict (ADR-0298).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            first = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
            second = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
        assert first.object_id != second.object_id

    asyncio.run(_run())


def test_check_ssh_reachable_not_ready_is_readiness_failure(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.PROVISIONING)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.READINESS_FAILURE.value

    asyncio.run(_run())


def test_check_ssh_reachable_unprovisioned_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(None))
            resp = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "ssh_not_provisioned"

    asyncio.run(_run())


def test_ssh_info_suggests_check_ssh_reachable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert "systems.check_ssh_reachable" in resp.suggested_next_actions

    asyncio.run(_run())

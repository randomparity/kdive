"""Tests for systems.ssh_info / systems.authorize_ssh_key (ADR-0271, #782)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import ExternalBootActivationState, SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job
from kdive.jobs.handlers.connectivity.ssh_authorize import authorize_ssh_key_handler
from kdive.jobs.handlers.connectivity.ssh_reachable import ReachResult, check_ssh_reachable_handler
from kdive.mcp.tools.lifecycle.systems.ssh_access import (
    authorize_ssh_key,
    check_ssh_reachable,
    ssh_info,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports.handles import SystemHandle
from kdive.security.authz.rbac import AuthorizationError, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.system_bootstrap_key import ensure_system_bootstrap_key
from tests.mcp.systems_support import TEST_DT as _DT
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import granted_allocation as _granted_allocation
from tests.mcp.systems_support import pool as _pool
from tests.mcp.systems_support import provider_resolver as _provider_resolver
from tests.mcp.systems_support import provisioning_profile as _profile
from tests.services.external_boot.conftest import seed_activation

_GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 agent@host"


class _FakeConnector:
    def __init__(
        self, endpoint: tuple[str, int] | None, *, raises: CategorizedError | None = None
    ) -> None:
        self._endpoint = endpoint
        self._raises = raises
        self.seen_handles: list[str] = []
        self.seen_forwards: list[int | None] = []

    def recorded_ssh_endpoint(
        self, system: object, *, ssh_forward_port: int | None = None
    ) -> tuple[str, int] | None:
        self.seen_forwards.append(ssh_forward_port)
        # Capture the handle: the connector resolves the libvirt domain by name, so the caller
        # must pass the System's `kdive-<id>` domain name, not the bare id (regression for the
        # live-proof bug where the bare id raised VIR_ERR_NO_DOMAIN -> spurious unprovisioned).
        self.seen_handles.append(str(system))
        if self._raises is not None:
            raise self._raises
        return self._endpoint


def _no_domain_error() -> CategorizedError:
    """The error local-libvirt now raises when the connection has no domain (#2502, ADR-0658)."""
    return CategorizedError(
        "System 'kdive-x' has no libvirt domain on this connection; check that the System is "
        "running and that this process and the worker that provisioned it read the same "
        "libvirt endpoint",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "system_domain_not_found"},
    )


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


async def _mark_authority_owned(
    pool,
    system_id: str,
    *,
    state: str = "ready",
    ssh_forward_port: int | None = None,
    provider_kind: str = "local-libvirt",
    activation: ExternalBootActivationState | None = None,
) -> None:
    """Seed the ownership row; ``activation`` first seeds a real row for ``activated`` to name."""
    async with pool.connection() as conn:
        first_activation_id = None
        if activation is not None:
            run_id = await _seed_run(conn, UUID(system_id))
            seeded = await seed_activation(
                conn,
                state=activation,
                cleanup_complete=activation is not ExternalBootActivationState.ACTIVE,
                system_id=UUID(system_id),
                run_id=run_id,
            )
            first_activation_id = seeded.activation.id
        await conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,"
            "authority_instance,profile_identity,root_identity,state,ssh_forward_port,"
            "first_activation_id) "
            "SELECT system.id,system.allocation_id,allocation.resource_id,%s,"
            "'host-a','authority-a','sha256:' || repeat('a',64),"
            "'sha256:' || repeat('b',64),%s,%s,%s "
            "FROM systems AS system JOIN allocations AS allocation "
            "ON allocation.id=system.allocation_id WHERE system.id=%s",
            (provider_kind, state, ssh_forward_port, first_activation_id, system_id),
        )


async def _seed_run(conn, system_id: UUID) -> UUID:
    investigation_id, run_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES "
        "(%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return run_id


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


def test_ssh_info_absent_domain_is_distinguishable_from_unprovisioned(migrated_url: str) -> None:
    # #2502: a missing domain and a missing forward are different faults with different fixes.
    # They must not share one reason, and the detail must name which one occurred.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(None, raises=_no_domain_error()))
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "system_domain_not_found"
        assert resp.detail is not None
        assert "libvirt endpoint" in resp.detail

    asyncio.run(_run())


def test_authorize_ssh_key_absent_domain_is_distinguishable(migrated_url: str) -> None:
    # The mutating tool reads the same port and must report the same distinction (#2502).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(None, raises=_no_domain_error()))
            resp = await authorize_ssh_key(pool, _ctx(), sys_id, _GOOD_KEY, resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "system_domain_not_found"

    asyncio.run(_run())


def test_check_ssh_reachable_absent_domain_is_distinguishable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(None, raises=_no_domain_error()))
            resp = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "system_domain_not_found"

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


def test_authorize_ssh_key_contributor_can_enqueue(migrated_url: str) -> None:
    # Leaseholder-control (#1080, ADR-0320): adding a key to a VM the caller already sudos is
    # weaker than the contributor's existing powers, so the gate is contributor, not operator.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await authorize_ssh_key(
                pool, _ctx(role=Role.CONTRIBUTOR), sys_id, _GOOD_KEY, resolver=resolver
            )
        assert resp.status == "queued"
        assert resp.data["kind"] == "authorize_ssh_key"

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


def test_authorize_ssh_key_fences_before_provider_lookup(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            await _mark_authority_owned(pool, sys_id)
            connector = _FakeConnector(("127.0.0.1", 22022))
            resp = await authorize_ssh_key(
                pool, _ctx(), sys_id, _GOOD_KEY, resolver=_provider_resolver(connector=connector)
            )

        assert resp.error_category == ErrorCategory.CONFLICT.value
        assert resp.data["reason"] == "authority_system_preactivation_mutation_fenced"
        assert connector.seen_handles == []

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
    # Unlike authorize_ssh_key (CONTRIBUTOR), the probe is read-only observability gated at VIEWER.
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


def test_check_ssh_reachable_suggests_authorize_ssh_key(migrated_url: str) -> None:
    # The auth-free probe must point a contributor at authorize_ssh_key so reachable=true is not
    # misread as login-ready (#1250); the generic lifecycle actions stay present too.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await check_ssh_reachable(
                pool, _ctx(role=Role.CONTRIBUTOR), sys_id, resolver=resolver
            )
        assert "systems.authorize_ssh_key" in resp.suggested_next_actions
        assert "jobs.wait" in resp.suggested_next_actions

    asyncio.run(_run())


def test_check_ssh_reachable_viewer_omits_authorize_next_action(migrated_url: str) -> None:
    # authorize_ssh_key is CONTRIBUTOR-gated; a viewer-only caller must not see it as a next action.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resolver = _provider_resolver(connector=_FakeConnector(("127.0.0.1", 22022)))
            resp = await check_ssh_reachable(
                pool, _ctx(role=Role.VIEWER), sys_id, resolver=resolver
            )
        assert "systems.authorize_ssh_key" not in resp.suggested_next_actions
        assert "jobs.wait" in resp.suggested_next_actions

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


# --- authority-owned Systems reach the tools through the stored port (#3541, ADR-0811) -----------

_FORWARD = 41234
_RECOVERED = ExternalBootActivationState.RECOVERED
_ACTIVE = ExternalBootActivationState.ACTIVE


def _invisible_domain_resolver() -> ProviderResolver:
    """A real local connector over a domain this process cannot see, as #3541 reported."""

    def invisible(_system: SystemHandle) -> tuple[str, int]:
        raise _no_domain_error()

    connector = LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1234),
        resolve_ssh_endpoint=invisible,
        ssh_connect=lambda _host, _port: True,
    )
    return _provider_resolver(connector=connector)


async def _authority_system(pool, **ownership: Any) -> str:
    alloc_id = await _granted_allocation(pool)
    sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
    await _mark_authority_owned(pool, sys_id, **ownership)
    return sys_id


async def _job_payload(pool, job_id: str) -> dict[str, object]:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT payload FROM jobs WHERE id=%s", (job_id,))
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


async def _job_count(pool) -> int:
    async with pool.connection() as conn:
        cursor = await conn.execute("SELECT count(*) FROM jobs")
        row = await cursor.fetchone()
    assert row is not None
    return row[0]


@pytest.mark.parametrize(
    ("state", "activation"),
    [("ready", None), ("activated", _RECOVERED), ("activated", _ACTIVE)],
    ids=["ready", "activated", "activated-restricting"],
)
def test_ssh_info_reports_the_stored_port(
    migrated_url: str, state: str, activation: ExternalBootActivationState | None
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(
                pool, state=state, activation=activation, ssh_forward_port=_FORWARD
            )
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=_invisible_domain_resolver())
        assert resp.status == "ok"
        ssh = resp.data["ssh"]
        assert isinstance(ssh, dict)
        assert (ssh["host"], ssh["port"]) == ("127.0.0.1", _FORWARD)
        if state == "ready":
            assert resp.suggested_next_actions == ["systems.get"]
        else:
            assert "systems.check_ssh_reachable" in resp.suggested_next_actions
            assert "systems.authorize_ssh_key" in resp.suggested_next_actions

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("ownership", "reason"),
    [
        ({"state": "ready"}, "authority_ssh_forward_unreported"),
        (
            {"state": "activated", "activation": _RECOVERED},
            "authority_ssh_forward_unreported",
        ),
        (
            {"state": "teardown-requested", "ssh_forward_port": _FORWARD},
            "authority_system_not_attachable",
        ),
    ],
    ids=["ready", "activated", "teardown-requested"],
)
def test_ssh_info_refuses_a_system_with_no_usable_forward(
    migrated_url: str, ownership: dict[str, Any], reason: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, **ownership)
            resp = await ssh_info(pool, _ctx(), sys_id, resolver=_invisible_domain_resolver())
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == reason

    asyncio.run(_run())


@pytest.mark.parametrize("activation", [_RECOVERED, _ACTIVE], ids=["no-restriction", "restricting"])
def test_check_ssh_reachable_carries_the_stored_port_once_activated(
    migrated_url: str, activation: ExternalBootActivationState
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(
                pool, state="activated", activation=activation, ssh_forward_port=_FORWARD
            )
            resp = await check_ssh_reachable(
                pool, _ctx(), sys_id, resolver=_invisible_domain_resolver()
            )
            assert resp.status == "queued"
            payload = await _job_payload(pool, resp.object_id)
        assert payload == {"system_id": sys_id, "ssh_forward_port": _FORWARD}

    asyncio.run(_run())


@pytest.mark.parametrize("state", ["ready", "teardown-requested"])
def test_check_ssh_reachable_stays_fenced_before_the_port_read(
    migrated_url: str, state: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, state=state, ssh_forward_port=_FORWARD)
            connector = _FakeConnector(("127.0.0.1", 22022))
            resp = await check_ssh_reachable(
                pool, _ctx(), sys_id, resolver=_provider_resolver(connector=connector)
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "authority_system_preactivation_mutation_fenced"
        assert connector.seen_handles == []

    asyncio.run(_run())


def test_check_ssh_reachable_refuses_an_unreported_forward_without_a_job(
    migrated_url: str,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, state="activated", activation=_RECOVERED)
            before = await _job_count(pool)
            resp = await check_ssh_reachable(
                pool, _ctx(), sys_id, resolver=_invisible_domain_resolver()
            )
            assert await _job_count(pool) == before
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "authority_ssh_forward_unreported"

    asyncio.run(_run())


def test_authorize_ssh_key_carries_the_stored_port_once_activated(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(
                pool, state="activated", activation=_RECOVERED, ssh_forward_port=_FORWARD
            )
            resp = await authorize_ssh_key(
                pool, _ctx(), sys_id, _GOOD_KEY, resolver=_invisible_domain_resolver()
            )
            assert resp.status == "queued"
            payload = await _job_payload(pool, resp.object_id)
        assert payload == {
            "system_id": sys_id,
            "public_key": _GOOD_KEY,
            "ssh_forward_port": _FORWARD,
        }

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("ownership", "reason"),
    [
        ({"state": "ready"}, "authority_system_preactivation_mutation_fenced"),
        ({"state": "teardown-requested"}, "authority_system_preactivation_mutation_fenced"),
        ({"state": "activated", "activation": _ACTIVE}, "external_boot_restricted"),
    ],
    ids=["ready", "teardown-requested", "activated-restricting"],
)
def test_authorize_ssh_key_keeps_its_denials_before_the_port_read(
    migrated_url: str, ownership: dict[str, Any], reason: str
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, ssh_forward_port=_FORWARD, **ownership)
            connector = _FakeConnector(("127.0.0.1", 22022))
            resp = await authorize_ssh_key(
                pool, _ctx(), sys_id, _GOOD_KEY, resolver=_provider_resolver(connector=connector)
            )
        assert resp.error_category == ErrorCategory.CONFLICT.value
        assert resp.data["reason"] == reason
        assert connector.seen_handles == []

    asyncio.run(_run())


def test_authorize_ssh_key_refuses_an_unreported_forward_without_a_job(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, state="activated", activation=_RECOVERED)
            before = await _job_count(pool)
            resp = await authorize_ssh_key(
                pool, _ctx(), sys_id, _GOOD_KEY, resolver=_invisible_domain_resolver()
            )
            assert await _job_count(pool) == before
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
        assert resp.data["reason"] == "authority_ssh_forward_unreported"

    asyncio.run(_run())


@pytest.mark.parametrize("remote", [False, True], ids=["non-authority", "remote-authority"])
def test_other_systems_keep_the_provider_read_and_a_port_free_payload(
    migrated_url: str, remote: bool
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            if remote:
                await _mark_authority_owned(
                    pool,
                    sys_id,
                    state="activated",
                    activation=_RECOVERED,
                    provider_kind="remote-libvirt",
                    ssh_forward_port=_FORWARD,
                )
            connector = _FakeConnector(("192.0.2.1", 22022))
            resolver = _provider_resolver(connector=connector)
            info = await ssh_info(pool, _ctx(), sys_id, resolver=resolver)
            probe = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
            key = await authorize_ssh_key(pool, _ctx(), sys_id, _GOOD_KEY, resolver=resolver)
            probe_payload = await _job_payload(pool, probe.object_id)
            key_payload = await _job_payload(pool, key.object_id)
        ssh = info.data["ssh"]
        assert isinstance(ssh, dict)
        assert ssh["port"] == 22022
        assert connector.seen_forwards == [None, None, None]
        assert probe_payload == {"system_id": sys_id}
        assert key_payload == {"system_id": sys_id, "public_key": _GOOD_KEY}

    asyncio.run(_run())


def test_ssh_info_keeps_its_next_actions_for_a_remote_authority_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(pool, provider_kind="remote-libvirt")
            connector = _FakeConnector(("192.0.2.1", 22022))
            resp = await ssh_info(
                pool, _ctx(), sys_id, resolver=_provider_resolver(connector=connector)
            )
        assert "systems.check_ssh_reachable" in resp.suggested_next_actions
        assert "systems.authorize_ssh_key" in resp.suggested_next_actions

    asyncio.run(_run())


async def _stored_job(pool, job_id: str) -> Job:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cursor:
        await cursor.execute("SELECT * FROM jobs WHERE id=%s", (job_id,))
        row = await cursor.fetchone()
    assert row is not None
    return Job.model_validate(row)


async def _assert_worker_is_refused_the_binding(
    worker_pool: AsyncConnectionPool, system_id: str
) -> None:
    async with worker_pool.connection() as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await conn.execute(
                "SELECT * FROM resolve_authority_system_control_binding(%s)", (system_id,)
            )


def test_a_reachability_job_reaches_the_owned_system_as_the_worker_role(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def _handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(b"SSH-2.0-test\r\n")
        await writer.drain()
        writer.close()

    async def _run() -> str | None:
        server = await asyncio.start_server(_handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        resolver = _invisible_domain_resolver()
        async with server, _pool(migrated_url) as pool:
            sys_id = await _authority_system(
                pool, state="activated", activation=_RECOVERED, ssh_forward_port=port
            )
            resp = await check_ssh_reachable(pool, _ctx(), sys_id, resolver=resolver)
            job = await _stored_job(pool, resp.object_id)
            async with AsyncConnectionPool(
                authority_role_dsns("kdive_worker"), min_size=1, max_size=2, open=False
            ) as worker_pool:
                await _assert_worker_is_refused_the_binding(worker_pool, sys_id)
                async with worker_pool.connection() as conn:
                    return await check_ssh_reachable_handler(
                        conn, job, resolver=resolver, secret_registry=SecretRegistry()
                    )

    verdict = asyncio.run(_run())
    assert verdict is not None
    assert json.loads(verdict)["reachable"] is True


def test_an_authorize_job_reaches_the_owned_system_as_the_worker_role(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    probed: list[tuple[str, int]] = []
    delivered: list[str] = []

    async def _probe(host: str, port: int) -> ReachResult:
        probed.append((host, port))
        return ReachResult.ok()

    async def _run() -> None:
        resolver = _invisible_domain_resolver()
        async with _pool(migrated_url) as pool:
            sys_id = await _authority_system(
                pool, state="activated", activation=_RECOVERED, ssh_forward_port=_FORWARD
            )
            async with pool.connection() as conn:
                await ensure_system_bootstrap_key(
                    conn, UUID(sys_id), secret_registry=SecretRegistry()
                )
            resp = await authorize_ssh_key(pool, _ctx(), sys_id, _GOOD_KEY, resolver=resolver)
            job = await _stored_job(pool, resp.object_id)
            async with AsyncConnectionPool(
                authority_role_dsns("kdive_worker"), min_size=1, max_size=2, open=False
            ) as worker_pool:
                await _assert_worker_is_refused_the_binding(worker_pool, sys_id)
                async with worker_pool.connection() as conn:
                    await authorize_ssh_key_handler(
                        conn,
                        job,
                        resolver=resolver,
                        secret_registry=SecretRegistry(),
                        ssh_exec=lambda _argv, key: delivered.append(key),
                        probe=_probe,
                    )

    asyncio.run(_run())
    assert probed == [("127.0.0.1", _FORWARD)]
    assert delivered == [_GOOD_KEY]

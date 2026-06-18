"""``resources.register_*`` / ``deregister`` / ``renew`` runtime-mutation tools (M2.6 #396).

Handlers are driven directly with an injected pool + RequestContext and an injected probe /
secrets root, so the per-kind preflight and authz gates are exercised deterministically without
a live provider or the worker transport plane.

Coverage:
* non-admin register/deregister/renew → ``authorization_denied`` (and audited for a platform
  over-reach)
* register→allocate→renew→deregister(force) round-trip
* register defaults owner_project to the single registering project; ``'*'`` → global (NULL)
* register rejects a name already owned by a ``config`` row → ``conflict``
* register duplicate runtime name → ``conflict``
* per-kind preflight: remote_libvirt needs reachability + secret refs + a registered
  base_image; fault_inject needs only the secret ref (no reachability, no base_image)
* deregister rejects a ``config``/``discovery`` row → ``conflict``; absent id → ``not_found``
* deregister of a resource with a live allocation requires ``force=True``
* renew is keyed to ``resource_id`` (a different session/principal renews it — handoff)
* no secret bytes leak into any audit row or error envelope
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from fastmcp import FastMCP
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.domain.catalog.resources import ManagedBy, ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.state import AllocationState
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.ops.resources import host_ops as resources_host_ops
from kdive.mcp.tools.ops.resources import register as register_module
from kdive.mcp.tools.ops.resources import registrar as resources_registrar
from kdive.mcp.tools.ops.resources._common import (
    DEREGISTER_TOOL,
    REGISTER_FAULT_INJECT_TOOL,
    REGISTER_LOCAL_LIBVIRT_TOOL,
    REGISTER_REMOTE_LIBVIRT_TOOL,
    RENEW_TOOL,
    ResourceProbe,
)
from kdive.mcp.tools.ops.resources.deregister import deregister_resource
from kdive.mcp.tools.ops.resources.register import (
    FaultInjectResourceRegistration,
    RemoteLibvirtResourceRegistration,
    register_fault_inject_resource,
    register_remote_libvirt_resource,
)
from kdive.mcp.tools.ops.resources.renew import renew_resource
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole

_SECRET_BYTES = "-----BEGIN CERTIFICATE-----FAKEKEYBYTES"  # pragma: allowlist secret


def test_resource_register_helper_name_matches_insert_operation() -> None:
    assert hasattr(register_module, "_insert_runtime_resource")
    assert not hasattr(register_module, "_do_insert")


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _admin_ctx(
    *, principal: str = "ops-admin", projects: tuple[str, ...] = ("team-a",)
) -> RequestContext:
    return RequestContext(
        principal=principal,
        agent_session="sess-admin",
        projects=projects,
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
        client_id="kdivectl",
    )


def _non_admin_ctx() -> RequestContext:
    return RequestContext(
        principal="proj-user",
        agent_session="sess-user",
        projects=("team-a",),
        roles={},
        platform_roles=frozenset(),
        client_id=None,
    )


def _auditor_ctx() -> RequestContext:
    return RequestContext(
        principal="ops-auditor",
        agent_session="sess-aud",
        projects=(),
        roles={},
        platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
        client_id="kdivectl",
    )


class _Reachable:
    """A probe that always reports reachable."""

    async def probe(self, host_uri: str) -> bool:
        return True


class _Unreachable:
    """A probe that always reports unreachable."""

    async def probe(self, host_uri: str) -> bool:
        return False


def _secrets_root(tmp_path: Path, *refs: str) -> Path:
    """Create a secrets root with each ``ref`` populated with fake bytes; return the root."""
    root = tmp_path / "secrets"
    root.mkdir(exist_ok=True)
    for ref in refs:
        (root / ref).write_text(_SECRET_BYTES)
    return root


async def _seed_registered_image(pool: AsyncConnectionPool, *, name: str, provider: str) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "  (provider, name, arch, format, root_device, visibility, state, object_key) "
            "VALUES (%s, %s, 'x86_64', 'qcow2', '/dev/vda', 'public', 'registered', %s)",
            (provider, name, f"images/{name}.qcow2"),
        )


async def _insert_resource(
    pool: AsyncConnectionPool,
    *,
    kind: str,
    name: str,
    managed_by: str,
    host_uri: str = "qemu+tls://host/system",
) -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO resources (kind, name, pool, cost_class, status, host_uri, managed_by) "
            "VALUES (%s, %s, 'default', 'standard', 'available', %s, %s) RETURNING id",
            (kind, name, host_uri, managed_by),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _insert_live_allocation(pool: AsyncConnectionPool, resource_id: UUID) -> UUID:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "INSERT INTO allocations (resource_id, state, principal, project) "
            "VALUES (%s, %s, 'p', 'team-a') RETURNING id",
            (resource_id, AllocationState.ACTIVE.value),
        )
        row = await cur.fetchone()
    assert row is not None
    return row[0]


async def _resource_row(url: str, resource_id: str) -> dict[str, object] | None:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT managed_by, owner_project, lease_expires_at, capabilities "
            "FROM resources WHERE id = %s",
            (UUID(resource_id),),
        )
        return await cur.fetchone()


async def _resource_full_row(url: str, resource_id: str) -> dict[str, object] | None:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT managed_by, owner_project, lease_expires_at, cordoned FROM resources "
            "WHERE id = %s",
            (UUID(resource_id),),
        )
        return await cur.fetchone()


async def _audit_rows(url: str) -> list[tuple[object, ...]]:
    conn = await psycopg.AsyncConnection.connect(url, autocommit=True)
    async with conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope, args_digest "
            "FROM platform_audit_log ORDER BY id"
        )
        return list(await cur.fetchall())


def _destructive_hint(tool: object) -> bool | None:
    annotations = getattr(tool, "annotations", None)
    value = getattr(annotations, "destructiveHint", None)
    return value if isinstance(value, bool) else None


def _fault_request(
    name: str,
    *,
    cost_class: str = "standard",
    concurrent_allocation_cap: int = 1,
    secret_refs: tuple[str, ...] = (),
    owner_project: str | None = None,
) -> FaultInjectResourceRegistration:
    return FaultInjectResourceRegistration(
        name=name,
        cost_class=cost_class,
        concurrent_allocation_cap=concurrent_allocation_cap,
        vcpus=8,
        memory_mb=16384,
        secret_refs=secret_refs,
        owner_project=owner_project,
    )


def _remote_request(
    name: str,
    *,
    cost_class: str = "standard",
    host_uri: str = "qemu+tls://host/system",
    base_image: str = "base-img",
    concurrent_allocation_cap: int = 1,
    secret_refs: tuple[str, ...] = (),
    owner_project: str | None = None,
) -> RemoteLibvirtResourceRegistration:
    return RemoteLibvirtResourceRegistration(
        name=name,
        cost_class=cost_class,
        host_uri=host_uri,
        base_image=base_image,
        concurrent_allocation_cap=concurrent_allocation_cap,
        vcpus=8,
        memory_mb=16384,
        secret_refs=secret_refs,
        owner_project=owner_project,
    )


async def _call_registered_tool(tool: object, *args: object) -> ToolResponse:
    fn = cast(Any, tool).fn
    result = await fn(*args)
    assert isinstance(result, ToolResponse)
    return result


# --- registrar boundary ---


def test_registrar_exposes_annotations_and_invokes_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, RequestContext, FaultInjectResourceRegistration]] = []
    pool = cast(AsyncConnectionPool, object())
    ctx = _admin_ctx(principal="registrar-admin", projects=("registrar-team",))

    async def fake_register_fault_inject(
        bound_pool: AsyncConnectionPool,
        bound_ctx: RequestContext,
        request: FaultInjectResourceRegistration,
    ) -> ToolResponse:
        calls.append((bound_pool, bound_ctx, request))
        return ToolResponse.success("resource:fi-registrar", "registered")

    async def _run() -> None:
        monkeypatch.setattr(resources_registrar, "current_context", lambda: ctx)
        monkeypatch.setattr(
            resources_registrar, "register_fault_inject_resource", fake_register_fault_inject
        )
        app = FastMCP("resources-registrar-test")
        resources_registrar.register(app, pool)
        tools = {tool.name: tool for tool in await app.list_tools()}

        assert set(tools) == {
            REGISTER_REMOTE_LIBVIRT_TOOL,
            REGISTER_LOCAL_LIBVIRT_TOOL,
            REGISTER_FAULT_INJECT_TOOL,
            DEREGISTER_TOOL,
            RENEW_TOOL,
        }
        assert _destructive_hint(tools[REGISTER_FAULT_INJECT_TOOL]) is False
        assert _destructive_hint(tools[RENEW_TOOL]) is False
        assert _destructive_hint(tools[DEREGISTER_TOOL]) is True

        resp = await _call_registered_tool(
            tools[REGISTER_FAULT_INJECT_TOOL],
            _fault_request(
                "fi-registrar",
                concurrent_allocation_cap=4,
                secret_refs=("ref-a", "ref-b"),
                owner_project="*",
            ),
        )

        assert resp.status == "registered"
        assert calls == [
            (
                pool,
                ctx,
                _fault_request(
                    "fi-registrar",
                    concurrent_allocation_cap=4,
                    secret_refs=("ref-a", "ref-b"),
                    owner_project="*",
                ),
            )
        ]

    asyncio.run(_run())


def test_host_ops_registrar_exposes_annotations_and_invokes_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, RequestContext, str, str]] = []
    pool = cast(AsyncConnectionPool, object())
    ctx = _admin_ctx(principal="host-op", projects=())

    async def fake_set_status(
        bound_pool: AsyncConnectionPool,
        bound_ctx: RequestContext,
        *,
        resource_id: str,
        status: str,
    ) -> ToolResponse:
        calls.append((bound_pool, bound_ctx, resource_id, status))
        return ToolResponse.success(resource_id, status)

    async def _run() -> None:
        monkeypatch.setattr(resources_host_ops, "current_context", lambda: ctx)
        monkeypatch.setattr(resources_host_ops, "set_resource_status", fake_set_status)
        app = FastMCP("resources-host-ops-test")
        resources_host_ops.register(app, pool)
        tools = {tool.name: tool for tool in await app.list_tools()}

        assert set(tools) == {
            "resources.set_status",
            "resources.cordon",
            "resources.uncordon",
            "resources.drain",
        }
        assert _destructive_hint(tools["resources.set_status"]) is False
        assert _destructive_hint(tools["resources.cordon"]) is False
        assert _destructive_hint(tools["resources.uncordon"]) is False
        assert _destructive_hint(tools["resources.drain"]) is True

        resp = await _call_registered_tool(tools["resources.set_status"], "resource-1", "degraded")

        assert resp.status == "degraded"
        assert calls == [(pool, ctx, "resource-1", "degraded")]

    asyncio.run(_run())


# --- authorization gate ---


def test_non_admin_register_denied(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_fault_inject_resource(
                pool,
                _non_admin_ctx(),
                _fault_request("fi-1"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())


def test_non_admin_deregister_and_renew_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rid = await _insert_resource(
                pool, kind="fault-inject", name="fi-x", managed_by=ManagedBy.RUNTIME.value
            )
            dereg = await deregister_resource(pool, _non_admin_ctx(), resource_id=str(rid))
            renew = await renew_resource(pool, _non_admin_ctx(), resource_id=str(rid))
        assert dereg.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        assert renew.error_category == ErrorCategory.AUTHORIZATION_DENIED.value

    asyncio.run(_run())


def test_platform_auditor_overreach_denied_and_audited(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_fault_inject_resource(
                pool,
                _auditor_ctx(),
                _fault_request("fi-aud", owner_project="*"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
        rows = await _audit_rows(migrated_url)
        assert any(
            r[2] == "resources.register_fault_inject" and "denied" in str(r[3]) for r in rows
        )

    asyncio.run(_run())


# --- round trip ---


def test_register_allocate_renew_deregister_round_trip(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            reg = await register_fault_inject_resource(
                pool,
                _admin_ctx(),
                _fault_request("fi-roundtrip"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
            assert reg.status == "registered", reg.model_dump()
            rid = str(reg.data["id"])
            row = await _resource_row(migrated_url, rid)
            assert row is not None
            assert str(row["managed_by"]) == ManagedBy.RUNTIME.value
            assert row["owner_project"] == "team-a"
            # The reporter's exact wall: a runtime-registered host must carry the vcpus/memory_mb
            # size ceiling so admission's <=-resource-caps check has a value to read.
            caps = cast("dict[str, Any]", row["capabilities"])
            assert caps["vcpus"] == 8
            assert caps["memory_mb"] == 16384
            first_lease = row["lease_expires_at"]
            assert isinstance(first_lease, datetime)

            await _insert_live_allocation(pool, UUID(rid))

            renew = await renew_resource(pool, _admin_ctx(), resource_id=rid)
            assert renew.status == "renewed", renew.model_dump()
            after = await _resource_row(migrated_url, rid)
            assert after is not None
            assert isinstance(after["lease_expires_at"], datetime)
            assert after["lease_expires_at"] >= first_lease

            # live allocation: bare deregister refused, forced succeeds (cordon disposition —
            # the FK to retained allocation rows makes a hard delete unsafe).
            refused = await deregister_resource(pool, _admin_ctx(), resource_id=rid)
            assert refused.error_category == ErrorCategory.CONFLICT.value
            forced = await deregister_resource(pool, _admin_ctx(), resource_id=rid, force=True)
            assert forced.status == "deregistered", forced.model_dump()
            assert forced.data["disposition"] == "cordoned"
            final = await _resource_full_row(migrated_url, rid)
            assert final is not None
            assert final["cordoned"] is True
            assert final["lease_expires_at"] is None

    asyncio.run(_run())


def test_deregister_idle_never_allocated_resource_is_hard_deleted(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rid = await _insert_resource(
                pool, kind="fault-inject", name="fi-idle", managed_by=ManagedBy.RUNTIME.value
            )
            resp = await deregister_resource(pool, _admin_ctx(), resource_id=str(rid))
        assert resp.status == "deregistered", resp.model_dump()
        assert resp.data["disposition"] == "deleted"
        assert await _resource_row(migrated_url, str(rid)) is None

    asyncio.run(_run())


def test_register_defaults_owner_to_single_project_and_star_is_global(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            scoped = await register_fault_inject_resource(
                pool,
                _admin_ctx(projects=("only-proj",)),
                _fault_request("fi-scoped"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
            glob = await register_fault_inject_resource(
                pool,
                _admin_ctx(projects=("only-proj",)),
                _fault_request("fi-global", owner_project="*"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        scoped_row = await _resource_row(migrated_url, str(scoped.data["id"]))
        glob_row = await _resource_row(migrated_url, str(glob.data["id"]))
        assert scoped_row is not None and scoped_row["owner_project"] == "only-proj"
        assert glob_row is not None and glob_row["owner_project"] is None

    asyncio.run(_run())


def test_register_ambiguous_project_requires_explicit(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_fault_inject_resource(
                pool,
                _admin_ctx(projects=("a", "b")),
                _fault_request("fi-amb"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


# --- config-name collision + duplicates ---


def test_register_rejects_config_name_collision(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _insert_resource(
                pool,
                kind="fault-inject",
                name="fi-config",
                managed_by=ManagedBy.CONFIG.value,
                host_uri="fault-inject://local",
            )
            resp = await register_fault_inject_resource(
                pool,
                _admin_ctx(),
                _fault_request("fi-config"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


def test_register_duplicate_runtime_name_conflict(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            root = _secrets_root(tmp_path)

            async def _reg() -> ToolResponse:
                return await register_fault_inject_resource(
                    pool,
                    _admin_ctx(),
                    _fault_request("fi-dup"),
                    probe=_Reachable(),
                    secrets_root=root,
                )

            first = await _reg()
            second = await _reg()
        assert first.status == "registered"
        assert second.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


# --- per-kind preflight ---


def test_remote_libvirt_register_requires_reachable_secrets_and_registered_base_image(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_registered_image(pool, name="base-img", provider="remote-libvirt")
            root = _secrets_root(tmp_path, "client.pem", "client.key", "ca.pem")
            ok = await register_remote_libvirt_resource(
                pool,
                _admin_ctx(),
                _remote_request(
                    "rl-ok",
                    secret_refs=("client.pem", "client.key", "ca.pem"),
                    owner_project="*",
                ),
                probe=_Reachable(),
                secrets_root=root,
            )
            assert ok.status == "registered", ok.model_dump()

            unreachable = await register_remote_libvirt_resource(
                pool,
                _admin_ctx(),
                _remote_request("rl-unreach", secret_refs=("client.pem",), owner_project="*"),
                probe=_Unreachable(),
                secrets_root=root,
            )
            assert unreachable.error_category == ErrorCategory.CONFIGURATION_ERROR.value

            missing_secret = await register_remote_libvirt_resource(
                pool,
                _admin_ctx(),
                _remote_request("rl-nosecret", secret_refs=("absent.pem",), owner_project="*"),
                probe=_Reachable(),
                secrets_root=root,
            )
            assert missing_secret.error_category == ErrorCategory.CONFIGURATION_ERROR.value

            no_image = await register_remote_libvirt_resource(
                pool,
                _admin_ctx(),
                _remote_request(
                    "rl-noimage",
                    base_image="nope",
                    secret_refs=("client.pem",),
                    owner_project="*",
                ),
                probe=_Reachable(),
                secrets_root=root,
            )
            assert no_image.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


def test_fault_inject_register_ignores_base_image_and_reachability(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # No base_image, an unreachable probe, no registered image: a fault-inject register
            # must still succeed (synthetic — preflight is secret-ref only).
            resp = await register_fault_inject_resource(
                pool,
                _admin_ctx(),
                _fault_request("fi-synthetic", owner_project="*"),
                probe=_Unreachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.status == "registered", resp.model_dump()
        row = await _resource_row(migrated_url, str(resp.data["id"]))
        assert row is not None

    asyncio.run(_run())


def test_fault_inject_register_fails_on_unresolvable_secret(
    migrated_url: str, tmp_path: Path
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await register_fault_inject_resource(
                pool,
                _admin_ctx(),
                _fault_request("fi-badsecret", secret_refs=("absent.key",), owner_project="*"),
                probe=_Reachable(),
                secrets_root=_secrets_root(tmp_path),
            )
        assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value

    asyncio.run(_run())


# --- deregister ownership gate ---


def test_deregister_rejects_config_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rid = await _insert_resource(
                pool,
                kind="fault-inject",
                name="fi-cfg",
                managed_by=ManagedBy.CONFIG.value,
                host_uri="fault-inject://local",
            )
            resp = await deregister_resource(pool, _admin_ctx(), resource_id=str(rid))
        assert resp.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


def test_deregister_absent_id_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await deregister_resource(pool, _admin_ctx(), resource_id=str(uuid4()))
        assert resp.error_category == ErrorCategory.NOT_FOUND.value

    asyncio.run(_run())


# --- renew handoff ---


def test_renew_keyed_to_resource_id_survives_session_handoff(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rid = await _insert_resource(
                pool, kind="fault-inject", name="fi-handoff", managed_by=ManagedBy.RUNTIME.value
            )
            # A different principal/session renews — keyed to the id, not the registrar.
            resp = await renew_resource(
                pool, _admin_ctx(principal="successor-admin"), resource_id=str(rid)
            )
        assert resp.status == "renewed", resp.model_dump()
        row = await _resource_row(migrated_url, str(rid))
        assert row is not None and isinstance(row["lease_expires_at"], datetime)

    asyncio.run(_run())


def test_renew_rejects_config_row(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            rid = await _insert_resource(
                pool,
                kind="fault-inject",
                name="fi-cfg-renew",
                managed_by=ManagedBy.CONFIG.value,
                host_uri="fault-inject://local",
            )
            resp = await renew_resource(pool, _admin_ctx(), resource_id=str(rid))
        assert resp.error_category == ErrorCategory.CONFLICT.value

    asyncio.run(_run())


# --- no-leak guard ---


def test_no_secret_bytes_in_audit_or_envelope(migrated_url: str, tmp_path: Path) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _seed_registered_image(pool, name="leak-img", provider="remote-libvirt")
            root = _secrets_root(tmp_path, "client.pem")
            resp = await register_remote_libvirt_resource(
                pool,
                _admin_ctx(),
                _remote_request(
                    "rl-leak",
                    base_image="leak-img",
                    secret_refs=("client.pem",),
                    owner_project="*",
                ),
                probe=_Reachable(),
                secrets_root=root,
            )
        assert resp.status == "registered"
        assert _SECRET_BYTES not in str(resp.model_dump())
        rows = await _audit_rows(migrated_url)
        assert all(_SECRET_BYTES not in str(r) for r in rows)

    asyncio.run(_run())


def test_probe_protocol_is_satisfied_by_fakes() -> None:
    assert isinstance(_Reachable(), ResourceProbe)
    assert isinstance(_Unreachable(), ResourceProbe)
    assert ResourceKind.FAULT_INJECT.value == "fault-inject"

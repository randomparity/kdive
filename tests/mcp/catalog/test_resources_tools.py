"""resources.* handler tests — handlers called directly with an injected pool."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, SYSTEMS
from kdive.domain.capacity.state import AllocationState, SystemState
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import resources as catalog_resources_tools
from kdive.mcp.tools.ops.resources import host_ops as resources_tools
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.resource_registration import register_discovered_resource
from kdive.providers.core.runtime import (
    ProviderRuntime,
    ProviderSupport,
    ResourceBindingCapabilities,
    ResourceDetailCapabilities,
)
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.providers.remote_libvirt.resource_details import (
    StagedVolumeProbe,
    project_resource_details,
)
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.services.allocation.release import ReleaseOutcome
from tests.mcp.json_data import data_sequence, json_mapping
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

CTX = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
VIEWER_CTX = RequestContext(
    principal="viewer-1",
    agent_session="s",
    projects=("proj",),
    roles={"proj": Role.VIEWER},
)


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


def _discovery(cap: int = 2, *, host_uri: str = "qemu:///system") -> LocalLibvirtDiscovery:
    return LocalLibvirtDiscovery(
        host_uri=host_uri,
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=cap,
    )


async def _register(pool: AsyncConnectionPool, *, host_uri: str = "qemu:///system") -> str:
    async with pool.connection() as conn:
        res = await register_discovered_resource(
            conn,
            _discovery(host_uri=host_uri).list_resources()[0],
            pool="local-libvirt",
            cost_class="local",
        )
    return str(res.id)


async def _list_resources(
    pool: AsyncConnectionPool, ctx: RequestContext, **kw: Any
) -> ToolResponse:
    request = catalog_resources_tools.ResourcesListRequest(**kw)
    return await catalog_resources_tools.list_resources(pool, ctx, request)


def _resolver_with_staged_projector(probe: StagedVolumeProbe) -> ProviderResolver:
    unused_port = cast(Any, object())
    runtime = ProviderRuntime(
        profile_policy=unused_port,
        provisioner=unused_port,
        installer=unused_port,
        booter=unused_port,
        connector=unused_port,
        controller=unused_port,
        retriever=unused_port,
        crash_postmortem=unused_port,
        vmcore_introspector=unused_port,
        live_introspector=unused_port,
        resource_details=ResourceDetailCapabilities(
            projector=lambda pool, viewer_projects: project_resource_details(
                pool, viewer_projects, staged_probe=probe
            )
        ),
    )
    return ProviderResolver({ResourceKind.REMOTE_LIBVIRT: runtime})


async def _set_affinity(pool: AsyncConnectionPool, res_id: str, *, owner_project: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET owner_project = %s WHERE id = %s",
            (owner_project, UUID(res_id)),
        )


def test_list_returns_host_with_flat_capability_projection(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            responses = await _list_resources(pool, CTX, kind=None)
        assert responses.object_id == "resources"
        assert responses.status == "ok"
        items = responses.items
        assert len(items) == 1
        resp = items[0]
        assert resp.object_id == res_id
        assert resp.status == "available"
        assert resp.data["kind"] == "local-libvirt"
        assert resp.data["arch"] == "x86_64"
        assert resp.data["vcpus"] == 8
        assert resp.data["memory_mb"] == 16384
        assert resp.data["transports"] == "gdbstub"
        assert resp.data["concurrent_allocation_cap"] == 2

    asyncio.run(_run())


def test_list_hides_resources_outside_project_affinity(migrated_url: str) -> None:
    async def _run() -> tuple[str, list[str]]:
        async with _pool(migrated_url) as pool:
            visible = await _register(pool, host_uri="qemu:///visible")
            hidden = await _register(pool, host_uri="qemu:///hidden")
            await _set_affinity(pool, hidden, owner_project="other")
            responses = await _list_resources(pool, CTX, kind=None)
        return visible, [item.object_id for item in responses.items]

    visible, item_ids = asyncio.run(_run())
    assert item_ids == [visible]


def test_list_hides_scoped_resources_without_viewer_role(migrated_url: str) -> None:
    async def _run() -> tuple[str, list[str], list[str]]:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_affinity(pool, res_id, owner_project="proj")
            member_resp = await _list_resources(pool, CTX, kind=None)
            viewer_resp = await _list_resources(pool, VIEWER_CTX, kind=None)
        return (
            res_id,
            [item.object_id for item in member_resp.items],
            [item.object_id for item in viewer_resp.items],
        )

    res_id, member_ids, viewer_ids = asyncio.run(_run())
    assert member_ids == []
    assert viewer_ids == [res_id]


def test_list_kind_filter_miss_is_configuration_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            await _register(pool)
            responses = await _list_resources(pool, CTX, kind="nope")
        assert responses.status == "error"
        assert responses.error_category == "configuration_error"

    asyncio.run(_run())


def test_list_malformed_resource_row_degrades_to_infrastructure_failure(
    migrated_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            async with pool.connection() as conn:
                await conn.execute("UPDATE resources SET capabilities = '[]'::jsonb")
            caplog.set_level(logging.WARNING, logger=catalog_resources_tools.__name__)
            responses = await _list_resources(pool, CTX, kind="local-libvirt")
        items = responses.items
        assert len(items) == 1
        assert items[0].object_id == res_id
        assert items[0].status == "error"
        assert items[0].error_category == "infrastructure_failure"
        assert any(
            record.exc_info is not None and f"resource {res_id}" in record.message
            for record in caplog.records
        )

    asyncio.run(_run())


def test_describe_adds_pool_cost_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await catalog_resources_tools.describe_resource(pool, CTX, res_id)
        assert resp.status == "available"
        assert resp.data["pool"] == "local-libvirt"
        assert resp.data["cost_class"] == "local"
        assert resp.data["host_uri"] == "qemu:///system"

    asyncio.run(_run())


def _descriptor_runtime(
    *,
    capture: frozenset[CaptureMethod],
    transports: frozenset[str],
    introspection: frozenset[str],
) -> ProviderRuntime:
    unused_port = cast(Any, object())
    return ProviderRuntime(
        profile_policy=unused_port,
        provisioner=unused_port,
        installer=unused_port,
        booter=unused_port,
        connector=unused_port,
        controller=unused_port,
        retriever=unused_port,
        crash_postmortem=unused_port,
        vmcore_introspector=unused_port,
        live_introspector=unused_port,
        support=ProviderSupport(
            capture_methods=capture,
            debug_transports=cast(Any, transports),
            introspection=cast(Any, introspection),
        ),
    )


def _resolver_with_descriptor(kind: ResourceKind, runtime: ProviderRuntime) -> ProviderResolver:
    return ProviderResolver({kind: runtime})


def test_describe_projects_local_partial_capability(migrated_url: str) -> None:
    # ADR-0208/0210: after B2 (#676) a local System reports build/boot/kdump AND introspect
    # (offline-vmcore wired), but still NOT debug (B1) or host-dump (B4).
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            runtime = _descriptor_runtime(
                capture=frozenset({CaptureMethod.KDUMP}),
                transports=frozenset(),
                introspection=frozenset({"offline-vmcore"}),
            )
            return await catalog_resources_tools.describe_resource(
                pool,
                CTX,
                res_id,
                resolver=_resolver_with_descriptor(ResourceKind.LOCAL_LIBVIRT, runtime),
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    capabilities = set(cast(list[str], resp.data["capabilities"]))
    assert capabilities == {"build", "boot", "kdump", "introspect"}
    assert "introspect" in capabilities
    assert "debug" not in capabilities
    assert "host-dump" not in capabilities
    assert resp.data["supported_capture_methods"] == ["kdump"]
    assert resp.data["supported_debug_transports"] == []
    assert resp.data["supported_introspection"] == ["offline-vmcore"]


def test_describe_surfaces_fadump_in_supported_capture_methods(migrated_url: str) -> None:
    # ADR-0349: a runtime advertising FADUMP surfaces "fadump" in supported_capture_methods, so an
    # agent sees the method vocabulary (per-host support is gated at admission, not surfaced here).
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            runtime = _descriptor_runtime(
                capture=frozenset(
                    {CaptureMethod.KDUMP, CaptureMethod.FADUMP, CaptureMethod.HOST_DUMP}
                ),
                transports=frozenset(),
                introspection=frozenset(),
            )
            return await catalog_resources_tools.describe_resource(
                pool,
                CTX,
                res_id,
                resolver=_resolver_with_descriptor(ResourceKind.LOCAL_LIBVIRT, runtime),
            )

    resp = asyncio.run(_run())
    assert resp.data["supported_capture_methods"] == ["fadump", "host_dump", "kdump"]


def test_describe_projects_remote_full_capability(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            runtime = _descriptor_runtime(
                capture=frozenset(
                    {
                        CaptureMethod.KDUMP,
                        CaptureMethod.HOST_DUMP,
                        CaptureMethod.GDBSTUB,
                        CaptureMethod.CONSOLE,
                    }
                ),
                transports=frozenset({"gdbstub", "drgn-live"}),
                introspection=frozenset({"offline-vmcore", "live"}),
            )
            return await catalog_resources_tools.describe_resource(
                pool,
                CTX,
                res_id,
                resolver=_resolver_with_descriptor(ResourceKind.REMOTE_LIBVIRT, runtime),
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    capabilities = set(cast(list[str], resp.data["capabilities"]))
    assert capabilities == {"build", "boot", "kdump", "host-dump", "debug", "introspect"}
    assert resp.data["supported_debug_transports"] == ["drgn-live", "gdbstub"]
    assert resp.data["supported_introspection"] == ["live", "offline-vmcore"]


def test_describe_omits_capabilities_when_no_resolver(migrated_url: str) -> None:
    # Degraded path: no resolver → omit the capability block, never fail the describe.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            return await catalog_resources_tools.describe_resource(pool, CTX, res_id)

    resp = asyncio.run(_run())
    assert resp.status == "available"
    assert "capabilities" not in resp.data
    assert "supported_capture_methods" not in resp.data


def test_describe_fails_when_kind_unregistered(migrated_url: str) -> None:
    # A present-but-misconfigured resolver should fail closed instead of hiding detail data.
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            empty_resolver = ProviderResolver({})
            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, resolver=empty_resolver
            )

    resp = asyncio.run(_run())
    assert resp.status == "error"
    assert resp.error_category == "configuration_error"
    assert resp.data["kind"] == ResourceKind.LOCAL_LIBVIRT.value
    assert resp.data["available"] == []


def test_describe_hides_resource_outside_project_affinity(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await _set_affinity(pool, res_id, owner_project="other")
            return await catalog_resources_tools.describe_resource(pool, CTX, res_id)

    resp = asyncio.run(_run())
    assert resp.status == "error"
    assert resp.error_category == "not_found"


def test_describe_unknown_is_not_found(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_resources_tools.describe_resource(pool, CTX, str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "not_found"

    asyncio.run(_run())


def test_describe_malformed_id_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await catalog_resources_tools.describe_resource(pool, CTX, "not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"

    asyncio.run(_run())


async def _register_remote(
    pool: AsyncConnectionPool,
    *,
    host_uri: str = "qemu+tls://h/system",
    name: str | None = None,
) -> str:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO resources (kind, name, capabilities, pool, cost_class, status, host_uri) "
            "VALUES (%s, %s, '{}', 'default', 'remote', 'available', %s) RETURNING id",
            (ResourceKind.REMOTE_LIBVIRT.value, name, host_uri),
        )
        row = await cur.fetchone()
    assert row is not None
    return str(row[0])


async def _insert_remote_image(
    pool: AsyncConnectionPool,
    *,
    name: str,
    volume: str,
    visibility: str = "public",
    owner: str | None = None,
) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO image_catalog "
            "(provider, name, arch, format, root_device, volume, visibility, owner, "
            " expires_at, state, pending_since) "
            "VALUES ('remote-libvirt', %(name)s, 'x86_64', 'qcow2', '/dev/vda', %(volume)s, "
            " %(vis)s, %(owner)s, "
            " CASE WHEN %(vis)s = 'private' THEN now() + interval '1 hour' ELSE NULL END, "
            " 'registered', now())",
            {"name": name, "volume": volume, "vis": visibility, "owner": owner},
        )


def test_describe_remote_reports_staged_base_images(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")
            await _insert_remote_image(pool, name="dbg", volume="dbg.qcow2")

            async def fake_probe(volumes: list[str]) -> dict[str, str]:
                return {"fedora.qcow2": "staged", "dbg.qcow2": "absent"}

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, resolver=_resolver_with_staged_projector(fake_probe)
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    staged = [json_mapping(r) for r in data_sequence(resp, "staged_base_images")]
    assert {(r["name"], r["volume"], r["staged"]) for r in staged} == {
        ("dbg", "dbg.qcow2", "absent"),
        ("fedora", "fedora.qcow2", "staged"),
    }


def test_describe_remote_uses_provider_runtime_staged_probe(migrated_url: str) -> None:
    calls: list[list[str]] = []

    async def runtime_probe(volumes: list[str]) -> dict[str, str]:
        calls.append(volumes)
        return {"fedora.qcow2": "staged"}

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")
            return await catalog_resources_tools.describe_resource(
                pool,
                CTX,
                res_id,
                resolver=_resolver_with_staged_projector(runtime_probe),
            )

    resp = asyncio.run(_run())
    staged = [json_mapping(r) for r in data_sequence(resp, "staged_base_images")]
    assert calls == [["fedora.qcow2"]]
    assert staged == [{"name": "fedora", "volume": "fedora.qcow2", "staged": "staged"}]


def _resolver_with_rebound_staged_probe(
    *,
    unbound: StagedVolumeProbe,
    bound_by_name: dict[str, StagedVolumeProbe],
) -> ProviderResolver:
    unused_port = cast(Any, object())

    def _runtime(probe: StagedVolumeProbe) -> ProviderRuntime:
        return ProviderRuntime(
            profile_policy=unused_port,
            provisioner=unused_port,
            installer=unused_port,
            booter=unused_port,
            connector=unused_port,
            controller=unused_port,
            retriever=unused_port,
            crash_postmortem=unused_port,
            vmcore_introspector=unused_port,
            live_introspector=unused_port,
            resource_details=ResourceDetailCapabilities(
                projector=lambda pool, viewer_projects: project_resource_details(
                    pool, viewer_projects, staged_probe=probe
                )
            ),
        )

    base = _runtime(unbound)
    object.__setattr__(
        base,
        "binding",
        ResourceBindingCapabilities(rebind_for_resource=lambda name: _runtime(bound_by_name[name])),
    )
    return ProviderResolver({ResourceKind.REMOTE_LIBVIRT: base})


def test_describe_remote_binds_staged_probe_to_described_host(migrated_url: str) -> None:
    # A named remote resource must probe through for_resource(name): the host-bound probe runs and
    # the unbound runtime's probe (which would degrade to "unknown") never does (#625, ADR-0194).
    async def unbound(volumes: list[str]) -> dict[str, str]:
        raise AssertionError("the unbound runtime probe must not run for a named remote resource")

    bound_calls: list[list[str]] = []

    async def bound(volumes: list[str]) -> dict[str, str]:
        bound_calls.append(volumes)
        return dict.fromkeys(volumes, "staged")

    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool, name="ub26")
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")
            return await catalog_resources_tools.describe_resource(
                pool,
                CTX,
                res_id,
                resolver=_resolver_with_rebound_staged_probe(
                    unbound=unbound, bound_by_name={"ub26": bound}
                ),
            )

    resp = asyncio.run(_run())
    staged = [json_mapping(r) for r in data_sequence(resp, "staged_base_images")]
    assert bound_calls == [["fedora.qcow2"]]
    assert staged == [{"name": "fedora", "volume": "fedora.qcow2", "staged": "staged"}]


def test_describe_remote_probe_degraded_does_not_fail_describe(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(pool, name="fedora", volume="fedora.qcow2")

            # The handler trusts the probe's returned map; a probe that degraded internally
            # returns 'unreachable' for every volume. The describe must still succeed.
            async def degraded(volumes: list[str]) -> dict[str, str]:
                return dict.fromkeys(volumes, "unreachable")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, resolver=_resolver_with_staged_projector(degraded)
            )

    resp = asyncio.run(_run())
    assert resp.status == "available"
    staged = [json_mapping(r) for r in data_sequence(resp, "staged_base_images")]
    assert staged[0]["staged"] == "unreachable"


def test_describe_no_staged_images_empty_list_probe_not_called(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)

            async def fail(volumes: list[str]) -> dict[str, str]:
                raise AssertionError("probe must not be called when there are no staged images")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, resolver=_resolver_with_staged_projector(fail)
            )

    resp = asyncio.run(_run())
    assert list(data_sequence(resp, "staged_base_images")) == []


def test_describe_local_resource_has_no_staged_base_images(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)

            async def fail(volumes: list[str]) -> dict[str, str]:
                raise AssertionError("probe must not be called for a local resource")

            return await catalog_resources_tools.describe_resource(
                pool, CTX, res_id, resolver=_resolver_with_staged_projector(fail)
            )

    resp = asyncio.run(_run())
    assert "staged_base_images" not in resp.data


def test_describe_remote_excludes_other_projects_private_image(migrated_url: str) -> None:
    async def _run() -> ToolResponse:
        async with _pool(migrated_url) as pool:
            res_id = await _register_remote(pool)
            await _insert_remote_image(
                pool, name="theirs", volume="theirs.qcow2", visibility="private", owner="proj-b"
            )

            async def probe(volumes: list[str]) -> dict[str, str]:
                return dict.fromkeys(volumes, "staged")

            # VIEWER_CTX is a viewer on 'proj', not on the owning 'proj-b'.
            return await catalog_resources_tools.describe_resource(
                pool, VIEWER_CTX, res_id, resolver=_resolver_with_staged_projector(probe)
            )

    resp = asyncio.run(_run())
    assert list(data_sequence(resp, "staged_base_images")) == []


_OPERATOR = RequestContext(
    principal="op-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
)
_NON_OPERATOR = RequestContext(principal="user-1", agent_session="s", projects=("proj",))
_AUDITOR = RequestContext(
    principal="auditor-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_AUDITOR}),
)


async def _row(pool: AsyncConnectionPool, res_id: str) -> dict[str, Any]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT status, cordoned FROM resources WHERE id = %s", (UUID(res_id),))
        fetched = await cur.fetchone()
    assert fetched is not None
    status, cordoned = fetched
    return {"status": status, "cordoned": cordoned}


async def _platform_audit_count(pool: AsyncConnectionPool, tool: str) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM platform_audit_log WHERE tool = %s", (tool,))
        fetched = await cur.fetchone()
    assert fetched is not None
    return int(fetched[0])


async def _platform_audit_rows(pool: AsyncConnectionPool) -> list[tuple[object, ...]]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT principal, platform_role, tool, scope FROM platform_audit_log ORDER BY ts"
        )
        return list(await cur.fetchall())


def test_set_status_changes_health_only(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_count(pool, "resources.set_status")
        assert resp.status == "degraded"
        assert row == {"status": "degraded", "cordoned": False}
        assert audited == 1

    asyncio.run(_run())


def test_set_status_same_value_is_noop_success(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="available"
            )
            row = await _row(pool, res_id)
        assert resp.status == "available"
        assert row["status"] == "available"

    asyncio.run(_run())


def test_set_status_invalid_value_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="nope"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=str(uuid4()), status="offline"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_set_status_malformed_resource_id_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id="not-a-uuid", status="offline"
            )
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"

    asyncio.run(_run())


def test_set_status_does_not_clear_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        # set_status offline must not clear an operator's cordon (orthogonal axes).
        assert row == {"status": "offline", "cordoned": True}

    asyncio.run(_run())


def test_cordon_then_uncordon_toggles_only_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            # Make the host degraded first; cordon/uncordon must not touch status.
            await resources_tools.set_resource_status(
                pool, _OPERATOR, resource_id=res_id, status="degraded"
            )
            cordoned = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_cordon = await _row(pool, res_id)
            await resources_tools.uncordon_resource(pool, _OPERATOR, resource_id=res_id)
            after_uncordon = await _row(pool, res_id)
            cordon_audited = await _platform_audit_count(pool, "resources.cordon")
            uncordon_audited = await _platform_audit_count(pool, "resources.uncordon")
        assert cordoned.status == "degraded"
        assert after_cordon == {"status": "degraded", "cordoned": True}
        # uncordon does not change status: still degraded.
        assert after_uncordon == {"status": "degraded", "cordoned": False}
        assert cordon_audited == 1
        assert uncordon_audited == 1

    asyncio.run(_run())


def test_cordon_unknown_host_is_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id=str(uuid4()))
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"

    asyncio.run(_run())


def test_cordon_malformed_resource_id_is_invalid_uuid(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.cordon_resource(pool, _OPERATOR, resource_id="not-a-uuid")
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"

    asyncio.run(_run())


def test_set_status_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _NON_OPERATOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        # The denied call must not have mutated the host.
        assert row == {"status": "available", "cordoned": False}

    asyncio.run(_run())


def test_set_status_denied_for_auditor_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.set_resource_status(
                pool, _AUDITOR, resource_id=res_id, status="offline"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row == {"status": "available", "cordoned": False}
        assert audited == [
            ("auditor-1", "platform_auditor", "resources.set_status", f"resource:{res_id}")
        ]

    asyncio.run(_run())


def test_cordon_denied_for_non_operator(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.cordon_resource(pool, _NON_OPERATOR, resource_id=res_id)
            row = await _row(pool, res_id)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False

    asyncio.run(_run())


# ---- resources.drain: release classifier (pure unit, no DB) -------------------------
# `skipped` (STALE_HANDLE) is a race-only outcome unreachable by DB seeding, so the
# released/skipped/failed mapping is pinned here against ReleaseOutcome directly (#143).


def test_classify_released_is_released_status() -> None:
    item = resources_tools._classify_drain_release("a-1", ReleaseOutcome(released=True))
    assert item.object_id == "a-1"
    assert item.status == "released"
    assert item.error_category is None
    assert "current_status" not in item.data


def test_classify_stale_handle_is_skipped_with_status() -> None:
    # Post-ADR-0293 a `released` grant returns idempotent ok, so the race-only STALE_HANDLE a
    # drain can still see comes from a grant that reached `expired`/`failed` between scan and
    # release; use that as the representative current_status.
    item = resources_tools._classify_drain_release(
        "a-2",
        ReleaseOutcome(
            released=False, category=ErrorCategory.STALE_HANDLE, current_status="expired"
        ),
    )
    assert item.status == "skipped"
    assert item.error_category is None
    assert item.data["current_status"] == "expired"


def test_classify_failed_with_status_carries_current_status() -> None:
    item = resources_tools._classify_drain_release(
        "a-3",
        ReleaseOutcome(
            released=False, category=ErrorCategory.CONFIGURATION_ERROR, current_status="active"
        ),
    )
    assert item.status == "error"
    assert item.error_category == "configuration_error"
    assert item.data["current_status"] == "active"


def test_classify_failed_without_status_omits_current_status() -> None:
    # The reconcile-failure path returns no current_status; the key must be omitted, not null
    # (matching the sibling break-glass envelope, breakglass.py:167).
    item = resources_tools._classify_drain_release(
        "a-4",
        ReleaseOutcome(released=False, category=ErrorCategory.CONFIGURATION_ERROR),
    )
    assert item.status == "error"
    assert item.error_category == "configuration_error"
    assert "current_status" not in item.data


# ---- resources.drain: handler (DB-backed) ------------------------------------------

_ADMIN = RequestContext(
    principal="admin-1",
    agent_session="s",
    projects=(),
    platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
)

_DRAIN_DT = datetime(2026, 1, 1, tzinfo=UTC)
_DRAIN_PROJECT = "tenant-x"
_DRAIN_PROFILE = {
    "schema_version": 1,
    "arch": "x86_64",
    "vcpu": 4,
    "memory_mb": 4096,
    "disk_gb": 20,
    "boot_method": "direct-kernel",
    "kernel_source_ref": "git+https://git.kernel.org/pub/scm/linux.git#v6.9",
    "provider": {
        "local-libvirt": {
            "domain_xml_params": {"machine": "q35"},
            "rootfs": {"kind": "local", "path": "/var/lib/kdive/rootfs/fedora-40.qcow2"},
            "crashkernel": "256M",
        }
    },
}


async def _ensure_budget(conn: AsyncConnection, project: str) -> None:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO budgets (project, limit_kcu, spent_kcu) VALUES (%s, 1000, 0) "
            "ON CONFLICT (project) DO NOTHING",
            (project,),
        )


async def _alloc_on(
    pool: AsyncConnectionPool,
    res_id: str,
    *,
    state: AllocationState,
    project: str = _DRAIN_PROJECT,
    sized: bool = True,
) -> UUID:
    async with pool.connection() as conn:
        await _ensure_budget(conn, project)
        active_started = _DRAIN_DT if state is AllocationState.ACTIVE else None
        alloc = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DRAIN_DT,
                updated_at=_DRAIN_DT,
                principal="tenant-user",
                project=project,
                resource_id=UUID(res_id),
                state=state,
                requested_vcpus=2 if sized else None,
                requested_memory_gb=4 if sized else None,
                active_started_at=active_started,
            ),
        )
    return alloc.id


async def _system_on(pool: AsyncConnectionPool, alloc_id: UUID, *, state: SystemState) -> UUID:
    async with pool.connection() as conn:
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DRAIN_DT,
                updated_at=_DRAIN_DT,
                principal="tenant-user",
                project=_DRAIN_PROJECT,
                allocation_id=alloc_id,
                state=state,
                provisioning_profile=_DRAIN_PROFILE,
            ),
        )
    return system.id


async def _alloc_state(pool: AsyncConnectionPool, alloc_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _system_state(pool: AsyncConnectionPool, system_id: UUID) -> str | None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s", (system_id,))
        row = await cur.fetchone()
    return None if row is None else str(row[0])


async def _audit_log_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM audit_log")
        row = await cur.fetchone()
    assert row is not None
    return int(row[0])


def _statuses(resp: Any) -> list[str]:
    return [item.status for item in resp.items]


# -- passive --------------------------------------------------------------------------


def test_drain_passive_cordons_and_reports_live_allocations(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            active = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            granted = await _alloc_on(pool, res_id, state=AllocationState.GRANTED)
            await _alloc_on(pool, res_id, state=AllocationState.RELEASED)  # terminal: excluded
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            breakglass_rows = await _audit_log_count(pool)
            active_state = await _alloc_state(pool, active)
            granted_state = await _alloc_state(pool, granted)
        assert resp.object_id == res_id
        assert resp.status == "cordoned"
        assert row == {"status": "available", "cordoned": True}
        assert {item.object_id for item in resp.items} == {str(active), str(granted)}
        assert sorted(_statuses(resp)) == ["active", "granted"]
        # Passive leaves them running: no release transitions written.
        assert breakglass_rows == 0
        assert active_state == "active"
        assert granted_state == "granted"

    asyncio.run(_run())


def test_drain_passive_empty_host_cordons_zero_items(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
        assert resp.status == "cordoned"
        assert resp.items == []
        assert row["cordoned"] is True

    asyncio.run(_run())


def test_drain_passive_denied_for_non_operator_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _NON_OPERATOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False  # denied call did not cordon
        assert audited == []  # project-only denial is not recorded

    asyncio.run(_run())


def test_drain_passive_denied_for_auditor_is_audited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _AUDITOR, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False
        assert audited == [
            ("auditor-1", "platform_auditor", "resources.drain", f"resource:{res_id}")
        ]

    asyncio.run(_run())


def test_drain_passive_denied_for_admin_only_token(migrated_url: str) -> None:
    # The role model is non-hierarchical (admin implies only auditor), so an admin-only token
    # is denied passive drain, which is a platform_operator action (ADR-0062 §3, rbac.py).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="passive"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False
        assert audited == [("admin-1", "platform_admin", "resources.drain", f"resource:{res_id}")]

    asyncio.run(_run())


# -- force_release --------------------------------------------------------------------


def test_drain_force_release_operator_denied(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=res_id, mode="force_release", reason="evict"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
            alloc_state = await _alloc_state(pool, alloc)
        assert resp.error_category == "authorization_denied"
        assert row["cordoned"] is False  # denied before cordon
        assert alloc_state == "active"  # untouched
        assert audited == [("op-1", "platform_operator", "resources.drain", f"resource:{res_id}")]

    asyncio.run(_run())


def test_drain_force_release_blank_reason_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="   "
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
            alloc_state = await _alloc_state(pool, alloc)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row["cordoned"] is False  # blank reason rejected before cordon
        assert alloc_state == "active"
        assert audited == []

    asyncio.run(_run())


def test_drain_force_release_admin_empties_host(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            a1 = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            a2 = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="maintenance"
            )
            row = await _row(pool, res_id)
            drain_rows = await _platform_audit_count(pool, "resources.drain")
            transitions = await _audit_log_count(pool)
            a1_state = await _alloc_state(pool, a1)
            a2_state = await _alloc_state(pool, a2)
        assert resp.status == "cordoned"
        assert row["cordoned"] is True
        assert _statuses(resp) == ["released", "released"]
        assert resp.data["released"] == "2"
        assert a1_state == "released"
        assert a2_state == "released"
        # 1 cordon row + 1 break-glass row per allocation.
        assert drain_rows == 3
        # 2 guard-exempt transition rows per released allocation.
        assert transitions == 4

    asyncio.run(_run())


def test_drain_force_release_empties_every_tenant_on_the_host(migrated_url: str) -> None:
    # The escalation to platform_admin exists because force_release empties EVERY tenant's
    # allocations on the host (ADR-0062 §3) — verify the snapshot is not project-scoped and
    # each release is attributed to its own project.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            a_x = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, project="tenant-x")
            a_y = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, project="tenant-y")
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="decommission"
            )
            x_state = await _alloc_state(pool, a_x)
            y_state = await _alloc_state(pool, a_y)
            audited = await _platform_audit_rows(pool)
        assert _statuses(resp) == ["released", "released"]
        assert resp.data["released"] == "2"
        assert x_state == "released"
        assert y_state == "released"
        # Each cross-tenant release is attributed to its own project via the break-glass scope.
        breakglass_scopes = {scope for _, _, tool, scope in audited if tool == "resources.drain"}
        assert f"tenant-x:{a_x}" in breakglass_scopes
        assert f"tenant-y:{a_y}" in breakglass_scopes

    asyncio.run(_run())


def test_drain_force_release_empty_host_is_idempotent_noop(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="already empty"
            )
            row = await _row(pool, res_id)
            drain_rows = await _platform_audit_count(pool, "resources.drain")
            transitions = await _audit_log_count(pool)
        assert resp.status == "cordoned"
        assert resp.items == []
        assert resp.data["released"] == "0"
        assert row["cordoned"] is True
        assert drain_rows == 1  # only the cordon row
        assert transitions == 0  # no break-glass releases

    asyncio.run(_run())


def test_drain_force_release_partial_failure_observable_and_reinvokable(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            ok = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            # NULL size + a budget row makes reconcile raise CONFIGURATION_ERROR.
            bad = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE, sized=False)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="drain"
            )
            released = [i for i in resp.items if i.status == "released"]
            failed = [i for i in resp.items if i.status == "error"]
            row = await _row(pool, res_id)
            ok_state = await _alloc_state(pool, ok)
            bad_state = await _alloc_state(pool, bad)

            # Re-invoke: the released one is gone from the snapshot; the failed one returns again.
            resp2 = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="drain again"
            )
            bad_state_after = await _alloc_state(pool, bad)
        assert len(released) == 1 and len(failed) == 1
        assert failed[0].error_category == "configuration_error"
        assert "current_status" not in failed[0].data  # reconcile path carries none
        assert ok_state == "released"
        # The failed one rolled back to active (not stranded in releasing) — re-releasable.
        assert bad_state == "active"
        assert row["cordoned"] is True
        assert _statuses(resp2) == ["error"]
        assert resp2.items[0].object_id == str(bad)
        assert bad_state_after == "active"

    asyncio.run(_run())


def test_drain_force_release_leaves_system_untouched(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            alloc = await _alloc_on(pool, res_id, state=AllocationState.ACTIVE)
            system = await _system_on(pool, alloc, state=SystemState.READY)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="force_release", reason="evict"
            )
            alloc_state = await _alloc_state(pool, alloc)
            system_state = await _system_state(pool, system)
        assert _statuses(resp) == ["released"]
        assert alloc_state == "released"
        assert system_state == "ready"  # drain does not tear down Systems

    asyncio.run(_run())


# -- input validation -----------------------------------------------------------------


def test_drain_bad_uuid_is_error_unaudited(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id="not-a-uuid", mode="passive"
            )
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert resp.data["reason"] == "invalid_uuid"
        assert audited == []

    asyncio.run(_run())


def test_drain_unknown_host_is_error_uncordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await resources_tools.drain_resource(
                pool, _OPERATOR, resource_id=str(uuid4()), mode="passive"
            )
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert audited == []  # no host to cordon, nothing audited

    asyncio.run(_run())


def test_drain_unknown_mode_rejected(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            res_id = await _register(pool)
            resp = await resources_tools.drain_resource(
                pool, _ADMIN, resource_id=res_id, mode="migrate", reason="x"
            )
            row = await _row(pool, res_id)
            audited = await _platform_audit_rows(pool)
        assert resp.status == "error"
        assert resp.error_category == "configuration_error"
        assert row["cordoned"] is False  # unknown mode → no role resolved → no cordon
        assert audited == []

    asyncio.run(_run())


def test_list_paginates_with_cursor(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(5):
                await _register(pool, host_uri=f"qemu:///h{i}")
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(10):
                page = await _list_resources(pool, CTX, kind=None, limit=2, cursor=cursor)
                seen.extend(item.object_id for item in page.items)
                if not page.data["truncated"]:
                    break
                cursor = cast(str, page.data["next_cursor"])
        assert len(seen) == 5
        assert len(set(seen)) == 5

    asyncio.run(_run())


def test_list_no_truncation_at_exactly_limit(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(2):
                await _register(pool, host_uri=f"qemu:///e{i}")
            resp = await _list_resources(pool, CTX, kind=None, limit=2)
        assert resp.data["truncated"] is False
        assert resp.data["next_cursor"] is None

    asyncio.run(_run())


def test_list_truncated_count_is_over_visible_rows(migrated_url: str) -> None:
    # A hidden row between visible rows must not consume a page slot: truncation is over
    # the VISIBLE rows, not the raw fetch (ADR-0192).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            for i in range(3):
                await _register(pool, host_uri=f"qemu:///v{i}")
            hidden = await _register(pool, host_uri="qemu:///hidden")
            await _set_affinity(pool, hidden, owner_project="other")
            resp = await _list_resources(pool, CTX, kind=None, limit=3)
        assert len(resp.items) == 3
        assert resp.data["truncated"] is False

    asyncio.run(_run())


def test_list_malformed_cursor_is_config_error(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resp = await _list_resources(pool, CTX, kind=None, cursor="!!!")
        assert resp.status == "error"
        assert resp.data["reason"] == "invalid_cursor"

    asyncio.run(_run())

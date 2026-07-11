"""FastMCP wrapper-boundary tests for representative catalog and lifecycle tools."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastmcp import Client
from fastmcp.server.auth.providers.jwt import JWTVerifier
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.storage import PresignedUpload, PresignPutRequest
from kdive.db.repositories import ALLOCATIONS, BUDGETS, INVESTIGATIONS, QUOTAS, RUNS, SYSTEMS
from kdive.db.resource_discovery import register_discovered_resource
from kdive.domain.accounting.records import Budget, Quota
from kdive.domain.capacity.state import AllocationState, InvestigationState, RunState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.lifecycle.records import Allocation, Investigation, Run, System
from kdive.mcp.app import build_app
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools.catalog import resources as resources_tools
from kdive.mcp.tools.catalog.artifacts import registrar as artifacts_tools
from kdive.mcp.tools.catalog.artifacts import uploads as artifact_upload_tools
from kdive.mcp.tools.debug import ops_breakpoints as debug_breakpoint_tools
from kdive.mcp.tools.debug import sessions as debug_sessions_tools
from kdive.mcp.tools.lifecycle.allocations import registrar as allocations_tools
from kdive.mcp.tools.lifecycle.runs import registrar as runs_tools
from kdive.mcp.tools.lifecycle.systems import registrar as systems_tools
from kdive.mcp.tools.ops.resources import host_ops as ops_resources_tools
from kdive.providers.assembly import composition
from kdive.providers.fault_inject.discovery import FaultInjectDiscovery
from kdive.providers.local_libvirt.discovery import LocalLibvirtDiscovery
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole, Role
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.conftest import AUDIENCE, ISSUER, make_keypair
from tests.mcp.systems_support import fault_inject_profile, granted_allocation, upload_profile
from tests.providers.local_libvirt.fakes import FakeLibvirtConn

_DT = datetime(2026, 1, 1, tzinfo=UTC)
_RUN_BUILD_PROFILE: dict[str, Any] = {"schema_version": 1}


def _verifier() -> JWTVerifier:
    keypair = make_keypair()
    return JWTVerifier(public_key=keypair.public_key, issuer=ISSUER, audience=AUDIENCE)


def _ctx() -> RequestContext:
    return RequestContext(
        principal="wrapper-user",
        agent_session="wrapper-session",
        projects=("proj",),
        roles={"proj": Role.OPERATOR},
        platform_roles=frozenset({PlatformRole.PLATFORM_OPERATOR}),
    )


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=3, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_resource_and_limits(pool: AsyncConnectionPool) -> str:
    discovery = LocalLibvirtDiscovery(
        host_uri="qemu:///system",
        connect=lambda: FakeLibvirtConn(),
        concurrent_allocation_cap=2,
    )
    async with pool.connection() as conn:
        resource = await register_discovered_resource(
            conn,
            discovery.list_resources()[0],
            pool="local-libvirt",
            cost_class="local",
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=_DT,
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=10,
                max_concurrent_systems=10,
                updated_at=_DT,
            ),
        )
    return str(resource.id)


async def _seed_fault_inject_resource_and_limits(pool: AsyncConnectionPool) -> str:
    discovery = FaultInjectDiscovery.from_env()
    async with pool.connection() as conn:
        resource = await register_discovered_resource(
            conn,
            discovery.list_resources()[0],
            pool="fault-inject",
            cost_class="local",
        )
        await conn.execute(
            "UPDATE resources SET capabilities = capabilities || %s::jsonb WHERE id = %s",
            ('{"vcpus": 8, "memory_mb": 8192}', resource.id),
        )
        await BUDGETS.upsert(
            conn,
            Budget(
                project="proj",
                limit_kcu=Decimal("1000000"),
                spent_kcu=Decimal(0),
                updated_at=_DT,
            ),
        )
        await QUOTAS.upsert(
            conn,
            Quota(
                project="proj",
                max_concurrent_allocations=10,
                max_concurrent_systems=10,
                updated_at=_DT,
            ),
        )
    return str(resource.id)


async def _seed_ready_system_and_investigation(pool: AsyncConnectionPool) -> tuple[str, str]:
    resource_id = await _seed_resource_and_limits(pool)
    async with pool.connection() as conn:
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                resource_id=UUID(resource_id),
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                allocation_id=allocation.id,
                state=SystemState.READY,
                provisioning_profile=upload_profile(),
            ),
        )
        investigation = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                title="wrapper",
                state=InvestigationState.OPEN,
            ),
        )
    return str(system.id), str(investigation.id)


async def _seed_fault_inject_run(pool: AsyncConnectionPool) -> str:
    resource_id = await _seed_fault_inject_resource_and_limits(pool)
    async with pool.connection() as conn:
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                resource_id=UUID(resource_id),
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                allocation_id=allocation.id,
                state=SystemState.READY,
                provisioning_profile=fault_inject_profile(),
                domain_name="fault-inject-wrapper",
            ),
        )
        investigation = await INVESTIGATIONS.insert(
            conn,
            Investigation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                title="wrapper",
                state=InvestigationState.ACTIVE,
            ),
        )
        run = await RUNS.insert(
            conn,
            Run(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="wrapper-user",
                project="proj",
                investigation_id=investigation.id,
                system_id=system.id,
                target_kind=ResourceKind.LOCAL_LIBVIRT,
                state=RunState.SUCCEEDED,
                build_profile=_RUN_BUILD_PROFILE,
            ),
        )
        await conn.execute(
            "INSERT INTO run_steps (run_id, step, state, result) "
            "VALUES (%s, 'boot', 'succeeded', %s)",
            (run.id, Jsonb({})),
        )
    return str(run.id)


async def _run_count(pool: AsyncConnectionPool) -> int:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM runs")
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


class _UploadStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def presign_put(self, request: PresignPutRequest) -> PresignedUpload:
        self.calls.append((request.key, request.sha256, request.size_bytes))
        return PresignedUpload(
            url=f"https://store/{request.key}", required_headers={"x-test": "ok"}
        )


async def _call_tool(client: Client, name: str, args: dict[str, Any] | None = None) -> ToolResponse:
    result = await client.call_tool(name, args or {}, raise_on_error=False)
    assert not getattr(result, "is_error", False)
    payload = result.structured_content
    assert isinstance(payload, dict)
    return ToolResponse.model_validate(payload)


async def _call_tool_schema_rejected(client: Client, name: str, args: dict[str, Any]) -> str:
    """Call a tool expecting input-schema rejection; return the joined error text.

    A closed-value-set param typed as an enum (ADR-0147) is rejected by FastMCP at the
    input-schema layer, before the handler runs, so the wire result is an error
    ``CallToolResult`` rather than a ``configuration_error`` ``ToolResponse``.
    """
    result = await client.call_tool(name, args, raise_on_error=False)
    assert getattr(result, "is_error", False), f"expected schema rejection, got {result!r}"
    return "\n".join(getattr(block, "text", "") for block in (result.content or []))


def test_catalog_resource_wrappers_roundtrip_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            resource_id = await _seed_resource_and_limits(pool)
            monkeypatch.setattr(resources_tools, "current_context", _ctx)
            monkeypatch.setattr(ops_resources_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            async with Client(app) as client:
                listed = await _call_tool(client, "resources.list")
                cordoned = await _call_tool(
                    client, "resources.cordon", {"resource_id": resource_id}
                )
            async with pool.connection() as conn:
                row = await conn.execute(
                    "SELECT cordoned FROM resources WHERE id = %s", (UUID(resource_id),)
                )
                cordoned_state = await row.fetchone()

        assert listed.object_id == "resources"
        assert listed.status == "ok"
        assert listed.items[0].object_id == resource_id
        assert listed.items[0].data["kind"] == "local-libvirt"
        assert cordoned.object_id == resource_id
        assert cordoned.status == "available"
        assert cordoned_state == (True,)

    asyncio.run(_run())


def test_lifecycle_allocation_wrappers_roundtrip_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with _pool(migrated_url) as pool:
            await _seed_resource_and_limits(pool)
            monkeypatch.setattr(allocations_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            async with Client(app) as client:
                granted = await _call_tool(
                    client,
                    "allocations.request",
                    {
                        "project": "proj",
                        "request": {
                            "vcpus": 1,
                            "memory_gb": 1,
                            "disk_gb": 10,
                            "resource": {"mode": "kind", "kind": "local-libvirt"},
                        },
                    },
                )
                fetched = await _call_tool(
                    client, "allocations.get", {"allocation_id": granted.object_id}
                )
        return granted, fetched

    granted, fetched = asyncio.run(_run())
    assert granted.status == "granted", granted
    assert granted.data["project"] == "proj"
    assert fetched.object_id == granted.object_id
    assert fetched.status == "granted"
    assert fetched.data["project"] == "proj"


def test_systems_provision_resolves_fault_inject_runtime(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with _pool(migrated_url) as pool:
            resource_id = await _seed_fault_inject_resource_and_limits(pool)
            monkeypatch.setattr(allocations_tools, "current_context", _ctx)
            monkeypatch.setattr(systems_tools, "current_context", _ctx)
            monkeypatch.setenv("KDIVE_FAULT_INJECT", "1")
            config.load()  # re-snapshot: the pool setup above already primed the snapshot
            secret_registry = SecretRegistry()
            provider_composition = composition.ProviderComposition(secret_registry=secret_registry)
            app = build_app(
                pool,
                verifier=_verifier(),
                provider_composition=provider_composition,
                secret_registry=secret_registry,
            )
            async with Client(app) as client:
                granted = await _call_tool(
                    client,
                    "allocations.request",
                    {
                        "project": "proj",
                        "request": {
                            "vcpus": 4,
                            "memory_gb": 4,
                            "disk_gb": 20,
                            "resource": {"mode": "id", "resource_id": resource_id},
                        },
                    },
                )
                provisioned = await _call_tool(
                    client,
                    "systems.provision",
                    {"allocation_id": granted.object_id, "profile": fault_inject_profile()},
                )
        return granted, provisioned

    granted, provisioned = asyncio.run(_run())
    assert granted.status == "granted", granted
    assert provisioned.status == "queued", provisioned


def test_debug_ops_resolve_fault_inject_runtime_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse]:
        async with _pool(migrated_url) as pool:
            run_id = await _seed_fault_inject_run(pool)
            monkeypatch.setattr(debug_sessions_tools, "current_context", _ctx)
            monkeypatch.setattr(debug_breakpoint_tools, "current_context", _ctx)
            monkeypatch.setenv("KDIVE_FAULT_INJECT", "1")
            config.load()  # re-snapshot: the pool setup above already primed the snapshot
            secret_registry = SecretRegistry()
            provider_composition = composition.ProviderComposition(secret_registry=secret_registry)
            app = build_app(
                pool,
                verifier=_verifier(),
                provider_composition=provider_composition,
                secret_registry=secret_registry,
            )
            async with Client(app) as client:
                session = await _call_tool(
                    client,
                    "debug.start_session",
                    {"run_id": run_id, "transport": "gdbstub"},
                )
                breakpoint = await _call_tool(
                    client,
                    "debug.set_breakpoint",
                    {"session_id": session.object_id, "location": "panic"},
                )
        return session, breakpoint

    session, breakpoint = asyncio.run(_run())
    assert session.status == "live", session
    assert breakpoint.status == "set", breakpoint
    assert breakpoint.data["number"] == "1"


def test_runs_wrappers_roundtrip_create_and_validation_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse, int]:
        async with _pool(migrated_url) as pool:
            system_id, investigation_id = await _seed_ready_system_and_investigation(pool)
            monkeypatch.setattr(runs_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            async with Client(app) as client:
                created = await _call_tool(
                    client,
                    "runs.create",
                    {
                        "request": {
                            "investigation_id": investigation_id,
                            "system_id": system_id,
                            "build_profile": _RUN_BUILD_PROFILE,
                            "reuse_requirement": {"vcpus": 1, "memory_gb": 1, "disk_gb": 1},
                        }
                    },
                )
                invalid = await _call_tool(client, "runs.get", {"run_id": "not-a-uuid"})
            count = await _run_count(pool)
        return created, invalid, count

    created, invalid, count = asyncio.run(_run())
    assert created.status == "created", created
    assert created.data["project"] == "proj"
    assert invalid.status == "error"
    assert invalid.error_category == "configuration_error"
    assert count == 1


def test_systems_wrappers_roundtrip_define_and_validation_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, str]:
        async with _pool(migrated_url) as pool:
            allocation_id = await granted_allocation(pool)
            monkeypatch.setattr(systems_tools, "current_context", _ctx)
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            async with Client(app) as client:
                defined = await _call_tool(
                    client,
                    "systems.define",
                    {"allocation_id": allocation_id, "profile": upload_profile()},
                )
                # ADR-0147: an invalid `state` filter is now rejected at the input-schema
                # layer (state is the SystemState enum), so the wire result is an error
                # CallToolResult whose message enumerates the valid states — not a
                # post-binding configuration_error envelope (that path stays covered by the
                # direct-handler tests in test_systems_list.py).
                invalid_state_error = await _call_tool_schema_rejected(
                    client, "systems.list", {"request": {"state": "bogus"}}
                )
        return defined, invalid_state_error

    defined, invalid_state_error = asyncio.run(_run())
    assert defined.status == "defined", defined
    assert defined.suggested_next_actions == [
        "artifacts.create_system_upload",
        "systems.provision_defined",
    ]
    assert "Input should be" in invalid_state_error
    assert "ready" in invalid_state_error


def test_artifact_upload_wrapper_roundtrips_and_validates_through_fastmcp(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _run() -> tuple[ToolResponse, ToolResponse, _UploadStore]:
        async with _pool(migrated_url) as pool:
            allocation_id = await granted_allocation(pool)
            store = _UploadStore()
            monkeypatch.setattr(systems_tools, "current_context", _ctx)
            monkeypatch.setattr(artifacts_tools, "current_context", _ctx)
            monkeypatch.setattr(artifact_upload_tools, "object_store_from_env", lambda: store)
            app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())
            async with Client(app) as client:
                defined = await _call_tool(
                    client,
                    "systems.define",
                    {"allocation_id": allocation_id, "profile": upload_profile()},
                )
                upload = await _call_tool(
                    client,
                    "artifacts.create_system_upload",
                    {
                        "system_id": defined.object_id,
                        "artifacts": [{"name": "rootfs", "sha256": "checksum", "size_bytes": 10}],
                    },
                )
                invalid = await _call_tool(
                    client,
                    "artifacts.create_system_upload",
                    {"system_id": defined.object_id, "artifacts": []},
                )
        return upload, invalid, store

    upload, invalid, store = asyncio.run(_run())
    assert upload.status == "upload_ready", upload
    assert upload.items[0].object_id.endswith("/rootfs")
    assert upload.items[0].refs["upload_url"].startswith("https://store/")
    assert invalid.status == "error"
    assert invalid.error_category == "configuration_error"
    assert store.calls == [(upload.items[0].object_id, "checksum", 10)]


def test_real_build_app_tools_advertise_envelope_output_schema() -> None:
    """Every build_app tool advertises the fielded envelope schema (#565, end-to-end enumeration).

    Exercises build_app's real registry sweep (ADR-0170): a renamed registry accessor makes
    build_app raise via the zero-count guard, and a non-fielded schema fails this assertion. No DB
    is needed — list_tools does not touch the pool — so the app is built on an unopened pool.
    """
    pool = AsyncConnectionPool("postgresql://unused", open=False)
    app = build_app(pool, verifier=_verifier(), secret_registry=SecretRegistry())

    async def _schemas() -> list[dict[str, Any] | None]:
        async with Client(app) as client:
            return [t.outputSchema for t in await client.list_tools()]

    schemas = asyncio.run(_schemas())
    assert schemas, "build_app registered no tools"
    expected = set(ToolResponse.model_fields)
    for schema in schemas:
        assert schema is not None
        assert set(schema["properties"]) == expected

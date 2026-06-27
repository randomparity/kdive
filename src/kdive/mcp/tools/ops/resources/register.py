"""``resources.register_*`` — register runtime provider resources (M2.6 #396, ADR-0112).

Imperative agent-native capacity registration. Writes a ``managed_by='runtime'`` row keyed by
``(kind, name)`` so it never collides with a declarative ``config`` row (those are removed by
editing ``systems.toml``, not by this tool). A ``name`` already owned by a ``config`` row is
rejected — the file owns that identity.

Authorization: ``platform_admin`` only. Audit: one ``platform_audit_log`` row (never carrying
secret bytes — only the secret *reference* strings are recorded).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import psycopg.errors
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

import kdive.config as config
from kdive.config.core_settings import RESOURCE_LEASE_TTL_SECONDS
from kdive.domain.capacity.state import ResourceStatus
from kdive.domain.catalog.resource_capabilities import (
    CONCURRENT_ALLOCATION_CAP_KEY,
    MEMORY_MB_KEY,
    VCPUS_KEY,
)
from kdive.domain.catalog.resources import ManagedBy, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.inventory.reconcile.locks import resource_identity_lock
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.tools.ops.resources._common import (
    REGISTER_FAULT_INJECT_TOOL,
    REGISTER_LOCAL_LIBVIRT_TOOL,
    REGISTER_REMOTE_LIBVIRT_TOOL,
    ResourceProbe,
    TcpResourceProbe,
    config_error,
    denied,
    secret_ref_resolves,
)
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role
from kdive.security.secrets.secrets import secrets_root_from_env

_log = logging.getLogger(__name__)

_FAULT_INJECT_HOST_URI = "fault-inject://local"


class RuntimeResourceRegistration(BaseModel):
    """Shared MCP request fields for runtime resource registration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="The (kind, name) identity for the new resource.")
    cost_class: str = Field(description="The cost class for pricing.")
    concurrent_allocation_cap: int = Field(
        default=1, description="Per-host concurrent-allocation cap (> 0)."
    )
    vcpus: int = Field(
        gt=0,
        description=(
            "The host's vCPU size ceiling. Admission rejects a selector larger than this "
            "(ADR-0007 §2), so a host registered without it is un-grantable."
        ),
    )
    memory_mb: int = Field(
        gt=0, description="The host's memory size ceiling in MiB (admission ≤-resource-caps check)."
    )
    secret_refs: tuple[str, ...] = Field(
        default=(),
        description=(
            "Credential reference strings to preflight-resolve, e.g. cert/key/CA refs. "
            "Only the references are stored; secret bytes are never fetched or logged."
        ),
    )
    owner_project: str | None = Field(
        default=None,
        description=(
            "Owning project; defaults to the single registering project. Pass '*' for a "
            "global (any-project) resource."
        ),
    )


class RemoteLibvirtResourceRegistration(RuntimeResourceRegistration):
    """Remote-libvirt runtime resource registration request."""

    host_uri: str = Field(description="Remote-libvirt provider host URI.")
    base_image: str = Field(description="Registered remote-libvirt base image name.")


class LocalLibvirtResourceRegistration(RuntimeResourceRegistration):
    """Local-libvirt runtime resource registration request."""

    host_uri: str = Field(description="Local-libvirt provider host URI.")


class FaultInjectResourceRegistration(RuntimeResourceRegistration):
    """Fault-inject runtime resource registration request."""


def _lease_deadline() -> datetime:
    """``now() + KDIVE_RESOURCE_LEASE_TTL_SECONDS`` — the runtime-resource lease horizon."""
    return datetime.now(UTC) + timedelta(seconds=config.require(RESOURCE_LEASE_TTL_SECONDS))


def _resolve_owner_project(
    ctx: RequestContext, owner_project: str | None
) -> str | None | ToolResponse:
    """Resolve the owner project: explicit wins; else default to the single registering project.

    A global (``None``) resource is requested with the literal sentinel ``"*"``. When no explicit
    project is given and the caller holds exactly one project, that project is the default; an
    ambiguous (multiple) or absent project membership requires an explicit ``owner_project``.
    """
    if owner_project == "*":
        return None
    if owner_project is not None:
        return owner_project
    if len(ctx.projects) == 1:
        return ctx.projects[0]
    return config_error(
        "owner_project",
        "owner_project could not be defaulted: caller has no single registering project; "
        "pass owner_project explicitly (or '*' for a global resource)",
    )


def _validate_secret_refs(
    *, name: str, secret_refs: tuple[str, ...], secrets_root: Path
) -> ToolResponse | None:
    for ref in secret_refs:
        if not secret_ref_resolves(ref, secrets_root):
            return config_error(name, f"secret reference {ref!r} does not resolve")
    return None


async def _validate_reachability(
    *, name: str, host_uri: str, probe: ResourceProbe
) -> ToolResponse | None:
    if not await probe.probe(host_uri):
        return config_error(name, f"host {host_uri!r} is not reachable")
    return None


async def _validate_runtime_name_available(
    conn: AsyncConnection, *, kind: ResourceKind, name: str
) -> ToolResponse | None:
    if await _reject_config_name(conn, kind, name):
        return ToolResponse.failure(
            name,
            ErrorCategory.CONFLICT,
            data={"reason": f"{name!r} is a config-managed resource; edit systems.toml"},
        )
    return None


async def _validate_remote_base_image(
    conn: AsyncConnection, *, name: str, base_image: str
) -> ToolResponse | None:
    if not base_image:
        return config_error(name, "remote-libvirt requires a base_image")
    if not await _base_image_registered(conn, ResourceKind.REMOTE_LIBVIRT, base_image):
        return config_error(
            name,
            f"base_image {base_image!r} is not a registered image for "
            f"{ResourceKind.REMOTE_LIBVIRT.value}",
        )
    return None


async def _base_image_registered(
    conn: AsyncConnection, kind: ResourceKind, base_image: str
) -> bool:
    """Whether ``base_image`` names a ``registered`` image_catalog row for ``kind``'s provider."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM image_catalog "
            "WHERE provider = %s AND name = %s AND state = 'registered' LIMIT 1",
            (kind.value, base_image),
        )
        return (await cur.fetchone()) is not None


async def _reject_config_name(conn: AsyncConnection, kind: ResourceKind, name: str) -> bool:
    """Whether a ``config``-owned row already owns ``(kind, name)`` (register must refuse it)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM resources WHERE kind = %s AND name = %s AND managed_by = %s LIMIT 1",
            (kind.value, name, ManagedBy.CONFIG.value),
        )
        return (await cur.fetchone()) is not None


async def _authorize_registration(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    name: str,
) -> ToolResponse | None:
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool, ctx, tool=tool, scope=f"denied:{name}", args={"name": name}
        )
        return denied(name, tool)
    return None


def _validate_registration_and_resolve_owner(
    ctx: RequestContext,
    request: RuntimeResourceRegistration,
) -> tuple[ToolResponse | None, str | None]:
    if request.concurrent_allocation_cap <= 0:
        return (
            config_error(request.name, "concurrent_allocation_cap must be a positive integer"),
            None,
        )
    resolved_owner = _resolve_owner_project(ctx, request.owner_project)
    if isinstance(resolved_owner, ToolResponse):
        return resolved_owner, None
    return None, resolved_owner


def _default_registration_seams(
    probe: ResourceProbe | None, secrets_root: Path | None
) -> tuple[ResourceProbe, Path]:
    return probe or TcpResourceProbe(), secrets_root or secrets_root_from_env()


type ResourceDbPreflight = Callable[[AsyncConnection], Awaitable[ToolResponse | None]]
type RegistrationPlanFactory = Callable[[str | None], "_RegistrationPlan"]


@dataclass(frozen=True, slots=True)
class _RegistrationPlan:
    kind: ResourceKind
    request: RuntimeResourceRegistration
    host_uri: str
    base_image: str | None
    owner_project: str | None
    tool: str
    db_preflight: ResourceDbPreflight


async def _register_with_plan(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    request: RuntimeResourceRegistration,
    tool: str,
    plan_factory: RegistrationPlanFactory,
    probe: ResourceProbe | None,
    secrets_root: Path | None,
    reachability_host_uri: str | None = None,
) -> ToolResponse:
    failure = await _authorize_registration(pool, ctx, tool=tool, name=request.name)
    if failure is not None:
        return failure
    failure, owner_project = _validate_registration_and_resolve_owner(ctx, request)
    if failure is not None:
        return failure
    if reachability_host_uri is None:
        _, secrets_root = _default_registration_seams(None, secrets_root)
    else:
        resolved_probe, secrets_root = _default_registration_seams(probe, secrets_root)
    failure = _validate_secret_refs(
        name=request.name, secret_refs=request.secret_refs, secrets_root=secrets_root
    )
    if failure is not None:
        return failure
    if reachability_host_uri is not None:
        failure = await _validate_reachability(
            name=request.name, host_uri=reachability_host_uri, probe=resolved_probe
        )
        if failure is not None:
            return failure
    return await _insert_registered_resource(
        pool,
        ctx,
        plan=plan_factory(owner_project),
    )


async def _insert_registered_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    plan: _RegistrationPlan,
) -> ToolResponse:
    """Run the DB preflight + the guarded INSERT in one transaction; map conflicts to envelopes."""
    request = plan.request
    lease = _lease_deadline()
    caps = {
        VCPUS_KEY: request.vcpus,
        MEMORY_MB_KEY: request.memory_mb,
        CONCURRENT_ALLOCATION_CAP_KEY: request.concurrent_allocation_cap,
    }
    try:
        # Serialize with the inventory reconcile on the (kind, name) identity so a concurrent
        # reconcile adopt/prune of this name and this register cannot interleave (ADR-0112).
        async with (
            pool.connection() as conn,
            conn.transaction(),
            resource_identity_lock(conn, plan.kind, request.name),
        ):
            failure = await plan.db_preflight(conn)
            if failure is not None:
                return failure
            resource_id = await _insert_runtime_resource(
                conn,
                kind=plan.kind,
                name=request.name,
                caps=caps,
                cost_class=request.cost_class,
                host_uri=plan.host_uri,
                owner_project=plan.owner_project,
                lease=lease,
            )
            await _audit_register(
                conn,
                ctx,
                plan=plan,
                resource_id=resource_id,
            )
    except psycopg.errors.UniqueViolation:
        return ToolResponse.failure(
            request.name,
            ErrorCategory.CONFLICT,
            data={"reason": f"a {plan.kind.value} resource named {request.name!r} already exists"},
        )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(request.name, exc)

    _log.info(
        "runtime resource %r (%s/%s) registered by %s",
        request.name,
        plan.kind.value,
        resource_id,
        ctx.principal,
    )
    return ToolResponse.success(
        str(resource_id),
        "registered",
        suggested_next_actions=["resources.list", "resources.renew"],
        data={"id": str(resource_id), "name": request.name, "kind": plan.kind.value},
    )


async def _insert_runtime_resource(
    conn: AsyncConnection,
    *,
    kind: ResourceKind,
    name: str,
    caps: dict[str, int],
    cost_class: str,
    host_uri: str,
    owner_project: str | None,
    lease: datetime,
) -> UUID:
    """INSERT the runtime resource row and return its id."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "INSERT INTO resources "
            "  (kind, name, capabilities, pool, cost_class, status, host_uri, managed_by, "
            "   owner_project, lease_expires_at) "
            "VALUES (%s, %s, %s, 'default', %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                kind.value,
                name,
                Jsonb(caps),
                cost_class,
                ResourceStatus.AVAILABLE.value,
                host_uri,
                ManagedBy.RUNTIME.value,
                owner_project,
                lease,
            ),
        )
        row = await cur.fetchone()
    return _returned_resource_id(row)


def _returned_resource_id(row: dict[str, object] | None) -> UUID:
    if row is None:
        raise CategorizedError(
            "runtime resource insert returned no row",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "registering runtime resource", "field": "id"},
        )
    resource_id = row.get("id")
    if not isinstance(resource_id, UUID):
        raise CategorizedError(
            "runtime resource insert returned an invalid id",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "registering runtime resource",
                "field": "id",
                "expected": "uuid",
            },
        )
    return resource_id


async def _audit_register(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    plan: _RegistrationPlan,
    resource_id: UUID,
) -> None:
    """Write the register audit row (secret references only — never secret bytes)."""
    request = plan.request
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=plan.tool,
            scope=f"resource:{resource_id}",
            args={
                "name": request.name,
                "kind": plan.kind.value,
                "host_uri": plan.host_uri,
                "base_image": plan.base_image,
                "cost_class": request.cost_class,
                "concurrent_allocation_cap": request.concurrent_allocation_cap,
                "secret_refs": list(request.secret_refs),
                "owner_project": plan.owner_project,
            },
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )


async def register_remote_libvirt_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RemoteLibvirtResourceRegistration,
    *,
    probe: ResourceProbe | None = None,
    secrets_root: Path | None = None,
) -> ToolResponse:
    """Register a runtime remote-libvirt resource. Requires ``platform_admin``."""
    if not request.host_uri.strip():
        return config_error(request.name, "remote_libvirt requires a host URI")

    async def db_preflight(conn: AsyncConnection) -> ToolResponse | None:
        failure = await _validate_runtime_name_available(
            conn, kind=ResourceKind.REMOTE_LIBVIRT, name=request.name
        )
        if failure is not None:
            return failure
        return await _validate_remote_base_image(
            conn, name=request.name, base_image=request.base_image
        )

    def plan_factory(owner_project: str | None) -> _RegistrationPlan:
        return _RegistrationPlan(
            kind=ResourceKind.REMOTE_LIBVIRT,
            request=request,
            host_uri=request.host_uri,
            base_image=request.base_image,
            owner_project=owner_project,
            tool=REGISTER_REMOTE_LIBVIRT_TOOL,
            db_preflight=db_preflight,
        )

    return await _register_with_plan(
        pool,
        ctx,
        request=request,
        tool=REGISTER_REMOTE_LIBVIRT_TOOL,
        plan_factory=plan_factory,
        probe=probe,
        secrets_root=secrets_root,
        reachability_host_uri=request.host_uri,
    )


async def register_local_libvirt_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: LocalLibvirtResourceRegistration,
    *,
    probe: ResourceProbe | None = None,
    secrets_root: Path | None = None,
) -> ToolResponse:
    """Register a runtime local-libvirt resource. Requires ``platform_admin``."""
    if not request.host_uri.strip():
        return config_error(request.name, "local_libvirt requires a host URI")

    async def db_preflight(conn: AsyncConnection) -> ToolResponse | None:
        return await _validate_runtime_name_available(
            conn, kind=ResourceKind.LOCAL_LIBVIRT, name=request.name
        )

    def plan_factory(owner_project: str | None) -> _RegistrationPlan:
        return _RegistrationPlan(
            kind=ResourceKind.LOCAL_LIBVIRT,
            request=request,
            host_uri=request.host_uri,
            base_image=None,
            owner_project=owner_project,
            tool=REGISTER_LOCAL_LIBVIRT_TOOL,
            db_preflight=db_preflight,
        )

    return await _register_with_plan(
        pool,
        ctx,
        request=request,
        tool=REGISTER_LOCAL_LIBVIRT_TOOL,
        plan_factory=plan_factory,
        probe=probe,
        secrets_root=secrets_root,
        reachability_host_uri=request.host_uri,
    )


async def register_fault_inject_resource(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: FaultInjectResourceRegistration,
    *,
    probe: ResourceProbe | None = None,
    secrets_root: Path | None = None,
) -> ToolResponse:
    """Register a runtime fault-inject resource. Requires ``platform_admin``."""

    async def db_preflight(conn: AsyncConnection) -> ToolResponse | None:
        return await _validate_runtime_name_available(
            conn, kind=ResourceKind.FAULT_INJECT, name=request.name
        )

    def plan_factory(owner_project: str | None) -> _RegistrationPlan:
        return _RegistrationPlan(
            kind=ResourceKind.FAULT_INJECT,
            request=request,
            host_uri=_FAULT_INJECT_HOST_URI,
            base_image=None,
            owner_project=owner_project,
            tool=REGISTER_FAULT_INJECT_TOOL,
            db_preflight=db_preflight,
        )

    return await _register_with_plan(
        pool,
        ctx,
        request=request,
        tool=REGISTER_FAULT_INJECT_TOOL,
        plan_factory=plan_factory,
        probe=probe,
        secrets_root=secrets_root,
    )


__all__ = [
    "FaultInjectResourceRegistration",
    "LocalLibvirtResourceRegistration",
    "RemoteLibvirtResourceRegistration",
    "register_fault_inject_resource",
    "register_local_libvirt_resource",
    "register_remote_libvirt_resource",
]

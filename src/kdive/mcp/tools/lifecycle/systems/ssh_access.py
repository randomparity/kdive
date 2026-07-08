"""systems.ssh_info / systems.authorize_ssh_key handlers (ADR-0271, #782).

``ssh_info`` discloses the provider-agnostic SSH connection descriptor for a ready System;
``authorize_ssh_key`` enqueues a worker job that appends the agent's validated public key to the
guest root ``authorized_keys`` over the managed-key loopback SSH. KDIVE never holds the agent's
private key.
"""

from __future__ import annotations

import hashlib
from uuid import uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import AuthorizeSshKeyPayload, CheckSshReachablePayload
from kdive.log import bind_context
from kdive.mcp.exposure import visible_next_actions
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import as_uuid as _as_uuid
from kdive.mcp.tools._common import invalid_uuid_error as _invalid_uuid_error
from kdive.mcp.tools._common import not_found as _not_found
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.handles import SystemHandle
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role, require_role
from kdive.security.ssh_authorized_key import validate_authorized_public_key

_SSH_USER = "root"
_NOT_READY_DETAIL = "System is not ready; SSH is available only on a ready System."
# The local-libvirt forward is rendered on every domain now (ADR-0281, #937), so a None endpoint
# on a ready System means the provider exposes no loopback SSH forward — direct SSH to a System is
# a local-libvirt capability, not a missing per-profile credential.
_UNPROVISIONED_DETAIL = (
    "This System's provider exposes no loopback SSH forward; direct SSH to a System is a "
    "local-libvirt capability."
)


async def ssh_info(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Return the SSH connection descriptor for a ready System (read-only, VIEWER)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                binding = await resolver.binding_for_system(conn, uid)
                recorded = binding.runtime.connector.recorded_ssh_endpoint(
                    SystemHandle(system.domain_name or str(system.id))
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
    if recorded is None:
        return ToolResponse.failure(
            system_id,
            ErrorCategory.CONFIGURATION_ERROR,
            detail=_UNPROVISIONED_DETAIL,
            data={"reason": "ssh_not_provisioned"},
        )
    host, port = recorded
    actions = visible_next_actions(
        ["systems.check_ssh_reachable", "systems.authorize_ssh_key", "systems.get"],
        ctx,
        system.project,
    )
    return ToolResponse.success(
        system_id,
        "ok",
        data={
            "ssh": {
                "user": _SSH_USER,
                "host": host,
                "port": port,
                "jump_host": None,
                "host_scope": "worker_loopback",
            }
        },
        suggested_next_actions=actions,
    )


async def authorize_ssh_key(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    public_key: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Authorize an agent public key in a ready System's guest (mutating, OPERATOR worker job)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.OPERATOR)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                binding = await resolver.binding_for_system(conn, uid)
                recorded = binding.runtime.connector.recorded_ssh_endpoint(
                    SystemHandle(system.domain_name or str(system.id))
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            if recorded is None:
                return ToolResponse.failure(
                    system_id,
                    ErrorCategory.CONFIGURATION_ERROR,
                    detail=_UNPROVISIONED_DETAIL,
                    data={"reason": "ssh_not_provisioned"},
                )
            try:
                normalized = validate_authorized_public_key(public_key)
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            # The dedup_key includes the key fingerprint so re-authorizing the *same* key is
            # idempotent, but a *distinct* key gets its own job — a System-only key would collapse
            # every key after the first into the first job (dedup_key is a permanent UNIQUE column).
            fingerprint = hashlib.sha256(normalized.encode()).hexdigest()[:16]
            job = await queue.enqueue(
                conn,
                JobKind.AUTHORIZE_SSH_KEY,
                AuthorizeSshKeyPayload(system_id=system_id, public_key=normalized),
                job_authorizing(ctx, system.project),
                f"{system_id}:authorize_ssh_key:{fingerprint}",
            )
    return ToolResponse.from_job(job)


async def check_ssh_reachable(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Enqueue a runtime SSH-reachability probe for a ready System (read-only, VIEWER)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                binding = await resolver.binding_for_system(conn, uid)
                recorded = binding.runtime.connector.recorded_ssh_endpoint(
                    SystemHandle(system.domain_name or str(system.id))
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            if recorded is None:
                return ToolResponse.failure(
                    system_id,
                    ErrorCategory.CONFIGURATION_ERROR,
                    detail=_UNPROVISIONED_DETAIL,
                    data={"reason": "ssh_not_provisioned"},
                )
            # A liveness probe is a fresh measurement each call: a nonce dedup_key mints a distinct
            # job so a re-issue never returns a prior (succeeded, permanent-UNIQUE) job's stale
            # verdict. authorize_ssh_key keys on the key fingerprint for the opposite, idempotent,
            # reason.
            job = await queue.enqueue(
                conn,
                JobKind.CHECK_SSH_REACHABLE,
                CheckSshReachablePayload(system_id=system_id),
                job_authorizing(ctx, system.project),
                f"{system_id}:check_ssh_reachable:{uuid4().hex}",
            )
    return ToolResponse.from_job(job)

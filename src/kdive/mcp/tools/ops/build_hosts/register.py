"""``build_hosts.register_*`` handlers — register new remote build hosts.

Two remote kinds are registerable: ``ssh`` and ``ephemeral_libvirt``. The
``worker-local`` ``local`` seed is injected at migration time and is not reproduced
through this path.

Authorization: ``platform_admin`` only.
Audit: one ``platform_audit_log`` row (never containing secret bytes).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import LiteralString
from uuid import UUID

import psycopg.errors
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field

from kdive.db.build_hosts import BuildHostKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

_log = logging.getLogger(__name__)

REGISTER_SSH_TOOL = "build_hosts.register_ssh"
REGISTER_EPHEMERAL_LIBVIRT_TOOL = "build_hosts.register_ephemeral_libvirt"


class BuildHostRegistration(BaseModel):
    """Shared MCP request fields for remote build-host registration."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Unique human-readable identifier for the new host.")
    workspace_root: str = Field(description="Absolute path where builds are staged.")
    max_concurrent: int = Field(
        description="Maximum simultaneous build leases this host may hold (> 0)."
    )
    toolchain_desc: str | None = Field(
        default=None,
        description=(
            "Operator-asserted toolchain summary shown to developers in build_envs.list, "
            "e.g. 'gcc11, binutils2.40; suits rhel9/5.14'. Not verified against the image."
        ),
    )


class SshBuildHostRegistration(BuildHostRegistration):
    """SSH build-host registration request."""

    address: str = Field(description="SSH hostname or IP address.")
    ssh_credential_ref: str = Field(
        description=(
            "Credential secret reference, e.g. 'ssh://build-host-key'. "
            "Only the reference string is stored; secret bytes are never fetched."
        )
    )


class EphemeralLibvirtBuildHostRegistration(BuildHostRegistration):
    """Ephemeral-libvirt build-host registration request."""

    base_image_volume: str = Field(
        description="Base build-image volume name in the remote storage pool."
    )


def _denied(object_id: str, tool: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[tool]
    )


# A `base_image_volume` whose name carries this marker is a guest/boot rootfs from the ADR-0188
# remote-libvirt catalog (`<distro>-kdive-remote-base-<ver>.qcow2`). That image boots and crash-
# captures but carries no kernel build toolchain, so registering it as an ephemeral build host
# would only fail minutes into the first build with `git: not found` (ADR-0196).
_GUEST_ROOTFS_VOLUME_MARKER = "kdive-remote-base"

# Surfaced in the registration rejection and on a toolchain-missing build failure so the operator
# can verify a registered host's builder. Referenced as a literal here (not imported from
# `diagnostics/checks.py`: the legal import direction is diagnostics → providers/mcp).
_BUILDHOST_AGENT_DIAGNOSTIC = "ops.diagnostics --with-buildhost-agent"


def _config_error(name: str, reason: str, *, detail: str | None = None) -> ToolResponse:
    data: dict[str, str] = {"reason": reason}
    if detail is not None:
        data["detail"] = detail
    return ToolResponse.failure(name, ErrorCategory.CONFIGURATION_ERROR, data=data)


def _validate_credential_ref(ref: str | None) -> bool:
    """Return True iff ``ref`` is a non-empty, non-blank credential reference string.

    We validate presence and non-blankness only — the bytes are never fetched here,
    keeping this tool free of secret material.
    """
    return bool(ref and ref.strip())


# A validated INSERT plan: a literal statement (fixed column set per kind, so the SQL stays a
# LiteralString — no dynamic SQL) plus its bound values; or a typed failure envelope.
_SSH_INSERT: LiteralString = (
    "INSERT INTO build_hosts "
    "  (name, kind, address, ssh_credential_ref, workspace_root, max_concurrent, toolchain_desc) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id"
)
_EPHEMERAL_INSERT: LiteralString = (
    "INSERT INTO build_hosts "
    "  (name, kind, base_image_volume, workspace_root, max_concurrent, toolchain_desc) "
    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id"
)


@dataclass(frozen=True, slots=True)
class _InsertPlan:
    sql: LiteralString
    values: tuple[object, ...]
    kind: BuildHostKind


def _ssh_plan(
    request: SshBuildHostRegistration,
) -> _InsertPlan | ToolResponse:
    if not _validate_credential_ref(request.ssh_credential_ref):
        return _config_error(
            request.name, "ssh_credential_ref must be a non-blank reference string"
        )
    if not request.address.strip():
        return _config_error(request.name, "an ssh build host requires an address")
    return _InsertPlan(
        sql=_SSH_INSERT,
        values=(
            request.name,
            BuildHostKind.SSH.value,
            request.address,
            request.ssh_credential_ref,
            request.workspace_root,
            request.max_concurrent,
            request.toolchain_desc,
        ),
        kind=BuildHostKind.SSH,
    )


def _ephemeral_plan(
    request: EphemeralLibvirtBuildHostRegistration,
) -> _InsertPlan | ToolResponse:
    if not request.base_image_volume.strip():
        return _config_error(
            request.name, "an ephemeral_libvirt build host requires a base_image_volume"
        )
    if _GUEST_ROOTFS_VOLUME_MARKER in request.base_image_volume.lower():
        return _config_error(
            request.name,
            "base_image_volume looks like a guest/boot rootfs (the kdive-remote-base catalog), "
            "which carries no kernel build toolchain — a build on it fails with 'git: not found'",
            detail=(
                "stage a build base image that carries the kernel toolchain (git, flex, bison, "
                "bc, make, objcopy, tar) per docs/operating/runbooks/remote-libvirt-host-setup.md, "
                f"and verify a registered host's builder with `{_BUILDHOST_AGENT_DIAGNOSTIC}`"
            ),
        )
    return _InsertPlan(
        sql=_EPHEMERAL_INSERT,
        values=(
            request.name,
            BuildHostKind.EPHEMERAL_LIBVIRT.value,
            request.base_image_volume,
            request.workspace_root,
            request.max_concurrent,
            request.toolchain_desc,
        ),
        kind=BuildHostKind.EPHEMERAL_LIBVIRT,
    )


def _insert_plan_for(
    request: SshBuildHostRegistration | EphemeralLibvirtBuildHostRegistration,
) -> _InsertPlan | ToolResponse:
    if isinstance(request, SshBuildHostRegistration):
        return _ssh_plan(request)
    return _ephemeral_plan(request)


async def _register_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    tool: str,
    request: SshBuildHostRegistration | EphemeralLibvirtBuildHostRegistration,
) -> ToolResponse:
    """INSERT a new remote build host row. Requires ``platform_admin``.

    Two remote kinds are registerable (the ``local`` ``worker-local`` seed is injected at
    migration time, not through this path):

    - ``ssh`` — requires ``address`` + ``ssh_credential_ref``; ``base_image_volume``
      must be absent.
    - ``ephemeral_libvirt`` — requires ``base_image_volume``; ``address``/``ssh_credential_ref``
      must be absent (the build VM lives on the configured remote-libvirt host; it has no SSH
      credential).

    Args:
        pool: The shared async connection pool.
        ctx: The caller's request context (must hold ``platform_admin``).
        tool: The public MCP tool name for this wrapper, either ``REGISTER_SSH_TOOL`` or
            ``REGISTER_EPHEMERAL_LIBVIRT_TOOL``. The value is reused in authorization-denial
            ``suggested_next_actions`` and as the ``platform_audit_log.tool`` value for the
            successful registration audit row.
        request: Validated per-kind request model. SSH requests carry address and credential
            reference; ephemeral-libvirt requests carry the operator-staged base image volume.
            ``ssh_credential_ref`` is only a secret reference string; this path never fetches
            or stores SSH secret bytes.

    Returns:
        A success envelope with ``suggested_next_actions`` set to ``build_hosts.list`` and
        ``runs.build``, plus ``data`` containing the new ``id`` and ``request.name``. Failures
        use typed envelopes for ``authorization_denied``, ``conflict``,
        ``configuration_error``, or ``infrastructure_failure``.
    """
    try:
        require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    except AuthorizationError:
        await audit_platform_denial(
            pool,
            ctx,
            tool=tool,
            scope=f"denied:{request.name}",
            args={"name": request.name},
        )
        return _denied(request.name, tool)

    if request.max_concurrent <= 0:
        return _config_error(request.name, "max_concurrent must be a positive integer")

    plan = _insert_plan_for(request)
    if isinstance(plan, ToolResponse):
        return plan

    try:
        async with pool.connection() as conn, conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(plan.sql, plan.values)
                row = await cur.fetchone()
            host_id = _returned_build_host_id(row)

            await audit.record_platform(
                conn,
                principal=ctx.principal,
                agent_session=ctx.agent_session,
                event=audit.PlatformAuditEvent(
                    tool=tool,
                    scope=f"build_host:{host_id}",
                    args={
                        "name": request.name,
                        "kind": plan.kind.value,
                        "address": getattr(request, "address", None),
                        "ssh_credential_ref": getattr(request, "ssh_credential_ref", None),
                        "base_image_volume": getattr(request, "base_image_volume", None),
                        "workspace_root": request.workspace_root,
                        "max_concurrent": request.max_concurrent,
                    },
                    platform_role=held_platform_roles(ctx),
                    actor=actor_for(ctx),
                ),
            )
    except psycopg.errors.UniqueViolation:
        return ToolResponse.failure(
            request.name,
            ErrorCategory.CONFLICT,
            data={"reason": f"a build host named {request.name!r} already exists"},
        )
    except CategorizedError as exc:
        return ToolResponse.failure_from_error(request.name, exc)

    _log.info("build host %r (%s) registered by %s", request.name, host_id, ctx.principal)
    return ToolResponse.success(
        str(host_id),
        "registered",
        suggested_next_actions=["build_hosts.list", "runs.build"],
        data={"id": str(host_id), "name": request.name},
    )


def _returned_build_host_id(row: dict[str, object] | None) -> UUID:
    if row is None:
        raise CategorizedError(
            "build host insert returned no row",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"operation": "registering build host", "field": "id"},
        )
    host_id = row.get("id")
    if not isinstance(host_id, UUID):
        raise CategorizedError(
            "build host insert returned an invalid id",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={
                "operation": "registering build host",
                "field": "id",
                "expected": "uuid",
            },
        )
    return host_id


async def register_ssh_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: SshBuildHostRegistration,
) -> ToolResponse:
    """INSERT a new SSH build host row. Requires ``platform_admin``."""
    return await _register_build_host(
        pool,
        ctx,
        tool=REGISTER_SSH_TOOL,
        request=request,
    )


async def register_ephemeral_libvirt_build_host(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: EphemeralLibvirtBuildHostRegistration,
) -> ToolResponse:
    """INSERT a new ephemeral-libvirt build host row. Requires ``platform_admin``."""
    return await _register_build_host(
        pool,
        ctx,
        tool=REGISTER_EPHEMERAL_LIBVIRT_TOOL,
        request=request,
    )


__all__ = [
    "REGISTER_EPHEMERAL_LIBVIRT_TOOL",
    "REGISTER_SSH_TOOL",
    "EphemeralLibvirtBuildHostRegistration",
    "SshBuildHostRegistration",
    "register_ephemeral_libvirt_build_host",
    "register_ssh_build_host",
]

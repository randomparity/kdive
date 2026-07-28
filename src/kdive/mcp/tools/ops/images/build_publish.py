"""``images.publish`` platform-operator workflow.

One tool covers the whole build -> validate -> publish path (ADR-0461): the job it enqueues is
the same ``IMAGE_BUILD`` job whether the image is being built for the first time or a realized
``defined`` baseline is being promoted, so there is no second entry point and no second promote
implementation.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.context import authorizing as job_authorizing
from kdive.jobs.payloads import ImageBuildPayload
from kdive.log import bind_context
from kdive.mcp.platform_auth import actor_for, audit_platform_denial, held_platform_roles
from kdive.mcp.responses import ToolResponse
from kdive.security import audit
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

PUBLISH_TOOL = "images.publish"
PLATFORM_PROJECT = "platform"


def _denied(object_id: str) -> ToolResponse:
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[PUBLISH_TOOL]
    )


async def _enqueue_image_build(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Audit the operator action, then enqueue the shared ``IMAGE_BUILD`` job idempotently.

    The dedup key is the image identity so a re-issued publish returns the same job rather than
    enqueuing a duplicate. The ``platform_audit_log`` accountability row is written in the same
    transaction as the enqueue (both commit or neither does).
    """
    dedup_key = f"image_build:{payload.provider}:{payload.name}"
    async with pool.connection() as conn, conn.transaction():
        await audit.record_platform(
            conn,
            principal=ctx.principal,
            agent_session=ctx.agent_session,
            event=audit.PlatformAuditEvent(
                tool=PUBLISH_TOOL,
                scope=f"{payload.provider}:{payload.name}",
                args={"provider": payload.provider, "name": payload.name},
                platform_role=held_platform_roles(ctx),
                actor=actor_for(ctx),
            ),
        )
        job = await queue.enqueue(
            conn,
            JobKind.IMAGE_BUILD,
            payload,
            job_authorizing(ctx, PLATFORM_PROJECT),
            dedup_key,
        )
    return ToolResponse.success(
        str(job.id),
        job.state.value,
        suggested_next_actions=["jobs.wait"],
        refs={"job": str(job.id)},
        data={"kind": job.kind.value, "name": payload.name},
    )


async def publish(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    payload: ImageBuildPayload,
) -> ToolResponse:
    """Enqueue an ``IMAGE_BUILD`` job for a public base image. Requires ``platform_operator``.

    The one job builds, validates, and publishes the catalog row, so a fresh build and the
    promotion of a realized ``defined`` baseline both land through this path. The
    ``platform_operator`` gate runs first, so a denial writes no job.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=PUBLISH_TOOL,
                scope=f"denied:{payload.name}",
                args={"name": payload.name},
            )
            return _denied(payload.name)
        return await _enqueue_image_build(pool, ctx, payload=payload)

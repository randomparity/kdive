"""Shared helpers for MCP tools backed by durable jobs."""

from __future__ import annotations

from uuid import UUID

from kdive.domain.models import Job
from kdive.jobs.payloads import Authorizing, load_authorizing
from kdive.mcp.auth import RequestContext
from kdive.mcp.responses import ToolResponse


def authorizing(ctx: RequestContext, project: str) -> Authorizing:
    return Authorizing(principal=ctx.principal, agent_session=ctx.agent_session, project=project)


def context_from_job(job: Job, project: str) -> RequestContext:
    auth = load_authorizing(job)
    return RequestContext(
        principal=auth.principal,
        agent_session=auth.agent_session,
        projects=(project,),
        roles={},
    )


def job_envelope(job: Job, object_key: str, object_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, object_key: str(object_id)}})

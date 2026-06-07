"""Shared helpers for MCP tools backed by durable jobs."""

from __future__ import annotations

from uuid import UUID

from kdive.domain.models import Job
from kdive.mcp.responses import ToolResponse


def job_envelope(job: Job, object_key: str, object_id: UUID) -> ToolResponse:
    base = ToolResponse.from_job(job)
    return base.model_copy(update={"data": {**base.data, object_key: str(object_id)}})

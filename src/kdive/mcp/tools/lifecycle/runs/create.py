"""MCP response adapter for `runs.create`."""

from __future__ import annotations

from typing import cast

from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue

from kdive.mcp.responses import ToolResponse
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.services.runs.admission import (
    TARGET_KIND_VOCAB_REASONS,
    RunCreateError,
    RunCreateResult,
)
from kdive.services.runs.admission import RunCreateRequest as RunCreateRequest
from kdive.services.runs.admission import RunReuseRequirementInput as RunReuseRequirementInput
from kdive.services.runs.admission import create_run as _create_run


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    try:
        result = await _create_run(pool, ctx, request, resolver=resolver)
    except RunCreateError as exc:
        return ToolResponse.failure_from_error(exc.object_id, exc, data=_vocab_for(exc, resolver))
    return _created_response(result)


def _vocab_for(exc: RunCreateError, resolver: ProviderResolver) -> dict[str, JsonValue] | None:
    """Attach the registered `available_target_kinds` to a target_kind failure (ADR-0169).

    A registered provider kind always has a builder, so the registered set is exactly the set
    an agent may pass as `target_kind`.
    """
    if exc.details.get("reason") not in TARGET_KIND_VOCAB_REASONS:
        return None
    ordered = sorted(k.value for k in resolver.registered_kinds())
    return {"available_target_kinds": cast(list[JsonValue], ordered)}


def _created_response(result: RunCreateResult) -> ToolResponse:
    data: dict[str, JsonValue] = {
        "project": result.project,
        "investigation_id": str(result.investigation_id),
        "system_id": str(result.system_id) if result.system_id is not None else None,
        "target_kind": result.target_kind.value,
    }
    if result.expected_boot_failure_kind is not None:
        data["expected_boot_failure"] = result.expected_boot_failure_kind
    return ToolResponse.success(
        str(result.run_id),
        "created",
        suggested_next_actions=["runs.get", "runs.build"],
        data=data,
    )


__all__ = ["RunCreateRequest", "RunReuseRequirementInput", "create_run"]

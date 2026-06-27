"""MCP response adapter for `runs.create`."""

from __future__ import annotations

from typing import cast

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue

from kdive.domain.errors import CategorizedError
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._idempotency import (
    record_envelope,
    resolve_conflict,
    resolve_envelope_replay,
    validate_idempotency_key,
)
from kdive.mcp.tools.catalog.artifacts.expected_uploads import EXPECTED_UPLOADS_TOOL
from kdive.mcp.tools.catalog.artifacts.uploads import CREATE_RUN_UPLOAD_TOOL
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

_RUNS_CREATE_KIND = "runs.create"


async def create_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
    *,
    resolver: ProviderResolver,
    idempotency_key: str | None = None,
) -> ToolResponse:
    if idempotency_key is None:
        try:
            result = await _create_run(pool, ctx, request, resolver=resolver)
        except RunCreateError as exc:
            return ToolResponse.failure_from_error(
                exc.object_id, exc, data=_vocab_for(exc, resolver)
            )
        return _created_response(result)
    return await _create_run_keyed(pool, ctx, request, resolver=resolver, key=idempotency_key)


async def _create_run_keyed(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    request: RunCreateRequest,
    *,
    resolver: ProviderResolver,
    key: str,
) -> ToolResponse:
    """Run runs.create under replay-idempotency (ADR-0193).

    Validates the key, resolves a replay up-front, else creates the Run while recording the
    success envelope inside the Run-insert transaction (atomic). A key collision is resolved
    read-after-conflict to the winner's envelope (or ``CONFLICT`` for cross-tool reuse).
    """
    try:
        validate_idempotency_key(key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error("idempotency_key", exc)
    async with pool.connection() as conn:
        replay = await resolve_envelope_replay(
            conn, principal=ctx.principal, key=key, kind=_RUNS_CREATE_KIND
        )
    if replay is not None:
        return replay

    async def _record(record_conn: AsyncConnection, result: RunCreateResult) -> None:
        await record_envelope(
            record_conn,
            principal=ctx.principal,
            key=key,
            project=result.project,
            kind=_RUNS_CREATE_KIND,
            envelope=_created_response(result),
        )

    try:
        result = await _create_run(pool, ctx, request, resolver=resolver, recorder=_record)
    except RunCreateError as exc:
        return ToolResponse.failure_from_error(exc.object_id, exc, data=_vocab_for(exc, resolver))
    except UniqueViolation:
        async with pool.connection() as conn:
            try:
                return await resolve_conflict(
                    conn, principal=ctx.principal, key=key, kind=_RUNS_CREATE_KIND
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error("idempotency_key", exc)
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
        "label": result.label,
    }
    if result.expected_boot_failure_kind is not None:
        data["expected_boot_failure"] = result.expected_boot_failure_kind
    # An external build does not use the warm-tree runs.build lane; it uploads prebuilt artifacts.
    # Point it at the format advisory + upload tool so the loop is self-describing (ADR-0234 §5).
    if result.is_external:
        next_actions = ["runs.get", EXPECTED_UPLOADS_TOOL, CREATE_RUN_UPLOAD_TOOL]
    else:
        next_actions = ["runs.get", "runs.build"]
    return ToolResponse.success(
        str(result.run_id),
        "created",
        suggested_next_actions=next_actions,
        data=data,
    )


__all__ = ["RunCreateRequest", "RunReuseRequirementInput", "create_run"]

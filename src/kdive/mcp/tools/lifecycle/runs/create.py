"""MCP response adapter for `runs.create`."""

from __future__ import annotations

from typing import cast

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg_pool import AsyncConnectionPool
from pydantic import JsonValue

from kdive.domain.errors import CategorizedError
from kdive.mcp.resources.external_build_contract import EXTERNAL_BUILD_CONTRACT_URI
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import external_boot_denial
from kdive.mcp.tools.catalog.artifacts.uploads import CREATE_RUN_UPLOAD_TOOL
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.authz.context import RequestContext
from kdive.services.external_boot import ExternalBootDenied
from kdive.services.idempotency.envelope import (
    record_result,
    resolve_conflict,
    resolve_replay,
    validate_idempotency_key,
)
from kdive.services.runs.admission import (
    TARGET_KIND_VOCAB_REASONS,
    RunCreateResult,
    run_create_result_from_stored,
    stored_run_create_result,
)
from kdive.services.runs.admission import RunCreateRequest as RunCreateRequest
from kdive.services.runs.admission import RunReuseRequirementInput as RunReuseRequirementInput
from kdive.services.runs.admission import create_run as _create_run
from kdive.services.runs.host_admission import RunCreateError

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
            result = await _create_run(
                pool,
                ctx,
                request,
                available_target_kinds=resolver.registered_kinds(),
            )
        except ExternalBootDenied as exc:
            return _external_boot_denial(request, exc, ctx)
        except RunCreateError as exc:
            return ToolResponse.failure_from_error(
                exc.object_id,
                exc,
                suggested_next_actions=_failure_actions(exc),
                data=_vocab_for(exc, resolver),
            )
        return _created_response(result, server_time=await _build_server_time(pool, result))
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
    success result inside the Run-insert transaction (atomic). A key collision is resolved
    read-after-conflict to the winner's envelope (or ``CONFLICT`` for cross-tool reuse).
    """
    try:
        validate_idempotency_key(key)
    except CategorizedError as exc:
        return ToolResponse.failure_from_error("idempotency_key", exc)
    async with pool.connection() as conn:
        replay = await resolve_replay(
            conn, principal=ctx.principal, key=key, kind=_RUNS_CREATE_KIND
        )
    if replay is not None:
        result = run_create_result_from_stored(replay)
        return _created_response(result, server_time=await _build_server_time(pool, result))

    async def _record(record_conn: AsyncConnection, result: RunCreateResult) -> None:
        await record_result(
            record_conn,
            principal=ctx.principal,
            key=key,
            project=result.project,
            kind=_RUNS_CREATE_KIND,
            result=stored_run_create_result(result),
        )

    try:
        result = await _create_run(
            pool,
            ctx,
            request,
            available_target_kinds=resolver.registered_kinds(),
            recorder=_record,
        )
    except ExternalBootDenied as exc:
        return _external_boot_denial(request, exc, ctx)
    except RunCreateError as exc:
        return ToolResponse.failure_from_error(
            exc.object_id,
            exc,
            suggested_next_actions=_failure_actions(exc),
            data=_vocab_for(exc, resolver),
        )
    except UniqueViolation:
        async with pool.connection() as conn:
            try:
                winner = await resolve_conflict(
                    conn, principal=ctx.principal, key=key, kind=_RUNS_CREATE_KIND
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error("idempotency_key", exc)
        result = run_create_result_from_stored(winner)
        return _created_response(result, server_time=await _build_server_time(pool, result))
    return _created_response(result, server_time=await _build_server_time(pool, result))


def _external_boot_denial(
    request: RunCreateRequest, exc: ExternalBootDenied, ctx: RequestContext
) -> ToolResponse:
    """Render a `runs.create` denial against the target System (ADR-0583).

    Attributed to the System rather than a Run: the denial happens before any Run row exists,
    and the System is the object the activation restricts. The project this frame does not hold
    rides on the denial, so the breadcrumbs are RBAC-filtered like every other site's.
    """
    return external_boot_denial(str(request.system_id), exc, ctx)


def _vocab_for(exc: RunCreateError, resolver: ProviderResolver) -> dict[str, JsonValue] | None:
    """Attach the registered `available_target_kinds` to a target_kind failure (ADR-0169).

    A registered provider kind always has a builder, so the registered set is exactly the set
    an agent may pass as `target_kind`.
    """
    if exc.details.get("reason") not in TARGET_KIND_VOCAB_REASONS:
        return None
    ordered = sorted(k.value for k in resolver.registered_kinds())
    return {"available_target_kinds": cast(list[JsonValue], ordered)}


def _failure_actions(exc: RunCreateError) -> list[str] | None:
    if exc.details.get("reason") == "build_ref_expired":
        return ["runs.create"]
    return None


async def _build_server_time(pool: AsyncConnectionPool, result: RunCreateResult) -> str | None:
    if result.build_expires_at is None:
        return None
    async with pool.connection() as conn:
        row = await (await conn.execute("SELECT clock_timestamp()")).fetchone()
    if row is None:
        raise RuntimeError("SELECT clock_timestamp() returned no row")
    return row[0].isoformat()


def _created_response(result: RunCreateResult, *, server_time: str | None = None) -> ToolResponse:
    data: dict[str, JsonValue] = {
        "project": result.project,
        "investigation_id": str(result.investigation_id),
        "system_id": str(result.system_id) if result.system_id is not None else None,
        "target_kind": result.target_kind.value,
        "label": result.label,
        "build_ref": result.build_ref,
        "build_expires_at": result.build_expires_at,
    }
    if result.expected_boot_failure_kind is not None:
        data["expected_boot_failure"] = result.expected_boot_failure_kind
    if result.build_ref is not None:
        if server_time is None:
            raise ValueError("server_time is required with a reusable build")
        data["server_time"] = server_time
        return ToolResponse.success(
            str(result.run_id),
            "succeeded",
            suggested_next_actions=[
                "runs.get",
                "runs.bind" if result.system_id is None else "runs.install",
            ],
            data=data,
        )
    return ToolResponse.success(
        str(result.run_id),
        "created",
        suggested_next_actions=[
            "runs.get",
            CREATE_RUN_UPLOAD_TOOL,
        ],
        refs={"external_build_contract": EXTERNAL_BUILD_CONTRACT_URI},
        data=data,
    )


__all__ = ["RunCreateRequest", "RunReuseRequirementInput", "create_run"]

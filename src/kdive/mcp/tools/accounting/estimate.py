"""Read-side accounting estimate MCP tool (ADR-0007)."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, cast

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field, ValidationError

from kdive.domain.accounting.cost import (
    W_CPU,
    W_MEM,
    Selector,
    cost,
    parse_window_hours,
    quantize_kcu,
    rate,
    resolve_coeff,
    validate_size,
    validate_window,
)
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import EstimateRequestPayload
from kdive.mcp.tools import _docmeta
from kdive.security.authz.context import RequestContext, require_project
from kdive.security.authz.rbac import Role, require_role
from kdive.serialization import JsonValue

_ESTIMATE_OBJECT_ID = "estimate"


async def estimate(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    *,
    project: str,
    request: EstimateRequestPayload,
) -> ToolResponse:
    """Price a hypothetical ``selector`` over ``window`` hours, without writing anything."""
    require_project(ctx, project)
    require_role(ctx, project, Role.VIEWER)
    with bind_context(principal=ctx.principal):
        try:
            return await _estimate_inner(
                pool,
                project=project,
                request=request,
            )
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(
                _ESTIMATE_OBJECT_ID,
                exc,
                suggested_next_actions=["accounting.estimate"],
            )
        except ValueError as exc:
            return _configuration_failure(exc)


def _configuration_failure(exc: ValueError) -> ToolResponse:
    """Map a fail-closed selector/window ``ValueError`` to a field-named configuration_error.

    The domain guards raise :class:`CategorizedError` with their own ``details``; this is the
    catch-all for any residual ``ValueError`` (notably a Pydantic ``ValidationError`` building
    the selector). It names the rejected field(s) so a caller can act, rather than returning an
    opaque failure.
    """
    fields = _rejected_fields(exc)
    detail = (
        f"rejected estimate field(s): {', '.join(fields)}"
        if fields
        else (str(exc) or "invalid estimate request")
    )
    data: dict[str, JsonValue] | None = (
        {"rejected_fields": cast("JsonValue", fields)} if fields else None
    )
    return ToolResponse.failure(
        _ESTIMATE_OBJECT_ID,
        ErrorCategory.CONFIGURATION_ERROR,
        detail=detail,
        suggested_next_actions=["accounting.estimate"],
        data=data,
    )


def _rejected_fields(exc: ValueError) -> list[str]:
    """Return the sorted field names a Pydantic ``ValidationError`` rejected (else empty)."""
    if not isinstance(exc, ValidationError):
        return []
    return sorted({str(err["loc"][-1]) for err in exc.errors() if err.get("loc")})


async def _estimate_inner(
    pool: AsyncConnectionPool,
    *,
    project: str,
    request: EstimateRequestPayload,
) -> ToolResponse:
    selector = Selector(
        vcpus=request.vcpus, memory_gb=request.memory_gb, cost_class=request.cost_class
    )
    validate_size(selector)
    window_hours = parse_window_hours(request.window)
    validate_window(window_hours)
    async with pool.connection() as conn:
        coeff = await resolve_coeff(conn, selector.cost_class)
    return _estimate_response(coeff, selector, window_hours, project=project)


def _estimate_response(
    coeff: Decimal, selector: Selector, window_hours: Decimal, *, project: str
) -> ToolResponse:
    rate_kcu_per_hr = rate(coeff, vcpus=selector.vcpus, memory_gb=selector.memory_gb)
    estimate_kcu = cost(rate_kcu_per_hr, window_hours)
    vcpu_component = coeff * W_CPU * selector.vcpus
    memory_component = coeff * W_MEM * selector.memory_gb
    return ToolResponse.success(
        _ESTIMATE_OBJECT_ID,
        "ok",
        suggested_next_actions=["allocations.request"],
        data={
            "project": project,
            "cost_class": selector.cost_class,
            "estimate_kcu": str(quantize_kcu(estimate_kcu)),
            "rate_kcu_per_hr": str(quantize_kcu(rate_kcu_per_hr)),
            "breakdown_vcpu_kcu_per_hr": str(quantize_kcu(vcpu_component)),
            "breakdown_memory_kcu_per_hr": str(quantize_kcu(memory_component)),
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool) -> None:
    """Register ``accounting.estimate`` on ``app``, bound to ``pool``."""

    @app.tool(
        name="accounting.estimate",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def accounting_estimate(
        project: Annotated[str, Field(description="Project to price the estimate for.")],
        request: Annotated[
            EstimateRequestPayload,
            Field(description="Estimate request payload: size, lease window in hours, cost class."),
        ],
    ) -> ToolResponse:
        """Price a hypothetical selector over a window without writing anything. Requires viewer."""
        return await estimate(
            pool,
            current_context(),
            project=project,
            request=request,
        )

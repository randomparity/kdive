"""``ops.diagnostics`` — the authz-gated aggregating diagnostics tool (ADR-0091 §1,2,4).

The server-side `doctor` head: it runs each read-only check from its correct vantage and
returns one coherent verdict, so an operator never has to probe the worker→hypervisor or
secret-backend hops from a laptop they cannot see them from. Gated to ``platform_operator``
(a ``platform_admin``-alone token is denied — admin implies only auditor) and audited under
the resolved ``(principal, operator-cli)`` actor on both the served run and the over-reach
denial (ADR-0006, ADR-0089).

The verdict keeps the three-state distinction: each item carries the check's
``status``/``detail``/``fix``/``provider``, and a down dependency surfaces as an ``error``
item (reported distinctly) rather than a contract ``fail`` — the tool that explains
breakage must not emit a confident wrong fix when a backend was simply down.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Protocol

from fastmcp import FastMCP
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.diagnostics.checks import CheckResult, CheckStatus
from kdive.diagnostics.service import DiagnosticsReport, DiagnosticsService
from kdive.domain.errors import ErrorCategory
from kdive.log import bind_context
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._platform_auth import (
    ALL_PROJECTS_SCOPE,
    actor_for,
    audit_platform_denial,
    held_platform_roles,
)
from kdive.security import audit
from kdive.security.authz.rbac import AuthorizationError, PlatformRole, require_platform_role

if TYPE_CHECKING:
    from kdive.security.authz.context import RequestContext

_TOOL = "ops.diagnostics"
# The distinct audit tool for the mutating opt-in egress probe (ADR-0091 §4): provisioning a
# guest is recorded under its own event so it cannot be amplified into resource exhaustion
# under cover of "just running doctor", and is separable in the audit trail from the read-only
# run.
_EGRESS_TOOL = "ops.diagnostics.egress"
# The distinct audit tool for the mutating opt-in build-host agent probe (ADR-0167): provisioning
# a throwaway builder is recorded under its own event, separate from the read-only run and from the
# egress probe, for the same non-amplification reason.
_BUILDHOST_TOOL = "ops.diagnostics.buildhost_agent"
_OBJECT_ID = "diagnostics"
_log = logging.getLogger(__name__)


class ServiceFactory(Protocol):
    """Builds the diagnostics service for a provider target and the mutating opt-ins.

    ``provider`` is a named registered provider, or ``None`` for all registered.
    ``with_egress`` assembles the heavy opt-in ``guest_egress`` check (provisions a probe
    guest) on top of the cheap read-only checks (ADR-0091 §3). ``with_buildhost_agent``
    assembles the heavy opt-in ``ephemeral_libvirt_buildhost_agent`` check (provisions a
    throwaway builder per ephemeral_libvirt host) — ADR-0167.
    """

    def __call__(
        self,
        provider: str | None,
        *,
        with_egress: bool = False,
        with_buildhost_agent: bool = False,
    ) -> DiagnosticsService: ...


def _denied() -> ToolResponse:
    return ToolResponse.failure(
        _OBJECT_ID, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL]
    )


async def run_diagnostics(
    pool: AsyncConnectionPool,
    service_factory: ServiceFactory,
    ctx: RequestContext,
    *,
    provider: str | None = None,
    with_egress: bool = False,
    with_buildhost_agent: bool = False,
) -> ToolResponse:
    """Run the read-only diagnostics and return one coherent verdict; operator-gated.

    A caller without ``platform_operator`` is denied; the denial is audited iff the caller
    holds any platform role (the over-reach accountability row), and the served run is
    always audited under the resolved actor. ``with_egress`` and ``with_buildhost_agent`` opt
    into the heavy mutating probes (each provisions infrastructure); when set, the provisioning
    action is audited **distinctly** from the read-only run so it cannot be amplified under cover
    of "just running doctor" (ADR-0091 §4, ADR-0167). The verdict carries each check's three-state
    result; an ``error`` item is reported distinctly and never inflated into a ``fail``.
    """
    with bind_context(principal=ctx.principal):
        try:
            require_platform_role(ctx, PlatformRole.PLATFORM_OPERATOR)
        except AuthorizationError:
            await audit_platform_denial(
                pool,
                ctx,
                tool=_TOOL,
                scope=ALL_PROJECTS_SCOPE,
                args=_audit_args(provider, with_egress, with_buildhost_agent),
            )
            return _denied()
        report = await _diagnostics_report_from_service(
            service_factory, provider, with_egress, with_buildhost_agent
        )
        await _audit_run(pool, ctx, provider, with_egress, with_buildhost_agent)
        return _verdict(report.results, report.has_failure, report.has_error)


async def _diagnostics_report_from_service(
    service_factory: ServiceFactory,
    provider: str | None,
    with_egress: bool,
    with_buildhost_agent: bool,
) -> DiagnosticsReport:
    """Run the diagnostics service, mapping assembly failures to an ``error`` verdict.

    Assembling the service can fail before any check runs (e.g. a malformed ``KDIVE_*``
    secret value the registry cannot parse, or a mutating opt-in requested without its seam).
    That is a check-cannot-run condition, not a contract ``fail`` — so it surfaces as one
    ``error`` result rather than an unhandled exception, keeping the served call diagnosable
    and auditable (the verdict that explains breakage must not 500 on the configuration it
    exists to inspect).
    """
    try:
        service = service_factory(
            provider, with_egress=with_egress, with_buildhost_agent=with_buildhost_agent
        )
    except Exception as exc:  # noqa: BLE001 - a build/config fault is an error verdict, not a crash
        _log.error(
            "diagnostics assembly failed for provider=%r egress=%s buildhost_agent=%s: %s",
            provider,
            with_egress,
            with_buildhost_agent,
            exc,
            exc_info=True,
        )
        return DiagnosticsReport(
            results=[
                CheckResult(
                    check_id=_OBJECT_ID,
                    status=CheckStatus.ERROR,
                    detail="diagnostics could not be assembled; check the KDIVE_* configuration",
                )
            ]
        )
    return await service.run()


def _audit_args(
    provider: str | None, with_egress: bool, with_buildhost_agent: bool
) -> dict[str, object]:
    return {
        "provider": provider,
        "with_egress": with_egress,
        "with_buildhost_agent": with_buildhost_agent,
    }


async def _audit_run(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    provider: str | None,
    with_egress: bool,
    with_buildhost_agent: bool,
) -> None:
    args = _audit_args(provider, with_egress, with_buildhost_agent)
    async with pool.connection() as conn, conn.transaction():
        await _record(conn, ctx, _TOOL, args)
        if with_egress:
            await _record(conn, ctx, _EGRESS_TOOL, args)
        if with_buildhost_agent:
            await _record(conn, ctx, _BUILDHOST_TOOL, args)


async def _record(
    conn: AsyncConnection, ctx: RequestContext, tool: str, args: dict[str, object]
) -> None:
    """Record one platform-audit event under ``tool`` for the resolved actor."""
    await audit.record_platform(
        conn,
        principal=ctx.principal,
        agent_session=ctx.agent_session,
        event=audit.PlatformAuditEvent(
            tool=tool,
            scope=ALL_PROJECTS_SCOPE,
            args=args,
            platform_role=held_platform_roles(ctx),
            actor=actor_for(ctx),
        ),
    )


def _item(result: CheckResult) -> ToolResponse:
    return ToolResponse.success(
        result.check_id,
        "ok",
        data={
            "check": result.check_id,
            "status": result.status.value,
            "detail": result.detail,
            "fix": result.fix,
            "provider": result.provider,
            "failure_category": result.failure_category,
        },
    )


def _verdict(results: list[CheckResult], has_failure: bool, has_error: bool) -> ToolResponse:
    return ToolResponse.collection(
        _OBJECT_ID,
        "ok",
        [_item(r) for r in results],
        suggested_next_actions=[_TOOL],
        data={
            "has_failure": "true" if has_failure else "false",
            "has_error": "true" if has_error else "false",
        },
    )


def register(app: FastMCP, pool: AsyncConnectionPool, service_factory: ServiceFactory) -> None:
    """Register ``ops.diagnostics`` on ``app``, bound to ``pool`` and the ``service_factory``."""

    @app.tool(name=_TOOL, annotations=_docmeta.mutating(), meta={"maturity": "implemented"})
    async def ops_diagnostics(
        provider: Annotated[
            str | None,
            Field(description="Diagnose one named registered provider; omit for all registered."),
        ] = None,
        with_egress: Annotated[
            bool,
            Field(
                description="Opt into the heavy guest_egress probe: provisions a short-lived "
                "guest on the target provider and execs a presigned request to object-store "
                "from inside it. Audited distinctly; off by default."
            ),
        ] = False,
        with_buildhost_agent: Annotated[
            bool,
            Field(
                description="Opt into the heavy ephemeral_libvirt_buildhost_agent probe: "
                "provisions a throwaway builder on each ephemeral_libvirt build host and checks "
                "its guest-agent reachability. Audited distinctly; off by default."
            ),
        ] = False,
    ) -> ToolResponse:
        """Run the deployment diagnostics. Platform operator-gated.

        Returns one verdict carrying each check's three-state status, detail, fix, and the
        provider it covered. A check that could not be run (a down dependency) reports an
        ``error`` distinctly — it is not a contract failure. ``with_egress`` and
        ``with_buildhost_agent`` add the mutating probes (off by default; each provisioning is
        audited distinctly).
        """
        return await run_diagnostics(
            pool,
            service_factory,
            current_context(),
            provider=provider,
            with_egress=with_egress,
            with_buildhost_agent=with_buildhost_agent,
        )

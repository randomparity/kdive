"""FastMCP registration for the `vmcore.*` / `postmortem.*` tools (ADR-0031)."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capture import CaptureMethod
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools.lifecycle.vmcore.handlers import VmcoreHandlers, list_vmcores
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.crash_commands import CRASH_COMMAND_ALLOWLIST
from kdive.security.secrets.secret_registry import SecretRegistry

_ALLOWED_CRASH_VERBS = ", ".join(sorted(CRASH_COMMAND_ALLOWLIST))


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `vmcore.*` / `postmortem.*` tools on ``app``, bound to ``pool``."""
    handlers = VmcoreHandlers(resolver=resolver, secret_registry=secret_registry)

    @app.tool(
        name="vmcore.fetch",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def vmcore_fetch(
        run_id: Annotated[str, Field(description="The crashed Run whose vmcore to capture.")],
        method: Annotated[
            CaptureMethod | None,
            Field(
                description=(
                    "Core-producing capture method (KDUMP/FADUMP/HOST_DUMP) the bound provider "
                    "must advertise. Omit to resolve the System profile's method; a profile with "
                    "no implicit core method requires an explicit one."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Capture and persist a vmcore from a crashed Run's bound System (contributor).

        Prerequisite: the Run's bound System must be in CRASHED state — induce a crash with
        ``control.force_crash`` (or capture a spontaneous panic) first; a non-CRASHED System is
        rejected with a configuration_error naming the current state. Async: this enqueues a
        ``capture_vmcore`` job and returns a job handle — poll it with ``jobs.wait`` / ``jobs.get``.
        On success the core lands as a redacted artifact; confirm it with ``vmcore.list``, then
        analyze it with ``postmortem.triage`` or ``postmortem.crash``. The capture ``method``
        resolves from the System profile when omitted; a kdump/fadump core also needs the guest
        kernel's crash symbols and a capable rootfs (gated before the job is admitted).
        """
        return await handlers.fetch_vmcore(
            pool,
            current_context(),
            run_id=run_id,
            method=method,
            idempotency_key=idempotency_key,
        )

    @app.tool(
        name="vmcore.list",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def vmcore_list(
        run_id: Annotated[
            str,
            Field(description="The Run whose redacted vmcore artifacts to list."),
        ],
    ) -> ToolResponse:
        """List the Run's redacted vmcore artifacts as one collection envelope.

        Read this after a ``vmcore.fetch`` job completes to confirm the captured core's artifact
        reference. Each item is a redacted artifact envelope; fetch bytes with ``artifacts.get`` or
        analyze the core with ``postmortem.triage`` / ``postmortem.crash``. Empty until a capture
        succeeds.
        """
        return await list_vmcores(pool, current_context(), run_id=run_id)

    @app.tool(
        name="postmortem.crash",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def postmortem_crash_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to analyze.")],
        commands: Annotated[
            list[str],
            Field(
                description=(
                    "crash(8) commands to run over the captured core. Each command's first token "
                    "must be one of the read-only allowlisted verbs: "
                    f"{_ALLOWED_CRASH_VERBS}. Shell metacharacters (| > < ` $( ; &), a leading '!' "
                    "shell escape, and control characters are rejected; a rejected command returns "
                    "a configuration_error whose detail names the offending command."
                )
            ),
        ],
    ) -> ToolResponse:
        """Run allowlisted crash(8) commands over a captured vmcore; returns a redacted report.

        Prerequisite: a captured core for the Run (see ``vmcore.fetch`` / ``vmcore.list``). For the
        default first-pass batch use ``postmortem.triage`` instead. Requires contributor: every
        command is validated against the crash allowlist before the core is opened, and the
        transcript is redacted before it is returned.
        """
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

    @app.tool(
        name="postmortem.triage",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def postmortem_triage_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to triage.")],
    ) -> ToolResponse:
        """Run the default first-pass crash(8) triage over a Run's captured vmcore (contributor).

        Runs the fixed triage batch (``log``, ``bt``) and returns a redacted report — the fast
        first look at a crash. Prerequisite: a captured core for the Run (see ``vmcore.fetch``, then
        ``vmcore.list`` to confirm it). For arbitrary allowlisted crash commands use
        ``postmortem.crash``; for programmable drgn introspection use ``introspect.from_vmcore``.
        """
        return await handlers.postmortem_triage(pool, current_context(), run_id=run_id)

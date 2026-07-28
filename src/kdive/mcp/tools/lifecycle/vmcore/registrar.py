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
from kdive.mcp.tools.lifecycle.vmcore.handlers import DEFAULT_CRASH_COMMANDS, VmcoreHandlers
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.crash_commands import CRASH_COMMAND_ALLOWLIST
from kdive.security.secrets.secret_registry import SecretRegistry

_ALLOWED_CRASH_VERBS = ", ".join(sorted(CRASH_COMMAND_ALLOWLIST))
_DEFAULT_CRASH_BATCH = ", ".join(DEFAULT_CRASH_COMMANDS)


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
        ``capture_vmcore`` job and returns a job handle — poll it with ``jobs.wait``.
        On success the core lands as a redacted artifact and the completed job carries its
        artifact id in ``refs.result``: read the bytes with ``artifacts.get`` or analyze the core
        with ``postmortem.crash``. ``runs.get`` carries the same id as ``refs.vmcore`` if you no
        longer hold the job id. The capture ``method``
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
        name="postmortem.crash",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def postmortem_crash_tool(
        run_id: Annotated[str, Field(description="The Run whose captured core to analyze.")],
        commands: Annotated[
            list[str] | None,
            Field(
                description=(
                    "crash(8) commands to run over the captured core. Omit to run the standard "
                    f"first-pass batch ({_DEFAULT_CRASH_BATCH}) — the fast first look at a crash. "
                    "Each command's first token must be one of the read-only allowlisted verbs: "
                    f"{_ALLOWED_CRASH_VERBS}. Shell metacharacters (| > < ` $( ; &), a leading '!' "
                    "shell escape, and control characters are rejected; a rejected command returns "
                    "a configuration_error whose detail names the offending command."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Run crash(8) over a captured vmcore; returns a redacted report (contributor).

        Omit ``commands`` for the standard first-pass batch — the fast first look at a crash —
        or pass your own allowlisted commands to go further. Prerequisite: a captured core for
        the Run (see ``vmcore.fetch``; its completed job's ``refs.result`` — or ``runs.get``'s
        ``refs.vmcore`` — confirms the core landed). Every command is
        validated against the crash allowlist before the core is opened, and the transcript is
        redacted before it is returned. For programmable drgn introspection use
        ``introspect.from_vmcore``.
        """
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

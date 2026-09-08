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
        run_id: Annotated[
            str,
            Field(
                description="The Run ID whose bound System is CRASHED; not a System or artifact ID."
            ),
        ],
        method: Annotated[
            CaptureMethod | None,
            Field(
                description=(
                    "Core-producing capture method (kdump/fadump/host_dump) the bound provider "
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
        """Capture a core from a Run's bound CRASHED System. Requires contributor.

        Pass the Run ID, not a System or artifact ID. Check systems.get first: a watch verdict
        or console signature does not mark the System CRASHED. A non-CRASHED System is refused.
        Do not force another crash just to change state; preserve existing console evidence.
        An active external boot requires the owning Run; other activation states can refuse
        capture. The chosen core-producing method must be supported by the provider.

        Omitting method resolves it from the System profile; a profile with no supported core
        method needs an explicit choice. Kdump/fadump admission rejects known-negative kernel
        and rootfs capability evidence; uncertainty can pass and is not proof of readiness.
        Returns a capture_vmcore job handle: poll jobs.wait and require terminal success.
        The same Run/method reuses its job, including terminal results; a new idempotency key
        does not force recapture. Follow the existing job's failure guidance.

        A fresh capture's completed job exposes the redacted artifact ID in refs.result;
        runs.get exposes it as refs.vmcore for non-failed Runs. For failed Runs, use the job
        reference. artifacts.get reads redacted log evidence (local-libvirt extracts dmesg
        text, not a sanitized binary core). A replay may omit the reference if the redacted
        sibling is gone while the raw core remains.
        For analysis, pass the Run ID to postmortem.crash, which resolves the raw core itself.
        For raw download use artifacts.fetch_raw(run_id, asset="vmcore") (contributor, URL-only).
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
                    f"first-pass batch ({_DEFAULT_CRASH_BATCH}). "
                    "Each command's first token must be one of the read-only allowlisted verbs: "
                    f"{_ALLOWED_CRASH_VERBS}. Shell metacharacters (| > < ` $( ; &), a leading '!' "
                    "shell escape, and control characters are rejected; a rejected command returns "
                    "a configuration_error whose detail names the offending command."
                )
            ),
        ] = None,
    ) -> ToolResponse:
        """Analyze a Run's captured core with server-side crash(8). Requires contributor.

        Pass the Run ID, not the artifact ID returned by vmcore.fetch. Prerequisites are the
        captured raw core, recorded vmlinux debug information, and the Run's build ID. The
        provider checks the core's build ID against that record before running commands.
        Analysis runs in the MCP server process; missing crash/provider dependencies in that
        environment produce a typed failure. Installing them only in the worker is insufficient.

        Omit commands for the standard first-pass batch, or pass allowlisted commands; each
        command is validated before opening the core. This call returns directly, not as a job:
        data.transcript contains redacted output and data.truncated marks byte-capped output.
        Inspect the transcript for per-command errors even on success; use a narrower batch
        if truncated. Missing inputs report data.reason no_vmcore, no_debuginfo, or no_build.
        A declared early-boot console_crash with no core instead reports expected_console_crash
        and directs you to runs.get for console evidence. For drgn analysis of the same Run,
        use introspect.from_vmcore.
        """
        return await handlers.postmortem_crash(
            pool, current_context(), run_id=run_id, commands=commands
        )

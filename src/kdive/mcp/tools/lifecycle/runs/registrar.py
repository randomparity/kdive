"""Registrar for the `runs.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.build_hosts import list_all_hosts
from kdive.domain.capacity.state import RunState
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools.lifecycle.runs.bind import RunBindRequest as _RunBindRequest
from kdive.mcp.tools.lifecycle.runs.bind import bind_run as _bind_run
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run as _cancel_run
from kdive.mcp.tools.lifecycle.runs.complete_build import (
    CompleteBuildHandlers as _CompleteBuildHandlers,
)
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest as _RunCreateRequest,
)
from kdive.mcp.tools.lifecycle.runs.create import (
    RunReuseRequirementInput as _RunReuseRequirementInput,
)
from kdive.mcp.tools.lifecycle.runs.create import create_run as _create_run
from kdive.mcp.tools.lifecycle.runs.list import RunsListRequest as _RunsListRequest
from kdive.mcp.tools.lifecycle.runs.list import list_runs as _list_runs
from kdive.mcp.tools.lifecycle.runs.profile_examples import (
    build_host_profile_examples as _build_host_profile_examples,
)
from kdive.mcp.tools.lifecycle.runs.server_build import BuildRunHandlers as _BuildRunHandlers
from kdive.mcp.tools.lifecycle.runs.steps import boot_run as _boot_run
from kdive.mcp.tools.lifecycle.runs.steps import install_run as _install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.profiles.build import (
    ExternalBuildProfile,
    ServerBuildProfile,
    dump_build_profile,
)
from kdive.profiles.types import ExpectedBootFailureInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.rbac import Role
from kdive.services.runs.build_host_selection import declared_remote_instance_names


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""
    _register_runs_get(app, pool, resolver)
    _register_runs_list(app, pool)
    _register_runs_create(app, pool, resolver)
    _register_runs_bind(app, pool)
    _register_runs_cancel(app, pool)
    _register_runs_build(app, pool, resolver)
    _register_runs_complete_build(app, pool, resolver)
    _register_runs_install(app, pool)
    _register_runs_boot(app, pool)
    _register_runs_profile_examples(app, pool)


def _build_handlers(runtime: ProviderRuntime) -> _BuildRunHandlers:
    return _BuildRunHandlers(
        runtime.component_sources,
        config_validator=runtime.build_config_validator,
    )


def _complete_build_handlers() -> _CompleteBuildHandlers:
    return _CompleteBuildHandlers()


def _register_runs_get(app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver) -> None:
    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Return one run; `succeeded` means build done. `data.steps` has install/boot status.

        `data.required_cmdline` is the platform-required boot args; append extra kernel debug
        args (e.g. `dhash_entries=1`) via `runs.build.cmdline` (bound on the Run's first build).
        """
        return await _get_run(pool, current_context(), run_id, resolver=resolver)


def _register_runs_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_list(
        system_id: Annotated[
            str | None, Field(description="Only Runs bound to this System id.")
        ] = None,
        investigation_id: Annotated[
            str | None, Field(description="Only Runs under this Investigation id.")
        ] = None,
        state: Annotated[
            RunState | None, Field(description="Only Runs in this build-phase state.")
        ] = None,
        limit: Annotated[
            int, Field(description="Maximum rows returned (capped at 200).")
        ] = _DEFAULT_LIST_LIMIT,
        cursor: Annotated[
            str | None,
            Field(description="Opaque continuation cursor from a prior page's next_cursor."),
        ] = None,
    ) -> ToolResponse:
        """List the caller's Runs, filterable by system/investigation/state. Requires viewer.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        request = _RunsListRequest(
            system_id=system_id,
            investigation_id=investigation_id,
            state=state,
            limit=limit,
            cursor=cursor,
        )
        return await _list_runs(pool, current_context(), request)


def _register_runs_create(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        build_profile: Annotated[
            ServerBuildProfile | ExternalBuildProfile,
            Field(
                description=(
                    "Build profile for the Run's kernel. source='server' builds from a kernel "
                    "tree (kernel_source_ref required); source='external' ingests a prebuilt "
                    "artifact. The optional 'config' is a catalog ComponentRef "
                    "(e.g. {'kind':'catalog','provider':'system','name':'kdump'}); OMIT it to get "
                    "the seeded kdump fragment (KEXEC, CRASH_DUMP, DEBUG_INFO_DWARF5, GDB_SCRIPTS) "
                    "for a kdump+debuginfo kernel. Call buildconfig.get to inspect a named "
                    "fragment. Extra kernel cmdline args (e.g. 'dhash_entries=1') are not set "
                    "here: append them via runs.build.cmdline (bound on the first build). "
                    "See resource://kdive/docs/operating/build-source-staging.md for "
                    "staging the source, or resource://kdive/docs/operating/"
                    "external-build-upload.md for shaping a source='external' upload."
                )
            ),
        ],
        system_id: Annotated[
            str | None,
            Field(
                description="Ready System (active Allocation) to bind now. OMIT to create an "
                "unbound Run that builds against 'target_kind' and is attached to a System "
                "later via runs.bind — this avoids holding target capacity to attempt a build."
            ),
        ] = None,
        target_kind: Annotated[
            str | None,
            Field(
                description="Resource kind the Run builds for (e.g. 'local-libvirt'). REQUIRED "
                "when system_id is omitted; discover valid values from a runs.create error's "
                "'available_target_kinds'. When system_id is set it is derived from the System, "
                "and an explicit mismatched value is rejected."
            ),
        ] = None,
        expected_boot_failure: Annotated[
            ExpectedBootFailureInput | None,
            Field(
                description=(
                    "Optional declared boot crash, e.g. "
                    "{'kind':'console_crash','pattern':'Unable to handle kernel'}. The pattern is "
                    "matched as a case-sensitive literal substring (NOT a regex), tested "
                    "line-by-line against the redacted console log; a single line containing the "
                    "substring is a match. Use '|' to OR alternatives (e.g. "
                    "'Oops|Unable to handle kernel') — up to 16 terms, 256 characters total, each "
                    "term non-empty. A match makes the expected crash the Run's success outcome."
                )
            ),
        ] = None,
        reuse_requirement: Annotated[
            _RunReuseRequirementInput | None,
            Field(
                description=(
                    "Optional System reuse assertion payload with vcpus, memory_gb, "
                    "disk_gb, and pcie fields. Omit to skip extra reuse matching."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Create a run, bound to a system or unbound against a target_kind."""
        request = _RunCreateRequest(
            investigation_id=investigation_id,
            system_id=system_id,
            target_kind=target_kind,
            build_profile=dump_build_profile(build_profile),
            expected_boot_failure=expected_boot_failure,
            reuse_requirement=reuse_requirement,
        )
        return await _create_run(
            pool,
            current_context(),
            request,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )


def _register_runs_bind(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.bind",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_bind(
        run_id: Annotated[str, Field(description="The unbound Run to attach a System to.")],
        system_id: Annotated[
            str,
            Field(
                description="Ready System (active Allocation) to bind. Its resource kind must "
                "equal the Run's target_kind; discover ready systems with systems.list and read "
                "each one's 'kind'."
            ),
        ],
        reuse_requirement: Annotated[
            _RunReuseRequirementInput | None,
            Field(
                description="Optional System reuse assertion payload with vcpus, memory_gb, "
                "disk_gb, and pcie fields. Omit to skip extra reuse matching."
            ),
        ] = None,
    ) -> ToolResponse:
        """Attach a ready system to an unbound run before install."""
        request = _RunBindRequest(
            run_id=run_id,
            system_id=system_id,
            reuse_requirement=reuse_requirement,
        )
        return await _bind_run(pool, current_context(), request)


def _register_runs_cancel(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.cancel",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_cancel(
        run_id: Annotated[str, Field(description="The non-terminal Run to cancel.")],
    ) -> ToolResponse:
        """Cancel a non-terminal run, freeing its system without a teardown."""
        return await _cancel_run(pool, current_context(), run_id)


def _register_runs_build(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.build",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Enqueues a kernel build the worker runs against the provider's build host; "
                "the toolchain build path is exercised only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run produces a real kernel artifact "
                "the build ledger records."
            ),
            providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
        ),
    )
    async def runs_build(
        run_id: Annotated[str, Field(description="The Run to build.")],
        cmdline: Annotated[
            str | None,
            Field(
                description="Kernel debug args appended to the platform-required boot args "
                "(e.g. 'dhash_entries=1'). Omit for no extra debug args. Bound on the first "
                "build of a Run."
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Enqueue a kernel build for a run."""
        ctx = current_context()
        return await with_runtime_for_run(
            pool,
            resolver,
            ctx,
            run_id,
            lambda runtime: _build_handlers(runtime).build_run(
                pool,
                ctx,
                run_id,
                cmdline=cmdline,
                idempotency_key=idempotency_key,
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_runs_complete_build(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.complete_build",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_complete_build(
        run_id: Annotated[str, Field(description="The external-build Run to finalize.")],
        cmdline: Annotated[
            str,
            Field(
                description="Kernel debug args appended to the platform-required boot args "
                "(e.g. 'dhash_entries=1'). Recorded in the build ledger and applied at boot "
                "via runs.install/runs.boot (ADR-0061)."
            ),
        ],
        build_id: Annotated[
            str | None,
            Field(
                description="GNU build-id as hex (e.g. from `readelf -n vmlinux`); required iff "
                "a vmlinux was uploaded. Case-insensitive."
            ),
        ] = None,
    ) -> ToolResponse:
        """Complete an externally built run."""
        ctx = current_context()
        return await with_runtime_for_run(
            pool,
            resolver,
            ctx,
            run_id,
            lambda _runtime: _complete_build_handlers().complete_build(
                pool, ctx, run_id, build_id=build_id, cmdline=cmdline
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_runs_install(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Installs a built kernel onto its System via the provider; the install path "
                "is exercised only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run installs a real kernel and the "
                "subsequent boot observes it."
            ),
            providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
        ),
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Install a built run onto its system."""
        return await _install_run(pool, current_context(), run_id, idempotency_key=idempotency_key)


def _register_runs_boot(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta(
            "partial",
            reason=_docmeta.MaturityReason.LIVE_DEPENDENCY,
            detail=(
                "Boots an installed kernel via the provider and waits for readiness; the boot "
                "path is exercised only under the gated live markers."
            ),
            promotion=(
                "A non-gated test or recorded live_stack run boots a real kernel and asserts "
                "the booted kernel identity."
            ),
            providers="local-libvirt: wired; remote-libvirt: wired; fault-inject: n/a.",
        ),
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Boot an installed run.

        The kernel cmdline is fixed at build time; append extra debug args (e.g.
        `dhash_entries=1`) via `runs.build.cmdline` (bound on the Run's first build), not here.
        """
        return await _boot_run(pool, current_context(), run_id, idempotency_key=idempotency_key)


def _register_runs_profile_examples(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.profile_examples",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_profile_examples() -> ToolResponse:
        """Return a ready-to-edit build profile per registered build host. Requires a token."""
        # Auth-only (ADR-0117): the verifier already gated the transport; enforce token
        # presence as defence-in-depth. No platform/project gate, no audit — the projection
        # is the public host-kind/source-kind rule only (ADR-0160).
        current_context()
        declared = declared_remote_instance_names()
        async with pool.connection() as conn:
            hosts = await list_all_hosts(conn)
        return _build_host_profile_examples(hosts, declared)

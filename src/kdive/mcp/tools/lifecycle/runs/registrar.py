"""Registrar for the `runs.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.db.build_hosts import list_all_hosts
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run
from kdive.mcp.tools.lifecycle.runs.build import RunBuildHandlers as _RunBuildHandlers
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run as _cancel_run
from kdive.mcp.tools.lifecycle.runs.create import (
    RunCreateRequest as _RunCreateRequest,
)
from kdive.mcp.tools.lifecycle.runs.create import (
    RunReuseRequirementInput as _RunReuseRequirementInput,
)
from kdive.mcp.tools.lifecycle.runs.create import create_run as _create_run
from kdive.mcp.tools.lifecycle.runs.profile_examples import (
    build_host_profile_examples as _build_host_profile_examples,
)
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


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""
    _register_runs_get(app, pool, resolver)
    _register_runs_create(app, pool)
    _register_runs_cancel(app, pool)
    _register_runs_build(app, pool, resolver)
    _register_runs_complete_build(app, pool, resolver)
    _register_runs_install(app, pool)
    _register_runs_boot(app, pool)
    _register_runs_profile_examples(app, pool)


def _build_handlers(runtime: ProviderRuntime) -> _RunBuildHandlers:
    return _RunBuildHandlers(
        runtime.component_sources,
        config_validator=runtime.build_config_validator,
    )


def _register_runs_get(app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver) -> None:
    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
    ) -> ToolResponse:
        """Return one run."""
        return await _get_run(pool, current_context(), run_id, resolver=resolver)


def _register_runs_create(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        investigation_id: Annotated[str, Field(description="Investigation to attach the Run to.")],
        system_id: Annotated[str, Field(description="Ready System (active Allocation) to bind.")],
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
                    "fragment. See docs/operating/build-source-staging.md for staging the source."
                )
            ),
        ],
        expected_boot_failure: Annotated[
            ExpectedBootFailureInput | None,
            Field(
                description=(
                    "Optional expected boot failure, e.g. "
                    "{'kind':'console_crash','pattern':'Oops'}."
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
    ) -> ToolResponse:
        """Create a run under a system."""
        request = _RunCreateRequest(
            investigation_id=investigation_id,
            system_id=system_id,
            build_profile=dump_build_profile(build_profile),
            expected_boot_failure=expected_boot_failure,
            reuse_requirement=reuse_requirement,
        )
        return await _create_run(pool, current_context(), request)


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
        meta={"maturity": "partial"},
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
    ) -> ToolResponse:
        """Enqueue a kernel build for a run."""
        return await with_runtime_for_run(
            pool,
            resolver,
            run_id,
            lambda runtime: _build_handlers(runtime).build_run(
                pool,
                current_context(),
                run_id,
                cmdline=cmdline,
            ),
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
        return await with_runtime_for_run(
            pool,
            resolver,
            run_id,
            lambda runtime: _build_handlers(runtime).complete_build(
                pool, current_context(), run_id, build_id=build_id, cmdline=cmdline
            ),
        )


def _register_runs_install(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
    ) -> ToolResponse:
        """Install a built run onto its system."""
        return await _install_run(pool, current_context(), run_id)


def _register_runs_boot(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
    ) -> ToolResponse:
        """Boot an installed run."""
        return await _boot_run(pool, current_context(), run_id)


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
        # is the public host-kind/source-kind rule only (ADR-0159).
        current_context()
        async with pool.connection() as conn:
            hosts = await list_all_hosts(conn)
        return _build_host_profile_examples(hosts)

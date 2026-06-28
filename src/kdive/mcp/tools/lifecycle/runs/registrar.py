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
from kdive.mcp.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run_target_kind
from kdive.mcp.tools.lifecycle.runs.bind import RunBindRequest as _RunBindRequest
from kdive.mcp.tools.lifecycle.runs.bind import bind_run as _bind_run
from kdive.mcp.tools.lifecycle.runs.cancel import cancel_run as _cancel_run
from kdive.mcp.tools.lifecycle.runs.complete_build import (
    CompleteBuildHandlers as _CompleteBuildHandlers,
)
from kdive.mcp.tools.lifecycle.runs.composite import CompositeRunHandlers as _CompositeRunHandlers
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
from kdive.mcp.tools.lifecycle.runs.validate_profile import (
    validate_build_profile as _validate_build_profile,
)
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.profiles.build import (
    ExternalBuildProfile,
    ServerBuildProfile,
    dump_build_profile,
)
from kdive.profiles.types import BuildProfileInput, ExpectedBootFailureInput
from kdive.providers.assembly.build_hosts import declared_remote_instance_names
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.rbac import Role


class _RunsCreatePayload(ToolPayload):
    """Public payload for ``runs.create``."""

    investigation_id: str = Field(description="Investigation to attach the Run to.")
    build_profile: ExternalBuildProfile | ServerBuildProfile = Field(
        description=(
            "Build profile for the Run's kernel. The recommended default is source='external': "
            "ingest a prebuilt artifact (ADR-0234). After runs.create with source='external', "
            "call artifacts.expected_uploads to learn the exact bytes to produce, "
            "artifacts.create_run_upload to upload, then runs.complete_build. source='server' "
            "builds from a kernel tree (kernel_source_ref required) and is a single-host "
            "convenience: for a local build host a warm-tree kernel_source_ref is a provenance "
            "label only - it does not select the tree; the operator stages the actual source "
            "via KDIVE_KERNEL_SRC on the worker. That lane builds the worker's working-tree "
            "state, not HEAD: runs.get reports data.build_provenance.{label, resolved_commit "
            "(the HEAD the tree is based on, decorative when dirty), dirty (bool), tree_sha "
            "(content digest of tracked changes, only when dirty)} - tracked git state only. "
            "The optional 'config' is a catalog ComponentRef "
            "(e.g. {'kind':'catalog','provider':'system','name':'kdump'}); omit it to get the "
            "seeded kdump fragment (KEXEC, CRASH_DUMP, DEBUG_INFO_DWARF5, GDB_SCRIPTS) for a "
            "kdump+debuginfo kernel. Call buildconfig.get to inspect a named fragment. Extra "
            "kernel cmdline args (e.g. 'dhash_entries=1') are not set here: pass the cmdline "
            "parameter to runs.build for server builds, or to runs.complete_build for external "
            "builds. See "
            "resource://kdive/docs/operating/external-build-upload.md for shaping a "
            "source='external' upload, or resource://kdive/docs/operating/build-source-staging.md "
            "for staging a server-build source."
        )
    )
    system_id: str | None = Field(
        default=None,
        description=(
            "Ready System to bind now. Omit to create an unbound Run that targets "
            "`target_kind` and is bound later with runs.bind."
        ),
    )
    target_kind: str | None = Field(
        default=None,
        description=(
            "Resource kind the Run builds for. Required when system_id is omitted; derived "
            "from the System when system_id is set."
        ),
    )
    expected_boot_failure: ExpectedBootFailureInput | None = Field(
        default=None,
        description=(
            "Optional declared boot crash. Use a named preset for a maintained, version- and "
            "arch-robust signature: {'kind':'panic'}, {'kind':'oops'}, or {'kind':'hung_task'} - "
            "a preset takes no 'pattern' and expands to a canonical kernel console signature. "
            "For a custom signature use {'kind':'console_crash','pattern':'Unable to handle "
            "kernel'}; a preset and a custom 'pattern' are mutually exclusive. The pattern is "
            "matched as a case-sensitive literal substring (not a regex), tested line-by-line "
            "against the redacted console log; a single line containing the substring is a match. "
            "Use '|' to OR alternatives (e.g. 'Oops|Unable to handle kernel') - up to 16 terms, "
            "256 characters total, each term non-empty. A match makes the expected crash the Run's "
            "success outcome."
        ),
    )
    reuse_requirement: _RunReuseRequirementInput | None = Field(
        default=None,
        description="Optional System reuse assertion payload.",
    )
    idempotency_key: str | None = Field(
        default=None,
        description="Replay-safe key; a repeated key returns the prior envelope.",
    )
    label: str | None = Field(
        default=None,
        description=(
            "Optional human handle for this Run, echoed back as data.label in runs.get / "
            "runs.list so you thread fewer bare UUIDs. Freeform and non-unique: 1..200 "
            "printable characters (surrounding whitespace trimmed); not a lookup key. Omit "
            "for no handle."
        ),
    )

    def to_create_request(self) -> _RunCreateRequest:
        """Convert the public MCP payload into the service request record."""
        return _RunCreateRequest(
            investigation_id=self.investigation_id,
            system_id=self.system_id,
            target_kind=self.target_kind,
            build_profile=dump_build_profile(self.build_profile),
            expected_boot_failure=self.expected_boot_failure,
            reuse_requirement=self.reuse_requirement,
            label=self.label,
        )


class _RunsListPayload(ToolPayload):
    """Public payload for ``runs.list`` filters and pagination."""

    system_id: str | None = Field(default=None, description="Only Runs bound to this System id.")
    investigation_id: str | None = Field(
        default=None, description="Only Runs under this Investigation id."
    )
    state: RunState | None = Field(default=None, description="Only Runs in this build-phase state.")
    limit: int = Field(
        default=_DEFAULT_LIST_LIMIT, description="Maximum rows returned (capped at 200)."
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )

    def to_list_request(self) -> _RunsListRequest:
        """Convert the public MCP payload into the handler request record."""
        return _RunsListRequest(
            system_id=self.system_id,
            investigation_id=self.investigation_id,
            state=self.state.value if self.state is not None else None,
            limit=self.limit,
            cursor=self.cursor,
        )


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
    _register_runs_build_install_boot(app, pool, resolver)
    _register_runs_complete_build(app, pool, resolver)
    _register_runs_install(app, pool)
    _register_runs_boot(app, pool)
    _register_runs_profile_examples(app, pool)
    _register_runs_validate_profile(app, pool)


def _build_handlers(runtime: ProviderRuntime) -> _BuildRunHandlers:
    return _BuildRunHandlers(
        runtime.component_sources,
        config_validator=runtime.build_config_validator,
    )


def _composite_handlers(runtime: ProviderRuntime) -> _CompositeRunHandlers:
    return _CompositeRunHandlers(
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
        args (e.g. `dhash_entries=1`) with the `cmdline` parameter on `runs.build` for server
        builds, or `runs.complete_build` for external builds.
        """
        return await _get_run(pool, current_context(), run_id, resolver=resolver)


def _register_runs_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_list(
        request: Annotated[
            _RunsListPayload | None,
            Field(description="Runs list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List the caller's Runs, filterable by system/investigation/state. Requires viewer.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await _list_runs(
            pool,
            current_context(),
            (request or _RunsListPayload()).to_list_request(),
        )


def _register_runs_create(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.create",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def runs_create(
        request: Annotated[
            _RunsCreatePayload,
            Field(
                description=(
                    "Run creation request. After source='external', call "
                    "artifacts.expected_uploads and artifacts.create_run_upload, then "
                    "runs.complete_build. Extra kernel cmdline args are passed later as "
                    "`cmdline` on runs.build for server builds, or runs.complete_build for "
                    "external builds."
                )
            ),
        ],
    ) -> ToolResponse:
        """Create a run, bound to a system or unbound against a target_kind."""
        return await _create_run(
            pool,
            current_context(),
            request.to_create_request(),
            resolver=resolver,
            idempotency_key=request.idempotency_key,
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
        meta=_docmeta.maturity_meta("implemented"),
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
        return await with_runtime_for_run_target_kind(
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


def _register_runs_build_install_boot(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.build_install_boot",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def runs_build_install_boot(
        run_id: Annotated[
            str,
            Field(
                description=(
                    "A created, bound, not-yet-built Run to drive build->install->boot "
                    "as a single pollable job (ADR-0268, #866). The Run must use a "
                    "source='server' build profile. Poll the returned job with jobs.wait."
                )
            ),
        ],
        cmdline: Annotated[
            str | None,
            Field(
                description=(
                    "Kernel debug args appended to the platform-required boot args "
                    "(e.g. 'dhash_entries=1'). Bound at build time and applied through "
                    "install and boot. Omit for no extra debug args."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Build, install, and boot a bound Run as a single pollable job (ADR-0268).

        Performs build-host admission (same as runs.build) then enqueues one
        BUILD_INSTALL_BOOT job. Requires operator role — the composite includes install
        and boot, whose gate is operator. Poll the returned job handle with jobs.wait.
        """
        ctx = current_context()
        return await with_runtime_for_run_target_kind(
            pool,
            resolver,
            ctx,
            run_id,
            lambda runtime: _composite_handlers(runtime).build_install_boot(
                pool,
                ctx,
                run_id,
                cmdline=cmdline,
                idempotency_key=idempotency_key,
            ),
            required_role=Role.OPERATOR,
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
            str | None,
            Field(
                description="Kernel debug args appended to the platform-required boot args "
                "(e.g. 'dhash_entries=1'). Recorded in the build ledger and applied at boot "
                "via runs.install/runs.boot (ADR-0061)."
            ),
        ] = None,
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
        return await with_runtime_for_run_target_kind(
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
        meta=_docmeta.maturity_meta("implemented"),
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
        meta=_docmeta.maturity_meta("implemented"),
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
        `dhash_entries=1`) with the `cmdline` parameter on `runs.build` for server builds,
        or `runs.complete_build` for external builds; do not pass them here.
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


def _register_runs_validate_profile(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.validate_profile",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_validate_profile(
        build_profile: Annotated[
            BuildProfileInput,
            Field(
                description=(
                    "A build_profile document to check before runs.create, returning the typed "
                    "validation envelope WITHOUT inserting a Run or consuming capacity. It runs "
                    "the same checks runs.create runs: structural parse (source='server' vs "
                    "'external'; warm-tree string vs {'git':{'remote','ref'}} kernel_source_ref) "
                    "and build-host/source-kind compatibility for a registered build_host. A "
                    "'valid' verdict means the document parses and (for a registered named host) "
                    "is source-kind compatible — it does NOT guarantee the source tree exists, "
                    "the config resolves, the kernel builds, or capacity is free; those are "
                    "checked later at runs.build/runs.complete_build. An unregistered build_host "
                    "is not rejected (data.build_host_registered=false discloses the compat check "
                    "was skipped). Call runs.profile_examples for a ready-to-edit shape."
                )
            ),
        ],
    ) -> ToolResponse:
        """Validate a build profile without inserting a Run. Requires a token."""
        # Auth-only (ADR-0117/0160), as runs.profile_examples: the verifier already gated the
        # transport; enforce token presence as defence-in-depth. No platform/project gate, no
        # audit — the tool only validates caller-supplied input and reads the public build-host
        # projection runs.profile_examples already exposes.
        current_context()
        return await _validate_build_profile(pool, build_profile)

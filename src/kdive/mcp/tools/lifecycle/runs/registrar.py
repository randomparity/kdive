"""Registrar for the `runs.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import RunState
from kdive.domain.external_provenance import PROVENANCE_FIELD_MAX_LEN
from kdive.domain.labels import LABEL_MAX_LEN
from kdive.domain.platform.arch_traits import default_crashkernel_summary
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import MAX_LIST_LIMIT as _MAX_LIST_LIMIT
from kdive.mcp.tools._runtime_resolution import with_runtime_for_run_target_kind
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
from kdive.mcp.tools.lifecycle.runs.steps import boot_run as _boot_run
from kdive.mcp.tools.lifecycle.runs.steps import install_run as _install_run
from kdive.mcp.tools.lifecycle.runs.view import get_run as _get_run
from kdive.profiles.build import BuildProfile, dump_build_profile
from kdive.profiles.types import ExpectedBootFailureInput
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.artifacts.artifact_search import MAX_PATTERN_CHARS, MAX_TERMS
from kdive.security.authz.rbac import Role
from kdive.security.secrets.secret_registry import SecretRegistry


class _RunsCreatePayload(ToolPayload):
    """Public payload for ``runs.create``."""

    investigation_id: str = Field(description="Investigation to attach the Run to.")
    build_profile: BuildProfile = Field(
        description=(
            "Build profile for the Run's kernel: a thin document, e.g. {'schema_version': 1} or "
            "{'schema_version': 1, 'arch': 'ppc64le'}. 'arch' (default x86_64) is the target CPU "
            "architecture and selects the boot/vmlinuz upload payload format (bzImage for x86_64, "
            "ELF vmlinux for ppc64le). The kernel is built locally and uploaded, so no source tree "
            "or config is named here. After runs.create, call artifacts.expected_uploads to learn "
            "the exact bytes to produce and artifacts.feature_config_requirements to learn which "
            "CONFIG_* each debug feature needs, artifacts.create_run_upload to upload, then "
            "runs.complete_build (where you may also record the optional source_label/source_ref "
            "provenance of the tree you built from - an unverified client claim, surfaced in "
            "runs.get data.build_provenance). Extra kernel cmdline args (e.g. 'dhash_entries=1') "
            "are not set here: pass the request.cmdline field to runs.complete_build. See "
            "resource://kdive/docs/operating/external-build-upload.md for shaping an upload."
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
            f"Use '|' to OR alternatives (e.g. 'Oops|Unable to handle kernel') - up to {MAX_TERMS} "
            f"terms, {MAX_PATTERN_CHARS} characters total, each term non-empty. A match makes the "
            "expected crash the Run's "
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
            "runs.list so you thread fewer bare UUIDs. Freeform and non-unique: "
            f"1..{LABEL_MAX_LEN} printable characters (surrounding whitespace trimmed); not a "
            "lookup key. Omit for no handle."
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
        default=_DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {_MAX_LIST_LIMIT}).",
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


class _RunsCompleteBuildPayload(ToolPayload):
    """Public payload for ``runs.complete_build`` operation fields."""

    cmdline: str | None = Field(
        default=None,
        description=(
            "Kernel debug args appended to the platform-required boot args "
            "(e.g. 'dhash_entries=1'). Recorded in the build ledger and applied at boot "
            "via runs.install/runs.boot."
        ),
    )
    build_id: str | None = Field(
        default=None,
        description=(
            "GNU build-id as hex (e.g. from `readelf -n vmlinux`); required iff a vmlinux was "
            "uploaded. Case-insensitive."
        ),
    )
    source_label: str | None = Field(
        default=None,
        description=(
            "Optional unverified provenance: a freeform handle for the local source tree that "
            "produced these uploaded artifacts (e.g. 'my-fix worktree'). Recorded as a client "
            "claim in runs.get data.build_provenance with client_attested=true; kdive does not "
            "clone, resolve, or verify it. "
            f"1..{PROVENANCE_FIELD_MAX_LEN} printable characters; bound on the first completion. "
            "Omit if unknown."
        ),
    )
    source_ref: str | None = Field(
        default=None,
        description=(
            "Optional unverified provenance: the ref/commit you claim produced these artifacts "
            "(e.g. a git SHA or 'v6.9-rc1+patch'). Recorded as a client claim in runs.get "
            "data.build_provenance with client_attested=true; treated as an opaque label, never "
            f"fetched. 1..{PROVENANCE_FIELD_MAX_LEN} printable characters; bound on the first "
            "completion. Omit if unknown."
        ),
    )


def register(
    app: FastMCP,
    pool: AsyncConnectionPool,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    """Register the `runs.*` tools on ``app``, bound to ``pool``."""
    _register_runs_get(app, pool, resolver, secret_registry)
    _register_runs_list(app, pool)
    _register_runs_create(app, pool, resolver)
    _register_runs_bind(app, pool)
    _register_runs_cancel(app, pool)
    _register_runs_complete_build(app, pool, resolver)
    _register_runs_install(app, pool, resolver)
    _register_runs_boot(app, pool)


def _complete_build_handlers() -> _CompleteBuildHandlers:
    return _CompleteBuildHandlers()


def _register_runs_get(
    app: FastMCP,
    pool: AsyncConnectionPool,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
) -> None:
    @app.tool(
        name="runs.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def runs_get(
        run_id: Annotated[str, Field(description="The Run to render.")],
        include_console_artifacts: Annotated[
            bool,
            Field(
                description=(
                    "Inline the Run-scoped console manifest under `data.console_artifacts`. "
                    "Defaults false: a status read stays token-cheap and the boot console "
                    "snapshot is always at `refs.console`. Set true only when you need the full "
                    "correlated console listing (boot snapshot plus rotating parts)."
                )
            ),
        ] = False,
    ) -> ToolResponse:
        """Return one run; `succeeded` means build done. `data.steps` has install/boot status.

        `data.required_cmdline` is the platform-required boot args; append extra kernel debug
        args (e.g. `dhash_entries=1`) with the `request.cmdline` field on
        `runs.complete_build`.

        Console evidence: `refs.console` is the boot-window console snapshot and
        `data.console_access` names how to read it (`artifacts.get` windowed/paged, or
        `artifacts.find` for literal search) — both always present on a booted Run.
        `data.console_artifacts` is the Run-scoped console manifest and is **opt-in**: it appears
        only when you pass `include_console_artifacts=true`. When requested it is an ordered,
        newest-first list of `{artifact_id, object_key, created_at}` for every console artifact
        correlated to this Run (the boot snapshot plus the post-readiness rotating parts), read
        with `artifacts.get`. It is bounded: when more exist than the cap,
        `data.console_artifacts_total` is the full count and `data.console_artifacts_truncated` is
        true (the oldest entries are dropped; the boot console stays at `refs.console`). The key is
        absent when not requested or when the Run has no correlated console.

        Build provenance: `data.build_provenance` (present once the build succeeded with a
        caller-supplied claim, absent otherwise) records the agent's client-attested source. On
        the upload lane KDIVE never clones or verifies a source tree, so this is the caller's own
        freeform claim: `client_attested: true` with the `source_label`/`source_ref` passed to
        `runs.complete_build`. Compare it across runs to track which local source produced each
        build.

        Liveness: `data.liveness` tells a healthy guest from one that livelocked **after** a ready
        boot — a case `boot_outcome=ready` and `control.watch_for_crash` (which sees no crash
        signature) both miss. It appears only on a ready-booted local-libvirt Run, and is
        `{state, console_storm, ssh_reachable, checked_at}`: `state` is `healthy`, `degraded`, or
        `unknown`; `console_storm` is true when the current console shows a runaway printk /
        OOM-retry storm (e.g. `callbacks suppressed`, `VM_FAULT_OOM`, `soft lockup`);
        `ssh_reachable` is the latest `systems.check_ssh_reachable` verdict (`null` until you have
        probed once — call it to refresh) and `checked_at` is when that probe ran (`null` when
        unprobed). `state` is `degraded` when the console storms or SSH is unreachable, so treat
        `degraded` as a wedged guest even while `status=succeeded`.
        """
        return await _get_run(
            pool,
            current_context(),
            run_id,
            resolver=resolver,
            secret_registry=secret_registry,
            include_console_artifacts=include_console_artifacts,
        )


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
                    "Run creation request. After runs.create, call artifacts.expected_uploads and "
                    "artifacts.create_run_upload, then runs.complete_build. Extra kernel cmdline "
                    "args are passed later as `request.cmdline` on runs.complete_build."
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
        request: Annotated[
            _RunsCompleteBuildPayload | None,
            Field(description="Optional build finalization fields: cmdline, build_id, and source."),
        ] = None,
    ) -> ToolResponse:
        """Finalize an externally built Run: validate the uploaded artifacts, mark it succeeded.

        The `kernel` tar's boot/vmlinuz member is validated against the Run's build-profile arch
        (declared at runs.create): a bzImage for x86_64, an ELF vmlinux for ppc64le. A payload that
        does not match the declared arch is rejected. See artifacts.expected_uploads for the
        per-arch byte contract.
        """
        payload = request or _RunsCompleteBuildPayload()
        ctx = current_context()
        return await with_runtime_for_run_target_kind(
            pool,
            resolver,
            ctx,
            run_id,
            lambda _runtime: _complete_build_handlers().complete_build(
                pool,
                ctx,
                run_id,
                build_id=payload.build_id,
                cmdline=payload.cmdline,
                source_label=payload.source_label,
                source_ref=payload.source_ref,
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_runs_install(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="runs.install",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def runs_install(
        run_id: Annotated[str, Field(description="The Run whose built kernel to install.")],
        cmdline: Annotated[
            str | None,
            Field(
                description=(
                    "Kernel debug args applied against the already-built kernel — no rebuild "
                    "needed. Replaces any build-time extra args. These platform args are always "
                    "present and cannot be overridden: the platform serial console "
                    "(console=ttyS0 on x86, console=hvc0 on pseries), root=/dev/vda, plus "
                    f"crashkernel (kdump, per-arch default: {default_crashkernel_summary()}) or "
                    "nokaslr (gdbstub) per the System's capture "
                    "method. Passing a value different from the currently installed one re-stages "
                    "the boot; sweep boot-parameter variants (e.g. 'dhash_entries=1' then "
                    "'dhash_entries=2') by calling runs.install with a new value then runs.boot, "
                    "using a distinct (or no) idempotency_key each time. Omit to reuse the "
                    "build-time cmdline."
                )
            ),
        ] = None,
        crashkernel: Annotated[
            str | None,
            Field(
                description=(
                    "kdump crash-capture reservation size, replacing the platform per-arch "
                    f"default ({default_crashkernel_summary()}) in the crashkernel= token (e.g. "
                    "'1G' for a KASAN kernel or a large guest). Pass only the reservation "
                    "argument, not the whole token; a size or a kernel range is accepted. Applies "
                    "only to kdump-capture Systems — a value on a non-kdump System is rejected. "
                    "Each install fully specifies both cmdline and crashkernel: omitting either "
                    "reverts "
                    "that one to its default (cmdline to the build-time args, crashkernel to the "
                    "per-arch default), so on an already-installed Run, restate both to keep "
                    "them. The live value is reported by runs.get as data.installed_crashkernel."
                )
            ),
        ] = None,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Install a built run onto its system.

        Pass `cmdline` and/or `crashkernel` to iterate boot parameters against the built kernel
        without a rebuild; `runs.get` reports the live variant as `data.installed_cmdline` and
        `data.installed_crashkernel`.
        """
        return await _install_run(
            pool,
            current_context(),
            run_id,
            cmdline=cmdline,
            crashkernel=crashkernel,
            resolver=resolver,
            idempotency_key=idempotency_key,
        )


def _register_runs_boot(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="runs.boot",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def runs_boot(
        run_id: Annotated[str, Field(description="The Run whose installed kernel to boot.")],
        force: Annotated[
            bool,
            Field(
                description=(
                    "Re-boot an already-booted Run. By default runs.boot is idempotent: a repeat "
                    "call on a Run whose boot already succeeded returns the prior job unchanged "
                    "(data.replayed=true) and does NOT re-boot. Set force=true to recycle the boot "
                    "and run a fresh boot of the same installed variant without a re-stage — use "
                    "this to reboot a wedged guest. A force call that reuses a prior "
                    "idempotency_key replays the stored envelope instead of re-booting; pass a "
                    "distinct (or no) idempotency_key to force a boot. Rejected with "
                    "configuration_error (step_in_progress) while a boot is already running."
                )
            ),
        ] = False,
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Boot an installed run.

        To iterate boot parameters (e.g. `dhash_entries=1`), pass `cmdline` to `runs.install`
        against the built kernel — no rebuild — then boot here; `runs.boot` takes no cmdline. Extra
        args can also be bound at build via `runs.complete_build`.

        The response `data.replayed` is `true` when this call returned an existing job without
        enqueuing a fresh boot (an already-booted or in-flight Run), and `false` for a fresh or
        force-recycled boot. Absent `force`, a fresh boot of an already-booted Run needs a
        `runs.install` re-stage (a changed cmdline/crashkernel) or `force=true`.
        """
        return await _boot_run(
            pool, current_context(), run_id, force=force, idempotency_key=idempotency_key
        )

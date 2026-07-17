"""Registrar for the `systems.*` MCP tools."""

from __future__ import annotations

from typing import Annotated

from fastmcp import FastMCP
from psycopg_pool import AsyncConnectionPool
from pydantic import Field

from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError
from kdive.domain.labels import LABEL_MAX_LEN
from kdive.mcp.auth import current_context
from kdive.mcp.responses import ToolResponse
from kdive.mcp.schema.provider_schema import assert_kind_composed
from kdive.mcp.schema.tool_payloads import ToolPayload
from kdive.mcp.tools import _docmeta
from kdive.mcp.tools._common import DEFAULT_LIST_LIMIT as _DEFAULT_LIST_LIMIT
from kdive.mcp.tools._common import MAX_LIST_LIMIT as _MAX_LIST_LIMIT
from kdive.mcp.tools._runtime_resolution import with_runtime_for_allocation, with_runtime_for_system
from kdive.mcp.tools.lifecycle.systems.admin import (
    SystemAdminHandlers as _SystemAdminHandlers,
)
from kdive.mcp.tools.lifecycle.systems.admin import (
    teardown_system as _teardown_system,
)
from kdive.mcp.tools.lifecycle.systems.profile_examples import (
    build_profile_examples as _build_profile_examples,
)
from kdive.mcp.tools.lifecycle.systems.profile_examples import (
    load_inventory_for_examples as _load_inventory_for_examples,
)
from kdive.mcp.tools.lifecycle.systems.provision import (
    SystemProvisionHandlers as _SystemProvisionHandlers,
)
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    delete_snapshot as _delete_snapshot,
)
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    list_snapshots as _list_snapshots,
)
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    restore_system as _restore_system,
)
from kdive.mcp.tools.lifecycle.systems.snapshot import (
    snapshot_system as _snapshot_system,
)
from kdive.mcp.tools.lifecycle.systems.ssh_access import (
    authorize_ssh_key as _authorize_ssh_key,
)
from kdive.mcp.tools.lifecycle.systems.ssh_access import (
    check_ssh_reachable as _check_ssh_reachable,
)
from kdive.mcp.tools.lifecycle.systems.ssh_access import (
    ssh_info as _ssh_info,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    SystemsListRequest as _SystemsListRequest,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    get_system as _get_system,
)
from kdive.mcp.tools.lifecycle.systems.view import (
    list_systems as _list_systems,
)
from kdive.profiles.provisioning import ProvisioningProfile, dump_profile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.security.authz.rbac import Role

_LABEL_DESCRIPTION = (
    "Optional human handle for this System, echoed back as data.label in systems.get / "
    f"systems.list so you thread fewer bare UUIDs. Freeform and non-unique: 1..{LABEL_MAX_LEN} "
    "printable characters (surrounding whitespace trimmed); not a lookup key. Omit for no handle."
)


class _SystemsListPayload(ToolPayload):
    """Public payload for ``systems.list`` filters and pagination."""

    allocation_id: str | None = Field(
        default=None, description="Only Systems under this Allocation id."
    )
    state: SystemState | None = Field(
        default=None, description="Only Systems in this lifecycle state."
    )
    shape: str | None = Field(
        default=None,
        description="Only Systems with this named shape, or '__custom__' for full-custom.",
    )
    pcie: str | None = Field(
        default=None,
        description="Only Systems whose Allocation claims a matching '<vendor>:<device>' spec.",
    )
    limit: int = Field(
        default=_DEFAULT_LIST_LIMIT,
        description=f"Maximum rows returned (capped at {_MAX_LIST_LIMIT}).",
    )
    cursor: str | None = Field(
        default=None, description="Opaque continuation cursor from a prior page's next_cursor."
    )

    def to_list_request(self) -> _SystemsListRequest:
        """Convert the public MCP payload into the handler request record."""
        return _SystemsListRequest(
            allocation_id=self.allocation_id,
            state=self.state.value if self.state is not None else None,
            shape=self.shape,
            pcie=self.pcie,
            limit=self.limit,
            cursor=self.cursor,
        )


def register(app: FastMCP, pool: AsyncConnectionPool, *, resolver: ProviderResolver) -> None:
    """Register the `systems.*` tools on ``app``, bound to ``pool``."""
    _register_systems_define(app, pool, resolver)
    _register_systems_provision(app, pool, resolver)
    _register_systems_provision_defined(app, pool, resolver)
    _register_systems_get(app, pool, resolver)
    _register_systems_list(app, pool)
    _register_systems_profile_examples(app, resolver)
    _register_systems_teardown(app, pool)
    _register_systems_reprovision(app, pool, resolver)
    _register_systems_ssh_info(app, pool, resolver)
    _register_systems_check_ssh_reachable(app, pool, resolver)
    _register_systems_authorize_ssh_key(app, pool, resolver)
    _register_systems_snapshot(app, pool, resolver)
    _register_systems_restore(app, pool, resolver)
    _register_systems_list_snapshots(app, pool, resolver)
    _register_systems_delete_snapshot(app, pool, resolver)


def _rootfs_validator(runtime: ProviderRuntime):
    if runtime.rootfs is None or runtime.rootfs.validator is None:
        return lambda _rootfs: None
    return runtime.rootfs.validator


def _provision_handlers(runtime: ProviderRuntime) -> _SystemProvisionHandlers:
    return _SystemProvisionHandlers(
        runtime.profile_policy, runtime.support.component_sources, _rootfs_validator(runtime)
    )


def _admin_handlers(runtime: ProviderRuntime) -> _SystemAdminHandlers:
    return _SystemAdminHandlers(
        runtime.profile_policy, runtime.support.component_sources, _rootfs_validator(runtime)
    )


def _register_systems_define(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.define",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_define(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to create a DEFINED System for.")
        ],
        profile: Annotated[
            ProvisioningProfile,
            Field(
                description="Provisioning profile for the System; an 'upload' rootfs opens a "
                "pre-provision rootfs-upload window."
            ),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        label: Annotated[
            str | None,
            Field(description=_LABEL_DESCRIPTION),
        ] = None,
    ) -> ToolResponse:
        """Create a System in 'defined' for a granted Allocation, opening a pre-provision
        rootfs-upload window; follow with `systems.provision_defined` once the upload is done.
        Use `systems.provision` instead when the profile needs no upload window. Requires
        contributor on the Allocation's project.
        """
        ctx = current_context()
        try:
            assert_kind_composed(profile.provider.kind, resolver.registered_kinds())
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(allocation_id, exc)
        return await with_runtime_for_allocation(
            pool,
            resolver,
            ctx,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).define_system(
                pool,
                ctx,
                allocation_id=allocation_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
                label=label,
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_systems_provision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.provision",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_provision(
        allocation_id: Annotated[
            str, Field(description="Granted Allocation to provision a System for.")
        ],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="Provisioning profile for the System create lane."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
        label: Annotated[
            str | None,
            Field(description=_LABEL_DESCRIPTION),
        ] = None,
    ) -> ToolResponse:
        """Mint a System for a granted Allocation and enqueue provision directly (no upload
        window). Use `systems.define` then `systems.provision_defined` instead when the rootfs
        must be uploaded before provisioning. One System per Allocation: if this Allocation's
        System already failed, retrying does not mint a new one — release this Allocation and
        request a fresh one (`allocations.release`, then `allocations.request`) for a fresh
        System. Requires contributor on the Allocation's project.

        A profile whose `arch` the backing host cannot boot is rejected `configuration_error`
        at admission — before any capacity is committed — naming the arches the host supports;
        pick one of those or an allocation on a host that offers the arch you need.
        """
        ctx = current_context()
        try:
            assert_kind_composed(profile.provider.kind, resolver.registered_kinds())
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(allocation_id, exc)
        return await with_runtime_for_allocation(
            pool,
            resolver,
            ctx,
            allocation_id,
            lambda runtime: _provision_handlers(runtime).provision_system(
                pool,
                ctx,
                allocation_id=allocation_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
                label=label,
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_systems_provision_defined(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.provision_defined",
        annotations=_docmeta.mutating(),
        meta={"maturity": "implemented"},
    )
    async def systems_provision_defined(
        system_id: Annotated[
            str,
            Field(description="Defined System whose stored profile should be provisioned."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Admit a DEFINED System after its upload window is complete; not for a fresh System —
        create it with `systems.define` first (this is the second step of that lane).
        Requires contributor on the System's project.
        """
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _provision_handlers(runtime).provision_defined_system(
                pool,
                ctx,
                system_id=system_id,
                idempotency_key=idempotency_key,
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_systems_get(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.get",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_get(
        system_id: Annotated[str, Field(description="The System to render.")],
    ) -> ToolResponse:
        """Return a System the caller can view.

        ``data.accel`` is the host-derived accelerator resolved at admission — ``kvm`` (native)
        or ``tcg`` (foreign-arch emulation) — or ``null`` when the backing host advertised no
        guest-arch capability. Expect a ``tcg`` System to boot and run notably slower.

        ``data.resolved_cpu`` is the ``{model, vendor?, arch, baseline_level?}`` guest CPU the
        System actually booted with — **live-verified** for local Systems (read from the running
        domain; a host-passthrough guest resolves to the host CPU, a TCG machine-default the host
        does not expand reads ``null``), and the **mint-time snapshot** for remote Systems.
        ``null`` means unrecorded/unreadable — treat as unknown. ``baseline_level``
        (``x86-64-vN``) is a nominal upper bound (see ``resources.describe``), not a guaranteed
        floor — confirm a hard instruction-set requirement against the guest.

        ``data.supports_snapshots`` is whether the backing provider can checkpoint/restore this
        System (``systems.snapshot`` / ``systems.restore``); ``false`` for a provider with no
        snapshot support.
        """
        return await _get_system(pool, current_context(), system_id, resolver=resolver)


def _register_systems_ssh_info(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.ssh_info",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_ssh_info(
        system_id: Annotated[
            str, Field(description="The ready System to return SSH coordinates for.")
        ],
    ) -> ToolResponse:
        """Return SSH coordinates (user, host, port, jump_host, host_scope) for a ready System.

        Available on any ready System whose provider exposes an SSH forward: local-libvirt always,
        and remote-libvirt only when the host is configured for SSH parity. Reports
        ``ssh_not_provisioned`` when there is no forward. For a remote System the endpoint is read
        live from the host, so an unreachable host surfaces as a transport failure rather than a
        cached value.

        ``host_scope`` is a locality signal for ``host``/``port``. ``worker_loopback`` means the
        coordinates are the worker host's own loopback — reachable only from a caller co-located
        with the worker, or via a populated ``jump_host`` — so a remote agent must not dial its
        own ``127.0.0.1`` expecting to reach the guest. ``jump_host`` is ``null`` for
        ``worker_loopback`` today (single-host deployment; the agent is co-located with the
        worker). A future ``routable`` scope will populate ``jump_host`` as ``{host, port,
        user}`` for ``ssh -J <jump_host> ...``, without a contract change.
        """
        return await _ssh_info(pool, current_context(), system_id, resolver=resolver)


def _register_systems_check_ssh_reachable(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.check_ssh_reachable",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_check_ssh_reachable(
        system_id: Annotated[
            str, Field(description="The ready System whose guest sshd reachability to probe.")
        ],
    ) -> ToolResponse:
        """Probe whether a ready System's guest sshd is answering right now.

        Enqueues a worker job and returns a job handle; poll ``jobs.wait`` until it is
        ``succeeded``, then read the verdict from ``refs.result`` — a compact JSON object
        ``{"reachable": bool, "checked_at", "endpoint": {host, port}, "detail", "layer",
        "checks"}``. On ``reachable=false``, ``layer`` names the lowest failing probe layer
        (``tcp_connect`` — nothing accepted the connection; or ``ssh_banner`` — connected but no
        ``SSH-`` banner) and ``checks`` lists each layer's pass/fail up to that point; ``layer``
        is ``null`` when reachable. ``reachable=false`` is a normal answer (a successful
        measurement), not an error. Each call is a fresh point-in-time measurement (a new job), so
        re-poll rather than reuse an old result. The probe tolerates the brief window after
        ``ready`` before sshd binds, so a single ``false`` right after provisioning may become
        ``true`` on a repeat call. Available on any ready System whose provider exposes an SSH
        forward; reports ``ssh_not_provisioned`` otherwise.

        ``reachable`` confirms sshd is answering, not that your key is authorized — the probe
        sends no handshake and attempts no login. Call ``systems.authorize_ssh_key`` if a real
        SSH attempt is denied with ``Permission denied (publickey)``.
        """
        return await _check_ssh_reachable(pool, current_context(), system_id, resolver=resolver)


def _register_systems_authorize_ssh_key(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.authorize_ssh_key",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_authorize_ssh_key(
        system_id: Annotated[str, Field(description="The ready System to authorize the key on.")],
        public_key: Annotated[
            str,
            Field(description="The agent SSH public key to authorize in the guest root account."),
        ],
    ) -> ToolResponse:
        """Authorize an agent SSH public key in a ready System's guest root account.

        Enqueues a worker job and returns a job handle; poll ``jobs.wait`` until it is
        ``succeeded`` before connecting — the key is not installed, and SSH will not
        authenticate with it, until the job completes. Once the job succeeds, use
        ``systems.ssh_info`` for the connection coordinates.
        """
        return await _authorize_ssh_key(
            pool, current_context(), system_id, public_key, resolver=resolver
        )


def _register_systems_list(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.list",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_list(
        request: Annotated[
            _SystemsListPayload | None,
            Field(description="Systems list filters and pagination request."),
        ] = None,
    ) -> ToolResponse:
        """List the caller's Systems, filterable by allocation/state/shape/PCIe. Requires viewer.

        Keyset-paginated: when ``data.truncated`` is true, pass ``data.next_cursor`` back as
        ``cursor`` for the next page.
        """
        return await _list_systems(
            pool,
            current_context(),
            (request or _SystemsListPayload()).to_list_request(),
        )


def _register_systems_profile_examples(app: FastMCP, resolver: ProviderResolver) -> None:
    @app.tool(
        name="systems.profile_examples",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def systems_profile_examples() -> ToolResponse:
        """Return a ready-to-edit example profile per configured provider. Requires a token."""
        # Auth-only (ADR-0117): the verifier already gated the transport; enforce token presence as
        # defence-in-depth. No platform/project gate, no audit — the projection is non-sensitive
        # inventory identifiers only (ADR-0124).
        current_context()
        return _build_profile_examples(_load_inventory_for_examples(), resolver.registered_kinds())


def _register_systems_teardown(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Enqueue teardown for a System. Requires admin on the System's project."""
        return await _teardown_system(
            pool, current_context(), system_id, idempotency_key=idempotency_key
        )


def _register_systems_reprovision(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.reprovision",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_reprovision(
        system_id: Annotated[str, Field(description="The ready System to reprovision in place.")],
        profile: Annotated[
            ProvisioningProfile,
            Field(description="New provisioning profile to re-stage on the READY System."),
        ],
        idempotency_key: Annotated[
            str | None,
            Field(description="Replay-safe key; a repeated key returns the prior envelope."),
        ] = None,
    ) -> ToolResponse:
        """Enqueue in-place reprovision for a ready System; not for creating a new System —
        use `systems.provision` instead. Requires contributor on the System's project (no
        destructive_ops opt-in).
        """
        ctx = current_context()
        try:
            assert_kind_composed(profile.provider.kind, resolver.registered_kinds())
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(system_id, exc)
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _admin_handlers(runtime).reprovision_system(
                pool,
                ctx,
                system_id=system_id,
                profile=dump_profile(profile),
                idempotency_key=idempotency_key,
            ),
            required_role=Role.CONTRIBUTOR,
        )


_SNAPSHOT_NAME_DESCRIPTION = (
    "Checkpoint label, unique per System: 1..64 characters of letters, digits, '.', '_', '-'. "
    "Reuse is guarded — capturing over an existing available checkpoint is rejected "
    "(systems.delete_snapshot it first); a failed one is auto-reclaimed."
)


def _register_systems_snapshot(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.snapshot",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_snapshot(
        system_id: Annotated[str, Field(description="The READY System to checkpoint.")],
        name: Annotated[str, Field(description=_SNAPSHOT_NAME_DESCRIPTION)],
        include_memory: Annotated[
            bool,
            Field(
                description="Capture live RAM+CPU as well as disk (default true), so "
                "systems.restore can resume the guest exactly where it was — including a paused "
                "restore for a debugger attach. Set false for a disk-only checkpoint (smaller, "
                "faster); restoring a disk-only checkpoint rolls back the filesystem and reboots "
                "the guest, and cannot be pause-restored."
            ),
        ] = True,
    ) -> ToolResponse:
        """Checkpoint a READY System's disk (and, by default, live RAM+CPU) so you can roll it
        back later with `systems.restore` — capture a fully-configured guest once, then restore in
        seconds between reproducer attempts instead of reprovisioning. Requires contributor.
        Allowed during a live run (snapshotting mid-debug is the point); a RAM capture briefly
        pauses the guest while its memory is written, so an in-flight SSH stalls then resumes.
        Enqueues a job and returns `{job_id, status: queued}` — poll `jobs.wait` until it is
        `succeeded`, then the checkpoint is listed by `systems.list_snapshots`. The System stays
        READY throughout."""
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _snapshot_system(
                pool, ctx, runtime, system_id=system_id, name=name, include_memory=include_memory
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_systems_restore(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.restore",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_restore(
        system_id: Annotated[str, Field(description="The READY System to roll back.")],
        name: Annotated[
            str, Field(description="An existing available checkpoint from systems.list_snapshots.")
        ],
        start_paused: Annotated[
            bool,
            Field(
                description="Leave the guest suspended after the revert instead of running it, so "
                "you can attach a gdbstub `debug.start_session` and set breakpoints before "
                'execution resumes; resume with `control.power(action="resume")`. Requires a '
                "RAM+CPU (include_memory) checkpoint — rejected against a disk-only one. Default "
                "false runs the guest immediately."
            ),
        ] = False,
    ) -> ToolResponse:
        """Roll a READY System back to a named checkpoint. Requires contributor. Refused while a
        run holds the System, while a snapshot capture/restore/delete is in progress, or while a
        debug session is attached (end it first, then attach a fresh session after the restore) —
        each would be corrupted by the revert. A RAM+CPU checkpoint resumes the guest exactly
        where it was; a disk-only checkpoint rolls back the filesystem and reboots (so
        `start_paused` is rejected for it). Enqueues a job and returns `{job_id, status: queued}` —
        poll `jobs.wait`. With `start_paused=true` the System lands PAUSED for a gdbstub attach
        (drgn-live over SSH does not work on a paused guest; use `debug.*`); resume it with
        `control.power(action="resume")`."""
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _restore_system(
                pool, ctx, runtime, system_id=system_id, name=name, start_paused=start_paused
            ),
            required_role=Role.CONTRIBUTOR,
        )


def _register_systems_list_snapshots(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.list_snapshots",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_list_snapshots(
        system_id: Annotated[str, Field(description="The System whose checkpoints to list.")],
    ) -> ToolResponse:
        """List a System's checkpoints newest first — each item carries `name`, `state`
        (`creating`/`available`/`failed`), `include_memory`, and `created_at`. Requires viewer.
        Only an `available` checkpoint can be restored. Returns an empty collection for a System
        with none."""
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _list_snapshots(pool, ctx, runtime, system_id=system_id),
            required_role=Role.VIEWER,
        )


def _register_systems_delete_snapshot(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.delete_snapshot",
        annotations=_docmeta.mutating(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_delete_snapshot(
        system_id: Annotated[str, Field(description="The System whose checkpoint to delete.")],
        name: Annotated[str, Field(description="The checkpoint to delete, freeing its name+disk.")],
    ) -> ToolResponse:
        """Delete a System's checkpoint, freeing its name for reuse and reclaiming its disk before
        teardown. Requires contributor. Refused for a `creating` checkpoint (cancel the capture
        first) or while the System is being restored. Enqueues a job and returns
        `{job_id, status: queued}` — poll `jobs.wait`; freeing a large RAM checkpoint takes time,
        so this is a background op."""
        ctx = current_context()
        return await with_runtime_for_system(
            pool,
            resolver,
            ctx,
            system_id,
            lambda runtime: _delete_snapshot(pool, ctx, runtime, system_id=system_id, name=name),
            required_role=Role.CONTRIBUTOR,
        )

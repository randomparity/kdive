"""Worker handlers for the `systems.*` plane."""

from __future__ import annotations

import asyncio
import functools
import logging
import shutil
from collections.abc import Awaitable, Callable
from typing import LiteralString, Protocol
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts import upload_manifest
from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import StoredArtifact
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import (
    ARTIFACTS,
    SNAPSHOTS,
    SYSTEMS,
    ObjectNotFound,
    delete_snapshots_for_system,
    snapshot_by_name,
)
from kdive.domain.capacity.state import IllegalTransition, SnapshotState, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.lifecycle.rules import TERMINAL_SYSTEM_STATES
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.connectivity.ssh_authorize import authorize_ssh_key_handler
from kdive.jobs.handlers.connectivity.ssh_reachable import check_ssh_reachable_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import (
    ReprovisionPayload,
    RestorePayload,
    SnapshotDeletePayload,
    SnapshotPayload,
    SystemPayload,
    load_payload,
)
from kdive.jobs.provider_context import set_provider_kind
from kdive.prereqs.system_bootstrap_key import (
    delete_system_bootstrap_key,
    ensure_system_bootstrap_key,
)
from kdive.profiles.provider_policy import ProfilePolicy, rootfs_upload_window_allowed
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.providers.console_parts.sidecar import sidecar_object_name
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.lifecycle import Snapshotter
from kdive.providers.shared.runtime_paths import domain_name_for, pcap_dir
from kdive.security import audit
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore, artifact_key, object_store_from_env

_log = logging.getLogger(__name__)

# The local-libvirt console-rotation parts and sidecar (#892) live under this tenant, matching the
# rotation handler (``console_rotate.py`` ``_TENANT``) so teardown reclaims the same owner prefix.
_CONSOLE_TENANT = "local"

# Matches local console part objects ``console-part-<gen>-<index>`` only; the remote collector's
# ``console-parts-<n>`` keys lack the trailing hyphen after ``part`` and are intentionally excluded.
_CONSOLE_PART_LIKE: LiteralString = "%console-part-%"

_DELETE_PART_ROWS_SQL: LiteralString = (
    "DELETE FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s AND object_key LIKE %s"
)

_SELECT_PART_KEYS_SQL: LiteralString = (
    "SELECT object_key FROM artifacts WHERE owner_kind = 'systems' "
    "AND owner_id = %s AND object_key LIKE %s"
)

# System-owned diagnostic SysRq captures (ADR-0285); reclaimed at teardown like console parts,
# since no gc expiry sweep touches owner_kind='systems'.
_SYSRQ_DIAGNOSTIC_LIKE: LiteralString = "%sysrq-diagnostic-%"


class _ProviderLifecycleCall(Protocol):
    def __call__(
        self,
        system_id: UUID,
        profile: ProvisioningProfile,
        *,
        overlay_customizers: tuple[Callable[[str], None], ...],
        bootstrap_pubkey: str,
    ) -> str: ...


async def _bootstrap_key_material(
    conn: AsyncConnection,
    system_id: UUID,
    runtime: ProviderRuntime,
    secret_registry: SecretRegistry,
) -> tuple[tuple[Callable[[str], None], ...], str]:
    """Ensure the System's bootstrap key (committed) and return ``(overlay_customizers, pubkey)``.

    The ``ensure`` commits in its own transaction BEFORE this returns (ADR-0289, #963): the
    overlay is created and the key injected into it strictly after this call, so a later
    rollback in the caller's transaction never un-records a key the overlay may already trust.
    A provider runtime with no ``bootstrap_key`` capability (no local overlay to
    customize) yields no customizers; the returned ``pubkey`` is still passed to the provider as
    ``bootstrap_pubkey`` so remote-libvirt can inject it over the guest agent (ADR-0291, #966).
    """
    async with conn.transaction():
        pubkey = await ensure_system_bootstrap_key(conn, system_id, secret_registry=secret_registry)
    customizers = (
        () if runtime.bootstrap_key is None else (runtime.bootstrap_key.customizer(pubkey),)
    )
    return customizers, pubkey


async def audit_transition(
    conn: AsyncConnection, job: Job, *, project: str, object_id: UUID, transition: str, tool: str
) -> None:
    await audit.record(
        conn,
        job_context_from_job(job, project),
        audit.AuditEvent(
            tool=tool,
            object_kind="systems",
            object_id=object_id,
            transition=transition,
            args={"system_id": str(object_id)},
            project=project,
        ),
    )


async def open_billing_interval(conn: AsyncConnection, allocation_id: UUID) -> None:
    """Stamp the allocation's ``active_started_at`` when its first System reaches ``ready``."""
    await conn.execute(
        "UPDATE allocations SET active_started_at = now() "
        "WHERE id = %s AND active_started_at IS NULL",
        (allocation_id,),
    )


async def _commit_uploaded_rootfs(
    conn: AsyncConnection,
    system: System,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    artifact_store: ObjectStore,
) -> None:
    """Commit the write-once artifacts row for an 'upload'-kind rootfs (ADR-0048 §6)."""
    if not rootfs_upload_window_allowed(profile_policy, profile):
        return
    key = artifact_key("local", "systems", str(system.id), "rootfs")
    head = await asyncio.to_thread(artifact_store.head, key)
    if head is None:
        raise CategorizedError(
            "upload-kind rootfs was never uploaded",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system.id)},
        )
    stored = StoredArtifact(key, head.etag, Sensitivity.SENSITIVE, "rootfs")
    await ARTIFACTS.insert(
        conn, register_artifact_row(stored, owner_kind="systems", owner_id=system.id)
    )
    await upload_manifest.delete_manifest(conn, "systems", system.id)


async def _finalize_provision_ready(
    conn: AsyncConnection,
    job: Job,
    system: System,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    artifact_store: ObjectStore,
) -> None:
    await _commit_uploaded_rootfs(conn, system, profile, profile_policy, artifact_store)
    await open_billing_interval(conn, system.allocation_id)
    await audit_transition(
        conn,
        job,
        project=system.project,
        object_id=system.id,
        transition="provisioning->ready",
        tool="systems.provision",
    )


async def _record_system_failure(
    conn: AsyncConnection,
    job: Job,
    *,
    system_id: UUID,
    project: str,
    transition: str,
    tool: str,
    operation: str,
) -> None:
    try:
        async with conn.transaction():
            await SYSTEMS.update_state(conn, system_id, SystemState.FAILED)
            await audit_transition(
                conn,
                job,
                project=project,
                object_id=system_id,
                transition=transition,
                tool=tool,
            )
    except IllegalTransition:
        _log.info("%s of system %s failed but it is already terminal", operation, system_id)
    except Exception:  # noqa: BLE001 - failure recording is best-effort; preserve provider error
        _log.warning(
            "%s of system %s failed but failure recording failed; preserving provider error",
            operation,
            system_id,
            exc_info=True,
        )


async def _locked_system_state(conn: AsyncConnection, system_id: UUID) -> SystemState | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT state FROM systems WHERE id = %s FOR UPDATE", (system_id,))
        row = await cur.fetchone()
    return SystemState(row["state"]) if row is not None else None


async def _commit_provision_result(
    conn: AsyncConnection,
    job: Job,
    system: System,
    profile: ProvisioningProfile,
    profile_policy: ProfilePolicy,
    domain_name: str,
    artifact_store: ObjectStore,
) -> SystemState | None:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        current = await _locked_system_state(conn, system.id)
        if current is SystemState.PROVISIONING:
            await conn.execute(
                "UPDATE systems SET state = %s, domain_name = %s WHERE id = %s",
                (SystemState.READY.value, domain_name, system.id),
            )
            await _finalize_provision_ready(
                conn, job, system, profile, profile_policy, artifact_store
            )
        return current


async def _commit_reprovision_result(
    conn: AsyncConnection,
    job: Job,
    system: System,
    profile: ProvisioningProfile,
    domain_name: str,
) -> SystemState | None:
    fingerprint = profile_digest(profile)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system.id):
        current = await _locked_system_state(conn, system.id)
        if current is SystemState.REPROVISIONING:
            await conn.execute(
                "UPDATE systems SET state = %s, domain_name = %s, "
                "target_fingerprint = %s WHERE id = %s",
                (SystemState.READY.value, domain_name, fingerprint, system.id),
            )
            # The recreated qcow2 destroys the old libvirt snapshots; drop their ledger rows in
            # the same commit so a restore of a stale name is refused, not silently mismatched.
            await delete_snapshots_for_system(conn, system.id)
            await audit_transition(
                conn,
                job,
                project=system.project,
                object_id=system.id,
                transition="reprovisioning->ready",
                tool="systems.reprovision",
            )
        return current


async def _provider_lifecycle_call(
    provider_call: _ProviderLifecycleCall,
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    customizers: tuple[Callable[[str], None], ...],
    pubkey: str,
) -> str:
    return await asyncio.to_thread(
        functools.partial(
            provider_call,
            system_id,
            profile,
            overlay_customizers=customizers,
            bootstrap_pubkey=pubkey,
        )
    )


async def _execute_system_lifecycle_call(
    conn: AsyncConnection,
    job: Job,
    system: System,
    runtime: ProviderRuntime,
    *,
    secret_registry: SecretRegistry,
    provider_call: _ProviderLifecycleCall,
    commit_result: Callable[
        [AsyncConnection, Job, System, ProvisioningProfile, str],
        Awaitable[SystemState | None],
    ],
    failure_transition: str,
    tool: str,
    operation: str,
    binding_kind: ResourceKind,
) -> str:
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    customizers, pubkey = await _bootstrap_key_material(conn, system.id, runtime, secret_registry)
    try:
        domain_name = await _provider_lifecycle_call(
            provider_call,
            system.id,
            profile,
            customizers=customizers,
            pubkey=pubkey,
        )
    except CategorizedError as exc:
        await _record_system_failure(
            conn,
            job,
            system_id=system.id,
            project=system.project,
            transition=failure_transition,
            tool=tool,
            operation=operation,
        )
        # The System is now terminally `failed`; dead-letter at once so a retry cannot mask it.
        exc.terminal = True
        raise
    current = await commit_result(conn, job, system, profile, domain_name)
    if current in TERMINAL_SYSTEM_STATES:
        await asyncio.to_thread(runtime.provisioner.teardown, domain_name)
        _log.info("%s of system %s superseded by teardown; domain reaped", operation, system.id)
    else:
        await _persist_local_resolved_cpu(conn, system, runtime, binding_kind)
    return str(system.id)


async def _persist_local_resolved_cpu(
    conn: AsyncConnection, system: System, runtime: ProviderRuntime, binding_kind: ResourceKind
) -> None:
    """Persist the local domain's live-verified resolved CPU at the READY boundary (ADR-0369).

    Local-libvirt only (remote keeps the ADR-0368 mint snapshot; fault-inject has no domain). Gated
    on the binding kind — not an ``isinstance`` on the concrete provisioner, which would cross the
    provider boundary (``read_resolved_cpu`` is on the ``Provisioner`` port; remote/fault return
    ``None``, so an unconditional write would clobber the remote snapshot with NULL). The read is
    best-effort (``read_resolved_cpu`` never raises) and runs off the event loop (a blocking libvirt
    call would starve the worker, cf. #583). The write is state-guarded to ``{PROVISIONING,
    READY}``, so a System that crashed / was reaped between the READY transition and this write
    takes a no-op rather than a stale value.
    """
    if binding_kind is not ResourceKind.LOCAL_LIBVIRT:
        return
    resolved = await asyncio.to_thread(runtime.provisioner.read_resolved_cpu, system.id)
    async with conn.transaction():
        await SYSTEMS.set_json_column(
            conn,
            system.id,
            "resolved_cpu",
            resolved,
            allowed_states=frozenset({SystemState.PROVISIONING, SystemState.READY}),
        )


async def provision_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry | None = None,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Define+start the tagged domain and drive the System ``provisioning -> ready``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "provision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    runtime = binding.runtime
    provisioner = runtime.provisioner
    if system.state is not SystemState.PROVISIONING:
        if system.state in TERMINAL_SYSTEM_STATES:
            await asyncio.to_thread(
                provisioner.teardown, system.domain_name or domain_name_for(system_id)
            )
        return str(system_id)
    secret_registry = secret_registry or SecretRegistry()
    artifact_store = artifact_store or object_store_from_env()

    async def _commit(
        conn: AsyncConnection,
        job: Job,
        system: System,
        profile: ProvisioningProfile,
        domain_name: str,
    ) -> SystemState | None:
        return await _commit_provision_result(
            conn, job, system, profile, runtime.profile_policy, domain_name, artifact_store
        )

    return await _execute_system_lifecycle_call(
        conn,
        job,
        system,
        runtime,
        secret_registry=secret_registry,
        provider_call=provisioner.provision,
        commit_result=_commit,
        failure_transition="provisioning->failed",
        tool="systems.provision",
        operation="provision",
        binding_kind=binding.kind,
    )


async def reprovision_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry | None = None,
) -> str | None:
    """Apply the new profile in place and drive ``reprovisioning -> ready`` or failed."""
    system_id = UUID(load_payload(job, ReprovisionPayload).system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "reprovision target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    runtime = binding.runtime
    provisioner = runtime.provisioner
    if system.state is not SystemState.REPROVISIONING:
        return str(system_id)
    secret_registry = secret_registry or SecretRegistry()
    return await _execute_system_lifecycle_call(
        conn,
        job,
        system,
        runtime,
        secret_registry=secret_registry,
        provider_call=provisioner.reprovision,
        commit_result=_commit_reprovision_result,
        failure_transition="reprovisioning->failed",
        tool="systems.reprovision",
        operation="reprovision",
        binding_kind=binding.kind,
    )


def _require_snapshotter(runtime: ProviderRuntime, system_id: UUID) -> Snapshotter:
    if runtime.snapshot is None:  # Defense in depth: the tool gates on supports_snapshots.
        raise CategorizedError(
            "provider does not support snapshots",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system_id)},
        )
    return runtime.snapshot


async def _bind_snapshotter(
    conn: AsyncConnection, resolver: ProviderResolver, system_id: UUID
) -> Snapshotter:
    """Resolve the provider binding, set the worker provider kind, return its Snapshotter."""
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    return _require_snapshotter(binding.runtime, system_id)


async def _fail_snapshot_row(conn: AsyncConnection, snapshot_id: UUID) -> None:
    """Drive a ``snapshots`` row to ``failed``; tolerant of an already-terminal/absent row."""
    try:
        await SNAPSHOTS.update_state(conn, snapshot_id, SnapshotState.FAILED)
    except IllegalTransition, ObjectNotFound:
        _log.info("snapshot row %s already terminal or gone; not marking failed", snapshot_id)


async def snapshot_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Create the libvirt snapshot and drive the ledger row ``creating -> available|failed``.

    The System row is never touched: a snapshot is non-destructive to System identity and is
    permitted during a live Run (ADR-0378).
    """
    payload = load_payload(job, SnapshotPayload)
    system_id = UUID(payload.system_id)
    snapshot_id = UUID(payload.snapshot_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        await _fail_snapshot_row(conn, snapshot_id)
        raise CategorizedError(
            "snapshot target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    snapshotter = await _bind_snapshotter(conn, resolver, system_id)
    if system.state is not SystemState.READY:  # Re-verify at start: a mid-flight teardown/revert.
        await _fail_snapshot_row(conn, snapshot_id)
        return str(snapshot_id)
    domain = system.domain_name or domain_name_for(system_id)
    try:
        await asyncio.to_thread(
            functools.partial(
                snapshotter.create, domain, payload.name, include_memory=payload.include_memory
            )
        )
    except CategorizedError as exc:
        await _fail_snapshot_row(conn, snapshot_id)
        # A failed capture is terminal (recycle_terminal on the dedup key frees a fresh re-issue),
        # so it dead-letters at once rather than retrying and racing the failed row to available.
        exc.terminal = True
        raise
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        # Idempotent under at-least-once delivery: a worker that dies after this transition commits
        # but before the job is marked complete re-runs the handler. Only a still-``creating`` row
        # transitions, so an already-``available`` row (the capture already landed) is a no-op
        # success rather than an ``available -> available`` IllegalTransition that would dead-letter
        # a snapshot that actually succeeded.
        row = await SNAPSHOTS.get(conn, snapshot_id)
        if row is not None and row.state is SnapshotState.CREATING:
            await SNAPSHOTS.update_state(conn, snapshot_id, SnapshotState.AVAILABLE)
    return str(snapshot_id)


async def _commit_restore_result(
    conn: AsyncConnection, job: Job, system_id: UUID, project: str, *, start_paused: bool
) -> None:
    """Commit ``restoring -> paused|ready`` under the SYSTEM lock (re-reading state first).

    Committed before the handler returns, so the worker marks the RESTORE job terminal only
    after this transition lands — the RESTORING stuck-transition repair therefore never observes
    a terminal job alongside a still-RESTORING System on the success path (ADR-0378).
    """
    target = SystemState.PAUSED if start_paused else SystemState.READY
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        current = await _locked_system_state(conn, system_id)
        if current is SystemState.RESTORING:
            await SYSTEMS.update_state(conn, system_id, target)
            await audit_transition(
                conn,
                job,
                project=project,
                object_id=system_id,
                transition=f"restoring->{target.value}",
                tool="systems.restore",
            )


async def restore_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Revert the domain and drive ``restoring -> ready|paused`` (or ``failed`` on error)."""
    payload = load_payload(job, RestorePayload)
    system_id = UUID(payload.system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "restore target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    snapshotter = await _bind_snapshotter(conn, resolver, system_id)
    if system.state is not SystemState.RESTORING:
        return str(system_id)
    domain = system.domain_name or domain_name_for(system_id)
    try:
        await asyncio.to_thread(
            functools.partial(
                snapshotter.revert, domain, payload.name, start_paused=payload.start_paused
            )
        )
    except CategorizedError as exc:
        # A half-reverted guest is indeterminate: route RESTORING -> FAILED, never back to READY.
        await _record_system_failure(
            conn,
            job,
            system_id=system_id,
            project=system.project,
            transition="restoring->failed",
            tool="systems.restore",
            operation="restore",
        )
        # Terminal: the System is now FAILED, so a retry would read `not RESTORING` and early-return
        # str(system_id), marking the RESTORE job succeeded while the System is actually FAILED.
        exc.terminal = True
        raise
    await _commit_restore_result(
        conn, job, system_id, system.project, start_paused=payload.start_paused
    )
    return str(system_id)


async def snapshot_delete_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
) -> str | None:
    """Delete the libvirt snapshot and remove the ledger row (idempotent, ABA-safe).

    Anchored on ``snapshot_id``: the delete proceeds only while the ``(system_id, name)`` row is
    still the exact one admission targeted. A row that is gone, or now a *different* snapshot that
    reused the name (an at-least-once redelivery after the name was recreated), is a no-op — so a
    stale redelivery never destroys a fresh, reported-successful checkpoint.
    """
    payload = load_payload(job, SnapshotDeletePayload)
    system_id = UUID(payload.system_id)
    snapshot_id = UUID(payload.snapshot_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        raise CategorizedError(
            "delete-snapshot target system is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    snapshotter = await _bind_snapshotter(conn, resolver, system_id)
    # The still-present row holds the name, so no concurrent snapshot can reuse it until we delete
    # it (admission rejects an in-use name); if the id no longer matches, this delete already ran
    # and the name belongs to a newer snapshot — leave it alone.
    row = await snapshot_by_name(conn, system_id, payload.name)
    if row is None or row.id != snapshot_id:
        return str(system_id)
    domain = system.domain_name or domain_name_for(system_id)
    await asyncio.to_thread(snapshotter.delete, domain, payload.name)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        row = await snapshot_by_name(conn, system_id, payload.name)
        if row is not None and row.id == snapshot_id:
            await SNAPSHOTS.delete(conn, row.id)
    return str(system_id)


async def _console_part_keys(conn: AsyncConnection, system_id: UUID) -> list[str]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT_PART_KEYS_SQL, (system_id, _CONSOLE_PART_LIKE))
        return [row["object_key"] for row in await cur.fetchall()]


async def _reclaim_console_artifacts(
    conn: AsyncConnection, store: ObjectStore, system_id: UUID
) -> None:
    """Delete the System's console-rotation part objects + rows and the sidecar object (#892).

    Part objects are deleted before their ``artifacts`` rows so a mid-cleanup store failure leaves
    the rows for the artifact-expiry reconciler (#768) to reclaim. The rotation-state sidecar has no
    ``artifacts`` row, so #768 never reaps it; deleting it here is the only thing that reclaims it.
    """
    part_keys = await _console_part_keys(conn, system_id)
    for key in part_keys:
        await asyncio.to_thread(store.delete, key)
    if part_keys:
        async with conn.transaction():
            await conn.execute(_DELETE_PART_ROWS_SQL, (system_id, _CONSOLE_PART_LIKE))
    sidecar_key = artifact_key(_CONSOLE_TENANT, "systems", str(system_id), sidecar_object_name())
    await asyncio.to_thread(store.delete, sidecar_key)


async def _reclaim_sysrq_artifacts(
    conn: AsyncConnection, store: ObjectStore, system_id: UUID
) -> None:
    """Delete the System's diagnostic SysRq capture objects + rows (ADR-0285).

    Objects are deleted before their ``artifacts`` rows so a mid-cleanup store failure leaves the
    rows for the artifact-expiry reconciler (#768) to reclaim, mirroring the console-part reclaim.
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_SELECT_PART_KEYS_SQL, (system_id, _SYSRQ_DIAGNOSTIC_LIKE))
        keys = [row["object_key"] for row in await cur.fetchall()]
    for key in keys:
        await asyncio.to_thread(store.delete, key)
    if keys:
        async with conn.transaction():
            await conn.execute(_DELETE_PART_ROWS_SQL, (system_id, _SYSRQ_DIAGNOSTIC_LIKE))


_DELETE_ROOTFS_ROW_SQL: LiteralString = (
    "DELETE FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
)


def _uploaded_rootfs_key(system_id: UUID) -> str:
    # Committed under the local tenant (ADR-0048 §6, `_commit_uploaded_rootfs`).
    return artifact_key("local", "systems", str(system_id), "rootfs")


async def _delete_uploaded_rootfs_object(
    conn: AsyncConnection, store: ObjectStore, system_id: UUID
) -> None:
    """Delete the System's uploaded rootfs object (best-effort) — ADR-0434 §4.

    ``owner_kind='systems'`` objects are exempt from the #768 expiry reaper, so a torn-down
    System's SENSITIVE uploaded rootfs must be reclaimed here. This byte-delete is best-effort (a
    store fault must not block teardown, like the console/sysrq reclaim); the ``artifacts`` row —
    the download handle — is deleted fail-loud in :func:`_delete_uploaded_rootfs_row`, so a store
    fault leaves at most an unreferenced orphan, never a live download handle. A non-upload System
    has no such object; the delete is a no-op. ``conn`` is unused (parity with the reclaim
    helpers' signature).
    """
    del conn
    await asyncio.to_thread(store.delete, _uploaded_rootfs_key(system_id))


async def _delete_uploaded_rootfs_row(conn: AsyncConnection, system_id: UUID) -> None:
    """Delete the uploaded rootfs ``artifacts`` row (fail-loud) so the download handle is revoked.

    Anchored on the exact object key, so a non-upload System (no such row) is a no-op and an
    at-least-once teardown redelivery is idempotent. Fail-loud like ``delete_system_bootstrap_key``
    (a stale row after teardown would keep the SENSITIVE rootfs downloadable), so it runs outside
    the best-effort reclaim block.
    """
    async with conn.transaction():
        await conn.execute(_DELETE_ROOTFS_ROW_SQL, (system_id, _uploaded_rootfs_key(system_id)))


async def _reclaim_snapshots(
    conn: AsyncConnection, snapshotter: Snapshotter | None, system_id: UUID, domain_name: str
) -> None:
    """Free a System's snapshots at teardown: provider data, then ledger rows (ADR-0378).

    ``delete_all`` is best-effort — the ``undefine`` metadata flag already lets a snapshotted
    domain tear down, and the internal snapshot data is freed with the overlay qcow2 this teardown
    reclaims — so a provider snapshot fault never blocks teardown. A provider that does not support
    snapshots (``snapshot is None``) skips it. The ledger rows are then deleted so a torn-down
    System reports no snapshots.
    """
    if snapshotter is not None:
        try:
            await asyncio.to_thread(snapshotter.delete_all, domain_name)
        except CategorizedError:
            _log.warning(
                "best-effort snapshot delete_all for system %s failed; continuing teardown",
                system_id,
                exc_info=True,
            )
    async with conn.transaction():
        await delete_snapshots_for_system(conn, system_id)


async def teardown_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Destroy the domain, reclaim console artifacts, and drive the System ``-> torn_down``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
    artifact_store = artifact_store or object_store_from_env()
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            return None
        domain_name = system.domain_name or domain_name_for(system_id)
        if system.state is not SystemState.TORN_DOWN:
            old = system.state
            # The console_rotate teardown-race guard (console_rotate.py) relies on this terminal
            # state write happening under the SYSTEM lock: once it commits, a rotation job that
            # acquires the lock sees torn_down and seals nothing. Keep the state-set under the lock.
            await SYSTEMS.update_state(conn, system_id, SystemState.TORN_DOWN)
            await audit_transition(
                conn,
                job,
                project=system.project,
                object_id=system_id,
                transition=f"{old.value}->torn_down",
                tool="systems.teardown",
            )
    binding = await resolver.binding_for_system(conn, system_id)
    set_provider_kind(binding.kind.value)
    provisioner = binding.runtime.provisioner
    await _reclaim_snapshots(conn, binding.runtime.snapshot, system_id, domain_name)
    await asyncio.to_thread(provisioner.teardown, domain_name)
    # The bootstrap key (ADR-0289, #963) is System-owned like the console/sysrq artifacts, but its
    # deletion is not best-effort: a stale row after teardown wrongly reports a System as
    # SSH-reachable, so it is not swallowed by the best-effort try/except that guards the reclaim.
    async with conn.transaction():
        await delete_system_bootstrap_key(conn, system_id)
    # Revoke the uploaded-rootfs download handle fail-loud (ADR-0434 §4), like the bootstrap key:
    # a stale artifacts row would keep the SENSITIVE image downloadable after teardown. The object
    # bytes are reclaimed best-effort below.
    await _delete_uploaded_rootfs_row(conn, system_id)
    # Host-filesystem reclaim (ADR-0385): capture_traffic pcaps are written to local disk by QEMU
    # under pcap_dir(system_id), not the object store, so they are removed here rather than through
    # the object-store _reclaim_* helpers. rmtree ignore_errors makes it best-effort on its own, so
    # an object-store reclaim failure below cannot skip it (and vice versa).
    await asyncio.to_thread(shutil.rmtree, str(pcap_dir(system_id)), ignore_errors=True)
    try:
        await _reclaim_console_artifacts(conn, artifact_store, system_id)
        await _reclaim_sysrq_artifacts(conn, artifact_store, system_id)
    except Exception:  # noqa: BLE001 - reclaim is best-effort; teardown must still succeed
        _log.warning(
            "best-effort System-artifact reclaim for system %s failed",
            system_id,
            exc_info=True,
        )
    # Isolated from the console/sysrq reclaim above: a fault there must not skip reclaiming the
    # SENSITIVE uploaded-rootfs object, which no #768 reaper would ever collect (ADR-0434 §4).
    try:
        await _delete_uploaded_rootfs_object(conn, artifact_store, system_id)
    except Exception:  # noqa: BLE001 - object reclaim is best-effort; the row was already revoked
        _log.warning(
            "best-effort uploaded-rootfs object reclaim for system %s failed",
            system_id,
            exc_info=True,
        )
    return str(system_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None = None,
) -> None:
    """Bind the provision/teardown/reprovision/authorize_ssh_key/check_ssh_reachable handlers."""
    registry.register(
        JobKind.PROVISION,
        lambda conn, job: provision_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            artifact_store=artifact_store,
        ),
    )
    registry.register(
        JobKind.TEARDOWN,
        lambda conn, job: teardown_handler(
            conn, job, resolver=resolver, artifact_store=artifact_store
        ),
    )
    registry.register(
        JobKind.REPROVISION,
        lambda conn, job: reprovision_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
    registry.register(
        JobKind.SNAPSHOT,
        lambda conn, job: snapshot_handler(conn, job, resolver=resolver),
    )
    registry.register(
        JobKind.RESTORE,
        lambda conn, job: restore_handler(conn, job, resolver=resolver),
    )
    registry.register(
        JobKind.DELETE_SNAPSHOT,
        lambda conn, job: snapshot_delete_handler(conn, job, resolver=resolver),
    )
    registry.register(
        JobKind.AUTHORIZE_SSH_KEY,
        lambda conn, job: authorize_ssh_key_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )
    registry.register(
        JobKind.CHECK_SSH_REACHABLE,
        lambda conn, job: check_ssh_reachable_handler(
            conn, job, resolver=resolver, secret_registry=secret_registry
        ),
    )

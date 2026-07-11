"""Worker handlers for the `systems.*` plane."""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable
from typing import LiteralString
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import StoredArtifact
from kdive.db import upload_manifest
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.capacity.state import IllegalTransition, SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.lifecycle.rules import TERMINAL_SYSTEM_STATES
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.connectivity.ssh_authorize import authorize_ssh_key_handler
from kdive.jobs.handlers.connectivity.ssh_reachable import check_ssh_reachable_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import ReprovisionPayload, SystemPayload, load_payload
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
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security import audit
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore, artifact_key

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
    artifact_store: ObjectStore | None,
) -> None:
    """Commit the write-once artifacts row for an 'upload'-kind rootfs (ADR-0048 §6)."""
    if not rootfs_upload_window_allowed(profile_policy, profile):
        return
    if artifact_store is None:
        raise CategorizedError(
            "object storage is not configured; cannot commit uploaded rootfs",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"system_id": str(system.id), "artifact": "rootfs"},
        )
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
    artifact_store: ObjectStore | None,
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
    artifact_store: ObjectStore | None,
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
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    secret_registry = secret_registry or SecretRegistry()
    customizers, pubkey = await _bootstrap_key_material(conn, system_id, runtime, secret_registry)
    try:
        domain_name = await asyncio.to_thread(
            functools.partial(
                provisioner.provision,
                system_id,
                profile,
                overlay_customizers=customizers,
                bootstrap_pubkey=pubkey,
            )
        )
    except CategorizedError as exc:
        await _record_system_failure(
            conn,
            job,
            system_id=system_id,
            project=system.project,
            transition="provisioning->failed",
            tool="systems.provision",
            operation="provision",
        )
        # The System is now terminally `failed`; a job retry would re-enter the terminal-state
        # branch above and return success, masking this failure. Dead-letter at once instead.
        exc.terminal = True
        raise
    current = await _commit_provision_result(
        conn, job, system, profile, runtime.profile_policy, domain_name, artifact_store
    )
    if current in TERMINAL_SYSTEM_STATES:
        await asyncio.to_thread(provisioner.teardown, domain_name)
        _log.info("provision of system %s superseded by teardown; domain reaped", system_id)
    return str(system_id)


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
    provisioner = binding.runtime.provisioner
    if system.state is not SystemState.REPROVISIONING:
        return str(system_id)
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    secret_registry = secret_registry or SecretRegistry()
    # Same commit-before-overlay ordering as provision_handler (ADR-0289, #963): reprovision wipes
    # and recreates the overlay, so it must re-inject the SAME stored key — ensure_ returns the
    # existing key on reuse, and this commits before the overlay is ever touched.
    customizers, pubkey = await _bootstrap_key_material(
        conn, system_id, binding.runtime, secret_registry
    )
    try:
        domain_name = await asyncio.to_thread(
            functools.partial(
                provisioner.reprovision,
                system_id,
                profile,
                overlay_customizers=customizers,
                bootstrap_pubkey=pubkey,
            )
        )
    except CategorizedError as exc:
        await _record_system_failure(
            conn,
            job,
            system_id=system_id,
            project=system.project,
            transition="reprovisioning->failed",
            tool="systems.reprovision",
            operation="reprovision",
        )
        # The System is now terminally `failed`; dead-letter at once so a retry cannot mask it.
        exc.terminal = True
        raise
    fingerprint = profile_digest(profile)
    current: SystemState | None = None
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT state FROM systems WHERE id = %s FOR UPDATE", (system_id,))
            row = await cur.fetchone()
            current = SystemState(row["state"]) if row is not None else None
            if current is SystemState.REPROVISIONING:
                await cur.execute(
                    "UPDATE systems SET state = %s, domain_name = %s, "
                    "target_fingerprint = %s WHERE id = %s",
                    (SystemState.READY.value, domain_name, fingerprint, system_id),
                )
        if current is SystemState.REPROVISIONING:
            await audit_transition(
                conn,
                job,
                project=system.project,
                object_id=system_id,
                transition="reprovisioning->ready",
                tool="systems.reprovision",
            )
    if current in TERMINAL_SYSTEM_STATES:
        await asyncio.to_thread(provisioner.teardown, domain_name)
        _log.info("reprovision of system %s superseded by teardown; domain reaped", system_id)
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


async def teardown_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Destroy the domain, reclaim console artifacts, and drive the System ``-> torn_down``."""
    system_id = UUID(load_payload(job, SystemPayload).system_id)
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
    await asyncio.to_thread(provisioner.teardown, domain_name)
    # The bootstrap key (ADR-0289, #963) is System-owned like the console/sysrq artifacts, but its
    # deletion is not best-effort: a stale row after teardown wrongly reports a System as
    # SSH-reachable, so it runs unconditionally (unlike the artifact_store-gated reclaim below) and
    # is not swallowed by the best-effort try/except.
    async with conn.transaction():
        await delete_system_bootstrap_key(conn, system_id)
    if artifact_store is not None:
        try:
            await _reclaim_console_artifacts(conn, artifact_store, system_id)
            await _reclaim_sysrq_artifacts(conn, artifact_store, system_id)
        except Exception:  # noqa: BLE001 - reclaim is best-effort; teardown must still succeed
            _log.warning(
                "best-effort System-artifact reclaim for system %s failed",
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

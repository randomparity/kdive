"""Console artifact capture and boot-failure evidence helpers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError
from kdive.domain.lifecycle.crash_signatures import CONSOLE_CRASH_KINDS
from kdive.domain.lifecycle.records import Run
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.ports.console import ConsoleSnapshotter
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.ports.lifecycle import Connector
from kdive.providers.shared.runtime_paths import (
    console_log_path,
    domain_name_for,
    read_console_log,
)
from kdive.security import audit
from kdive.security.artifacts.artifact_search import ArtifactSearchInputError, search_text
from kdive.security.authz.context import RequestContext
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

_log = logging.getLogger(__name__)

_CONSOLE_ROW_SQL: LiteralString = (
    "SELECT id, etag FROM artifacts "
    "WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
)

_REFRESH_CONSOLE_ETAG_SQL: LiteralString = "UPDATE artifacts SET etag = %s WHERE id = %s"
_GENERIC_PANIC_PATTERN = "Kernel panic - not syncing"


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


class ConsoleArtifact(NamedTuple):
    id: UUID
    object_key: str
    data: bytes


async def _existing_console_row(
    conn: AsyncConnection, system_id: UUID, object_key: str
) -> _ConsoleRow | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_CONSOLE_ROW_SQL, (system_id, object_key))
        row = await cur.fetchone()
    return None if row is None else _ConsoleRow(row["id"], str(row["etag"]))


async def _capture_console_artifact(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
) -> ConsoleArtifact | None:
    try:
        if artifact_store is None:
            return None
        redacted = await read_redacted_console(system_id, secret_registry)
        if redacted is None:
            return None
        stored = await _store_console_artifact(artifact_store, system_id, run_id, redacted)
        return await _upsert_console_artifact_row(conn, system_id, stored, redacted)
    except CategorizedError as exc:
        if exc.details.get("operation") == "read_console_log":
            raise
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None
    except Exception:
        _log.warning(
            "console artifact registration failed for system %s; boot outcome unaffected",
            system_id,
            exc_info=True,
        )
        return None


async def capture_run_console(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> ConsoleArtifact | None:
    """Persist this Run's boot-window console, dispatching to the provider."""
    if snapshotter is not None:
        try:
            snap = await snapshotter.snapshot(conn, system_id, run_id, mark)
        except Exception:
            _log.warning(
                "console snapshot failed for system %s run %s; boot outcome unaffected",
                system_id,
                run_id,
                exc_info=True,
            )
            return None
        return None if snap is None else ConsoleArtifact(snap.id, snap.object_key, snap.data)
    return await _capture_console_artifact(conn, system_id, run_id, secret_registry, artifact_store)


async def mark_boot_window(system_id: UUID, snapshotter: ConsoleSnapshotter | None) -> int:
    """Return the provider boot-window start mark, degrading to cumulative capture on failure."""
    if snapshotter is None:
        return 0
    try:
        return await snapshotter.mark_boot_window(system_id)
    except Exception:
        _log.warning(
            "reading the console boot-window mark for system %s failed; "
            "capturing the cumulative console for this boot",
            system_id,
            exc_info=True,
        )
        return 0


async def read_redacted_console(system_id: UUID, secret_registry: SecretRegistry) -> bytes | None:
    raw = await asyncio.to_thread(read_console_log, console_log_path(system_id))
    if not raw:
        _log.warning(
            "console log for system %s is empty or unreadable; registering no console artifact",
            system_id,
        )
        return None
    return (
        Redactor(registry=secret_registry)
        .redact_text(raw.decode("utf-8", "replace"))
        .encode("utf-8")
    )


async def _store_console_artifact(
    artifact_store: ObjectStore, system_id: UUID, run_id: UUID, redacted: bytes
) -> StoredArtifact:
    def _put() -> StoredArtifact:
        return artifact_store.put_artifact(
            ArtifactWriteRequest(
                tenant="local",
                owner_kind="systems",
                owner_id=str(system_id),
                name=f"console-{run_id}",
                data=redacted,
                sensitivity=Sensitivity.REDACTED,
                retention_class="console",
            )
        )

    return await asyncio.to_thread(_put)


async def _upsert_console_artifact_row(
    conn: AsyncConnection,
    system_id: UUID,
    stored: StoredArtifact,
    redacted: bytes,
) -> ConsoleArtifact:
    async with conn.transaction():
        existing = await _existing_console_row(conn, system_id, stored.key)
        if existing is None:
            inserted = await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="systems", owner_id=system_id)
            )
            return ConsoleArtifact(inserted.id, inserted.object_key, redacted)
        if existing.etag != stored.etag:
            await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        return ConsoleArtifact(existing.id, stored.key, redacted)


def expected_crash_matched_line(run: Run, redacted_console: bytes) -> str | None:
    """Return the first redacted console line matching this Run's console-crash expectation."""
    expected = run.expected_boot_failure
    if expected is None or expected.get("kind") not in CONSOLE_CRASH_KINDS:
        return None
    pattern = expected.get("pattern")
    if not isinstance(pattern, str):
        return None
    try:
        result = search_text(
            redacted_console,
            pattern=pattern,
            before_lines=0,
            after_lines=0,
            max_matches=1,
        )
    except ArtifactSearchInputError:
        return None
    if not result.matches:
        return None
    return result.matches[0]["text"]


def generic_panic_matches(redacted_console: bytes) -> bool:
    """True iff the redacted console shows a generic kernel panic; fails closed on bad input."""
    try:
        return (
            search_text(
                redacted_console,
                pattern=_GENERIC_PANIC_PATTERN,
                before_lines=0,
                after_lines=0,
                max_matches=1,
            ).match_count
            > 0
        )
    except ArtifactSearchInputError:
        return False


def available_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    """Return the follow-up capture methods available for a halted System."""
    methods = [CaptureMethod.GDBSTUB.value, CaptureMethod.CONSOLE.value]
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    return methods


def inert_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    """Return provisioned capture methods that will not fire on an expected crash."""
    methods: list[str] = []
    if profile_policy.gdbstub_provisioned(profile):
        methods.append(CaptureMethod.GDBSTUB.value)
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    if profile_policy.capture_method(profile) is CaptureMethod.KDUMP:
        methods.append(CaptureMethod.KDUMP.value)
    return methods


def gdbstub_reachable(connector: Connector, system_id: UUID) -> bool:
    """Probe the gdbstub via the connector's read-only open path."""
    try:
        connector.open_transport(SystemHandle(domain_name_for(system_id)), "gdbstub")
    except CategorizedError:
        return False
    return True


async def record_crash_halted_live(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    system_id: UUID,
    connector: Connector,
    profile_policy: ProfilePolicy,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> dict[str, Any] | None:
    """Record ``crashed_halted_live`` iff console panics and the provisioned stub answers."""
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        return None
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    if not profile_policy.gdbstub_provisioned(profile):
        return None
    artifact = await capture_run_console(
        conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
    )
    if artifact is None or not artifact.data or not generic_panic_matches(artifact.data):
        return None
    if not await asyncio.to_thread(gdbstub_reachable, connector, system_id):
        return None
    await record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "crashed_halted_live",
        "evidence_kind": "console",
        "evidence_artifact_id": str(artifact.id),
        "available_capture": available_capture(profile_policy, profile),
    }


async def _expected_crash_inert_capture(
    conn: AsyncConnection,
    system_id: UUID,
    profile_policy: ProfilePolicy,
) -> list[str]:
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        return []
    try:
        profile = ProvisioningProfile.parse(system.provisioning_profile)
    except Exception:
        _log.warning(
            "could not parse provisioning profile for system %s; inert capture set omitted",
            system_id,
            exc_info=True,
        )
        return []
    return inert_capture(profile_policy, profile)


async def record_expected_crash(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    system_id: UUID,
    profile_policy: ProfilePolicy,
    artifact: ConsoleArtifact,
    matched_line: str,
) -> dict[str, Any]:
    """Record ``expected_crash_observed`` with console evidence and inert capture disclosure."""
    inert = await _expected_crash_inert_capture(conn, system_id, profile_policy)
    await record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "expected_crash_observed",
        "expectation_matched": True,
        "evidence_kind": "console",
        "evidence_artifact_id": str(artifact.id),
        "available_capture": [CaptureMethod.CONSOLE.value],
        "inert_capture": inert,
        "matched_line": matched_line,
    }


async def record_boot_audit(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
) -> None:
    await audit.record(
        conn,
        job_ctx,
        audit.AuditEvent(
            tool="runs.boot",
            object_kind="runs",
            object_id=run.id,
            transition="boot",
            args={"run_id": str(run.id)},
            project=run.project,
        ),
    )

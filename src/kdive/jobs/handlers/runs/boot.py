"""Worker boot handler and console artifact capture for the `runs.*` plane."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact
from kdive.db.idempotency import claim_run_step, complete_run_step
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, RUNS, SYSTEMS
from kdive.domain.capture import CaptureMethod
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Run
from kdive.domain.operations.jobs import Job
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.handlers.runs.common import abandon_run_step_best_effort
from kdive.jobs.payloads import RunPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.profiles.provider_policy import ProfilePolicy
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports import Booter, Connector, ConsoleSnapshotter, SystemHandle
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


class _ConsoleRow(NamedTuple):
    id: UUID
    etag: str


class _ConsoleArtifact(NamedTuple):
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
) -> _ConsoleArtifact | None:
    try:
        if artifact_store is None:
            return None
        redacted = await _read_redacted_console(system_id, secret_registry)
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


async def _capture_run_console(
    conn: AsyncConnection,
    system_id: UUID,
    run_id: UUID,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> _ConsoleArtifact | None:
    """Persist this Run's boot-window console (ADR-0235/0241/0258), dispatching to the provider.

    ``mark`` (read before ``booter.boot``) scopes the remote capture to this boot: the next part
    index for the out-of-band ``snapshotter`` (remote-libvirt). The local-libvirt path takes no
    slice — its serial ``<log>`` is ``append="off"`` and truncated per power-cycle (ADR-0258), so
    the whole current file is this boot. Both write an immutable per-Run artifact and return its
    id + bytes; both are best-effort and never raise.
    """
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
        return None if snap is None else _ConsoleArtifact(snap.id, snap.object_key, snap.data)
    return await _capture_console_artifact(conn, system_id, run_id, secret_registry, artifact_store)


async def _mark_boot_window(system_id: UUID, snapshotter: ConsoleSnapshotter | None) -> int:
    """The boot-window start mark, read before ``booter.boot``.

    Remote (ADR-0241): the snapshotter's next part index; a failure degrades best-effort to ``0``
    (cumulative) and never fails the boot. Local: always ``0`` — the serial ``<log>`` is
    ``append="off"`` and truncated per power-cycle (ADR-0258), so the whole current file is this
    boot and no slice is taken.
    """
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


async def _read_redacted_console(system_id: UUID, secret_registry: SecretRegistry) -> bytes | None:
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
                # Per-Run name (ADR-0235): each Run's boot writes an immutable
                # `…/console-<run>` object so a later boot of the same System never overwrites
                # the bytes an earlier Run's evidence_artifact_id still references.
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
) -> _ConsoleArtifact:
    async with conn.transaction():
        existing = await _existing_console_row(conn, system_id, stored.key)
        if existing is None:
            inserted = await ARTIFACTS.insert(
                conn, register_artifact_row(stored, owner_kind="systems", owner_id=system_id)
            )
            return _ConsoleArtifact(inserted.id, inserted.object_key, redacted)
        if existing.etag != stored.etag:
            await conn.execute(_REFRESH_CONSOLE_ETAG_SQL, (stored.etag, existing.id))
        return _ConsoleArtifact(existing.id, stored.key, redacted)


def _expected_crash_matched_line(run: Run, redacted_console: bytes) -> str | None:
    """The first console line matching this Run's ``console_crash`` expectation, else ``None``.

    Returns ``SearchResult.matches[0]["text"]`` so the caller can record *which* line matched (not
    just that one did), letting an agent confirm the boot reproduced the intended crash rather than
    an unrelated earlier line that shares the pattern substring (#840). The searched bytes are the
    Run's already-redacted boot-window console (``_read_redacted_console`` runs the ``Redactor``
    before any match) and ``search_text`` clips the line to ``MAX_LINE_CHARS``, so the returned line
    is redacted-and-bounded at its source. Fails closed to ``None`` for a non-``console_crash``
    expectation, a non-string pattern, or a malformed pattern (ADR-0260).
    """
    expected = run.expected_boot_failure
    if expected is None or expected.get("kind") != "console_crash":
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


# A generic, provider-neutral kernel-panic signature for an undeclared early-boot crash. The
# console match (not the RSP probe) is the crash signal, so the probe's halt-on-connect side
# effect cannot promote a slow-but-healthy boot to a live-debuggable crash (ADR-0233, #747). A
# console without this line abandons to FAILED — the gate errs toward the safe side.
_GENERIC_PANIC_PATTERN = "Kernel panic - not syncing"


def _generic_panic_matches(redacted_console: bytes) -> bool:
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


def _available_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    """The genuinely-available follow-up methods for a halted System (policy predicates only).

    Built from provider-neutral ``ProfilePolicy`` predicates, never a provider-specific profile
    section, so this generic handler stays correct for every provider (ADR-0233).
    """
    methods = [CaptureMethod.GDBSTUB.value, CaptureMethod.CONSOLE.value]
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    return methods


def _inert_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]:
    """Capture methods the System was provisioned for that will NOT fire on an expected crash.

    The ``expected_crash_observed`` outcome leaves the System ``READY`` and is routed to the
    console A/B flow (ADR-0227), so a provisioned ``gdbstub`` (live-attach refused),
    ``host_dump``, or ``kdump`` (both need ``CRASHED``) is inert here. Built from provider-neutral
    ``ProfilePolicy`` predicates so the generic boot handler stays correct for every provider
    (ADR-0239).
    """
    methods: list[str] = []
    if profile_policy.gdbstub_provisioned(profile):
        methods.append(CaptureMethod.GDBSTUB.value)
    if profile_policy.host_dump_provisioned(profile):
        methods.append(CaptureMethod.HOST_DUMP.value)
    if profile_policy.capture_method(profile) is CaptureMethod.KDUMP:
        methods.append(CaptureMethod.KDUMP.value)
    return methods


def _gdbstub_reachable(connector: Connector, system_id: UUID) -> bool:
    """Probe the gdbstub via the connector's read-only open path; True iff it answers.

    Reuses ``open_transport`` (which runs the bounded ``rsp_reachable`` probe + loopback guard
    and holds no session row), so no port is re-resolved and the single-attach slot is untouched.
    """
    try:
        connector.open_transport(SystemHandle(domain_name_for(system_id)), "gdbstub")
    except CategorizedError:
        return False
    return True


async def _record_crash_halted_live(
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
    """Record ``crashed_halted_live`` iff gdbstub-provisioned, console panics, stub reachable.

    Returns the succeeded ``boot`` step result, or ``None`` to let the caller abandon to FAILED.
    """
    system = await SYSTEMS.get(conn, system_id)
    if system is None:
        return None
    profile = ProvisioningProfile.parse(system.provisioning_profile)
    if not profile_policy.gdbstub_provisioned(profile):
        return None
    artifact = await _capture_run_console(
        conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
    )
    if artifact is None or not artifact.data or not _generic_panic_matches(artifact.data):
        return None
    if not await asyncio.to_thread(_gdbstub_reachable, connector, system_id):
        return None
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "crashed_halted_live",
        "evidence_kind": "console",
        "evidence_artifact_id": str(artifact.id),
        "available_capture": _available_capture(profile_policy, profile),
    }


async def _expected_crash_inert_capture(
    conn: AsyncConnection,
    system_id: UUID,
    profile_policy: ProfilePolicy,
) -> list[str]:
    """The inert capture set for an expected crash; ``[]`` on a missing/unparseable profile.

    Best-effort: a torn-down System or a profile that fails to parse yields an empty set so the
    disclosure never downgrades a correctly-observed expected crash to a failed boot (ADR-0239).
    """
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
    return _inert_capture(profile_policy, profile)


async def _record_expected_crash(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    system_id: UUID,
    profile_policy: ProfilePolicy,
    artifact: _ConsoleArtifact,
    matched_line: str,
) -> dict[str, Any]:
    """Record ``expected_crash_observed``, disclosing the reachable + inert capture surface.

    The System stays ``READY`` and is routed to the console A/B flow (ADR-0227), so
    ``available_capture`` is ``["console"]``; ``inert_capture`` lists the provisioned-but-
    unreachable methods (ADR-0239). The inert disclosure is best-effort: a missing or
    unparseable System profile degrades to an empty inert set rather than failing a
    correctly-observed expected crash.

    ``matched_line`` is the console line that matched the Run's expectation (#840, ADR-0260),
    recorded so ``runs.get`` can surface which line matched. It is read from the already-redacted
    boot-window console and clipped by ``search_text``, so it is redacted-and-bounded at source.
    """
    inert = await _expected_crash_inert_capture(conn, system_id, profile_policy)
    await _record_boot_audit(conn, job_ctx, run)
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


async def _record_boot_audit(
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


async def _run_boot_and_capture_outcome(
    conn: AsyncConnection,
    job_ctx: RequestContext,
    run: Run,
    booter: Booter,
    connector: Connector,
    profile_policy: ProfilePolicy,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None,
    snapshotter: ConsoleSnapshotter | None,
    mark: int,
) -> dict[str, Any]:
    system_id = run.require_system_id()
    try:
        await asyncio.to_thread(booter.boot, system_id)
    except CategorizedError as exc:
        artifact = None
        if (
            exc.category is ErrorCategory.READINESS_FAILURE
            and run.expected_boot_failure is not None
        ):
            artifact = await _capture_run_console(
                conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
            )
        matched_line = (
            _expected_crash_matched_line(run, artifact.data)
            if artifact is not None and artifact.data
            else None
        )
        if artifact is not None and matched_line is not None:
            return await _record_expected_crash(
                conn, job_ctx, run, system_id, profile_policy, artifact, matched_line
            )
        if exc.category is ErrorCategory.READINESS_FAILURE:
            crash = await _record_crash_halted_live(
                conn,
                job_ctx,
                run,
                system_id,
                connector,
                profile_policy,
                secret_registry,
                artifact_store,
                snapshotter,
                mark,
            )
            if crash is not None:
                return crash
        raise
    artifact = await _capture_run_console(
        conn, system_id, run.id, secret_registry, artifact_store, snapshotter, mark
    )
    await _record_boot_audit(conn, job_ctx, run)
    return {
        "system_id": str(system_id),
        "boot_outcome": "ready",
        **({"evidence_artifact_id": str(artifact.id)} if artifact else {}),
    }


async def boot_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore | None = None,
) -> str | None:
    """Boot the installed kernel and confirm run-readiness, recording the `boot` step."""
    run_id = UUID(load_payload(job, RunPayload).run_id)
    run = await RUNS.get(conn, run_id)
    if run is None:
        raise CategorizedError(
            "boot target run is gone",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"run_id": str(run_id)},
        )
    job_ctx = job_context_from_job(job, run.project)
    claim = await claim_run_step(conn, run_id, "boot")
    if not claim.claimed:
        return str(run_id)
    binding = await resolver.binding_for_run(conn, run_id)
    set_provider_kind(binding.kind.value)
    booter = binding.runtime.booter
    snapshotter = binding.runtime.console_snapshotter
    system_id = run.require_system_id()
    mark = await _mark_boot_window(system_id, snapshotter)

    try:
        result = await _run_boot_and_capture_outcome(
            conn,
            job_ctx,
            run,
            booter,
            binding.runtime.connector,
            binding.runtime.profile_policy,
            secret_registry,
            artifact_store,
            snapshotter,
            mark,
        )
    except CategorizedError:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        try:
            await _capture_run_console(
                conn, system_id, run_id, secret_registry, artifact_store, snapshotter, mark
            )
        finally:
            raise
    except Exception:
        await abandon_run_step_best_effort(conn, run_id, "boot")
        raise
    async with (
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.SYSTEM, system_id),
        advisory_xact_lock(conn, LockScope.RUN, run_id),
    ):
        await complete_run_step(conn, run_id, "boot", result)
    return str(run_id)

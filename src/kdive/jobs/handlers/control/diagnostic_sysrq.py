"""Worker handler for the `diagnostic_sysrq` job (ADR-0285, #925; ADR-0292, #952; ADR-0433, #1435).

Injects one allowlisted magic-SysRq keystroke into a ready guest and captures the console dump
the kernel prints as a redacted System-owned artifact. The provider Control port is called
lock-free between two brief per-System-locked transactions: the first snapshots the domain +
console-read seam and verifies the System supports SysRq and is READY, the second re-verifies and
stores the redacted capture. Correctness allows the lock-free poll because the console only grows
while the System is READY (local-libvirt's serial log with ``append="off"`` truncates only on
power-cycle, ADR-0258; remote-libvirt's S3 parts are immutable and assembled cumulatively,
ADR-0429), so the tail read needs no cross-op exclusion. The console source is provider-gated: a
worker-local serial log for local-libvirt, the strict S3-part read seam for remote-libvirt.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import LiteralString, NamedTuple
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.artifacts.registration import register_artifact_row
from kdive.artifacts.storage import ArtifactWriteRequest, StoredArtifact, artifact_key
from kdive.db.locks import LockScope, advisory_xact_lock
from kdive.db.repositories import ARTIFACTS, SYSTEMS
from kdive.domain.capacity.state import SystemState
from kdive.domain.catalog.artifacts import Sensitivity
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import System
from kdive.domain.operations.jobs import Job, JobKind
from kdive.domain.operations.sysrq import SysRqCommand
from kdive.jobs.context import context_from_job as job_context_from_job
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import SysRqPayload, load_payload
from kdive.jobs.provider_context import set_provider_kind
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.console import RemoteConsoleReader
from kdive.providers.ports.lifecycle import Controller
from kdive.providers.shared.runtime_paths import (
    console_log_path,
    domain_name_for,
    read_console_log,
)
from kdive.security import audit
from kdive.security.secrets.redaction import Redactor
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore

# System-owned, redacted, console-class evidence — same owner prefix as console parts, so
# `artifacts.get` serves it and teardown reclaims it (systems.py `sysrq-diagnostic-*` clause).
_TENANT = "local"
_OWNER_KIND = "systems"
_RETENTION_CLASS = "console"

_ARTIFACT_ROW_SQL: LiteralString = (
    "SELECT id FROM artifacts WHERE owner_kind = 'systems' AND owner_id = %s AND object_key = %s"
)

# Bounded, count-driven capture window (no wall-clock, so the poll is deterministic under test).
SEAM_OVERLAP = 4 * 1024
"""Bytes of pre-injection console included before redaction so a secret straddling the capture
start stays contiguous and cannot leak its tail (mirrors console_rotate's seam-overlap carry)."""
POLL_INTERVAL_SECONDS = 0.5
MAX_POLLS = 10
SETTLE_POLLS = 2
"""Consecutive no-growth reads (after growth past the mark) that end the poll as `stabilized`."""

DISABLED_MARKER = b"This sysrq operation is disabled."
"""Substring the kernel prints (under the ``sysrq: `` prefix, `drivers/tty/sysrq.c`) when
``kernel.sysrq`` restricts the requested operation. Matched on the distinctive text because the
prefix is synthesized from ``KBUILD_MODNAME`` at build time (ADR-0292, #952)."""


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The redacted-input console slice and why the capture poll ended."""

    raw: bytes
    exit_reason: str  # "stabilized" | "hit_bound" | "no_output" | "disabled"


async def capture_console_delta(
    read_console: Callable[[], Awaitable[bytes]],
    inject: Callable[[], Awaitable[None]],
    sleep: Callable[[float], Awaitable[None]],
    *,
    seam_overlap: int,
    poll_interval: float,
    max_polls: int,
    settle_polls: int,
) -> CaptureResult:
    """Inject a SysRq and capture the console growth with a bounded, count-driven settle poll.

    Reads the console length before injection (``mark``), injects, then polls for growth up to
    ``max_polls`` times, ending early once the log grows past ``mark`` and stabilizes for
    ``settle_polls`` consecutive reads. Returns the console bytes from ``mark - seam_overlap``
    to the end (the overlap keeps a boundary-straddling secret intact for redaction), plus an
    exit reason: ``stabilized`` (settled), ``hit_bound`` (still growing at ``max_polls`` — a
    possible truncation), ``no_output`` (no growth past ``mark``), or ``disabled`` (the guest
    rejected the SysRq: the post-injection growth carries the kernel's ``kernel.sysrq``-disabled
    marker; ADR-0292, #952). ``disabled`` is decided against the post-``mark`` slice only, so a
    stale marker already in the retained boot log cannot trigger a false failure.
    """
    before = await read_console()
    mark = len(before)
    await inject()

    last_len = mark
    stable = 0
    exit_reason = "hit_bound"
    body = before
    for _ in range(max_polls):
        await sleep(poll_interval)
        body = await read_console()
        if len(body) > last_len:
            last_len = len(body)
            stable = 0
        elif len(body) > mark:
            stable += 1
            if stable >= settle_polls:
                exit_reason = "stabilized"
                break

    if len(body) <= mark:
        return CaptureResult(raw=b"", exit_reason="no_output")
    if DISABLED_MARKER in body[mark:]:
        return CaptureResult(raw=b"", exit_reason="disabled")
    overlap_start = max(0, mark - seam_overlap)
    return CaptureResult(raw=body[overlap_start:], exit_reason=exit_reason)


class _Snapshot(NamedTuple):
    domain_name: str
    project: str
    controller: Controller
    # The strict console read seam when the provider exposes one (remote-libvirt, ADR-0429/0433);
    # ``None`` for a provider whose console is a worker-local file (local-libvirt).
    reader: RemoteConsoleReader | None


def _console_reader(
    conn: AsyncConnection, system_id: UUID, reader: RemoteConsoleReader | None
) -> Callable[[], Awaitable[bytes]]:
    """Pick the console source: the remote read seam when present, else the worker-local log file.

    Remote-libvirt's console lives in reconciler-pumped S3 parts the worker cannot reach as a file
    (ADR-0429). A SysRq capture is one-shot — the whole output is the dump it just triggered — so
    an un-pumped console read is fatal here (unlike the retrying crash-watch): a ``pumped=False``
    window means the console source is unreachable, and returning its empty bytes would masquerade
    as "the kernel printed nothing". It raises so the handler fails with a ``configuration_error``.
    """
    if reader is None:
        return lambda: asyncio.to_thread(read_console_log, console_log_path(system_id))

    async def _read_remote() -> bytes:
        window = await reader.read_window(conn, system_id)
        if not window.pumped:
            raise CategorizedError(
                "the System's console is not being pumped; cannot capture the SysRq dump",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"reason": "console_not_pumped", "system_id": str(system_id)},
            )
        return window.data

    return _read_remote


def _resolved_domain_name(system: System) -> str:
    return system.domain_name or domain_name_for(system.id)


def _changed_state_error(system_id: UUID) -> CategorizedError:
    return CategorizedError(
        "system left the ready local-libvirt state during SysRq capture",
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "system_changed_state", "system_id": str(system_id)},
    )


async def _snapshot(
    conn: AsyncConnection, system_id: UUID, resolver: ProviderResolver
) -> _Snapshot:
    """Under the per-System lock (tx 1): verify READY + SysRq capability, resolve domain+controller.

    The pre-injection mark is read by the capture core just before injection (tighter than a
    lock-held read here), so this snapshot only validates state and resolves the ports.
    """
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.state is not SystemState.READY:
            raise _changed_state_error(system_id)
        binding = await resolver.binding_for_system(conn, system_id)
        set_provider_kind(binding.kind.value)
        if not binding.runtime.support.supports_diagnostic_sysrq:
            raise CategorizedError(
                "provider does not support diagnostic SysRq injection",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={
                    "reason": "diagnostic_sysrq_unsupported",
                    "provider_kind": binding.kind.value,
                },
            )
        console = binding.runtime.console
        reader = (
            console.reader_factory()
            if console is not None and console.reader_factory is not None
            else None
        )
        return _Snapshot(
            domain_name=_resolved_domain_name(system),
            project=system.project,
            controller=binding.runtime.controller,
            reader=reader,
        )


def _put_artifact(store: ObjectStore, system_id: UUID, name: str, data: bytes) -> StoredArtifact:
    return store.put_artifact(
        ArtifactWriteRequest(
            tenant=_TENANT,
            owner_kind=_OWNER_KIND,
            owner_id=str(system_id),
            name=name,
            data=data,
            sensitivity=Sensitivity.REDACTED,
            retention_class=_RETENTION_CLASS,
        )
    )


async def _existing_artifact_id(
    conn: AsyncConnection, system_id: UUID, object_key: str
) -> UUID | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_ARTIFACT_ROW_SQL, (system_id, object_key))
        row = await cur.fetchone()
    return row["id"] if row is not None else None


async def _store_capture(
    conn: AsyncConnection,
    store: ObjectStore,
    job: Job,
    system_id: UUID,
    command: SysRqCommand,
    redacted: bytes,
) -> UUID:
    """Under the per-System lock (tx 2): re-verify state, store the artifact, audit.

    Insert-if-absent on the object key: jobs are at-least-once, so a retry that re-runs the
    handler returns the existing artifact id rather than duplicating the row.
    """
    name = f"sysrq-diagnostic-{job.id}"
    object_key = artifact_key(_TENANT, _OWNER_KIND, str(system_id), name)
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None or system.state is not SystemState.READY:
            raise _changed_state_error(system_id)
        existing = await _existing_artifact_id(conn, system_id, object_key)
        if existing is not None:
            return existing
        stored = await asyncio.to_thread(_put_artifact, store, system_id, name, redacted)
        artifact = register_artifact_row(
            stored, owner_kind=_OWNER_KIND, owner_id=system_id, run_id=None
        )
        await ARTIFACTS.insert(conn, artifact)
        await audit.record(
            conn,
            job_context_from_job(job, system.project),
            audit.AuditEvent(
                tool="control.diagnostic_sysrq",
                object_kind="systems",
                object_id=system_id,
                transition=f"sysrq:{command.value}",
                args={"system_id": str(system_id), "command": command.value},
                project=system.project,
            ),
        )
        return artifact.id


async def diagnostic_sysrq_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore,
) -> str | None:
    """Inject one allowlisted SysRq and store the redacted console dump; return its artifact id.

    Outcomes are observable via the worker's per-kind job telemetry: a captured dump completes
    the job (result_ref = artifact id); ``no_console_output``, ``sysrq_disabled``, and
    ``control_failure`` fail it as ``configuration_error``, so a rising failure rate for
    ``kind=diagnostic_sysrq`` surfaces a silently-broken mechanism (guest lacks the keyboard
    driver, or ``kernel.sysrq`` restricts the requested operation, ADR-0292).
    """
    payload = load_payload(job, SysRqPayload)
    system_id = UUID(payload.system_id)
    command = payload.command
    snapshot = await _snapshot(conn, system_id, resolver)

    async def _inject() -> None:
        await asyncio.to_thread(
            snapshot.controller.diagnostic_sysrq, snapshot.domain_name, command.trigger
        )

    result = await capture_console_delta(
        _console_reader(conn, system_id, snapshot.reader),
        _inject,
        asyncio.sleep,
        seam_overlap=SEAM_OVERLAP,
        poll_interval=POLL_INTERVAL_SECONDS,
        max_polls=MAX_POLLS,
        settle_polls=SETTLE_POLLS,
    )
    if result.exit_reason == "no_output":
        raise CategorizedError(
            "no console output after SysRq injection",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "no_console_output",
                "remediation": (
                    "build the guest kernel with CONFIG_MAGIC_SYSRQ=y (see "
                    "artifacts.feature_config_requirements) and a PS/2 keyboard driver "
                    "(i8042/atkbd), and enable kernel.sysrq in the guest for this command"
                ),
            },
        )
    if result.exit_reason == "disabled":
        raise CategorizedError(
            "the guest rejected the SysRq (kernel.sysrq restricts this operation)",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "sysrq_disabled",
                "remediation": (
                    "permit this SysRq in the guest's kernel.sysrq bitmask "
                    "(e.g. sysctl kernel.sysrq=1 or set the bit for this command)"
                ),
            },
        )
    redactor = Redactor(registry=secret_registry)
    redacted = redactor.redact_text(result.raw.decode("utf-8", "replace")).encode("utf-8")
    artifact_id = await _store_capture(conn, artifact_store, job, system_id, command, redacted)
    return str(artifact_id)


def register_handlers(
    registry: HandlerRegistry,
    *,
    resolver: ProviderResolver,
    secret_registry: SecretRegistry,
    artifact_store: ObjectStore,
) -> None:
    """Bind the ``diagnostic_sysrq`` job handler with its provider, redaction, and store deps."""
    registry.register(
        JobKind.DIAGNOSTIC_SYSRQ,
        lambda conn, job: diagnostic_sysrq_handler(
            conn,
            job,
            resolver=resolver,
            secret_registry=secret_registry,
            artifact_store=artifact_store,
        ),
    )

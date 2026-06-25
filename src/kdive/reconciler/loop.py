"""The reconciler loop: periodic drift repair between Postgres and libvirt (ADR-0021).

A :class:`Reconciler` owns an ``AsyncConnectionPool`` and an :class:`InfraReaper`, and
runs :func:`reconcile_once` on an interval. Each pass runs the repairs — allocation
expiry, orphaned System, abandoned (zombie) job, dead DebugSession, leaked libvirt domain,
idempotency-key GC, and (when an image store is wired) the three image-catalog sweeps:
leaked image objects, dangling image rows, and expired private images — each on a fresh
pooled connection, each fencing its writes, each isolated so one failing repair does not
starve the others. The expiry sweep runs first so an allocation it reclaims orphans its
System in the same pass. Time predicates use Postgres ``now()`` (never a Python clock).
Provider reaper contracts live in :mod:`kdive.providers.infra.reaping`; the Postgres-only repair
path can use ``NullReaper`` there when no provider contributes leaked-infra repair.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import IMAGE_PUBLISH_GRACE
from kdive.mcp.tools.debug.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.core.transport_reset import NullResetter, TransportResetter
from kdive.providers.infra.console_hosting import CollectorRegistry
from kdive.providers.infra.reaping import (
    BuildVmReaper,
    DumpVolumeReaper,
    InfraReaper,
    NullBuildVmReaper,
    NullDumpVolumeReaper,
)
from kdive.providers.shared.build_host.reachability import BuildHostProber
from kdive.reconciler.build_host_fleet import BuildHostTelemetry, read_build_host_snapshot
from kdive.reconciler.cleanup import gc as gc_repairs
from kdive.reconciler.cleanup.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.cleanup.images import (
    repair_leaked_images as _repair_leaked_images,
)
from kdive.reconciler.cleanup.provider_reaping import (
    repair_leaked_domains as _repair_leaked_domains,
)
from kdive.reconciler.cleanup.provider_reaping import (
    repair_leaked_probe_guests as _repair_leaked_probe_guests,
)
from kdive.reconciler.cleanup.runtime_resources import ResourceProbe
from kdive.reconciler.cleanup.runtime_resources import (
    reap_expired_runtime_resources as _reap_expired_runtime_resources,
)
from kdive.reconciler.cleanup.uploads import (
    UploadStore,
)
from kdive.reconciler.cleanup.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from kdive.reconciler.fleet import FleetTelemetry, read_fleet_snapshot
from kdive.reconciler.inventory import InventoryReconcilePass
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
from kdive.reconciler.repairs import allocations as allocation_repairs
from kdive.reconciler.repairs import build_hosts as build_host_repairs
from kdive.reconciler.repairs import debug_sessions as debug_session_repairs
from kdive.reconciler.repairs import jobs as job_repairs
from kdive.reconciler.repairs import systems as system_repairs
from kdive.services.allocation.admission.metrics import AdmissionMetrics
from kdive.services.images.retention import (
    ImageSweepStore,
)
from kdive.services.images.retention import (
    repair_expired_private_images as _repair_expired_private_images,
)

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat

_log = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX_WAIT = allocation_repairs.DEFAULT_QUEUE_MAX_WAIT
DEFAULT_IDEMPOTENCY_RETENTION = gc_repairs.DEFAULT_IDEMPOTENCY_RETENTION
DEFAULT_DUMP_VOLUME_GRACE = gc_repairs.DEFAULT_DUMP_VOLUME_GRACE
DEFAULT_REPORT_ARTIFACT_RETENTION = gc_repairs.DEFAULT_REPORT_ARTIFACT_RETENTION
DEFAULT_INVESTIGATION_CLEANUP_GRACE = gc_repairs.DEFAULT_INVESTIGATION_CLEANUP_GRACE
DEFAULT_BUILD_ARTIFACT_RETENTION = gc_repairs.DEFAULT_BUILD_ARTIFACT_RETENTION

_expire_one = allocation_repairs._expire_one
_gc_idempotency_keys = gc_repairs.gc_idempotency_keys
_gc_report_artifacts = gc_repairs.gc_report_artifacts
_gc_investigation_artifacts = gc_repairs.gc_investigation_artifacts
_gc_expired_build_artifacts = gc_repairs.gc_expired_build_artifacts
_promote_pending = allocation_repairs.promote_pending
_reap_console_collectors = gc_repairs.reap_console_collectors
_reap_orphaned_dump_volumes = gc_repairs.reap_orphaned_dump_volumes
_reap_orphaned_active_allocations = allocation_repairs.reap_orphaned_active_allocations
_reap_queue_timeouts = allocation_repairs.reap_queue_timeouts
_reap_queue_timeouts_for = allocation_repairs.reap_queue_timeouts_for
_reclaim_build_host_leases = build_host_repairs.reclaim_orphan_build_host_leases
_reap_orphan_build_vms = build_host_repairs.reap_orphan_build_vms
_probe_build_host_reachability = build_host_repairs.probe_build_host_reachability
_repair_abandoned_jobs = job_repairs.repair_abandoned_jobs
_repair_dead_sessions = debug_session_repairs.repair_dead_sessions
_repair_orphaned_systems = system_repairs.repair_orphaned_systems
_sweep_expired_allocations = allocation_repairs.sweep_expired_allocations

__all__ = [
    "ReconcileConfig",
    "ReconcileReport",
    "Reconciler",
    "_expire_one",
    "_gc_expired_build_artifacts",
    "_gc_idempotency_keys",
    "_gc_investigation_artifacts",
    "_gc_report_artifacts",
    "_probe_build_host_reachability",
    "_promote_pending",
    "_reap_expired_runtime_resources",
    "_reap_orphaned_active_allocations",
    "_reap_console_collectors",
    "_reap_orphaned_dump_volumes",
    "_reap_queue_timeouts",
    "_reclaim_build_host_leases",
    "_repair_abandoned_jobs",
    "_repair_dead_sessions",
    "_repair_orphaned_systems",
    "_sweep_expired_allocations",
    "reconcile_once",
]

# The default transport resetter (ADR-0086): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_RESETTER: TransportResetter = NullResetter()

# The default dump-volume reaper (ADR-0094): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_DUMP_VOLUME_REAPER: DumpVolumeReaper = NullDumpVolumeReaper()

# The default build-VM reaper (ADR-0100): a module-level stateless singleton (see above).
_NULL_BUILD_VM_REAPER: BuildVmReaper = NullBuildVmReaper()

# The default (no-op) admission metrics (ADR-0190 D): a module-level singleton so it is a
# stateless default field without a per-call construction (ruff B008).
_NULL_ADMISSION_METRICS: AdmissionMetrics = AdmissionMetrics.disabled()

# The default (no-op) debug-session telemetry (ADR-0191 H3): a module-level singleton so it
# is a stateless default field without a per-call construction (ruff B008).
_NULL_DEBUG_SESSION_TELEMETRY: DebugSessionTelemetry = DebugSessionTelemetry.disabled()

# The process-singleton inventory reconcile pass (ADR-0112): held here so its last-good
# parse cache (keyed by the systems.toml hash) survives across reconcile passes — the parse
# step is skipped when the file is unchanged, but the reconcile-against-DB step still runs
# every pass so DB drift is repaired even on an unchanged file.
_INVENTORY_PASS = InventoryReconcilePass()

DEFAULT_INTERVAL = timedelta(seconds=30)
DEFAULT_DEBUG_SESSION_STALE_AFTER = timedelta(minutes=2)
# Fallback image publish-deadline grace when the config setting is unset (its declared
# default is the same 3600s). A pending image row (or an orphan object with no row) is
# protected from the leaked/dangling image sweeps until this window past pending_since/mtime.
DEFAULT_IMAGE_PUBLISH_GRACE = timedelta(seconds=3600)

type _RepairFn = Callable[[AsyncConnection], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class _RepairSpec:
    name: str
    repair: _RepairFn


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """Per-category counts of one pass, plus the names of repairs that raised."""

    expired_allocations: int
    orphaned_systems: int
    abandoned_jobs: int
    dead_sessions: int
    leaked_domains: int
    idempotency_keys_gc_count: int
    failures: tuple[str, ...]
    abandoned_uploads: int = 0
    reconciled_inventory: int = 0
    reaped_active_allocations: int = 0
    promoted_allocations: int = 0
    queue_timeouts: int = 0
    leaked_probe_guests: int = 0
    leaked_images: int = 0
    dangling_images: int = 0
    expired_private_images: int = 0
    console_collectors_reaped: int = 0
    reaped_dump_volumes: int = 0
    reaped_build_vms: int = 0
    reclaimed_build_host_leases: int = 0
    build_host_states_changed: int = 0
    reaped_runtime_resources: int = 0
    investigation_artifacts_gc_count: int = 0
    expired_build_artifacts_gc_count: int = 0
    #: The raw per-kind repair counts, keyed by ``_RepairSpec.name`` (ADR-0190 A). The scalar
    #: fields above feed callers that read named categories; this dict feeds the repairs
    #: counter with the exact spec names so ``repair_kind`` == ``ALL_REPAIR_KINDS``. Excluded
    #: from equality (``compare=False``): it is a derived mirror of the scalar counts, so
    #: existing report-equality assertions stay meaningful without enumerating it.
    repair_counts: Mapping[str, int] = field(default_factory=dict, compare=False)


@dataclass(frozen=True, slots=True)
class ReconcileConfig:
    """Optional reconciler ports and timing values."""

    resetter: TransportResetter = _NULL_RESETTER
    dump_volume_reaper: DumpVolumeReaper = _NULL_DUMP_VOLUME_REAPER
    build_vm_reaper: BuildVmReaper = _NULL_BUILD_VM_REAPER
    build_host_prober: BuildHostProber | None = None
    resource_probe: ResourceProbe | None = None
    upload_store: UploadStore | None = None
    image_store: ImageSweepStore | None = None
    console_registry: CollectorRegistry | None = None
    interval: timedelta = DEFAULT_INTERVAL
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER
    idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION
    report_artifact_retention: timedelta = DEFAULT_REPORT_ARTIFACT_RETENTION
    investigation_cleanup_grace: timedelta = DEFAULT_INVESTIGATION_CLEANUP_GRACE
    build_artifact_retention: timedelta = DEFAULT_BUILD_ARTIFACT_RETENTION
    queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT
    dump_volume_grace: timedelta = DEFAULT_DUMP_VOLUME_GRACE
    heartbeat: Heartbeat | None = None
    heartbeat_tick: timedelta = timedelta(seconds=1)
    telemetry: ReconcilerTelemetry | None = None
    fleet_telemetry: FleetTelemetry | None = None
    build_host_telemetry: BuildHostTelemetry | None = None
    admission_metrics: AdmissionMetrics = field(default=_NULL_ADMISSION_METRICS)
    debug_session_telemetry: DebugSessionTelemetry = field(default=_NULL_DEBUG_SESSION_TELEMETRY)


_DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


def _repair_plan(
    *,
    reaper: InfraReaper,
    config: ReconcileConfig,
    image_publish_grace: timedelta,
) -> tuple[_RepairSpec, ...]:
    repairs = [
        _RepairSpec("expired_allocations", _sweep_expired_allocations),
        # Release leaked `active` allocations whose System is terminal/absent (ADR-0109) BEFORE
        # the promotion sweep, so a host-cap slot this reaper frees is filled in the same pass.
        _RepairSpec("reaped_active_allocations", _reap_orphaned_active_allocations),
        _RepairSpec(
            "promoted_allocations",
            lambda conn: _promote_pending(conn, config.admission_metrics),
        ),
        _RepairSpec(
            "queue_timeouts",
            _reap_queue_timeouts_for(config.queue_max_wait, config.admission_metrics),
        ),
        _RepairSpec("orphaned_systems", _repair_orphaned_systems),
        _RepairSpec("abandoned_jobs", _repair_abandoned_jobs),
        # Reap (or cordon, if still live) runtime resources whose lease lapsed — the leak
        # backstop for an agent that registered capacity then vanished (ADR-0112). Cordon-only
        # / refuse-if-live: a live allocation is never auto-drained.
        _RepairSpec(
            "reaped_runtime_resources",
            lambda conn: _reap_expired_runtime_resources(conn, config.resource_probe),
        ),
        # Reap leaked build VMs BEFORE reclaiming their lease, so a freed slot never coexists
        # with a still-running leaked VM (ADR-0100 §4.6 over-admission window).
        _RepairSpec(
            "reaped_build_vms",
            lambda conn: _reap_orphan_build_vms(conn, config.build_vm_reaper),
        ),
        _RepairSpec("reclaimed_build_host_leases", _reclaim_build_host_leases),
        _RepairSpec(
            "dead_sessions",
            lambda conn: _repair_dead_sessions(
                conn,
                config.debug_session_stale_after,
                config.resetter,
                config.debug_session_telemetry,
            ),
        ),
        _RepairSpec("leaked_domains", lambda conn: _repair_leaked_domains(conn, reaper)),
        _RepairSpec("leaked_probe_guests", lambda conn: _repair_leaked_probe_guests(conn, reaper)),
        _RepairSpec(
            "idempotency_keys_gc_count",
            lambda conn: _gc_idempotency_keys(conn, config.idempotency_retention),
        ),
        _RepairSpec(
            "reaped_dump_volumes",
            lambda conn: _reap_orphaned_dump_volumes(
                conn, config.dump_volume_reaper, config.dump_volume_grace
            ),
        ),
    ]
    if config.build_host_prober is not None:
        build_host_prober = config.build_host_prober
        repairs.append(
            _RepairSpec(
                "build_host_states_changed",
                lambda conn: _probe_build_host_reachability(conn, build_host_prober),
            )
        )
    if config.upload_store is not None:
        upload_store = config.upload_store
        report_retention = config.report_artifact_retention
        repairs.append(
            _RepairSpec(
                "abandoned_uploads",
                lambda conn: _repair_abandoned_uploads(conn, upload_store),
            )
        )
        # Reap report spreadsheet artifacts past retention (ADR-0212); the synthetic report
        # owner has no teardown trigger, so this sweep is their only cleanup path.
        repairs.append(
            _RepairSpec(
                "report_artifacts_gc_count",
                lambda conn: _gc_report_artifacts(conn, upload_store, report_retention),
            )
        )
        # Reclaim run-owned uploaded build artifacts: clear-on-close (grace-gated by the
        # investigation cleanup marker) and a TTL backstop for never-closed investigations
        # (ADR-0234 §4, #768). Never touch console/crash evidence (system-owned).
        cleanup_grace = config.investigation_cleanup_grace
        build_retention = config.build_artifact_retention
        repairs.append(
            _RepairSpec(
                "investigation_artifacts_gc_count",
                lambda conn: _gc_investigation_artifacts(conn, upload_store, cleanup_grace),
            )
        )
        repairs.append(
            _RepairSpec(
                "expired_build_artifacts_gc_count",
                lambda conn: _gc_expired_build_artifacts(conn, upload_store, build_retention),
            )
        )
    if config.console_registry is not None:
        console_registry = config.console_registry
        repairs.append(
            _RepairSpec(
                "console_collectors_reaped",
                lambda conn: _reap_console_collectors(conn, console_registry),
            )
        )
    if config.image_store is not None:
        image_store = config.image_store
        repairs.append(_RepairSpec("reconcile_inventory", _INVENTORY_PASS.make_repair(image_store)))
        repairs.extend(
            (
                _RepairSpec(
                    "leaked_images",
                    lambda conn: _repair_leaked_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "dangling_images",
                    lambda conn: _repair_dangling_images(conn, image_store, image_publish_grace),
                ),
                _RepairSpec(
                    "expired_private_images",
                    lambda conn: _repair_expired_private_images(conn, image_store),
                ),
            )
        )
    return tuple(repairs)


#: Every ``repair_kind`` the repairs counter can emit — the union of the base repairs and the
#: optional-port repairs (ADR-0190 A). Pinned to :func:`_repair_plan` by
#: ``test_all_repair_kinds_matches_a_fully_populated_plan`` so the cardinality bound and the
#: plan never drift. Bounded and low-cardinality; never a per-object identifier.
ALL_REPAIR_KINDS: tuple[str, ...] = (
    "expired_allocations",
    "reaped_active_allocations",
    "promoted_allocations",
    "queue_timeouts",
    "orphaned_systems",
    "abandoned_jobs",
    "reaped_runtime_resources",
    "reaped_build_vms",
    "reclaimed_build_host_leases",
    "dead_sessions",
    "leaked_domains",
    "leaked_probe_guests",
    "idempotency_keys_gc_count",
    "reaped_dump_volumes",
    "build_host_states_changed",
    "abandoned_uploads",
    "report_artifacts_gc_count",
    "investigation_artifacts_gc_count",
    "expired_build_artifacts_gc_count",
    "console_collectors_reaped",
    "reconcile_inventory",
    "leaked_images",
    "dangling_images",
    "expired_private_images",
)


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    config: ReconcileConfig = _DEFAULT_RECONCILE_CONFIG,
) -> ReconcileReport:
    """Run the repairs once, each isolated, each on a fresh pooled connection.

    A repair that raises is logged, its name recorded in ``failures``, and the pass
    continues — one repair never starves the others. Returns the partial counts.

    The ``→expired`` allocation sweep runs **first** so that the allocations it moves to
    ``expired`` are seen as orphaning their System by :func:`_repair_orphaned_systems` in
    the **same** pass (ADR-0036 §4). The **promotion sweep runs right after the expiry
    sweep** so a slot a lease just freed is filled in the same pass; the
    **queue_timeout reaper runs after the promotion sweep** so every aged request already had
    its placement chance this pass (ADR-0069). The idempotency-key GC runs last.

    Counts are **best-effort**: a repair that commits some work and then raises (e.g. a
    transient DB error in a later iteration) reports ``0`` for its category and appears
    in ``failures`` — the committed work stands but is not reflected in the count. The
    per-domain ``destroy`` in :func:`_repair_leaked_domains` is caught individually, so
    the irreversible case (a domain destroyed, then a later failure) keeps its count.
    """
    counts, failures = await _run_repair_plan(
        pool,
        _repair_plan(
            reaper=reaper,
            config=config,
            image_publish_grace=_image_publish_grace(),
        ),
    )

    return ReconcileReport(
        expired_allocations=counts["expired_allocations"],
        orphaned_systems=counts["orphaned_systems"],
        abandoned_jobs=counts["abandoned_jobs"],
        dead_sessions=counts["dead_sessions"],
        leaked_domains=counts["leaked_domains"],
        idempotency_keys_gc_count=counts["idempotency_keys_gc_count"],
        failures=tuple(failures),
        abandoned_uploads=counts["abandoned_uploads"],
        reconciled_inventory=counts.get("reconcile_inventory", 0),
        reaped_active_allocations=counts["reaped_active_allocations"],
        promoted_allocations=counts["promoted_allocations"],
        queue_timeouts=counts["queue_timeouts"],
        leaked_probe_guests=counts["leaked_probe_guests"],
        leaked_images=counts.get("leaked_images", 0),
        dangling_images=counts.get("dangling_images", 0),
        expired_private_images=counts.get("expired_private_images", 0),
        console_collectors_reaped=counts.get("console_collectors_reaped", 0),
        reaped_dump_volumes=counts.get("reaped_dump_volumes", 0),
        reaped_build_vms=counts.get("reaped_build_vms", 0),
        reclaimed_build_host_leases=counts["reclaimed_build_host_leases"],
        build_host_states_changed=counts.get("build_host_states_changed", 0),
        reaped_runtime_resources=counts["reaped_runtime_resources"],
        investigation_artifacts_gc_count=counts.get("investigation_artifacts_gc_count", 0),
        expired_build_artifacts_gc_count=counts.get("expired_build_artifacts_gc_count", 0),
        repair_counts=dict(counts),
    )


def _image_publish_grace() -> timedelta:
    """Resolve the image publish-deadline grace from config (default 3600s)."""
    seconds = config.get(IMAGE_PUBLISH_GRACE)
    if seconds is None:
        return DEFAULT_IMAGE_PUBLISH_GRACE
    return timedelta(seconds=seconds)


async def _run_repair_plan(
    pool: AsyncConnectionPool, repairs: tuple[_RepairSpec, ...]
) -> tuple[dict[str, int], list[str]]:
    counts = {spec.name: 0 for spec in repairs}
    counts.setdefault("abandoned_uploads", 0)
    failures: list[str] = []
    for spec in repairs:
        try:
            async with pool.connection() as conn:
                counts[spec.name] = await spec.repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", spec.name, exc_info=True)
            failures.append(spec.name)
    return counts, failures


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        config: ReconcileConfig = _DEFAULT_RECONCILE_CONFIG,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._config = config
        self._heartbeat_tick = config.heartbeat_tick.total_seconds()
        self._telemetry = config.telemetry or ReconcilerTelemetry.disabled()
        self._fleet_telemetry = config.fleet_telemetry or FleetTelemetry.disabled()
        self._build_host_telemetry = config.build_host_telemetry or BuildHostTelemetry.disabled()

    async def run_once(self) -> ReconcileReport:
        """Run one reconciliation pass."""
        return await reconcile_once(
            self._pool,
            self._reaper,
            config=self._config,
        )

    async def run(self, stop: asyncio.Event) -> None:
        """Loop :meth:`run_once` every ``interval``, surviving a transient pass error.

        The ``/livez`` heartbeat is bumped by a **background ticker** at
        :attr:`_heartbeat_tick` cadence (ADR-0090 §5), *not* per pass — so a single slow
        pass (an over-interval idempotency GC or a large domain sweep) never makes the
        reconciler read not-live; liveness tracks the event loop, not a repair. A wedged
        event loop stops the ticker too and ``/livez`` goes stale. Each pass also opens a
        span and records its duration plus the reconcile-lag (the gap between the
        scheduled and actual start, which grows when a pass overruns its interval).

        ``reconcile_once`` already isolates each repair, so a raise here is a rare
        whole-pass failure (e.g. pool acquisition); it is logged and the loop continues
        — a durable reconciler must not die on one bad pass.
        """
        ticker = self._start_heartbeat_ticker(stop)
        try:
            await self._pass_loop(stop)
        finally:
            if ticker is not None:
                ticker.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ticker

    async def _refresh_fleet_snapshot(self) -> None:
        """Read the fleet inventory + capacity into the gauge cache (ADR-0190 B; best-effort).

        A read failure is logged and leaves the previous cached snapshot in place — the
        inventory gauges are observability, never load-bearing for the repair pass.
        """
        try:
            async with self._pool.connection() as conn:
                snapshot = await read_fleet_snapshot(conn)
        except Exception:  # noqa: BLE001 - a snapshot read must never starve the repair loop
            _log.warning("reconciler: fleet snapshot read failed this pass", exc_info=True)
            return
        self._fleet_telemetry.refresh(snapshot)

    async def _refresh_build_host_snapshot(self) -> None:
        """Read build-host lease/capacity/reachability into the gauge cache (ADR-0191 G2/G3).

        Best-effort: a read failure is logged and leaves the previous cached snapshot in place
        — the build-host gauges are observability, never load-bearing for the repair pass.
        """
        try:
            async with self._pool.connection() as conn:
                snapshot = await read_build_host_snapshot(conn)
        except Exception:  # noqa: BLE001 - a snapshot read must never starve the repair loop
            _log.warning("reconciler: build-host snapshot read failed this pass", exc_info=True)
            return
        self._build_host_telemetry.refresh(snapshot)

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._config.heartbeat is None:
            return None
        return asyncio.create_task(
            _tick_until_stop(self._config.heartbeat, stop, self._heartbeat_tick)
        )

    async def _pass_loop(self, stop: asyncio.Event) -> None:
        interval = self._config.interval.total_seconds()
        next_due = time.monotonic()
        while not stop.is_set():
            self._telemetry.observe_lag(time.monotonic() - next_due)
            with self._telemetry.pass_span() as span:
                try:
                    report = await self.run_once()
                    self._telemetry.record_repairs(report.repair_counts, report.failures)
                except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                    span.set_outcome("error")
                    _log.exception("reconcile pass failed; continuing after %ss", interval)
            await self._refresh_fleet_snapshot()
            await self._refresh_build_host_snapshot()
            next_due = time.monotonic() + interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)


async def _tick_until_stop(heartbeat: Heartbeat, stop: asyncio.Event, interval: float) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the pass loop so a long-running pass never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck reconciler still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval)
        if stop.is_set():
            break
        heartbeat.tick()

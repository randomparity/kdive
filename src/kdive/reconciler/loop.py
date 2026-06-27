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
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import IMAGE_PUBLISH_GRACE
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
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
from kdive.services.allocation import promotion as allocation_promotion
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
_promote_pending = allocation_promotion.promote_pending
_reap_console_collectors = gc_repairs.reap_console_collectors
_reap_orphaned_dump_volumes = gc_repairs.reap_orphaned_dump_volumes
_reap_orphaned_active_allocations = allocation_repairs.reap_orphaned_active_allocations
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
class _RepairCatalogEntry:
    name: str
    factory: Callable[[InfraReaper, ReconcileConfig, timedelta], _RepairFn | None]
    report_field: str | None = None


async def _sleep_until_stop(stop: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=timeout)


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

    @classmethod
    def from_counts(cls, counts: Mapping[str, int], failures: Sequence[str]) -> ReconcileReport:
        full_counts = _repair_count_defaults(counts)
        return cls(
            expired_allocations=_report_count(full_counts, "expired_allocations"),
            orphaned_systems=_report_count(full_counts, "orphaned_systems"),
            abandoned_jobs=_report_count(full_counts, "abandoned_jobs"),
            dead_sessions=_report_count(full_counts, "dead_sessions"),
            leaked_domains=_report_count(full_counts, "leaked_domains"),
            idempotency_keys_gc_count=_report_count(full_counts, "idempotency_keys_gc_count"),
            failures=tuple(failures),
            abandoned_uploads=_report_count(full_counts, "abandoned_uploads"),
            reconciled_inventory=_report_count(full_counts, "reconciled_inventory"),
            reaped_active_allocations=_report_count(full_counts, "reaped_active_allocations"),
            promoted_allocations=_report_count(full_counts, "promoted_allocations"),
            queue_timeouts=_report_count(full_counts, "queue_timeouts"),
            leaked_probe_guests=_report_count(full_counts, "leaked_probe_guests"),
            leaked_images=_report_count(full_counts, "leaked_images"),
            dangling_images=_report_count(full_counts, "dangling_images"),
            expired_private_images=_report_count(full_counts, "expired_private_images"),
            console_collectors_reaped=_report_count(full_counts, "console_collectors_reaped"),
            reaped_dump_volumes=_report_count(full_counts, "reaped_dump_volumes"),
            reaped_build_vms=_report_count(full_counts, "reaped_build_vms"),
            reclaimed_build_host_leases=_report_count(full_counts, "reclaimed_build_host_leases"),
            build_host_states_changed=_report_count(full_counts, "build_host_states_changed"),
            reaped_runtime_resources=_report_count(full_counts, "reaped_runtime_resources"),
            investigation_artifacts_gc_count=_report_count(
                full_counts, "investigation_artifacts_gc_count"
            ),
            expired_build_artifacts_gc_count=_report_count(
                full_counts, "expired_build_artifacts_gc_count"
            ),
            repair_counts=full_counts,
        )


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
    heartbeat_sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]] = (
        _sleep_until_stop
    )
    telemetry: ReconcilerTelemetry | None = None
    fleet_telemetry: FleetTelemetry | None = None
    build_host_telemetry: BuildHostTelemetry | None = None
    admission_metrics: AdmissionMetrics = field(default=_NULL_ADMISSION_METRICS)
    debug_session_telemetry: DebugSessionTelemetry = field(default=_NULL_DEBUG_SESSION_TELEMETRY)


_DEFAULT_RECONCILE_CONFIG = ReconcileConfig()


def _reconcile_inventory_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    image_store = config.image_store
    if image_store is None:
        return None
    return _INVENTORY_PASS.make_repair(image_store)


def _leaked_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, image_publish_grace: timedelta
) -> _RepairFn | None:
    image_store = config.image_store
    if image_store is None:
        return None
    return lambda conn: _repair_leaked_images(conn, image_store, image_publish_grace)


def _dangling_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, image_publish_grace: timedelta
) -> _RepairFn | None:
    image_store = config.image_store
    if image_store is None:
        return None
    return lambda conn: _repair_dangling_images(conn, image_store, image_publish_grace)


def _expired_private_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    image_store = config.image_store
    if image_store is None:
        return None
    return lambda conn: _repair_expired_private_images(conn, image_store)


def _build_host_states_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    build_host_prober = config.build_host_prober
    if build_host_prober is None:
        return None
    return lambda conn: _probe_build_host_reachability(conn, build_host_prober)


def _abandoned_uploads_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    upload_store = config.upload_store
    if upload_store is None:
        return None
    return lambda conn: _repair_abandoned_uploads(conn, upload_store)


def _report_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    upload_store = config.upload_store
    if upload_store is None:
        return None
    return lambda conn: _gc_report_artifacts(conn, upload_store, config.report_artifact_retention)


def _investigation_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    upload_store = config.upload_store
    if upload_store is None:
        return None
    return lambda conn: _gc_investigation_artifacts(
        conn, upload_store, config.investigation_cleanup_grace
    )


def _expired_build_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    upload_store = config.upload_store
    if upload_store is None:
        return None
    return lambda conn: _gc_expired_build_artifacts(
        conn, upload_store, config.build_artifact_retention
    )


def _console_collectors_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    console_registry = config.console_registry
    if console_registry is None:
        return None
    return lambda conn: _reap_console_collectors(conn, console_registry)


_REPAIR_CATALOG: tuple[_RepairCatalogEntry, ...] = (
    _RepairCatalogEntry("expired_allocations", lambda _r, _c, _g: _sweep_expired_allocations),
    _RepairCatalogEntry(
        "reaped_active_allocations", lambda _r, _c, _g: _reap_orphaned_active_allocations
    ),
    _RepairCatalogEntry(
        "promoted_allocations",
        lambda _r, c, _g: lambda conn: _promote_pending(conn, c.admission_metrics),
    ),
    _RepairCatalogEntry(
        "queue_timeouts",
        lambda _r, c, _g: _reap_queue_timeouts_for(c.queue_max_wait, c.admission_metrics),
    ),
    _RepairCatalogEntry("orphaned_systems", lambda _r, _c, _g: _repair_orphaned_systems),
    _RepairCatalogEntry("abandoned_jobs", lambda _r, _c, _g: _repair_abandoned_jobs),
    _RepairCatalogEntry(
        "reaped_runtime_resources",
        lambda _r, c, _g: lambda conn: _reap_expired_runtime_resources(conn, c.resource_probe),
    ),
    _RepairCatalogEntry(
        "reaped_build_vms",
        lambda _r, c, _g: lambda conn: _reap_orphan_build_vms(conn, c.build_vm_reaper),
    ),
    _RepairCatalogEntry(
        "reclaimed_build_host_leases", lambda _r, _c, _g: _reclaim_build_host_leases
    ),
    _RepairCatalogEntry(
        "dead_sessions",
        lambda _r, c, _g: (
            lambda conn: _repair_dead_sessions(
                conn,
                c.debug_session_stale_after,
                c.resetter,
                c.debug_session_telemetry,
            )
        ),
    ),
    _RepairCatalogEntry(
        "leaked_domains", lambda r, _c, _g: lambda conn: _repair_leaked_domains(conn, r)
    ),
    _RepairCatalogEntry(
        "leaked_probe_guests", lambda r, _c, _g: lambda conn: _repair_leaked_probe_guests(conn, r)
    ),
    _RepairCatalogEntry(
        "idempotency_keys_gc_count",
        lambda _r, c, _g: lambda conn: _gc_idempotency_keys(conn, c.idempotency_retention),
    ),
    _RepairCatalogEntry(
        "reaped_dump_volumes",
        lambda _r, c, _g: (
            lambda conn: _reap_orphaned_dump_volumes(
                conn, c.dump_volume_reaper, c.dump_volume_grace
            )
        ),
    ),
    _RepairCatalogEntry("build_host_states_changed", _build_host_states_repair),
    _RepairCatalogEntry("abandoned_uploads", _abandoned_uploads_repair),
    _RepairCatalogEntry("report_artifacts_gc_count", _report_artifacts_gc_repair),
    _RepairCatalogEntry("investigation_artifacts_gc_count", _investigation_artifacts_gc_repair),
    _RepairCatalogEntry("expired_build_artifacts_gc_count", _expired_build_artifacts_gc_repair),
    _RepairCatalogEntry("console_collectors_reaped", _console_collectors_repair),
    _RepairCatalogEntry("reconcile_inventory", _reconcile_inventory_repair, "reconciled_inventory"),
    _RepairCatalogEntry("leaked_images", _leaked_images_repair),
    _RepairCatalogEntry("dangling_images", _dangling_images_repair),
    _RepairCatalogEntry("expired_private_images", _expired_private_images_repair),
)


def _repair_plan(
    *,
    reaper: InfraReaper,
    config: ReconcileConfig,
    image_publish_grace: timedelta,
) -> tuple[_RepairSpec, ...]:
    repairs: list[_RepairSpec] = []
    for entry in _REPAIR_CATALOG:
        repair = entry.factory(reaper, config, image_publish_grace)
        if repair is not None:
            repairs.append(_RepairSpec(entry.name, repair))
    return tuple(repairs)


#: Every ``repair_kind`` the repairs counter can emit — the union of the base repairs and the
#: optional-port repairs (ADR-0190 A). Pinned to :func:`_repair_plan` by
#: ``test_all_repair_kinds_matches_a_fully_populated_plan`` so the cardinality bound and the
#: plan never drift. Bounded and low-cardinality; never a per-object identifier.
ALL_REPAIR_KINDS: tuple[str, ...] = tuple(entry.name for entry in _REPAIR_CATALOG)

_REPORT_FIELD_TO_REPAIR_KIND = {
    entry.report_field or entry.name: entry.name for entry in _REPAIR_CATALOG
}


def _repair_count_defaults(counts: Mapping[str, int]) -> dict[str, int]:
    return {repair_kind: counts.get(repair_kind, 0) for repair_kind in ALL_REPAIR_KINDS}


def _report_count(counts: Mapping[str, int], report_field: str) -> int:
    return counts[_REPORT_FIELD_TO_REPAIR_KIND[report_field]]


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

    return ReconcileReport.from_counts(counts, failures)


def _image_publish_grace() -> timedelta:
    """Resolve the image publish-deadline grace from config (default 3600s)."""
    seconds = config.get(IMAGE_PUBLISH_GRACE)
    if seconds is None:
        return DEFAULT_IMAGE_PUBLISH_GRACE
    return timedelta(seconds=seconds)


async def _run_repair_plan(
    pool: AsyncConnectionPool, repairs: tuple[_RepairSpec, ...]
) -> tuple[dict[str, int], list[str]]:
    counts = _repair_count_defaults({})
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
            _tick_until_stop(
                self._config.heartbeat,
                stop,
                self._heartbeat_tick,
                self._config.heartbeat_sleep_until_stop,
            )
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


async def _tick_until_stop(
    heartbeat: Heartbeat,
    stop: asyncio.Event,
    interval: float,
    sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]] = _sleep_until_stop,
) -> None:
    """Bump ``heartbeat`` every ``interval`` seconds until ``stop`` is set or cancelled.

    Runs concurrently with the pass loop so a long-running pass never starves the
    ``/livez`` signal (ADR-0090 §5); a wedged event loop stops this ticker too, so a truly
    stuck reconciler still reads not-live.
    """
    heartbeat.tick()
    while not stop.is_set():
        await sleep_until_stop(stop, interval)
        if stop.is_set():
            break
        heartbeat.tick()

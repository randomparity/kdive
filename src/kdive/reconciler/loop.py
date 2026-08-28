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
from types import MappingProxyType
from typing import Any, Protocol, cast

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.artifacts.uploads.write_lease import reap_stale_write_leases as _reap_stale_write_leases
from kdive.config.core_settings import (
    IMAGE_PUBLISH_GRACE,
    UPLOAD_ORPHAN_GRACE,
    UPLOAD_TTL_SECONDS,
)
from kdive.health.heartbeat import Heartbeat, tick_until_stop
from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
from kdive.providers.core.transport_reset import NullResetter, TransportResetter
from kdive.providers.infra.reaping import (
    CaptureReaper,
    DumpVolumeReaper,
    InfraReaper,
    NullDumpVolumeReaper,
)
from kdive.reconciler.cleanup import idempotency
from kdive.reconciler.cleanup.artifacts import artifact_retention, investigation_rootfs
from kdive.reconciler.cleanup.images import (
    repair_dangling_images as _repair_dangling_images,
)
from kdive.reconciler.cleanup.images import (
    repair_leaked_images as _repair_leaked_images,
)
from kdive.reconciler.cleanup.provider_resources.capture_reaping import (
    DEFAULT_CAPTURE_REAP_BATCH,
    DEFAULT_CAPTURE_RETRY_BASE,
    DEFAULT_CAPTURE_RETRY_CAP,
    DEFAULT_CAPTURE_SETTLE,
)
from kdive.reconciler.cleanup.provider_resources.capture_reaping import (
    reap_orphaned_captures as _reap_orphaned_captures,
)
from kdive.reconciler.cleanup.provider_resources.console_reaping import (
    reap_console_collectors as _reap_console_collectors,
)
from kdive.reconciler.cleanup.provider_resources.dump_volume_reaping import (
    DEFAULT_DUMP_VOLUME_GRACE,
)
from kdive.reconciler.cleanup.provider_resources.dump_volume_reaping import (
    reap_orphaned_dump_volumes as _reap_orphaned_dump_volumes,
)
from kdive.reconciler.cleanup.provider_resources.provider_domain_reaping import (
    repair_leaked_domains as _repair_leaked_domains,
)
from kdive.reconciler.cleanup.provider_resources.provider_domain_reaping import (
    repair_leaked_probe_guests as _repair_leaked_probe_guests,
)
from kdive.reconciler.cleanup.provider_resources.reaping_common import (
    DEFAULT_LANE_BUDGET,
    ReapLaneOutcome,
)
from kdive.reconciler.cleanup.provider_resources.runtime_resources import ResourceProbe
from kdive.reconciler.cleanup.provider_resources.runtime_resources import (
    reap_expired_runtime_resources as _reap_expired_runtime_resources,
)
from kdive.reconciler.cleanup.system_object_versions import (
    SystemObjectHostingGate,
    SystemObjectVersionStore,
    sweep_local_system_object_versions,
    sweep_remote_system_object_versions,
)
from kdive.reconciler.cleanup.uploads.upload_orphans import (
    UploadOrphanStore,
)
from kdive.reconciler.cleanup.uploads.upload_orphans import (
    repair_leaked_upload_objects as _repair_leaked_upload_objects,
)
from kdive.reconciler.cleanup.uploads.uploads import (
    UploadStore,
)
from kdive.reconciler.cleanup.uploads.uploads import (
    repair_abandoned_uploads as _repair_abandoned_uploads,
)
from kdive.reconciler.fleet import FleetTelemetry, read_fleet_snapshot
from kdive.reconciler.inventory import InventoryReconcilePass
from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
from kdive.reconciler.repairs import allocations as allocation_repairs
from kdive.reconciler.repairs import console_rotation as console_rotation_repairs
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

_log = logging.getLogger(__name__)

DEFAULT_QUEUE_MAX_WAIT = allocation_repairs.DEFAULT_QUEUE_MAX_WAIT
DEFAULT_CRASHED_IDLE_GRACE = allocation_repairs.DEFAULT_CRASHED_IDLE_GRACE
DEFAULT_IDEMPOTENCY_RETENTION = idempotency.DEFAULT_IDEMPOTENCY_RETENTION
DEFAULT_REPORT_ARTIFACT_RETENTION = artifact_retention.DEFAULT_REPORT_ARTIFACT_RETENTION
DEFAULT_INVESTIGATION_CLEANUP_GRACE = artifact_retention.DEFAULT_INVESTIGATION_CLEANUP_GRACE
DEFAULT_BUILD_ARTIFACT_RETENTION = artifact_retention.DEFAULT_BUILD_ARTIFACT_RETENTION
DEFAULT_INVESTIGATION_ROOTFS_RETENTION = investigation_rootfs.DEFAULT_INVESTIGATION_ROOTFS_RETENTION

_expire_one = allocation_repairs._expire_one
_gc_idempotency_keys = idempotency.gc_idempotency_keys
_gc_report_artifacts = artifact_retention.gc_report_artifacts
_gc_system_artifacts = artifact_retention.gc_system_artifacts
_gc_investigation_artifacts = artifact_retention.gc_investigation_artifacts
_gc_expired_build_artifacts = artifact_retention.gc_expired_build_artifacts
_sweep_investigation_rootfs_reclaim = investigation_rootfs.sweep_investigation_rootfs_reclaim
_sweep_expired_investigation_rootfs_reclaim = (
    investigation_rootfs.sweep_expired_investigation_rootfs_reclaim
)
_sweep_unowned_investigation_rootfs_staging = (
    investigation_rootfs.sweep_unowned_investigation_rootfs_staging
)
_promote_pending = allocation_promotion.promote_pending
_reap_orphaned_active_allocations = allocation_repairs.reap_orphaned_active_allocations
_reap_queue_timeouts_for = allocation_repairs.reap_queue_timeouts_for
_repair_abandoned_jobs = job_repairs.repair_abandoned_jobs
_repair_dead_sessions = debug_session_repairs.repair_dead_sessions
_repair_orphaned_systems = system_repairs.repair_orphaned_systems
_repair_stalled_crashing_systems = system_repairs.repair_stalled_crashing_systems
_repair_stalled_restoring_systems = system_repairs.repair_stalled_restoring_systems
_repair_stalled_creating_snapshots = system_repairs.repair_stalled_creating_snapshots
_sweep_expired_allocations = allocation_repairs.sweep_expired_allocations
_sweep_console_rotation = console_rotation_repairs.sweep_console_rotation

__all__ = [
    "ALL_REPAIR_KINDS",
    "ReconcileConfig",
    "ReconcileReport",
    "ReconcileUploadStore",
    "Reconciler",
    "SystemObjectHostingGate",
    "UploadOrphanStore",
    "UploadStore",
    "reconcile_once",
]

# The default transport resetter (ADR-0086): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_RESETTER: TransportResetter = NullResetter()

# The default dump-volume reaper (ADR-0094): a module-level singleton so it can be a
# stateless default argument without a per-call construction (ruff B008).
_NULL_DUMP_VOLUME_REAPER: DumpVolumeReaper = NullDumpVolumeReaper()

# The default capture-reaper registry: empty, not a mapping of Null reapers (ADR-0556). A
# deployment that composes no provider must reap no capture, and an empty registry makes that a
# selection property rather than something a no-op call has to be trusted to honour.
_NO_CAPTURE_REAPERS: Mapping[str, CaptureReaper] = MappingProxyType({})


class ReconcileUploadStore(
    UploadOrphanStore,
    UploadStore,
    SystemObjectVersionStore,
    artifact_retention.ArtifactObjectDeleter,
    Protocol,
):
    """Object-store surface required by upload and artifact-retention reconciler lanes.

    Investigation build generations additionally use the inherited store's exact-version
    deletion operation; legacy artifact lanes retain bounded whole-key retirement.
    """


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
#: The two budgeted reaping lanes return a full outcome instead of a bare count (#1982); the
#: plan runner unpacks it into the reaped count plus the report's per-lane signal fields.
type _LaneRepairFn = Callable[[AsyncConnection], Awaitable[ReapLaneOutcome]]
type _AnyRepairFn = _RepairFn | _LaneRepairFn


@dataclass(frozen=True, slots=True)
class _RepairSpec:
    name: str
    repair: _AnyRepairFn


@dataclass(frozen=True, slots=True)
class _RepairCatalogEntry:
    name: str
    factory: Callable[[InfraReaper, ReconcileConfig, timedelta], _AnyRepairFn | None]
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
    local_system_object_versions_deleted: int = 0
    remote_system_object_versions_deleted: int = 0
    reaped_dump_volumes: int = 0
    reaped_captures: int = 0
    #: Candidates the capture lane's ADR-0565 pass budget stopped it from starting (#1982).
    #: Signal, not failure — never folded into ``reaped_captures`` and never ``kdive.errors``.
    captures_budget_unattempted: int = 0
    #: Same, for the dump-volume lane.
    dump_volumes_budget_unattempted: int = 0
    reaped_runtime_resources: int = 0
    investigation_artifacts_gc_count: int = 0
    expired_build_artifacts_gc_count: int = 0
    investigation_rootfs_reclaims_enqueued: int = 0
    expired_investigation_rootfs_reclaims_enqueued: int = 0
    unowned_investigation_rootfs_staging_drains_enqueued: int = 0
    #: The raw per-kind repair counts, keyed by ``_RepairSpec.name`` (ADR-0190 A). The scalar
    #: fields above feed callers that read named categories; this dict feeds the repairs
    #: counter with the exact spec names so ``repair_kind`` == ``ALL_REPAIR_KINDS``. Excluded
    #: from equality (``compare=False``): it is a derived mirror of the scalar counts, so
    #: existing report-equality assertions stay meaningful without enumerating it.
    repair_counts: Mapping[str, int] = field(default_factory=dict, compare=False)

    @classmethod
    def from_counts(
        cls,
        counts: Mapping[str, int],
        failures: Sequence[str],
        lane_outcomes: Mapping[str, ReapLaneOutcome] | None = None,
    ) -> ReconcileReport:
        """Build a report from repair counts keyed by ``_RepairSpec.name``.

        ``lane_outcomes`` optionally carries the :class:`ReapLaneOutcome` of the two budgeted
        reaping lanes, keyed by their repair-kind names; the per-lane unattempted counts land on
        the report's scalar fields and everything else on the report is unchanged (#1982).
        """
        full_counts = _repair_count_defaults(counts)
        report_counts = cast("dict[str, Any]", _report_field_counts(full_counts))
        outcomes = lane_outcomes or {}
        capture_outcome = outcomes.get("reaped_captures")
        volume_outcome = outcomes.get("reaped_dump_volumes")
        return cls(
            captures_budget_unattempted=(
                capture_outcome.budget_unattempted if capture_outcome else 0
            ),
            dump_volumes_budget_unattempted=(
                volume_outcome.budget_unattempted if volume_outcome else 0
            ),
            failures=tuple(failures),
            repair_counts=full_counts,
            **report_counts,
        )

    def lane_budget_unattempted(self) -> Mapping[str, int]:
        """Per-lane unattempted-candidate counts, keyed by the lanes' log names (telemetry)."""
        return {
            "capture": self.captures_budget_unattempted,
            "dump-volume": self.dump_volumes_budget_unattempted,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReconcileConfig:
    """Reconciler ports and timing values.

    ``kw_only`` lets ``upload_store``/``image_store`` be required (S3 is a required backend,
    ADR-0337) without reordering the defaulted fields.
    """

    upload_store: ReconcileUploadStore
    image_store: ImageSweepStore
    resetter: TransportResetter = _NULL_RESETTER
    dump_volume_reaper: DumpVolumeReaper = _NULL_DUMP_VOLUME_REAPER
    resource_probe: ResourceProbe | None = None
    system_object_hosting_gate: SystemObjectHostingGate | None = None
    interval: timedelta = DEFAULT_INTERVAL
    debug_session_stale_after: timedelta = DEFAULT_DEBUG_SESSION_STALE_AFTER
    idempotency_retention: timedelta = DEFAULT_IDEMPOTENCY_RETENTION
    report_artifact_retention: timedelta = DEFAULT_REPORT_ARTIFACT_RETENTION
    investigation_cleanup_grace: timedelta = DEFAULT_INVESTIGATION_CLEANUP_GRACE
    build_artifact_retention: timedelta = DEFAULT_BUILD_ARTIFACT_RETENTION
    investigation_rootfs_retention: timedelta = DEFAULT_INVESTIGATION_ROOTFS_RETENTION
    queue_max_wait: timedelta = DEFAULT_QUEUE_MAX_WAIT
    dump_volume_grace: timedelta = DEFAULT_DUMP_VOLUME_GRACE
    #: ``Resource kind -> CaptureReaper`` for the ADR-0556 orphaned-capture sweep. A kind wired to
    #: ``NullCaptureReaper`` is disabled: it is excluded from selection, so its rows are never
    #: dispatched and never marked complete. Both capture-capable kinds ship concrete reapers
    #: (#1947 remote, #1948 local).
    capture_reapers: Mapping[str, CaptureReaper] = _NO_CAPTURE_REAPERS
    #: How long a terminal capture row sits untouched before the sweep considers it. Pacing, not a
    #: safety fence — the per-job ownership fence is what keeps a live worker's state safe.
    capture_settle: timedelta = DEFAULT_CAPTURE_SETTLE
    #: Candidates per pass, so the historical backlog the migration exposes drains over several
    #: intervals instead of opening one hypervisor connection per row at once.
    capture_reap_batch: int = DEFAULT_CAPTURE_REAP_BATCH
    #: First retry delay after an attempt that did not reclaim, and the ceiling its doubling stops
    #: at, both measured on the database clock.
    capture_retry_base: timedelta = DEFAULT_CAPTURE_RETRY_BASE
    capture_retry_cap: timedelta = DEFAULT_CAPTURE_RETRY_CAP
    #: How long each host-state reaping lane may keep starting candidates in one pass (ADR-0565).
    #: Seconds on the reconciler's monotonic clock, per lane per pass, consulted only between
    #: candidates — so it never ends a transaction a provider call may still be mutating host state
    #: under. A lane that spends it returns early with the candidate in flight completed; that is
    #: not a fault and is not counted, and the next pass re-derives the rest.
    lane_budget: timedelta = DEFAULT_LANE_BUDGET
    #: How long a `crashed` System's crash investigation must show no activity before its
    #: still-`active` allocation is reclaimed (ADR-0480). The operator's brake on the one repair
    #: that can end a live investigation: raise it where investigations idle for long stretches.
    crashed_idle_grace: timedelta = DEFAULT_CRASHED_IDLE_GRACE
    heartbeat: Heartbeat | None = None
    heartbeat_tick: timedelta = timedelta(seconds=1)
    heartbeat_sleep_until_stop: Callable[[asyncio.Event, float], Awaitable[None]] = (
        _sleep_until_stop
    )
    telemetry: ReconcilerTelemetry | None = None
    fleet_telemetry: FleetTelemetry | None = None
    admission_metrics: AdmissionMetrics = field(default=_NULL_ADMISSION_METRICS)
    debug_session_telemetry: DebugSessionTelemetry = field(default=_NULL_DEBUG_SESSION_TELEMETRY)


def _reconcile_inventory_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return _INVENTORY_PASS.make_repair(config.image_store)


def _leaked_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _repair_leaked_images(conn, config.image_store, image_publish_grace)


def _dangling_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _repair_dangling_images(conn, config.image_store, image_publish_grace)


def _expired_private_images_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _repair_expired_private_images(conn, config.image_store)


def _abandoned_uploads_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _repair_abandoned_uploads(conn, config.upload_store)


def _leaked_upload_objects_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _repair_leaked_upload_objects(
        conn, config.upload_store, _upload_orphan_grace(), _upload_window_ttl()
    )


def _report_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _gc_report_artifacts(
        conn,
        config.upload_store,
        config.report_artifact_retention,
    )


def _investigation_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _gc_investigation_artifacts(
        conn,
        config.upload_store,
        config.investigation_cleanup_grace,
    )


def _expired_build_artifacts_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _gc_expired_build_artifacts(
        conn,
        config.upload_store,
        config.build_artifact_retention,
    )


def _investigation_rootfs_reclaim_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _sweep_investigation_rootfs_reclaim(
        conn, config.investigation_cleanup_grace
    )


def _expired_investigation_rootfs_reclaim_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _sweep_expired_investigation_rootfs_reclaim(
        conn, config.investigation_rootfs_retention
    )


def _unowned_investigation_rootfs_staging_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _sweep_unowned_investigation_rootfs_staging(
        conn, config.investigation_rootfs_retention
    )


def _console_collectors_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    gate = config.system_object_hosting_gate
    if gate is None:
        return None
    return lambda conn: _reap_console_collectors(conn, gate.registry)


def _local_system_object_versions_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: sweep_local_system_object_versions(conn, config.upload_store)


def _system_artifact_rows_gc_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    return lambda conn: _gc_system_artifacts(conn, config.upload_store)


def _remote_system_object_versions_repair(
    _reaper: InfraReaper, config: ReconcileConfig, _image_publish_grace: timedelta
) -> _RepairFn | None:
    gate = config.system_object_hosting_gate
    if gate is None:
        return None
    return lambda conn: sweep_remote_system_object_versions(conn, config.upload_store, gate)


_REPAIR_CATALOG: tuple[_RepairCatalogEntry, ...] = (
    _RepairCatalogEntry(
        "expired_allocations",
        lambda _r, _c, _g: _sweep_expired_allocations,
        report_field="expired_allocations",
    ),
    _RepairCatalogEntry(
        "reaped_active_allocations",
        lambda _r, c, _g: (
            lambda conn: _reap_orphaned_active_allocations(
                conn, crashed_idle_grace=c.crashed_idle_grace
            )
        ),
        report_field="reaped_active_allocations",
    ),
    _RepairCatalogEntry(
        "promoted_allocations",
        lambda _r, c, _g: lambda conn: _promote_pending(conn, c.admission_metrics),
        report_field="promoted_allocations",
    ),
    _RepairCatalogEntry(
        "queue_timeouts",
        lambda _r, c, _g: _reap_queue_timeouts_for(c.queue_max_wait, c.admission_metrics),
        report_field="queue_timeouts",
    ),
    _RepairCatalogEntry(
        "orphaned_systems",
        lambda _r, _c, _g: _repair_orphaned_systems,
        report_field="orphaned_systems",
    ),
    _RepairCatalogEntry(
        "abandoned_jobs",
        lambda _r, _c, _g: _repair_abandoned_jobs,
        report_field="abandoned_jobs",
    ),
    # Runs after abandoned_jobs, which dead-letters a lease-lapsed-and-exhausted force_crash job.
    _RepairCatalogEntry(
        "stalled_crashing_systems", lambda _r, _c, _g: _repair_stalled_crashing_systems
    ),
    # Runs after abandoned_jobs, which dead-letters a lease-lapsed-and-exhausted restore/snapshot
    # job (ADR-0378): a stranded RESTORING System -> failed, a stranded creating snapshot -> failed.
    _RepairCatalogEntry(
        "stalled_restoring_systems", lambda _r, _c, _g: _repair_stalled_restoring_systems
    ),
    _RepairCatalogEntry(
        "stalled_creating_snapshots", lambda _r, _c, _g: _repair_stalled_creating_snapshots
    ),
    _RepairCatalogEntry("console_rotations_enqueued", lambda _r, _c, _g: _sweep_console_rotation),
    _RepairCatalogEntry(
        "reaped_runtime_resources",
        lambda _r, c, _g: lambda conn: _reap_expired_runtime_resources(conn, c.resource_probe),
        report_field="reaped_runtime_resources",
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
        report_field="dead_sessions",
    ),
    _RepairCatalogEntry(
        "leaked_domains",
        lambda r, _c, _g: lambda conn: _repair_leaked_domains(conn, r),
        report_field="leaked_domains",
    ),
    _RepairCatalogEntry(
        "leaked_probe_guests",
        lambda r, _c, _g: lambda conn: _repair_leaked_probe_guests(conn, r),
        report_field="leaked_probe_guests",
    ),
    _RepairCatalogEntry(
        "idempotency_keys_gc_count",
        lambda _r, c, _g: lambda conn: _gc_idempotency_keys(conn, c.idempotency_retention),
        report_field="idempotency_keys_gc_count",
    ),
    _RepairCatalogEntry(
        "reaped_dump_volumes",
        lambda _r, c, _g: (
            lambda conn: _reap_orphaned_dump_volumes(
                conn, c.dump_volume_reaper, c.dump_volume_grace, budget=c.lane_budget
            )
        ),
        report_field="reaped_dump_volumes",
    ),
    # Runs after abandoned_jobs, which dead-letters a lease-lapsed-and-exhausted capture job and is
    # therefore what puts a stranded capture into the terminal state this sweep selects on. Placed
    # beside the dump-volume sweep because both reclaim provider host state a job row still owns,
    # but the two share no state and either order would be correct.
    _RepairCatalogEntry(
        "reaped_captures",
        lambda _r, c, _g: (
            lambda conn: _reap_orphaned_captures(
                conn,
                c.capture_reapers,
                settle=c.capture_settle,
                batch=c.capture_reap_batch,
                retry_base=c.capture_retry_base,
                retry_cap=c.capture_retry_cap,
                budget=c.lane_budget,
            )
        ),
        report_field="reaped_captures",
    ),
    # Collects for table growth, not for exposure (ADR-0502): the orphan sweep's classify honours a
    # lease only while its holder is live, so a lease this pass has not yet reached already fences
    # nothing and running late costs no correctness — unlike the ADR-0444 window reaper. The growth
    # is guaranteed rather than exceptional, because capture_handler's `except` releases no lease.
    # Placed ahead of the sweep for readability only; the two are independent by that same argument.
    _RepairCatalogEntry("stale_write_leases", lambda _r, _c, _g: _reap_stale_write_leases),
    _RepairCatalogEntry(
        "abandoned_uploads", _abandoned_uploads_repair, report_field="abandoned_uploads"
    ),
    # Runs after the reaper so a window reaped this pass is already row-less. It is the reclaim
    # threshold (orphan grace *plus* the upload TTL), not the ordering, that keeps a
    # just-reaped window's bytes out of this same pass (ADR-0455 §2).
    _RepairCatalogEntry("leaked_upload_objects", _leaked_upload_objects_repair),
    _RepairCatalogEntry("report_artifacts_gc_count", _report_artifacts_gc_repair),
    _RepairCatalogEntry(
        "investigation_artifacts_gc_count",
        _investigation_artifacts_gc_repair,
        report_field="investigation_artifacts_gc_count",
    ),
    _RepairCatalogEntry(
        "expired_build_artifacts_gc_count",
        _expired_build_artifacts_gc_repair,
        report_field="expired_build_artifacts_gc_count",
    ),
    _RepairCatalogEntry(
        "investigation_rootfs_reclaims_enqueued",
        _investigation_rootfs_reclaim_repair,
        report_field="investigation_rootfs_reclaims_enqueued",
    ),
    _RepairCatalogEntry(
        "expired_investigation_rootfs_reclaims_enqueued",
        _expired_investigation_rootfs_reclaim_repair,
        report_field="expired_investigation_rootfs_reclaims_enqueued",
    ),
    # Runs after both row-keyed lanes so an investigation either of them just enqueued for holds the
    # shared per-investigation slot and this one skips it. Ordering is a courtesy, not the
    # correctness argument: `_UNOWNED_STAGING_INV_SQL`'s `NOT EXISTS` already makes the worklists
    # disjoint (ADR-0494 §2).
    _RepairCatalogEntry(
        "unowned_investigation_rootfs_staging_drains_enqueued",
        _unowned_investigation_rootfs_staging_repair,
        report_field="unowned_investigation_rootfs_staging_drains_enqueued",
    ),
    _RepairCatalogEntry(
        "console_collectors_reaped",
        _console_collectors_repair,
        report_field="console_collectors_reaped",
    ),
    _RepairCatalogEntry("system_artifact_rows_gc_count", _system_artifact_rows_gc_repair),
    _RepairCatalogEntry(
        "local_system_object_versions_deleted",
        _local_system_object_versions_repair,
        report_field="local_system_object_versions_deleted",
    ),
    _RepairCatalogEntry(
        "remote_system_object_versions_deleted",
        _remote_system_object_versions_repair,
        report_field="remote_system_object_versions_deleted",
    ),
    _RepairCatalogEntry(
        "reconcile_inventory",
        _reconcile_inventory_repair,
        report_field="reconciled_inventory",
    ),
    _RepairCatalogEntry("leaked_images", _leaked_images_repair, report_field="leaked_images"),
    _RepairCatalogEntry("dangling_images", _dangling_images_repair, report_field="dangling_images"),
    _RepairCatalogEntry(
        "expired_private_images",
        _expired_private_images_repair,
        report_field="expired_private_images",
    ),
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
    entry.report_field: entry.name for entry in _REPAIR_CATALOG if entry.report_field is not None
}


def _repair_count_defaults(counts: Mapping[str, int]) -> dict[str, int]:
    return {repair_kind: counts.get(repair_kind, 0) for repair_kind in ALL_REPAIR_KINDS}


def _report_count(counts: Mapping[str, int], report_field: str) -> int:
    return counts[_REPORT_FIELD_TO_REPAIR_KIND[report_field]]


def _report_field_counts(counts: Mapping[str, int]) -> dict[str, int]:
    return {
        field_name: _report_count(counts, field_name) for field_name in _REPORT_FIELD_TO_REPAIR_KIND
    }


async def reconcile_once(
    pool: AsyncConnectionPool,
    reaper: InfraReaper,
    *,
    config: ReconcileConfig,
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
    counts, failures, lane_outcomes = await _run_repair_plan(
        pool,
        _repair_plan(
            reaper=reaper,
            config=config,
            image_publish_grace=_image_publish_grace(),
        ),
    )

    return ReconcileReport.from_counts(counts, failures, lane_outcomes)


def _image_publish_grace() -> timedelta:
    """Resolve the image publish-deadline grace from config (default 3600s)."""
    seconds = config.get(IMAGE_PUBLISH_GRACE)
    if seconds is None:
        return DEFAULT_IMAGE_PUBLISH_GRACE
    return timedelta(seconds=seconds)


def _upload_orphan_grace() -> timedelta:
    """Resolve the upload-orphan grace from config (ADR-0455 §8); the operator's brake.

    A **restart** engages it, not a redeploy: ``Registry.load`` snapshots ``KDIVE_*`` once at the
    bootstrap in ``__main__``, so this reads the same frozen value on every pass, and an outside
    operator cannot mutate a running process's environment anyway. Resolving here rather than at
    ``ReconcileConfig`` construction keeps both threshold terms in one place and out of the
    provider-shaped config object; it does not make the brake live. ``require`` rather than
    ``get``: the setting declares a default, so there is no unset case to fall back for.
    """
    return timedelta(seconds=config.require(UPLOAD_ORPHAN_GRACE))


def _upload_window_ttl() -> timedelta:
    """Resolve the upload-window TTL the orphan grace is stacked on top of (ADR-0455 §2).

    Read from config rather than baked into a constant because raising ``KDIVE_UPLOAD_TTL_SECONDS``
    postpones every reap by the same amount, and an orphan grace that did not move with it would
    let the sweep reclaim a window's bytes in the very pass that reaped them. Like the grace, the
    value is a process-start snapshot; a change engages on restart.
    """
    return timedelta(seconds=config.require(UPLOAD_TTL_SECONDS))


async def _run_repair_plan(
    pool: AsyncConnectionPool, repairs: tuple[_RepairSpec, ...]
) -> tuple[dict[str, int], list[str], dict[str, ReapLaneOutcome]]:
    """Run each repair on a fresh pooled connection, isolating failures.

    Returns the per-kind counts, the names of repairs that raised, and the
    :class:`ReapLaneOutcome` of the budgeted reaping lanes (the two repairs whose repair
    function returns an outcome rather than a bare int; #1982).
    """
    counts = _repair_count_defaults({})
    failures: list[str] = []
    lane_outcomes: dict[str, ReapLaneOutcome] = {}
    for spec in repairs:
        try:
            async with pool.connection() as conn:
                result = await spec.repair(conn)
        except Exception:  # noqa: BLE001 - isolate each repair; one failure must not starve the rest
            _log.warning("reconciler: repair %s failed this pass", spec.name, exc_info=True)
            failures.append(spec.name)
            continue
        if isinstance(result, ReapLaneOutcome):
            counts[spec.name] = result.reaped
            lane_outcomes[spec.name] = result
        else:
            counts[spec.name] = result
    return counts, failures, lane_outcomes


class Reconciler:
    """Runs :func:`reconcile_once` on an interval until stopped."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        reaper: InfraReaper,
        *,
        config: ReconcileConfig,
    ) -> None:
        self._pool = pool
        self._reaper = reaper
        self._config = config
        self._heartbeat_tick = config.heartbeat_tick.total_seconds()
        self._telemetry = config.telemetry or ReconcilerTelemetry.disabled()
        self._fleet_telemetry = config.fleet_telemetry or FleetTelemetry.disabled()

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

    def _start_heartbeat_ticker(self, stop: asyncio.Event) -> asyncio.Task[None] | None:
        if self._config.heartbeat is None:
            return None
        return asyncio.create_task(
            tick_until_stop(
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
                    self._telemetry.record_lane_budget_unattempted(report.lane_budget_unattempted())
                except Exception:  # noqa: BLE001 - a durable reconciler survives a transient per-pass error
                    span.set_outcome("error")
                    _log.exception("reconcile pass failed; continuing after %ss", interval)
            await self._refresh_fleet_snapshot()
            next_due = time.monotonic() + interval
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)

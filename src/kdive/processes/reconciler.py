"""Reconciler process runner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import (
    BUILD_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_CLEANUP_GRACE_DAYS,
    REPORT_ARTIFACT_RETENTION_DAYS,
    S3_BUCKET,
    S3_ENDPOINT_URL,
    S3_REGION,
)
from kdive.db.pool import create_pool
from kdive.domain.errors import CategorizedError
from kdive.processes.runtime import cancel, install_stop, run_process_runtime
from kdive.providers.infra.console_hosting import start_console_hosting

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.providers.core.resolver import ProviderResolver
    from kdive.security.secrets.secret_registry import SecretRegistry
    from kdive.store.objectstore import ObjectStore

RECONCILER_HEARTBEAT_STALE_SECONDS = 90.0
PROVIDER_DISCOVERY_TIMEOUT_SECONDS = 30.0

_log = logging.getLogger(__name__)
_S3_OPTIONAL_ENV_NAMES = frozenset({S3_ENDPOINT_URL.name, S3_BUCKET.name, S3_REGION.name})


async def run_reconciler(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.health.processes.server import build_postgres_ping
    from kdive.health.processes.worker import build_worker_probe
    from kdive.providers.infra.libvirt_event_loop import ensure_libvirt_event_loop
    from kdive.store.objectstore import object_store_from_env

    ensure_libvirt_event_loop()
    stop = install_stop()

    def build_probe(pool: AsyncConnectionPool) -> HealthProbe:
        return reconciler_probe(
            pool, build_postgres_ping, build_worker_probe, object_store_from_env
        )

    async def run_reconciler_process(
        pool: AsyncConnectionPool, heartbeat: Heartbeat, probe: HealthProbe
    ) -> None:
        del probe
        await run_reconciler_body(pool, heartbeat, stop, secret_registry, telemetry)

    await run_process_runtime(
        process="reconciler",
        pool=create_pool(min_size=1),
        secret_registry=secret_registry,
        telemetry=telemetry,
        heartbeat_stale_after=RECONCILER_HEARTBEAT_STALE_SECONDS,
        probe_builder=build_probe,
        body=run_reconciler_process,
    )


async def run_reconciler_body(
    pool: AsyncConnectionPool,
    heartbeat: Heartbeat,
    stop: asyncio.Event,
    secret_registry: SecretRegistry,
    telemetry: Telemetry,
) -> None:
    from kdive.providers.assembly.composition import ProviderComposition
    from kdive.store.objectstore import object_store_from_env

    upload_store = optional_reconciler_object_store(object_store_from_env)
    provider_composition = ProviderComposition(secret_registry=secret_registry)
    provider_resolver = provider_composition.build_provider_resolver()
    discovery_task = asyncio.create_task(register_provider_resources(pool, provider_resolver))
    try:
        await run_reconciler_with_composition(
            pool,
            heartbeat,
            stop,
            telemetry,
            provider_composition,
            upload_store,
        )
    finally:
        await cancel(discovery_task)


async def run_reconciler_with_composition(
    pool: AsyncConnectionPool,
    heartbeat: Heartbeat,
    stop: asyncio.Event,
    telemetry: Telemetry,
    provider_composition: Any,
    upload_store: ObjectStore | None,
) -> None:
    from kdive.observability.console_telemetry import ConsoleTelemetry
    from kdive.reconciler.loop import Reconciler

    console_hosting = await provider_composition.build_reconciler_console_hosting(
        console_telemetry=ConsoleTelemetry(
            meter=telemetry.meter_provider.get_meter("kdive.reconciler")
        ),
    )
    reconciler = Reconciler(
        pool,
        provider_composition.build_reconciler_reaper(),
        config=build_reconcile_config(
            provider_composition,
            upload_store=upload_store,
            console_registry=console_hosting.registry if console_hosting else None,
            heartbeat=heartbeat,
            telemetry=telemetry,
        ),
    )
    hosting_task = start_console_hosting(console_hosting, stop)
    try:
        await reconciler.run(stop)
    finally:
        await cancel(*([hosting_task] if hosting_task else []))
        if console_hosting is not None:
            await console_hosting.close()


def build_reconcile_config(
    provider_composition: Any,
    *,
    upload_store: ObjectStore | None,
    console_registry: Any,
    heartbeat: Heartbeat,
    telemetry: Telemetry,
) -> Any:
    from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
    from kdive.reconciler.fleet import FleetTelemetry
    from kdive.reconciler.loop import ReconcileConfig
    from kdive.reconciler.loop_telemetry import ReconcilerTelemetry
    from kdive.services.allocation.admission.metrics import AdmissionMetrics

    meter = telemetry.meter_provider.get_meter("kdive.reconciler")
    return ReconcileConfig(
        upload_store=upload_store,
        image_store=upload_store,
        report_artifact_retention=timedelta(days=config.require(REPORT_ARTIFACT_RETENTION_DAYS)),
        investigation_cleanup_grace=timedelta(
            days=config.require(INVESTIGATION_CLEANUP_GRACE_DAYS)
        ),
        build_artifact_retention=timedelta(days=config.require(BUILD_ARTIFACT_RETENTION_DAYS)),
        console_registry=console_registry,
        resetter=provider_composition.build_reconciler_transport_resetter(),
        dump_volume_reaper=provider_composition.build_reconciler_dump_volume_reaper(),
        heartbeat=heartbeat,
        telemetry=ReconcilerTelemetry(
            tracer=telemetry.tracer_provider.get_tracer("kdive.reconciler"),
            meter=meter,
        ),
        fleet_telemetry=FleetTelemetry(meter=meter),
        admission_metrics=AdmissionMetrics(meter=meter),
        debug_session_telemetry=DebugSessionTelemetry(meter=meter),
    )


def optional_reconciler_object_store(
    store_factory: Callable[[], ObjectStore],
) -> ObjectStore | None:
    """Return the object store, or ``None`` only when S3 is wholly unconfigured."""
    try:
        return store_factory()
    except CategorizedError:
        if s3_env_is_absent():
            return None
        raise


def s3_env_is_absent() -> bool:
    env = config.env_snapshot()
    return _S3_OPTIONAL_ENV_NAMES.isdisjoint(env)


def reconciler_probe(
    pool: AsyncConnectionPool,
    build_postgres_ping: Callable[[AsyncConnectionPool], Callable[[], Awaitable[None]]],
    build_worker_probe: Callable[..., HealthProbe],
    object_store_factory: Callable[[], object],
) -> HealthProbe:
    """Build the reconciler readiness probe."""
    return build_worker_probe(
        postgres_ping=build_postgres_ping(pool), object_store_factory=object_store_factory
    )


async def register_provider_resources(
    pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    """Best-effort provider discovery registration so allocations.request has a Resource."""
    try:
        await asyncio.wait_for(
            resolver.register_all_discovery(pool),
            timeout=PROVIDER_DISCOVERY_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        _log.warning(
            "reconciler: provider discovery registration timed out after %ss",
            PROVIDER_DISCOVERY_TIMEOUT_SECONDS,
        )
    except Exception:  # noqa: BLE001 - registration failure must not crash the reconciler
        _log.warning("reconciler: provider discovery registration failed", exc_info=True)

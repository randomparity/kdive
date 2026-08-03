"""Reconciler process runner."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg
from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import (
    BUILD_ARTIFACT_RETENTION_DAYS,
    INVESTIGATION_CLEANUP_GRACE_DAYS,
    KUBERNETES_CREDENTIAL_BROKER_CA,
    KUBERNETES_CREDENTIAL_BROKER_HOST,
    KUBERNETES_CREDENTIAL_BROKER_PORT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_CERT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_KEY,
    KUBERNETES_CREDENTIAL_ENVELOPE_KEY,
    KUBERNETES_WITNESS_NAMESPACE,
    KUBERNETES_WITNESS_ORDINAL_CEILING,
    KUBERNETES_WITNESS_WORKER_NAME,
    LIFECYCLE_WITNESS_DATABASE_URL,
    REPORT_ARTIFACT_RETENTION_DAYS,
)
from kdive.db.pool import create_pool
from kdive.processes.runtime import cancel, install_stop, run_process_runtime
from kdive.providers.infra.console_hosting import start_console_hosting

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.providers.assembly.composition import ProviderComposition
    from kdive.providers.core.resolver import ProviderResolver
    from kdive.providers.infra.console_hosting import ConsoleHosting
    from kdive.reconciler.loop import ReconcileConfig
    from kdive.security.secrets.secret_registry import SecretRegistry
    from kdive.store.objectstore import ObjectStore

RECONCILER_HEARTBEAT_STALE_SECONDS = 90.0
PROVIDER_DISCOVERY_TIMEOUT_SECONDS = 30.0

_log = logging.getLogger(__name__)


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

    upload_store = object_store_from_env()
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
    provider_composition: ProviderComposition,
    upload_store: ObjectStore,
) -> None:
    from kdive.observability.console_telemetry import ConsoleTelemetry
    from kdive.processes.kubernetes_credential_broker import (
        KubernetesCredentialBroker,
        PodIdentity,
        envelope_codec,
        run_pre_registration,
        serve_broker,
        tls_server_context,
        token_review,
    )
    from kdive.processes.kubernetes_termination_witness import (
        KubernetesTerminationWitness,
        patch_finalizers,
        read_pod,
        run_witness,
    )
    from kdive.reconciler.loop import Reconciler
    from kdive.services.runs.worker_incarnations import (
        CURRENT_WORKER_FENCE_PROTOCOL,
        acknowledge_kubernetes_credential_envelope,
        read_kubernetes_credential_envelope,
        register_kubernetes_worker_incarnation,
        terminate_worker_incarnation,
    )

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
            system_object_hosting_gate=console_hosting,
            heartbeat=heartbeat,
            telemetry=telemetry,
        ),
    )
    hosting_task = start_console_hosting(console_hosting, stop)
    witness_task: asyncio.Task[None] | None = None
    broker_task: asyncio.Task[None] | None = None
    registration_task: asyncio.Task[None] | None = None
    witness_namespace = config.require(KUBERNETES_WITNESS_NAMESPACE)
    witness_name = config.require(KUBERNETES_WITNESS_WORKER_NAME)
    witness_ceiling = config.require(KUBERNETES_WITNESS_ORDINAL_CEILING)
    if witness_namespace and witness_name and witness_ceiling:

        async def with_lifecycle_connection[T](
            operation: Callable[[psycopg.AsyncConnection], Awaitable[T]],
        ) -> T:
            connection = await psycopg.AsyncConnection.connect(
                config.require(LIFECYCLE_WITNESS_DATABASE_URL)
            )
            try:
                return await operation(connection)
            finally:
                await connection.close()

        async def terminate(
            incarnation: str, authority_binding: dict[str, str], outcome: str
        ) -> bool:
            terminal_outcome = "succeeded" if outcome.endswith("succeeded") else "failed"
            return await with_lifecycle_connection(
                lambda connection: terminate_worker_incarnation(
                    connection,
                    incarnation,
                    "kubernetes",
                    authority_binding,
                    terminal_outcome,
                )
            )

        def incarnation(identity: PodIdentity) -> str:
            return f"kubernetes:{identity.namespace}:{identity.name}:{identity.uid}"

        def binding(identity: PodIdentity) -> dict[str, str]:
            return {
                "namespace": identity.namespace,
                "name": identity.name,
                "uid": identity.uid,
            }

        async def register(identity: PodIdentity, credential_hash: bytes, envelope: bytes) -> bool:
            return await with_lifecycle_connection(
                lambda connection: register_kubernetes_worker_incarnation(
                    connection,
                    incarnation(identity),
                    binding(identity),
                    credential_hash,
                    envelope,
                    CURRENT_WORKER_FENCE_PROTOCOL,
                )
            )

        async def pending_envelope(identity: PodIdentity) -> bytes | None:
            return await with_lifecycle_connection(
                lambda connection: read_kubernetes_credential_envelope(
                    connection, incarnation(identity), binding(identity)
                )
            )

        async def acknowledge(identity: PodIdentity) -> bool:
            return await with_lifecycle_connection(
                lambda connection: acknowledge_kubernetes_credential_envelope(
                    connection, incarnation(identity), binding(identity)
                )
            )

        encrypt, decrypt = envelope_codec(Path(config.require(KUBERNETES_CREDENTIAL_ENVELOPE_KEY)))
        broker = KubernetesCredentialBroker(
            namespace=witness_namespace,
            worker_name=witness_name,
            ordinal_ceiling=witness_ceiling,
            pass_limit=witness_ceiling,
            read_pod=read_pod,
            register=register,
            pending_envelope=pending_envelope,
            acknowledge=acknowledge,
            token_review=token_review,
            encrypt=encrypt,
            decrypt=decrypt,
        )
        broker_task = asyncio.create_task(
            serve_broker(
                broker,
                stop,
                host=config.require(KUBERNETES_CREDENTIAL_BROKER_HOST),
                port=config.require(KUBERNETES_CREDENTIAL_BROKER_PORT),
                ssl_context=tls_server_context(
                    certificate=config.require(KUBERNETES_CREDENTIAL_BROKER_TLS_CERT),
                    private_key=config.require(KUBERNETES_CREDENTIAL_BROKER_TLS_KEY),
                    ca=config.require(KUBERNETES_CREDENTIAL_BROKER_CA),
                ),
            )
        )
        registration_task = asyncio.create_task(run_pre_registration(broker, stop))

        witness = KubernetesTerminationWitness(
            namespace=witness_namespace,
            worker_name=witness_name,
            ordinal_ceiling=witness_ceiling,
            read_pod=read_pod,
            patch_finalizers=lambda namespace, name, operations: asyncio.to_thread(
                patch_finalizers, namespace, name, operations
            ),
            terminate=terminate,
        )
        witness_task = asyncio.create_task(run_witness(witness, stop))
    try:
        await reconciler.run(stop)
    finally:
        tasks = [
            task
            for task in (hosting_task, witness_task, broker_task, registration_task)
            if task is not None
        ]
        await cancel(*tasks)
        if console_hosting is not None:
            await console_hosting.close()


def build_reconcile_config(
    provider_composition: ProviderComposition,
    *,
    upload_store: ObjectStore,
    system_object_hosting_gate: ConsoleHosting | None,
    heartbeat: Heartbeat,
    telemetry: Telemetry,
) -> ReconcileConfig:
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
        system_object_hosting_gate=system_object_hosting_gate,
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


def reconciler_probe(
    pool: AsyncConnectionPool,
    build_postgres_ping: Callable[[AsyncConnectionPool], Callable[[], Awaitable[None]]],
    build_worker_probe: Callable[..., HealthProbe],
    object_store_factory: Callable[[], object],
) -> HealthProbe:
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

"""Job worker process runner."""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

from kdive.db.pool import create_pool
from kdive.processes.runtime import (
    HEARTBEAT_STALE_SECONDS,
    install_stop,
    readiness,
    run_process_runtime,
)

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry


async def run_worker(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.health.processes.server import build_postgres_ping
    from kdive.health.processes.worker import build_worker_probe
    from kdive.jobs.worker import Worker, WorkerConfig
    from kdive.jobs.worker_telemetry import WorkerTelemetry
    from kdive.mcp.assembly.app import build_handler_registry
    from kdive.store.objectstore import object_store_from_env

    stop = install_stop()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"

    def build_probe(pool: AsyncConnectionPool) -> HealthProbe:
        return build_worker_probe(
            postgres_ping=build_postgres_ping(pool), object_store_factory=object_store_from_env
        )

    async def run_worker_body(
        pool: AsyncConnectionPool, heartbeat: Heartbeat, probe: HealthProbe
    ) -> None:
        worker = Worker(
            pool,
            build_handler_registry(secret_registry=secret_registry),
            worker_id=worker_id,
            secret_registry=secret_registry,
            config=WorkerConfig(
                heartbeat=heartbeat,
                readiness=readiness(probe),
                telemetry=WorkerTelemetry(
                    tracer=telemetry.tracer_provider.get_tracer("kdive.worker"),
                    meter=telemetry.meter_provider.get_meter("kdive.worker"),
                ),
            ),
        )
        await worker.run(stop)

    await run_process_runtime(
        process="worker",
        pool=create_pool(min_size=2, max_size=4),
        secret_registry=secret_registry,
        telemetry=telemetry,
        heartbeat_stale_after=HEARTBEAT_STALE_SECONDS,
        probe_builder=build_probe,
        body=run_worker_body,
    )

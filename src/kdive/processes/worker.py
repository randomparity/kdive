"""Job worker process runner."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import WORKER_ACCEPTED_LANES
from kdive.db.pool import create_pool
from kdive.jobs.worker import worker_pool_floor
from kdive.processes.lifecycle.worker_incarnation import (
    worker_incarnation_credential,
    worker_incarnation_id,
)
from kdive.processes.runtime import (
    HEARTBEAT_STALE_SECONDS,
    install_stop,
    readiness,
    run_process_runtime,
)
from kdive.services.runs.worker_incarnations import (
    CURRENT_WORKER_FENCE_PROTOCOL,
    WorkerIncarnation,
    authenticate_worker_incarnation,
)

# The historic pool ceiling. Kept as a floor of its own so a single-lane worker keeps the same
# headroom it always had rather than being narrowed to its bare correctness minimum.
_MIN_POOL_MAX_SIZE = 4

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.health.probe import HealthProbe
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry


def _validate_worker_incarnation(
    incarnation: WorkerIncarnation, *, configured_worker_id: str
) -> str:
    if incarnation.incarnation != configured_worker_id:
        raise RuntimeError(
            "authenticated worker incarnation does not match configured runtime identity"
        )
    if incarnation.fence_protocol != CURRENT_WORKER_FENCE_PROTOCOL:
        raise RuntimeError(
            "authenticated worker incarnation does not use the current fence protocol"
        )
    return incarnation.incarnation


async def run_worker(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    from kdive.health.processes.server import build_postgres_ping
    from kdive.health.processes.worker import build_worker_probe
    from kdive.jobs.assembly import build_handler_registry
    from kdive.jobs.worker import Worker, WorkerConfig
    from kdive.jobs.worker_telemetry import WorkerTelemetry
    from kdive.store.objectstore import object_store_from_env

    stop = install_stop()
    configured_worker_id = worker_incarnation_id(os.getpid())
    incarnation_credential = worker_incarnation_credential()
    secret_registry.register(incarnation_credential.get_secret_value(), scope=None)
    # Read once, here: the pool is built as an argument to `run_process_runtime` (outside
    # `run_worker_body`) while `WorkerConfig` is built inside it, so a read at either site alone
    # is out of scope for the other — and the two must agree or the worker's own floor check
    # fails at construction on every start (ADR-0550).
    accepted_lanes = config.require(WORKER_ACCEPTED_LANES)
    pool_max_size = max(_MIN_POOL_MAX_SIZE, worker_pool_floor(accepted_lanes))

    def build_probe(pool: AsyncConnectionPool) -> HealthProbe:
        return build_worker_probe(
            postgres_ping=build_postgres_ping(pool), object_store_factory=object_store_from_env
        )

    async def run_worker_body(
        pool: AsyncConnectionPool, heartbeat: Heartbeat, probe: HealthProbe
    ) -> None:
        async with pool.connection() as conn:
            incarnation = await authenticate_worker_incarnation(conn, incarnation_credential)
        worker_id = _validate_worker_incarnation(
            incarnation, configured_worker_id=configured_worker_id
        )
        worker = Worker(
            pool,
            build_handler_registry(
                secret_registry=secret_registry,
                incarnation_credential=incarnation_credential,
            ),
            worker_id=worker_id,
            incarnation_credential=incarnation_credential,
            secret_registry=secret_registry,
            config=WorkerConfig(
                accepted_lanes=accepted_lanes,
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
        pool=create_pool(min_size=2, max_size=pool_max_size),
        secret_registry=secret_registry,
        telemetry=telemetry,
        heartbeat_stale_after=HEARTBEAT_STALE_SECONDS,
        probe_builder=build_probe,
        body=run_worker_body,
    )

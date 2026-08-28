"""Dedicated Kubernetes worker lifecycle authority process."""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING

from psycopg_pool import AsyncConnectionPool

import kdive.config as config
from kdive.config.core_settings import (
    KUBERNETES_CREDENTIAL_BROKER_CA,
    KUBERNETES_CREDENTIAL_BROKER_HOST,
    KUBERNETES_CREDENTIAL_BROKER_PORT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_CERT,
    KUBERNETES_CREDENTIAL_BROKER_TLS_KEY,
    KUBERNETES_CREDENTIAL_ENVELOPE_KEY,
    KUBERNETES_WITNESS_NAMESPACE,
    KUBERNETES_WITNESS_ORDINAL_CEILING,
    KUBERNETES_WITNESS_WORKER_NAME,
)
from kdive.db.pool import create_pool
from kdive.health.probe import BackendCheck, HealthProbe
from kdive.processes.runtime import install_stop, run_process_runtime
from kdive.worker_lifecycle.authority_store import KubernetesAuthorityBinding
from kdive.worker_lifecycle.contracts import TerminationOutcome

if TYPE_CHECKING:
    from kdive.health.heartbeat import Heartbeat
    from kdive.observability.facade import Telemetry
    from kdive.security.secrets.secret_registry import SecretRegistry

LIFECYCLE_WITNESS_HEARTBEAT_STALE_SECONDS = 10.0

type AuthorityChild = tuple[str, Coroutine[object, object, None]]


async def _supervise_authority_tasks(stop: asyncio.Event, *children: AuthorityChild) -> None:
    """Stop all authority children together and surface every unexpected child exit."""
    named_tasks = [(name, asyncio.create_task(child)) for name, child in children]
    stop_task = asyncio.create_task(stop.wait())
    tasks = [task for _, task in named_tasks]
    try:
        done, _ = await asyncio.wait([stop_task, *tasks], return_when=asyncio.FIRST_COMPLETED)
        if stop_task in done:
            return

        failed_name, failed_task = next((name, task) for name, task in named_tasks if task in done)
        if failed_task.cancelled():
            raise RuntimeError(f"lifecycle authority {failed_name} was cancelled unexpectedly")
        failure = failed_task.exception()
        if failure is not None:
            raise failure
        raise RuntimeError(f"lifecycle authority {failed_name} exited unexpectedly")
    finally:
        stop_task.cancel()
        for task in tasks:
            task.cancel()
        await asyncio.gather(stop_task, *tasks, return_exceptions=True)


async def run_lifecycle_witness(secret_registry: SecretRegistry, telemetry: Telemetry) -> None:
    """Run only Kubernetes credential delivery and immutable termination evidence."""
    from kdive.health.processes.server import build_postgres_ping

    stop = install_stop()

    def build_probe(pool: AsyncConnectionPool) -> HealthProbe:
        return HealthProbe(checks=[BackendCheck(name="postgres", probe=build_postgres_ping(pool))])

    async def body(pool: AsyncConnectionPool, heartbeat: Heartbeat, probe: HealthProbe) -> None:
        del heartbeat, probe
        await run_lifecycle_witness_body(pool, stop)

    await run_process_runtime(
        process="lifecycle-witness",
        pool=create_pool(min_size=1),
        secret_registry=secret_registry,
        telemetry=telemetry,
        heartbeat_stale_after=LIFECYCLE_WITNESS_HEARTBEAT_STALE_SECONDS,
        probe_builder=build_probe,
        body=body,
        tick_heartbeat=True,
    )


async def run_lifecycle_witness_body(pool: AsyncConnectionPool, stop: asyncio.Event) -> None:
    """Run the broker, pre-registration, and termination loops on one authority pool."""
    from kdive.processes.lifecycle.kubernetes.kubernetes_credential_broker import (
        KubernetesCredentialBroker,
        PodIdentity,
        envelope_codec,
        run_pre_registration,
        serve_broker,
        tls_server_context,
        token_review,
    )
    from kdive.processes.lifecycle.kubernetes.kubernetes_termination_witness import (
        KubernetesTerminationWitness,
        patch_finalizers,
        read_pod,
        run_witness,
    )
    from kdive.worker_lifecycle.authority_store import (
        CURRENT_WORKER_FENCE_PROTOCOL,
        acknowledge_kubernetes_credential_envelope,
        read_kubernetes_credential_envelope,
        register_kubernetes_worker_incarnation,
        terminate_worker_incarnation,
    )

    namespace = config.require(KUBERNETES_WITNESS_NAMESPACE)
    worker_name = config.require(KUBERNETES_WITNESS_WORKER_NAME)
    ceiling = config.require(KUBERNETES_WITNESS_ORDINAL_CEILING)

    async def terminate(
        incarnation: str,
        authority_binding: KubernetesAuthorityBinding,
        outcome: TerminationOutcome,
    ) -> bool:
        async with pool.connection() as connection:
            return await terminate_worker_incarnation(
                connection,
                incarnation,
                "kubernetes",
                authority_binding,
                outcome,
            )

    def incarnation(identity: PodIdentity) -> str:
        return f"kubernetes:{identity.namespace}:{identity.name}:{identity.uid}"

    def binding(identity: PodIdentity) -> KubernetesAuthorityBinding:
        return {"namespace": identity.namespace, "name": identity.name, "uid": identity.uid}

    async def register(identity: PodIdentity, credential_hash: bytes, envelope: bytes) -> bool:
        async with pool.connection() as connection:
            return await register_kubernetes_worker_incarnation(
                connection,
                incarnation(identity),
                binding(identity),
                credential_hash,
                envelope,
                CURRENT_WORKER_FENCE_PROTOCOL,
            )

    async def pending_envelope(identity: PodIdentity) -> bytes | None:
        async with pool.connection() as connection:
            return await read_kubernetes_credential_envelope(
                connection, incarnation(identity), binding(identity)
            )

    async def acknowledge(identity: PodIdentity) -> bool:
        async with pool.connection() as connection:
            return await acknowledge_kubernetes_credential_envelope(
                connection, incarnation(identity), binding(identity)
            )

    encrypt, decrypt = envelope_codec(Path(config.require(KUBERNETES_CREDENTIAL_ENVELOPE_KEY)))
    broker = KubernetesCredentialBroker(
        namespace=namespace,
        worker_name=worker_name,
        ordinal_ceiling=ceiling,
        pass_limit=ceiling,
        read_pod=read_pod,
        register=register,
        pending_envelope=pending_envelope,
        acknowledge=acknowledge,
        token_review=token_review,
        encrypt=encrypt,
        decrypt=decrypt,
    )
    witness = KubernetesTerminationWitness(
        namespace=namespace,
        worker_name=worker_name,
        ordinal_ceiling=ceiling,
        read_pod=read_pod,
        patch_finalizers=lambda pod_namespace, name, operations: asyncio.to_thread(
            patch_finalizers, pod_namespace, name, operations
        ),
        terminate=terminate,
    )
    await _supervise_authority_tasks(
        stop,
        (
            "credential broker",
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
            ),
        ),
        ("credential pre-registration", run_pre_registration(broker, stop)),
        ("termination witness", run_witness(witness, stop)),
    )

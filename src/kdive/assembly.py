"""Process composition root shared by application and provider assembly."""

from __future__ import annotations

from dataclasses import dataclass

from kdive.processes.lifecycle.worker_incarnation import (
    DockerWorkerDeathVerifier,
    KubernetesWorkerDeathVerifier,
    WorkerDeathVerifier,
    worker_death_verifier_from_env,
)
from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly


@dataclass(frozen=True, slots=True)
class ProcessAssembly:
    """One process's object store and the provider composition built over that store."""

    object_stores: ObjectStoreAssembly
    providers: ProviderComposition
    worker_death_verifier: WorkerDeathVerifier | None = None


def _durable_worker_death_verifier() -> WorkerDeathVerifier | None:
    verifier = worker_death_verifier_from_env()
    if isinstance(verifier, (DockerWorkerDeathVerifier, KubernetesWorkerDeathVerifier)):
        return verifier
    return None


def build_process_assembly(secret_registry: SecretRegistry) -> ProcessAssembly:
    """Resolve the object store once and bind all provider ports to that exact instance."""
    stores = build_object_store_assembly()
    return ProcessAssembly(
        object_stores=stores,
        providers=ProviderComposition(secret_registry=secret_registry, object_store=stores.store),
        worker_death_verifier=_durable_worker_death_verifier(),
    )

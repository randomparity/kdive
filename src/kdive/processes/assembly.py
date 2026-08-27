"""Process composition root shared by application and provider assembly."""

from __future__ import annotations

from dataclasses import dataclass

from kdive.providers.assembly.composition import ProviderComposition
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.assembly import ObjectStoreAssembly, build_object_store_assembly


@dataclass(frozen=True, slots=True)
class ProcessAssembly:
    """One process's object store and the provider composition built over that store."""

    object_stores: ObjectStoreAssembly
    providers: ProviderComposition


def build_process_assembly(secret_registry: SecretRegistry) -> ProcessAssembly:
    """Resolve the object store once and bind all provider ports to that exact instance."""
    stores = build_object_store_assembly()
    return ProcessAssembly(
        object_stores=stores,
        providers=ProviderComposition(secret_registry=secret_registry, object_store=stores.store),
    )

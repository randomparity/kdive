"""Dependency injection container for the runs.* job handlers (ADR-0268, #866)."""

from __future__ import annotations

from dataclasses import dataclass

from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


@dataclass(frozen=True, slots=True)
class RunHandlerPorts:
    """Dependencies shared by the install and boot job handlers."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    artifact_store: ObjectStore | None = None

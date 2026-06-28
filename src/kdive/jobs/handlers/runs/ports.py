"""Dependency injection container for the runs.* job handlers (ADR-0268, #866).

Extracted from `registrar.py` so that `composite.py` can import `RunHandlerPorts` without
creating a circular import cycle (`composite` ← `registrar` ← `composite`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kdive.jobs.handlers.runs.build import BuildHostTransportFactories
from kdive.observability.build_telemetry import BuildPhaseRecorder
from kdive.providers.core.resolver import ProviderResolver
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.store.objectstore import ObjectStore


@dataclass(frozen=True, slots=True)
class RunHandlerPorts:
    """Dependencies shared by the build, install, boot, and composite job handlers."""

    resolver: ProviderResolver
    secret_registry: SecretRegistry
    transport_factories: BuildHostTransportFactories | None = None
    artifact_store: ObjectStore | None = None
    build_phase_recorder: BuildPhaseRecorder = field(default_factory=BuildPhaseRecorder.disabled)

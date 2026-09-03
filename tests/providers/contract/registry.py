"""Discovery registry for the provider-neutral external-boot contract suite (ADR-0583).

A provider registers by adding one module under ``bindings/`` that exposes a ``BINDING``.
The suite's assertions never name a provider, so admitting a provider is an addition here
and never an edit to the assertions.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable
from dataclasses import dataclass

from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPorts,
    OpaqueProviderRef,
)
from tests.providers.contract import bindings


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """One provider's binding of ``ExternalBootPorts``, described provider-neutrally.

    Every factory returns provider-neutral port values. A provider that needs host
    resources supplies test doubles for them inside ``build``; the suite never learns that
    it did, so the contract it proves is the same one for every provider.
    """

    name: str
    build: Callable[[], ExternalBootPorts]
    plan: Callable[[], ExternalBootPlan]
    activation: Callable[[ExternalBootMaterialization], ExternalBootActivationBinding]
    authority: Callable[[], OpaqueProviderRef]


def discover() -> list[ProviderBinding]:
    """Return every registered provider binding, ordered by name for stable parametrization."""
    discovered: list[ProviderBinding] = []
    for module in pkgutil.iter_modules(bindings.__path__):
        registered = importlib.import_module(f"{bindings.__name__}.{module.name}")
        binding = getattr(registered, "BINDING", None)
        if not isinstance(binding, ProviderBinding):
            raise TypeError(
                f"contract binding module {module.name!r} must expose a ProviderBinding BINDING"
            )
        discovered.append(binding)
    if not discovered:
        raise RuntimeError("no external-boot contract provider bindings are registered")
    return sorted(discovered, key=lambda entry: entry.name)

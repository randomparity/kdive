"""Dependency injection container for the runs.* job handlers (ADR-0268, #866)."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr

from kdive.providers.core.resolver import ProviderResolver


@dataclass(frozen=True, slots=True)
class RunHandlerPorts:
    """Dependencies shared by the install and boot job handlers."""

    resolver: ProviderResolver
    incarnation_credential: SecretStr

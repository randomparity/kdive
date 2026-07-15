"""Provider diagnostic registrations for production service assembly."""

from __future__ import annotations

from kdive.diagnostics.multiarch_gdb import diagnostic_contribution as local_diagnostics
from kdive.diagnostics.provider_contracts import DiagnosticProviderContribution
from kdive.providers.remote_libvirt.diagnostics.contribution import (
    diagnostic_contribution as remote_diagnostics,
)


def diagnostic_provider_contributions() -> tuple[DiagnosticProviderContribution, ...]:
    # local-libvirt has a single contribution (one dispatcher per contribution, keyed by
    # provider); it carries every local worker-vantage check — multiarch-gdb and pseries-fadump.
    return (local_diagnostics(), remote_diagnostics())

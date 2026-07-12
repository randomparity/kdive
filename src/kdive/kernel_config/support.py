"""Check a parsed kernel config against a feature's required clauses (ADR-0318, ADR-0330)."""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import Clause, FeatureRequirement


def _unmet(config: KernelConfig, clauses: tuple[Clause, ...]) -> tuple[Clause, ...]:
    return tuple(
        clause for clause in clauses if not any(config.is_enabled(symbol) for symbol in clause)
    )


def unmet_clauses(config: KernelConfig, feature: FeatureRequirement) -> tuple[Clause, ...]:
    """Clauses of ``feature.gate_required`` the config fails to enable (the refusal set)."""
    return _unmet(config, feature.gate_required)


def unmet_advertised_clauses(
    config: KernelConfig, feature: FeatureRequirement
) -> tuple[Clause, ...]:
    """Clauses of ``feature.advertised`` the config fails to enable (the advisory set)."""
    return _unmet(config, feature.advertised)


def missing_symbols(unmet: tuple[Clause, ...]) -> list[str]:
    return sorted({symbol for clause in unmet for symbol in clause})

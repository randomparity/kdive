"""Check a parsed kernel config against a feature's gate_required clauses (ADR-0318)."""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import Clause, FeatureRequirement


def unmet_clauses(config: KernelConfig, feature: FeatureRequirement) -> tuple[Clause, ...]:
    """The ``gate_required`` OR-groups with no enabled member (empty tuple = fully supported)."""
    return tuple(
        clause
        for clause in feature.gate_required
        if not any(config.is_enabled(symbol) for symbol in clause)
    )


def feature_supported(config: KernelConfig, feature: FeatureRequirement) -> bool:
    """True when every ``gate_required`` clause is satisfied by ``config``."""
    return not unmet_clauses(config, feature)


def missing_symbols(unmet: tuple[Clause, ...]) -> list[str]:
    """Flatten unmet clauses into a sorted symbol list for a refusal reason."""
    return sorted({symbol for clause in unmet for symbol in clause})

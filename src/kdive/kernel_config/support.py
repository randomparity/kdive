"""Check a parsed kernel config against a feature's clauses (ADR-0318, ADR-0322)."""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import Clause, FeatureRequirement


def _clauses_without_enabled(
    config: KernelConfig, clauses: tuple[Clause, ...]
) -> tuple[Clause, ...]:
    """The OR-group clauses with no enabled member (empty tuple = all satisfied)."""
    return tuple(
        clause for clause in clauses if not any(config.is_enabled(symbol) for symbol in clause)
    )


def unmet_clauses(config: KernelConfig, feature: FeatureRequirement) -> tuple[Clause, ...]:
    """The ``gate_required`` OR-groups with no enabled member (empty tuple = fully supported)."""
    return _clauses_without_enabled(config, feature.gate_required)


def unmet_advertised_clauses(
    config: KernelConfig, feature: FeatureRequirement
) -> tuple[Clause, ...]:
    """The ``advertised`` OR-groups with no enabled member (warn-only; ignores ``gate_required``).

    Used by the advertise-and-warn features (e.g. ``debuginfo``, ADR-0322) whose ``gate_required``
    is empty: they never refuse an action, but still warn when the uploaded config provably lacks
    their symbols.
    """
    return _clauses_without_enabled(config, feature.advertised)


def missing_symbols(unmet: tuple[Clause, ...]) -> list[str]:
    """Flatten unmet clauses into a sorted symbol list for a refusal reason."""
    return sorted({symbol for clause in unmet for symbol in clause})

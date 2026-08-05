"""Check a parsed kernel config against a feature's required clauses (ADR-0318, ADR-0330)."""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import BuiltIn, Clause, FeatureRequirement


def _needs_builtin(clause: Clause, *, has_initrd: bool) -> bool:
    """Whether ``=m`` fails this clause, given what the build uploaded (#1860)."""
    if clause.built_in is BuiltIn.REQUIRED:
        return True
    return clause.built_in is BuiltIn.UNLESS_INITRD and not has_initrd


def _satisfied(config: KernelConfig, clause: Clause, *, has_initrd: bool) -> bool:
    check = (
        config.is_builtin if _needs_builtin(clause, has_initrd=has_initrd) else config.is_enabled
    )
    return any(check(symbol) for symbol in clause.symbols)


def _unmet(
    config: KernelConfig, clauses: tuple[Clause, ...], *, has_initrd: bool
) -> tuple[Clause, ...]:
    return tuple(
        clause for clause in clauses if not _satisfied(config, clause, has_initrd=has_initrd)
    )


def unmet_clauses(
    config: KernelConfig, feature: FeatureRequirement, *, has_initrd: bool = False
) -> tuple[Clause, ...]:
    """Clauses of ``feature.gate_required`` the config fails to enable (the refusal set).

    ``has_initrd`` says whether the build uploaded an initrd artifact, which is what relieves an
    ``UNLESS_INITRD`` clause of needing ``=y``. It defaults to the strict reading, so a seam that
    does not supply it over-reports rather than falling silent.
    """
    return _unmet(config, feature.gate_required, has_initrd=has_initrd)


def unmet_advertised_clauses(
    config: KernelConfig, feature: FeatureRequirement, *, has_initrd: bool = False
) -> tuple[Clause, ...]:
    """Clauses of ``feature.advertised`` the config fails to enable (the advisory set).

    ``has_initrd`` carries the same meaning and the same strict default as in
    :func:`unmet_clauses`. Spelled out here too because this is the variant the live seam calls:
    omitting the keyword changes the verdict on an ``UNLESS_INITRD`` clause, in the over-reporting
    direction.
    """
    return _unmet(config, feature.advertised, has_initrd=has_initrd)


def missing_symbols(unmet: tuple[Clause, ...]) -> list[str]:
    return sorted({symbol for clause in unmet for symbol in clause.symbols})


def built_in_required_symbols(config: KernelConfig, unmet: tuple[Clause, ...]) -> list[str]:
    """The symbols of :func:`missing_symbols` the config does enable, but only as ``=m`` (#1860).

    The subset that separates "you do not have this" from "you have this in a form that cannot
    load in time" — without it, a payload naming ``VIRTIO_BLK`` against a config that contains
    ``CONFIG_VIRTIO_BLK=m`` reads as a kdive bug to the agent holding that config.
    """
    return [
        symbol
        for symbol in missing_symbols(unmet)
        if config.is_enabled(symbol) and not config.is_builtin(symbol)
    ]

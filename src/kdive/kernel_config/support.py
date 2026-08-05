"""Check a parsed kernel config against a feature's required clauses (ADR-0318, ADR-0330).

The clause model these checks read - which symbols a clause may name, when ``=m`` fails it, and
which arch it applies to - is ADR-0544, as widened by ADR-0545. Every conditional axis is supplied
by the caller (``has_initrd``, ``guest_builds_initramfs``, ``arch``) rather than read here, and
each defaults to the reading that over-reports rather than the one that falls silent.
"""

from __future__ import annotations

from kdive.kernel_config.parse import KernelConfig
from kdive.kernel_config.requirements import BuiltIn, Clause, FeatureRequirement


def _needs_builtin(clause: Clause, *, has_initrd: bool, guest_builds_initramfs: bool) -> bool:
    """Whether ``=m`` fails this clause, given how the target boots (#1860, #1881).

    ``UNLESS_INITRD`` asks whether *anything* can load a module before root is mounted, and two
    independent facts answer it: the build uploaded an initrd artifact, or the target boots through
    its own bootloader and builds its initramfs in-guest (ADR-0545). Either alone relieves the
    clause; a clause needs ``=y`` only when neither holds.
    """
    if clause.built_in is BuiltIn.REQUIRED:
        return True
    relieved = has_initrd or guest_builds_initramfs
    return clause.built_in is BuiltIn.UNLESS_INITRD and not relieved


def _satisfied(
    config: KernelConfig, clause: Clause, *, has_initrd: bool, guest_builds_initramfs: bool
) -> bool:
    needs_builtin = _needs_builtin(
        clause, has_initrd=has_initrd, guest_builds_initramfs=guest_builds_initramfs
    )
    check = config.is_builtin if needs_builtin else config.is_enabled
    return any(check(symbol) for symbol in clause.symbols)


def _applies_to(clause: Clause, *, arch: str | None) -> bool:
    """Whether ``clause`` is in scope for ``arch`` (#1859).

    An unscoped clause (``arches is None``) applies everywhere. A scoped clause applies only on a
    listed arch, and is skipped entirely when the arch is unknown: kdive would otherwise invent a
    requirement it cannot establish - reporting SERIAL_8250 missing against a config it cannot
    tell is ppc64le, where that symbol does not apply at all.

    Skipping under-reports, which is the wrong direction for a refusal set: an omitted arch would
    turn a refusal into a pass silently. That is why :func:`unmet_clauses` takes ``arch`` without a
    default, and why the invariant in ``tests/kernel_config/test_requirements.py`` allows a scoped
    clause only where every seam evaluating its feature supplies one (ADR-0544 §3, §7, #1875).
    """
    if clause.arches is None:
        return True
    return arch is not None and arch in clause.arches


def _unmet(
    config: KernelConfig,
    clauses: tuple[Clause, ...],
    *,
    has_initrd: bool,
    guest_builds_initramfs: bool,
    arch: str | None,
) -> tuple[Clause, ...]:
    return tuple(
        clause
        for clause in clauses
        if _applies_to(clause, arch=arch)
        and not _satisfied(
            config, clause, has_initrd=has_initrd, guest_builds_initramfs=guest_builds_initramfs
        )
    )


def unmet_clauses(
    config: KernelConfig,
    feature: FeatureRequirement,
    *,
    arch: str | None,
    has_initrd: bool = False,
    guest_builds_initramfs: bool = False,
) -> tuple[Clause, ...]:
    """Clauses of ``feature.gate_required`` the config fails to enable (the refusal set).

    ``has_initrd`` says whether the build uploaded an initrd artifact and
    ``guest_builds_initramfs`` whether the target builds its own in-guest; either relieves an
    ``UNLESS_INITRD`` clause of needing ``=y`` (ADR-0545). Both default to the strict reading, so a
    seam that does not supply one over-reports rather than falling silent.

    ``arch`` carries the clause's arch scope and **has no default**, unlike its two neighbours
    (#1875). Their strict default over-reports; an omitted arch under-reports, because
    :func:`_applies_to` skips a scoped clause it cannot place - and a skipped clause inside a
    refusal set is a refusal that silently became a pass. ``None`` still means unknown and still
    skips, but it has to be written down.
    """
    return _unmet(
        config,
        feature.gate_required,
        has_initrd=has_initrd,
        guest_builds_initramfs=guest_builds_initramfs,
        arch=arch,
    )


def unmet_advertised_clauses(
    config: KernelConfig,
    feature: FeatureRequirement,
    *,
    has_initrd: bool = False,
    guest_builds_initramfs: bool = False,
    arch: str | None = None,
) -> tuple[Clause, ...]:
    """Clauses of ``feature.advertised`` the config fails to enable (the advisory set).

    The three keywords carry the same meaning as in :func:`unmet_clauses`. ``arch`` keeps its
    unknown default here, where :func:`unmet_clauses` dropped it: this variant produces advisories,
    never refusals, so an omitted arch costs a warning that stays quiet rather than a gate that
    stops holding. Omitting any of them still changes the verdict - on an ``UNLESS_INITRD`` clause
    in the over-reporting direction, and on an arch-scoped clause in the under-reporting one.
    """
    return _unmet(
        config,
        feature.advertised,
        has_initrd=has_initrd,
        guest_builds_initramfs=guest_builds_initramfs,
        arch=arch,
    )


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

"""Per-family rootfs customizers and the family-name registry (ADR-0251).

Each :class:`~kdive.images.families.base.FamilyCustomizer` encodes how an OS family customizes a
base image into a kdive-ready rootfs (package install, kdump/sshd enable, readiness unit, image
normalization). The MVP ships :class:`~kdive.images.families.rhel.RhelFamily`; ``family_for``
resolves a catalog row's ``family`` name to its customizer. The registry lives here (not in the
provider) so the shared ``images`` layer can resolve a family without importing provider details.
"""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.images.families.base import FamilyCustomizer
from kdive.images.families.rhel import RhelFamily

_FAMILIES: dict[str, FamilyCustomizer] = {"rhel": RhelFamily()}


def family_for(name_or_family: str) -> FamilyCustomizer:
    """Resolve a FamilyCustomizer by family name.

    Args:
        name_or_family: The catalog row's ``family`` (e.g. ``"rhel"``).

    Returns:
        The matching :class:`~kdive.images.families.base.FamilyCustomizer`.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` naming the family and the available families
            when ``name_or_family`` is not implemented.
    """
    family = _FAMILIES.get(name_or_family)
    if family is None:
        raise CategorizedError(
            f"unknown rootfs family: {name_or_family}",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"family": name_or_family, "available": sorted(_FAMILIES)},
        )
    return family

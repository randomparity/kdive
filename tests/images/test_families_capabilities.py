"""Per-family capabilities() declarations (ADR-0287)."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability
from kdive.images.families.rhel import RhelFamily


def test_rhel_debug_capabilities() -> None:
    caps = RhelFamily().capabilities("debug", "fedora", "44")
    assert set(caps) == {
        Capability.SSH,
        Capability.SELINUX,
        Capability.KDUMP,
        Capability.DRGN,
    }
    assert Capability.AGENT not in caps


def test_rhel_build_capabilities() -> None:
    caps = RhelFamily().capabilities("build", "fedora", "44")
    assert set(caps) == {Capability.SELINUX, Capability.BUILD}


def test_rhel_capabilities_el_major_invariant() -> None:
    # EL8 and EL10 differ in packages() but not in the declared trait set.
    assert set(RhelFamily().capabilities("debug", "rocky", "8")) == set(
        RhelFamily().capabilities("debug", "rocky", "10")
    )

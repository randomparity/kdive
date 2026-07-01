"""_mac_tag mapping and the registry-iterated anti-drift guard (ADR-0287)."""

from __future__ import annotations

import pytest

from kdive.domain.catalog.images import Capability
from kdive.images.families import _FAMILIES
from kdive.images.families.base import FamilyCustomizer, _mac_tag


def test_mac_tag_selinux_permissive() -> None:
    assert _mac_tag("selinux-permissive") == Capability.SELINUX


def test_mac_tag_apparmor() -> None:
    assert _mac_tag("apparmor") == Capability.APPARMOR


def test_mac_tag_unmapped_raises_naming_posture() -> None:
    with pytest.raises(ValueError, match="tomoyo"):
        _mac_tag("tomoyo")


_KINDS = ("debug", "build")
# EVERY (distro, version) pair whose packages() output is distinct, so the evidence check covers
# the EL-major branch in RhelFamily.packages() (EL8/EL9 vs EL10/Fedora) — not just one
# representative. A tag declared but unbacked on *any* of these fails the guard.
_PROBE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "rhel": (("fedora", "44"), ("rocky", "8"), ("rocky", "9"), ("rocky", "10")),
    "debian": (("debian", "12"), ("debian", "13")),
}


def _evidenced(packages: tuple[str, ...], guest_mac: str, kind: str, tag: Capability) -> bool:
    if tag is Capability.SSH:
        return "openssh-server" in packages
    if tag in (Capability.SELINUX, Capability.APPARMOR):
        return tag == _mac_tag(guest_mac)
    if tag is Capability.KDUMP:
        return any(p in packages for p in ("kexec-tools", "kdump-tools"))
    if tag is Capability.DRGN:
        return any(p in packages for p in ("drgn", "python3-drgn"))
    if tag is Capability.BUILD:
        return kind == "build"
    return False  # any other declared tag has no evidence rule -> unbacked


@pytest.mark.parametrize("family", _FAMILIES.values(), ids=list(_FAMILIES))
def test_every_declared_tag_is_evidenced(family: FamilyCustomizer) -> None:
    for name, version in _PROBE_PAIRS[family.family]:
        for kind in _KINDS:
            packages = family.packages(kind, name, version)
            for tag in family.capabilities(kind, name, version):
                assert _evidenced(packages, family.guest_mac, kind, tag), (
                    f"{family.family}/{name}-{version}/{kind}: {tag} unbacked"
                )


@pytest.mark.parametrize("family", _FAMILIES.values(), ids=list(_FAMILIES))
def test_guest_mac_maps_to_a_tag(family: FamilyCustomizer) -> None:
    # A new family with an unmapped posture fails here, not at build-fs.
    assert _mac_tag(family.guest_mac) in (Capability.SELINUX, Capability.APPARMOR)


def test_no_local_family_declares_agent() -> None:
    for family in _FAMILIES.values():
        for name, version in _PROBE_PAIRS[family.family]:
            for kind in _KINDS:
                assert Capability.AGENT not in family.capabilities(kind, name, version)

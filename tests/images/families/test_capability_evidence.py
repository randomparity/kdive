"""_mac_tag mapping and the registry-iterated anti-drift guard (ADR-0287)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kdive.domain.catalog.images import Capability
from kdive.images.families import _FAMILIES
from kdive.images.families.base import CustomizeContext, FamilyCustomizer, _mac_tag
from kdive.images.rootfs_kinds import RootfsImageKind
from kdive.images.validation import GUEST_CONTRACT_PATHS


def test_mac_tag_selinux_permissive() -> None:
    assert _mac_tag("selinux-permissive") == Capability.SELINUX


def test_mac_tag_apparmor() -> None:
    assert _mac_tag("apparmor") == Capability.APPARMOR


def test_mac_tag_unmapped_raises_naming_posture() -> None:
    with pytest.raises(ValueError, match="tomoyo"):
        _mac_tag("tomoyo")


_KINDS: tuple[RootfsImageKind, ...] = ("debug", "build")
# EVERY (distro, version) pair whose packages() output is distinct, so the evidence check covers
# the EL-major branch in RhelFamily.packages() (EL8/EL9 vs EL10/Fedora) — not just one
# representative. A tag declared but unbacked on *any* of these fails the guard.
_PROBE_PAIRS: dict[str, tuple[tuple[str, str], ...]] = {
    "rhel": (("fedora", "44"), ("rocky", "8"), ("rocky", "9"), ("rocky", "10")),
    "debian": (("debian", "12"), ("debian", "13")),
}


def _evidenced(
    packages: tuple[str, ...], guest_mac: str, kind: RootfsImageKind, tag: Capability
) -> bool:
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


def _baked_paths(argv: list[str]) -> set[str]:
    """Guest paths the argv actually *creates*: ``--write dest:content`` / ``--upload src:dest``.

    Matches only file-creating destinations — not any substring of the joined argv — so a path
    merely *referenced* in a ``--run-command`` (never materialized) is not mistaken for a baked
    marker. The validator does an exact ``exists <path>``, so the guard must prove creation, not
    mention.
    """
    created: set[str] = set()
    for flag, value in zip(argv, argv[1:], strict=False):
        if flag == "--write":  # dest:content
            created.add(value.split(":", 1)[0])
        elif flag == "--upload":  # host-src:dest
            created.add(value.rsplit(":", 1)[1])
    return created


def test_guest_contract_markers_are_baked_by_declaring_families(tmp_path: Path) -> None:
    # The tests above prove a declared tag maps to an installed *package*. This proves the second
    # vocabulary — the guest-contract *file probe* (validation.GUEST_CONTRACT_PATHS) — is created
    # by customize_argv for every family that declares the tag, and NOT created when the tag is
    # absent. A phantom marker (a probe path no customize_argv writes, e.g. the never-written
    # drgn-ready) fails here, not at a live IMAGE_BUILD/upload; and a marker that leaked onto a
    # non-declaring image (which would wrongly satisfy that contract) fails too.
    readiness = tmp_path / "kdive-ready.service"
    readiness.write_text("[Unit]\n")
    for family in _FAMILIES.values():
        for name, version in _PROBE_PAIRS[family.family]:
            for kind in _KINDS:
                ctx = CustomizeContext(
                    kind=kind,
                    packages=family.packages(kind, name, version),
                    readiness_unit_path=readiness,
                    is_cloud_image=True,
                    cleanup=[],
                    distro=name,
                    version=version,
                )
                created = _baked_paths(family.customize_argv(ctx))
                declared = family.capabilities(kind, name, version)
                for element, path in GUEST_CONTRACT_PATHS.items():
                    baked = path in created
                    wants = Capability(element) in declared
                    assert baked == wants, (
                        f"{family.family}/{name}-{version}/{kind}: declares {element}={wants} but "
                        f"customize_argv {'omits' if wants else 'leaks'} its marker {path}"
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

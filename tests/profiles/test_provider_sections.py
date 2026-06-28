from __future__ import annotations

from kdive.domain.catalog.resources import ResourceKind
from kdive.profiles.provider_sections import (
    PROVIDER_SECTIONS,
    aliases_for,
)
from kdive.profiles.provisioning import (
    FaultInjectProfile,
    LibvirtProfile,
    RemoteLibvirtProfile,
)


def test_registry_covers_every_resource_kind() -> None:
    assert set(PROVIDER_SECTIONS) == set(ResourceKind)


def test_alias_is_the_resource_kind_value() -> None:
    for kind, spec in PROVIDER_SECTIONS.items():
        assert spec.alias == kind.value


def test_section_models_match_provisioning() -> None:
    assert PROVIDER_SECTIONS[ResourceKind.LOCAL_LIBVIRT].model is LibvirtProfile
    assert PROVIDER_SECTIONS[ResourceKind.REMOTE_LIBVIRT].model is RemoteLibvirtProfile
    assert PROVIDER_SECTIONS[ResourceKind.FAULT_INJECT].model is FaultInjectProfile


def test_aliases_for_filters_to_the_live_set() -> None:
    one = frozenset({ResourceKind.LOCAL_LIBVIRT})
    assert aliases_for(one) == frozenset({"local-libvirt"})
    assert aliases_for(frozenset()) == frozenset()

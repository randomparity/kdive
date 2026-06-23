"""Pin the local-libvirt provider's co-located ``KDIVE_LIBVIRT_*`` settings (ADR-0087)."""

from __future__ import annotations

from kdive.providers.local_libvirt import settings

_RT = frozenset({"worker", "reconciler"})


def test_uri_setting_fields() -> None:
    s = settings.LIBVIRT_URI
    assert s.name == "KDIVE_LIBVIRT_URI"
    assert s.default == "qemu:///system"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_allocation_cap_setting_fields() -> None:
    s = settings.LIBVIRT_ALLOCATION_CAP
    assert s.name == "KDIVE_LIBVIRT_ALLOCATION_CAP"
    assert s.default == "1"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_settings_list_is_the_two_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [settings.LIBVIRT_URI, settings.LIBVIRT_ALLOCATION_CAP]

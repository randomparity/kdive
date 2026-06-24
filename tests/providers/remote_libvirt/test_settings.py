"""Pin the remote-libvirt provider's operational ``KDIVE_REMOTE_LIBVIRT_*`` settings (ADR-0087)."""

from __future__ import annotations

from kdive.providers.remote_libvirt import settings

_RT = frozenset({"worker", "reconciler"})


def test_storage_pool_setting_fields() -> None:
    s = settings.REMOTE_LIBVIRT_STORAGE_POOL
    assert s.name == "KDIVE_REMOTE_LIBVIRT_STORAGE_POOL"
    assert s.default == "default"
    assert s.group == "remote-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_network_setting_fields() -> None:
    s = settings.REMOTE_LIBVIRT_NETWORK
    assert s.name == "KDIVE_REMOTE_LIBVIRT_NETWORK"
    assert s.default == "default"
    assert s.group == "remote-libvirt"
    assert s.processes == _RT


def test_machine_setting_fields() -> None:
    s = settings.REMOTE_LIBVIRT_MACHINE
    assert s.name == "KDIVE_REMOTE_LIBVIRT_MACHINE"
    assert s.default == "pc"
    assert s.group == "remote-libvirt"
    assert s.processes == _RT


def test_settings_list_is_the_three_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [
        settings.REMOTE_LIBVIRT_STORAGE_POOL,
        settings.REMOTE_LIBVIRT_NETWORK,
        settings.REMOTE_LIBVIRT_MACHINE,
    ]

"""Pin the local-libvirt provider's co-located ``KDIVE_LIBVIRT_*`` settings (ADR-0087)."""

from __future__ import annotations

import pytest

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


def test_tcg_multiplier_setting_fields() -> None:
    s = settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER
    assert s.name == "KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER"
    assert s.default == "10.0"
    assert s.group == "local-libvirt"
    assert s.processes == _RT
    assert s.secret is False


def test_tcg_multiplier_default_parses_to_ten() -> None:
    s = settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER
    assert s.default is not None
    assert s.parse(s.default) == 10.0


def test_tcg_multiplier_accepts_one_as_opt_out() -> None:
    assert settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("1") == 1.0


def test_tcg_multiplier_rejects_below_one() -> None:
    # A multiplier < 1 would make a TCG deadline tighter than the KVM baseline (ADR-0341).
    with pytest.raises(ValueError):
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("0.5")


def test_tcg_multiplier_rejects_non_float() -> None:
    with pytest.raises(ValueError):
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER.parse("abc")


def test_settings_list_is_the_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [
        settings.LIBVIRT_URI,
        settings.LIBVIRT_ALLOCATION_CAP,
        settings.LIBVIRT_TCG_DEADLINE_MULTIPLIER,
    ]

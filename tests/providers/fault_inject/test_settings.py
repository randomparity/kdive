"""Pin the fault-inject provider's co-located settings (ADR-0087).

A mutated default, a flipped ``secret`` flag (redaction correctness), or a dropped/renamed
``Setting`` must fail here — these are the surface mutmut targets.
"""

from __future__ import annotations

from kdive.providers.fault_inject import settings


def test_uri_setting_fields() -> None:
    s = settings.FAULT_INJECT_URI
    assert s.name == "KDIVE_FAULT_INJECT_URI"
    assert s.default == "fault-inject://local"
    assert s.group == "fault-inject"
    assert s.processes == frozenset({"worker", "reconciler"})
    assert s.secret is False


def test_allocation_cap_setting_fields() -> None:
    s = settings.FAULT_INJECT_ALLOCATION_CAP
    assert s.name == "KDIVE_FAULT_INJECT_ALLOCATION_CAP"
    assert s.default == "1"
    assert s.processes == frozenset({"worker", "reconciler"})
    assert s.secret is False


def test_seed_setting_fields() -> None:
    s = settings.FAULT_INJECT_SEED
    assert s.name == "KDIVE_FAULT_INJECT_SEED"
    assert s.default == "0"
    assert s.secret is False


def test_secret_ref_setting_is_marked_secret() -> None:
    s = settings.FAULT_INJECT_SECRET_REF
    assert s.name == "KDIVE_FAULT_INJECT_SECRET_REF"
    assert s.default == "fault-inject/console-sentinel"
    # the secret flag drives redaction registration — a flip must fail loudly
    assert s.secret is True


def test_settings_list_is_the_four_declared_settings_in_order() -> None:
    assert settings.SETTINGS == [
        settings.FAULT_INJECT_URI,
        settings.FAULT_INJECT_ALLOCATION_CAP,
        settings.FAULT_INJECT_SEED,
        settings.FAULT_INJECT_SECRET_REF,
    ]


def test_only_the_secret_ref_is_secret() -> None:
    assert [s.secret for s in settings.SETTINGS] == [False, False, False, True]

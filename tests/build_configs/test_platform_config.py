"""Platform always-on kernel-config requirements (ADR-0316, #1036).

The surfaced payload must be derived from the same constants the build guard enforces
(``surfaced == enforced``), and the constants must not drift from the seeded ``kdump.config``.
"""

from __future__ import annotations

from kdive.build_configs.platform_config import (
    PLATFORM_REQUIRED_CONFIG,
    REQUIRED_KERNEL_CONFIG,
    platform_required_payload,
)
from kdive.build_configs.seed import KDUMP_FRAGMENT_PATH


def _kdump_declarations() -> dict[str, str]:
    declared: dict[str, str] = {}
    for line in KDUMP_FRAGMENT_PATH.read_text().splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, _, value = line.partition("=")
            declared[key] = value
    return declared


def test_payload_is_derived_from_the_enforced_constants() -> None:
    payload = platform_required_payload()
    assert payload["all_of"] == dict(PLATFORM_REQUIRED_CONFIG.required)
    assert payload["any_of"] == [list(group) for group in REQUIRED_KERNEL_CONFIG]


def test_drift_guard_all_of_symbols_declared_in_seed() -> None:
    declared = _kdump_declarations()
    for symbol, value in PLATFORM_REQUIRED_CONFIG.required.items():
        assert declared.get(symbol) == value, symbol


def test_drift_guard_each_or_group_has_a_seeded_member() -> None:
    declared = _kdump_declarations()
    for group in REQUIRED_KERNEL_CONFIG:
        assert any(declared.get(sym) == "y" for sym in group), group

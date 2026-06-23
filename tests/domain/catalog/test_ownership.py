"""Pin the row-ownership partition shared by declarative and runtime inventory (ADR-0112)."""

from __future__ import annotations

from kdive.domain.catalog.ownership import ManagedBy


def test_managed_by_member_values() -> None:
    assert ManagedBy.CONFIG.value == "config"
    assert ManagedBy.DISCOVERY.value == "discovery"
    assert ManagedBy.RUNTIME.value == "runtime"


def test_managed_by_is_exactly_three_members() -> None:
    assert {m.value for m in ManagedBy} == {"config", "discovery", "runtime"}


def test_managed_by_is_a_str_enum() -> None:
    # StrEnum members compare equal to their string value (used in SQL/JSON boundaries)
    assert ManagedBy.CONFIG == "config"

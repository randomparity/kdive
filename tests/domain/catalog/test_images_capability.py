"""The closed image-capability vocabulary (ADR-0286)."""

from __future__ import annotations

from kdive.domain.catalog.images import Capability


def test_capability_values_are_the_closed_build_vocabulary() -> None:
    assert {c.value for c in Capability} == {"agent", "kdump", "drgn", "build", "helpers"}


def test_capability_is_str_subclass_for_db_and_membership() -> None:
    # StrEnum: a Capability compares equal to its wire string, so a DB text[] round-trips and
    # membership works in both directions.
    assert Capability.KDUMP == "kdump"
    assert Capability.KDUMP in ["kdump"]
    assert "kdump" in [Capability.KDUMP]

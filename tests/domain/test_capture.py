"""Tests for the capture-method vocabulary (`kdive.domain.capture`)."""

from __future__ import annotations

from kdive.domain.capture import CaptureMethod


def test_vocabulary_has_the_expected_methods() -> None:
    # fadump (ADR-0349) is the pseries firmware-assisted variant of the kdump capture family.
    assert {m.value for m in CaptureMethod} == {
        "console",
        "host_dump",
        "gdbstub",
        "kdump",
        "fadump",
    }

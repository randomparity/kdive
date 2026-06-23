"""Pin the DB single-flight fence error contract."""

from __future__ import annotations

from kdive.db import probe_fence
from kdive.db.probe_fence import ProbeInFlightError


def test_probe_in_flight_error_is_an_exception() -> None:
    assert issubclass(ProbeInFlightError, Exception)


def test_probe_in_flight_error_can_carry_a_subject_message() -> None:
    err = ProbeInFlightError("subject-key")
    assert str(err) == "subject-key"


def test_module_exports_only_the_error() -> None:
    assert probe_fence.__all__ == ["ProbeInFlightError"]

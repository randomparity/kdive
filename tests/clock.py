"""A frozen-clock test helper for billing/lease assertions (issue #931).

Some service paths read ``datetime.now(UTC)`` at the moment they act — release/reconcile
stamps ``active_ended_at`` (``services/allocation/release.py``), renew samples the clamp
ceiling (``services/allocation/renew.py``). A test that seeds the *other* end of such an
interval from its own independent wall-clock read and then asserts an **exact** derived
amount is racing two clocks: whatever real time elapses between the two reads leaks into
the result. Under parallel (`pytest -n`) load that drift was ~120 ms — enough to bill
``2.0000333h × 3.0 = 6.0001`` against an expected exact ``6.0000`` and fail (issues #854,
#931).

When a test must assert an exact amount across a service clock read, monkeypatch the
service module's ``datetime`` with :class:`FrozenClock` and seed the interval relative to
the *same* frozen instant, so the billed interval is exact regardless of run-time drift::

    frozen = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(release_service, "datetime", FrozenClock(frozen))
    # seed active_started_at = frozen - 2h  ->  release stamps active_ended_at = frozen
    # -> active_hours is exactly 2.0, billed exactly rate * 2h

The alternative — asserting a tolerance band (``5.9 < net < 6.1``) — is correct when the
exact value is not load-bearing; several reconcile/sweep tests use it. Prefer the frozen
clock when the exactness of the amount is itself the thing under test.
"""

from __future__ import annotations

from datetime import datetime


class FrozenClock:
    """A ``datetime`` stand-in whose ``now`` returns a fixed instant.

    Monkeypatch it over a service module's module-level ``datetime`` name
    (``monkeypatch.setattr(service_module, "datetime", FrozenClock(instant))``) so every
    ``datetime.now(...)`` the service makes returns ``instant`` instead of an independently
    sampled wall clock. Only ``now`` is stubbed; construct ``datetime`` values the normal
    way and pass the same ``instant`` in.
    """

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self, _tz: object = None) -> datetime:
        """Return the frozen instant, ignoring any ``tz`` argument (it is already aware)."""
        return self._instant

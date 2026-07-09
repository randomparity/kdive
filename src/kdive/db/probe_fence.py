"""The shared single-flight fence error for DB-backed liveness probes.

The egress probe (:mod:`kdive.diagnostics.egress_probe`) registers a row guarded by a
partial-unique index so only one probe per subject can be live at a time. When a second
caller loses that race, the registration raises :class:`ProbeInFlightError` carrying the
conflicting subject key, so the check reports "a probe is already in flight" rather than a
generic registration failure (the cross-process second-caller signal).
"""

from __future__ import annotations


class ProbeInFlightError(Exception):
    """A live probe row already exists for this subject — the DB single-flight fence fired.

    Distinct from a backend-down error so the check can report "a probe is already in flight"
    rather than a generic registration failure (the cross-process second-caller signal).
    """


__all__ = ["ProbeInFlightError"]

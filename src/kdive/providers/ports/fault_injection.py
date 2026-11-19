"""Fault-injection inspector port (ADR-0825).

An optional ``ProviderRuntime`` port: only local-libvirt wires it. It reads the state of the
debugfs fault-injection knobs the reviewed in-guest helper exposes (ADR-0822) and returns a
validated, integer-only value; guest text never crosses this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from kdive.domain.errors import CategorizedError, ErrorCategory

# The closed knob set and, per knob, the helper's parameter names (ADR-0822). The helper's own
# `read` output is held to this table by a test, so the two cannot drift silently.
FAULT_KNOB_PARAMETERS: Mapping[str, tuple[str, ...]] = {
    "failslab": ("probability", "interval", "times", "space", "verbose"),
    "fail_page_alloc": ("probability", "interval", "times", "space", "verbose", "min-order"),
}

# Inclusive (minimum, maximum) per helper parameter, mirroring the helper's own bounds table
# (ADR-0822); a test parses the helper script and fails if the two drift. `reset` writes the
# helper's defaults, which are not restated here.
FAULT_PARAMETER_BOUNDS: Mapping[str, tuple[int, int]] = {
    "probability": (0, 100),
    "interval": (1, 1_000_000),
    "times": (-1, 1_000_000),
    "space": (0, 1_073_741_824),
    "verbose": (0, 2),
    "min-order": (0, 10),
}

# What `set` accepts beyond the helper's own bounds (ADR-0826): it only arms, and only finitely,
# so a guest that can no longer run `reset` still exhausts the fault by itself.
ARMING_BOUNDS: Mapping[str, tuple[int, int]] = {
    **FAULT_PARAMETER_BOUNDS,
    "probability": (1, 100),
    "times": (1, FAULT_PARAMETER_BOUNDS["times"][1]),
}


def _rejection(reason: str, detail: str, **data: object) -> CategorizedError:
    return CategorizedError(
        detail,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": reason, **data},
    )


def validate_knob(knob: str) -> None:
    """Reject a knob name outside the closed allowlist with ``reason=unknown_knob``."""
    if knob not in FAULT_KNOB_PARAMETERS:
        raise _rejection(
            "unknown_knob",
            f"unknown knob; use one of {', '.join(sorted(FAULT_KNOB_PARAMETERS))}",
            accepted_values=sorted(FAULT_KNOB_PARAMETERS),
        )


def validate_set_request(knob: str, parameters: Mapping[str, int]) -> None:
    """Reject a `set` the tool contract does not allow, before any guest call (ADR-0826).

    Raises ``CategorizedError`` (``CONFIGURATION_ERROR``) with ``details.reason`` one of
    ``unknown_knob``, ``parameter_not_applicable`` or ``parameter_out_of_range``.
    """
    validate_knob(knob)
    for name in ("probability", "times"):
        if name not in parameters:
            raise _rejection(
                "parameter_out_of_range",
                f"{name} is required: a fault must be armed with a finite times and a probability",
                parameter=name,
                minimum=ARMING_BOUNDS[name][0],
                maximum=ARMING_BOUNDS[name][1],
            )
    for name, value in parameters.items():
        if name not in FAULT_KNOB_PARAMETERS[knob]:
            raise _rejection(
                "parameter_not_applicable",
                f"{knob} has no {name} parameter; omit it",
                parameter=name,
            )
        low, high = ARMING_BOUNDS[name]
        if type(value) is not int or not low <= value <= high:
            raise _rejection(
                "parameter_out_of_range",
                f"{name} must be an integer from {low} to {high}",
                parameter=name,
                minimum=low,
                maximum=high,
            )


# The agent-facing limits of one read, stated in the tool docstring: the reply cap in bytes and the
# worst-case call time in seconds (one retry window plus one attempt; a test ties the provider's
# timeouts to this figure).
REPLY_BYTE_CAP = 4096
READ_WORST_CASE_SECONDS = 25


@dataclass(frozen=True, slots=True)
class FaultKnobState:
    """One knob's state; ``parameters`` is empty when the kernel lacks the knob."""

    present: bool
    parameters: Mapping[str, int]

    @property
    def armed(self) -> bool:
        """True when the knob can fail allocations: probability > 0 and times != 0."""
        return (
            self.present
            and self.parameters.get("probability", 0) > 0
            and self.parameters.get("times", 0) != 0
        )


class FaultInjectionInspector(Protocol):
    """Read and change the fault-injection knobs of one guest over its loopback SSH transport.

    Every call runs one fixed ``kdive-faultinject`` argv (ADR-0822); the name predates the
    mutating methods (ADR-0826).
    """

    def read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]:
        """Return the state of every knob in ``FAULT_KNOB_PARAMETERS``.

        Raises ``CategorizedError``: ``CONFIGURATION_ERROR`` (``data.reason``
        ``debugfs_not_mounted``, ``fault_injection_unsupported`` or ``helper_not_installed``),
        ``TRANSPORT_FAILURE`` for an ssh fault, ``INFRASTRUCTURE_FAILURE`` for any other failure.
        """
        ...

    def set_knob(
        self, *, transport_handle: str, key_path: str, knob: str, parameters: Mapping[str, int]
    ) -> dict[str, FaultKnobState]:
        """Arm ``knob`` with ``parameters`` (see ``validate_set_request``); return its state.

        Raises the same errors as ``read_state``, plus ``CONFIGURATION_ERROR`` ``data.reason``
        ``knob_absent`` and the ``validate_set_request`` rejections.
        """
        ...

    def reset(
        self, *, transport_handle: str, key_path: str, knob: str | None
    ) -> dict[str, FaultKnobState]:
        """Write the kernel defaults to ``knob``, or to every present knob when ``None``.

        Returns the state of the knob(s) touched. Raises as ``set_knob`` does.
        """
        ...

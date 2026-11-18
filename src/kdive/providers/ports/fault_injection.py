"""Fault-injection inspector port (ADR-0825).

An optional ``ProviderRuntime`` port: only local-libvirt wires it. It reads the state of the
debugfs fault-injection knobs the reviewed in-guest helper exposes (ADR-0822) and returns a
validated, integer-only value; guest text never crosses this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

# The closed knob set and, per knob, the helper's parameter names (ADR-0822). The helper's own
# `read` output is held to this table by a test, so the two cannot drift silently.
FAULT_KNOB_PARAMETERS: Mapping[str, tuple[str, ...]] = {
    "failslab": ("probability", "interval", "times", "space", "verbose"),
    "fail_page_alloc": ("probability", "interval", "times", "space", "verbose", "min-order"),
}


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
    """Read the fault-injection knob state of one guest over its loopback SSH transport."""

    def read_state(self, *, transport_handle: str, key_path: str) -> dict[str, FaultKnobState]:
        """Return the state of every knob in ``FAULT_KNOB_PARAMETERS``.

        Raises ``CategorizedError``: ``CONFIGURATION_ERROR`` (``data.reason``
        ``debugfs_not_mounted``, ``fault_injection_unsupported`` or ``helper_not_installed``),
        ``TRANSPORT_FAILURE`` for an ssh fault, ``INFRASTRUCTURE_FAILURE`` for any other failure.
        """
        ...

"""Fault-inject introspection ports."""

from __future__ import annotations

from kdive.providers.ports import IntrospectOutput, LiveScriptOutput


class FaultInjectIntrospect:
    """VmcoreIntrospector + LiveIntrospector ports: synthetic introspection output."""

    def from_vmcore(
        self, *, vmcore_ref: str, debuginfo_ref: str, expected_build_id: str
    ) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def introspect_live(self, *, transport_handle: str, helper: str) -> IntrospectOutput:
        return IntrospectOutput(tasks={}, modules={}, sysinfo={}, truncated=False)

    def run_script(
        self, *, transport_handle: str, script: str, timeout_sec: float
    ) -> LiveScriptOutput:
        # fault-inject does not advertise the "live-script" mode, so the descriptor gate rejects
        # before this is reached; the synthetic body only satisfies the port for ty (ADR-0240).
        return LiveScriptOutput(output="", truncated=False)

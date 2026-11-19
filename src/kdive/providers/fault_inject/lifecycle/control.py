"""Fault-inject Control plane."""

from __future__ import annotations

from kdive.domain.operations.jobs import PowerAction
from kdive.providers.fault_inject.lifecycle.hotplug import FaultInjectHotplug


class FaultInjectControl:
    def __init__(self, hotplug: FaultInjectHotplug | None = None) -> None:
        self._hotplug = hotplug

    def power(self, domain_name: str, action: PowerAction) -> None:
        # Hot-plugged devices are live-only (ADR-0823): the table follows the power state.
        if self._hotplug is not None:
            self._hotplug.note_power(domain_name, action)

    def force_crash(self, domain_name: str) -> None:
        return None

    def diagnostic_sysrq(self, domain_name: str, trigger: str) -> None:
        return None

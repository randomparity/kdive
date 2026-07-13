"""Fault-inject Install and Boot planes."""

from __future__ import annotations

from uuid import UUID

from kdive.providers.ports.lifecycle import InstallRequest


class FaultInjectInstall:
    def install(self, request: InstallRequest) -> None:
        del request
        return None

    def boot(self, system_id: UUID, *, accel: str | None = None) -> None:
        del system_id, accel
        return None

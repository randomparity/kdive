"""Compatibility exports for debug session tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from kdive.mcp.tools.debug.sessions.lifecycle import (
    _GDBSTUB,
    _AttachRequest,
    _insert_session_locked,
    _InsertSession,
    _resolved_connector_for_run,
    _resolved_detach_resources,
)
from kdive.mcp.tools.debug.sessions.registrar import (
    DebugSessionHandlers as _RegistrarDebugSessionHandlers,
)
from kdive.mcp.tools.debug.sessions.registrar import register

if TYPE_CHECKING:
    from kdive.mcp.tools.debug.operations import DebugRuntimeResolver
    from kdive.observability.debug_session_telemetry import DebugSessionTelemetry
    from kdive.providers.core.resolver import ProviderResolver
    from kdive.security.secrets.secret_registry import SecretRegistry


class DebugSessionHandlers(_RegistrarDebugSessionHandlers):
    """Compatibility handler that preserves package-level monkeypatch seams."""

    @classmethod
    def from_resolver(
        cls,
        resolver: ProviderResolver,
        *,
        runtime_resolver: DebugRuntimeResolver | None,
        insert_session_locked: _InsertSession | None = None,
        secret_registry: SecretRegistry,
        telemetry: DebugSessionTelemetry | None = None,
    ) -> DebugSessionHandlers:
        return cls(
            connector_for_run=_resolved_connector_for_run(resolver),
            detach_resources=_resolved_detach_resources(resolver, runtime_resolver),
            insert_session_locked=(
                _insert_session_locked if insert_session_locked is None else insert_session_locked
            ),
            secret_registry=secret_registry,
            telemetry=telemetry,
        )


__all__ = [
    "DebugSessionHandlers",
    "_AttachRequest",
    "_GDBSTUB",
    "_insert_session_locked",
    "register",
]

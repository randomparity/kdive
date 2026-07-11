"""Shared introspection tool gates."""

from __future__ import annotations

from kdive.mcp.responses import ToolResponse
from kdive.mcp.tools._common import capability_unsupported as _capability_unsupported
from kdive.providers.core.runtime import ProviderRuntime
from kdive.providers.ports.lifecycle import IntrospectionMode

_OFFLINE_VMCORE: IntrospectionMode = "offline-vmcore"
_LIVE_INTROSPECTION: IntrospectionMode = "live"
_LIVE_SCRIPT: IntrospectionMode = "live-script"


def _require_introspection(
    object_id: str, runtime: ProviderRuntime, mode: IntrospectionMode
) -> ToolResponse | None:
    """Reject an introspection mode the bound provider's descriptor lacks (ADR-0209).

    Returns a ``capability_unsupported`` ``configuration_error`` on a miss (no port is touched), or
    ``None`` when the provider advertises ``mode``. The check reads ``supported_introspection`` and
    never branches on ``ResourceKind``.
    """
    if mode in runtime.support.introspection:
        return None
    return _capability_unsupported(
        object_id,
        capability=f"introspection:{mode}",
        provider=runtime.support.component_sources.provider,
        supported=sorted(runtime.support.introspection),
    )

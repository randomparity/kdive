"""ProviderRuntime capability-descriptor default tests (ADR-0208).

The descriptor (``supported_capture_methods`` + ``supported_debug_transports`` +
``supported_introspection``) is fail-closed: an unconfigured runtime advertises *no*
capability, so the surface can never report a stubbed plane as working.
"""

from __future__ import annotations

from typing import Any, cast

from kdive.providers.core.runtime import ProviderRuntime


def _unconfigured_runtime() -> ProviderRuntime:
    """Build a runtime with only the required ports, none of the descriptor fields set."""
    port = cast(Any, object())
    return ProviderRuntime(
        profile_policy=port,
        provisioner=port,
        builder=port,
        installer=port,
        booter=port,
        connector=port,
        controller=port,
        retriever=port,
        crash_postmortem=port,
        vmcore_introspector=port,
        live_introspector=port,
    )


def test_unconfigured_runtime_reports_empty_for_every_capability_field() -> None:
    runtime = _unconfigured_runtime()

    assert runtime.supported_capture_methods == frozenset()
    assert runtime.supported_debug_transports == frozenset()
    assert runtime.supported_introspection == frozenset()

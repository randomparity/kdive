"""ProviderRuntime capability-descriptor default tests (ADR-0208).

The descriptor (``supported_capture_methods`` + ``supported_debug_transports`` +
``supported_introspection``) is fail-closed: an unconfigured runtime advertises *no*
capability, so the surface can never report a stubbed plane as working.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport


def _unconfigured_runtime() -> ProviderRuntime:
    """Build a runtime with only the required ports, none of the descriptor fields set."""
    port = cast(Any, object())
    return ProviderRuntime(
        profile_policy=port,
        provisioner=port,
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

    assert runtime.support.capture_methods == frozenset()
    assert runtime.support.debug_transports == frozenset()
    assert runtime.support.introspection == frozenset()


def test_runtime_rejects_advertised_snapshots_without_snapshotter() -> None:
    support = ProviderSupport(supports_snapshots=True)

    with pytest.raises(ValueError, match=r"support\.supports_snapshots.*snapshot"):
        replace(_unconfigured_runtime(), support=support)


def test_runtime_rejects_snapshotter_without_advertised_snapshots() -> None:
    with pytest.raises(ValueError, match=r"support\.supports_snapshots.*snapshot"):
        replace(_unconfigured_runtime(), snapshot=cast(Any, object()))


def test_runtime_rejects_advertised_traffic_capture_without_capturer() -> None:
    support = ProviderSupport(supports_traffic_capture=True)

    with pytest.raises(ValueError, match=r"support\.supports_traffic_capture.*traffic_capturer"):
        replace(_unconfigured_runtime(), support=support)


def test_runtime_rejects_traffic_capturer_without_advertised_support() -> None:
    with pytest.raises(ValueError, match=r"support\.supports_traffic_capture.*traffic_capturer"):
        replace(_unconfigured_runtime(), traffic_capturer=cast(Any, object()))


def test_runtime_accepts_advertised_capabilities_with_matching_ports() -> None:
    port = cast(Any, object())
    support = ProviderSupport(supports_snapshots=True, supports_traffic_capture=True)

    runtime = replace(
        _unconfigured_runtime(),
        support=support,
        snapshot=port,
        traffic_capturer=port,
    )

    assert runtime.support == support

"""The TrafficCapturer port is fail-closed on the runtime (ADR-0384)."""

from __future__ import annotations

from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport
from kdive.providers.ports.traffic import TrafficCapturer


def test_traffic_capture_is_fail_closed_by_default() -> None:
    assert ProviderSupport.__dataclass_fields__["supports_traffic_capture"].default is False
    assert ProviderRuntime.__dataclass_fields__["traffic_capturer"].default is None


def test_traffic_capturer_is_a_runtime_protocol() -> None:
    # Structural check: an object with attach/detach satisfies the protocol at type-check time;
    # here we assert the protocol names the two primitives the handler drives.
    assert hasattr(TrafficCapturer, "attach")
    assert hasattr(TrafficCapturer, "detach")

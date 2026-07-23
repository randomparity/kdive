"""The TrafficCapturer port is fail-closed on the runtime (ADR-0385)."""

from __future__ import annotations

from kdive.providers.core.runtime import ProviderRuntime, ProviderSupport
from kdive.providers.ports.traffic import TrafficCapturer


def test_traffic_capture_is_fail_closed_by_default() -> None:
    assert ProviderSupport.__dataclass_fields__["supports_traffic_capture"].default is False
    assert ProviderRuntime.__dataclass_fields__["traffic_capturer"].default is None


def test_traffic_capturer_names_the_file_side_primitives() -> None:
    # Structural check: the protocol names attach/detach plus the provider-dispatched file side
    # (ADR-0432) the handler drives — prepare/captured_size/fetch/reclaim/write_remediation — so a
    # remote provider can own where the pcap lives, not just how it is captured.
    for name in (
        "prepare",
        "attach",
        "detach",
        "captured_size",
        "fetch",
        "reclaim",
        "write_remediation",
    ):
        assert hasattr(TrafficCapturer, name)

"""Fault-inject introspection: synthetic output; the live-script mode stays unadvertised."""

from __future__ import annotations

from kdive.providers.fault_inject.debug.introspect import FaultInjectIntrospect


def test_run_script_returns_synthetic_output() -> None:
    out = FaultInjectIntrospect().run_script(
        transport_handle="x", script="print(1)", timeout_sec=5.0
    )
    assert out.output == ""
    assert out.truncated is False

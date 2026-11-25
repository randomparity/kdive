"""``ProviderRuntime.for_resource`` capabilities hook is additive (ADR-0074, #3716)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from kdive.providers.core.runtime import ProviderRuntime, ResourceBindingCapabilities
from kdive.providers.fault_inject import composition


def _base() -> ProviderRuntime:
    return composition.build_runtime()


def test_a_provider_without_the_hook_ignores_capabilities() -> None:
    runtime = replace(
        _base(),
        binding=ResourceBindingCapabilities(rebind_for_resource=lambda name: bound(name)),
    )
    bound_to: list[str] = []

    def bound(name: str) -> ProviderRuntime:
        bound_to.append(name)
        return runtime

    assert runtime.for_resource("r1", {"fault_rate": {"provision": 1.0}}) is runtime
    assert bound_to == ["r1"]


def test_a_provider_with_no_binding_returns_itself() -> None:
    runtime = replace(_base(), binding=None)

    assert runtime.for_resource("r1", {"seed": 1}) is runtime


def test_the_hook_receives_the_name_and_capabilities() -> None:
    seen: list[tuple[str, Mapping[str, object]]] = []
    runtime = _base()
    target = replace(runtime, binding=None)

    def hook(name: str, capabilities: Mapping[str, object]) -> ProviderRuntime:
        seen.append((name, capabilities))
        return target

    hooked = replace(
        runtime,
        binding=ResourceBindingCapabilities(
            rebind_for_resource=lambda _name: runtime, rebind_with_capabilities=hook
        ),
    )

    assert hooked.for_resource("r1", {"seed": 1}) is target
    assert seen == [("r1", {"seed": 1})]
    assert hooked.for_resource("r1") is runtime

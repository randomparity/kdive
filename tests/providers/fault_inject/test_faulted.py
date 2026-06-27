"""Tests for the faulting wrapper that threads the seeded engine into the mock ports.

ADR-0074: a thin `FaultedProvisioning` / `FaultedInstall` consults a `FaultEngine` before
delegating to the happy-path port — a drawn `fail` raises `CategorizedError(category)`, a
drawn `latency` blocks the (sync) port via an injected `sleep_s` seam, and `attempt` is a
caller-supplied durable input (default 1), never a port-held counter.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import UUID, uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.fault_inject.faulting.engine import FaultDecision, FaultEngine, FaultPlane
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.providers.fault_inject.lifecycle.faulted import (
    FaultedInstall,
    FaultedProvisioning,
    _apply,
)
from kdive.providers.fault_inject.lifecycle.install import FaultInjectInstall
from kdive.providers.fault_inject.lifecycle.provisioning import FaultInjectProvisioning
from kdive.providers.ports.lifecycle import InstallRequest

_SYSTEM = UUID("00000000-0000-0000-0000-0000000000aa")
_RUN = UUID("00000000-0000-0000-0000-0000000000bb")
_PROFILE = cast(ProvisioningProfile, object())
_INSTALL_REQUEST = InstallRequest(
    system_id=_SYSTEM,
    run_id=_RUN,
    kernel_ref="kernel-ref",
    cmdline="console=ttyS0",
)


def _noop_sleep(_delay: float) -> None:
    return None


class _SpyEngine:
    """Records ``decide`` kwargs and returns a no-op decision."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def decide(self, *, system_id: UUID, plane: FaultPlane, attempt: int) -> FaultDecision:
        self.calls.append({"system_id": system_id, "plane": plane, "attempt": attempt})
        return FaultDecision(fail=False, category=None, latency_s=0.0)


class _SpyProvisioningInner:
    def __init__(self) -> None:
        self.provision_calls: list[tuple[UUID, object]] = []
        self.reprovision_calls: list[tuple[UUID, object]] = []
        self.teardown_calls: list[str] = []

    def provision(self, system_id: UUID, profile: object) -> str:
        self.provision_calls.append((system_id, profile))
        return "spy-domain"

    def reprovision(self, system_id: UUID, profile: object) -> str:
        self.reprovision_calls.append((system_id, profile))
        return "spy-domain"

    def teardown(self, domain_name: str) -> None:
        self.teardown_calls.append(domain_name)


class _SpyInstallInner:
    def __init__(self) -> None:
        self.install_calls: list[InstallRequest] = []
        self.boot_calls: list[UUID] = []

    def install(self, request: InstallRequest) -> None:
        self.install_calls.append(request)

    def boot(self, system_id: UUID) -> None:
        self.boot_calls.append(system_id)


def _provision(
    engine: FaultEngine,
    *,
    attempt_for: Callable[[UUID], int] = lambda _sid: 1,
    sleep_s: Callable[[float], None] = _noop_sleep,
) -> FaultedProvisioning:
    inventory = FaultInjectInventory()
    return FaultedProvisioning(
        FaultInjectProvisioning(inventory), engine, attempt_for=attempt_for, sleep_s=sleep_s
    )


def _seed_that_fails(plane: FaultPlane) -> FaultEngine:
    """An engine certain to draw a failure for ``plane`` (fault_rate 1.0)."""
    return FaultEngine(seed=7, fault_rate={plane.value: 1.0}, max_latency_s={})


def _seed_that_never_fails(plane: FaultPlane, *, max_latency_s: float = 0.0) -> FaultEngine:
    return FaultEngine(
        seed=7, fault_rate={plane.value: 0.0}, max_latency_s={plane.value: max_latency_s}
    )


def test_provision_fail_draw_raises_categorized_error_with_catalog_category() -> None:
    engine = _seed_that_fails(FaultPlane.PROVISION)
    wrapper = _provision(engine)
    with pytest.raises(CategorizedError) as exc:
        wrapper.provision(_SYSTEM, _PROFILE)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE


def test_fail_decision_without_category_is_an_invariant_error() -> None:
    decision = FaultDecision(fail=True, category=None, latency_s=0.0)

    with pytest.raises(RuntimeError, match="without a category"):
        _apply(decision, _noop_sleep)


def test_provision_no_fail_draw_delegates_and_returns_synthetic_domain() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION)
    wrapper = _provision(engine)
    domain = wrapper.provision(_SYSTEM, _PROFILE)
    assert domain == f"fault-inject-{_SYSTEM}"


def test_provision_latency_sleeps_for_the_engine_computed_delay() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    recorded: list[float] = []
    wrapper = _provision(engine, sleep_s=recorded.append)
    wrapper.provision(_SYSTEM, _PROFILE)
    expected = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1).latency_s
    assert recorded == [expected]
    assert expected > 0.0  # a real seed-derived delay, not a no-op


def test_latency_and_fail_both_drawn_sleeps_before_raising() -> None:
    # A slow-then-failing op spends its delay (ADR-0074 _apply ordering): the sleep is recorded
    # AND the failure is raised, proving the sleep happens before the raise.
    engine = FaultEngine(
        seed=7, fault_rate={FaultPlane.PROVISION.value: 1.0}, max_latency_s={"provision": 1000.0}
    )
    recorded: list[float] = []
    wrapper = _provision(engine, sleep_s=recorded.append)
    with pytest.raises(CategorizedError) as exc:
        wrapper.provision(_SYSTEM, _PROFILE)
    assert exc.value.category is ErrorCategory.PROVISIONING_FAILURE
    expected = engine.decide(system_id=_SYSTEM, plane=FaultPlane.PROVISION, attempt=1).latency_s
    assert recorded == [expected] and expected > 0.0  # the delay was spent before the raise


def test_provision_absent_plane_config_neither_sleeps_nor_raises() -> None:
    engine = FaultEngine(seed=7, fault_rate={}, max_latency_s={})
    recorded: list[float] = []
    wrapper = _provision(engine, sleep_s=recorded.append)
    domain = wrapper.provision(_SYSTEM, _PROFILE)
    assert domain == f"fault-inject-{_SYSTEM}"
    assert recorded == []  # absent plane => zero latency => no sleep call


def test_attempt_for_is_threaded_into_the_draw() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    first: list[float] = []
    second: list[float] = []
    _provision(engine, attempt_for=lambda _sid: 1, sleep_s=first.append).provision(
        _SYSTEM, _PROFILE
    )
    _provision(engine, attempt_for=lambda _sid: 2, sleep_s=second.append).provision(
        _SYSTEM, _PROFILE
    )
    assert first != second  # a different durable attempt yields a different latency draw


def test_zero_latency_does_not_call_sleep() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=0.0)
    recorded: list[float] = []
    _provision(engine, sleep_s=recorded.append).provision(_SYSTEM, _PROFILE)
    assert recorded == []


def test_teardown_and_reprovision_delegate_unchanged() -> None:
    engine = _seed_that_fails(FaultPlane.PROVISION)
    inventory = FaultInjectInventory()
    inner = FaultInjectProvisioning(inventory)
    wrapper = FaultedProvisioning(inner, engine, sleep_s=lambda _s: None)
    # teardown never draws a fault (it is a compensation, not a perturbed op).
    wrapper.teardown("fault-inject-x")
    # reprovision draws on the provision plane; a fail-certain engine raises.
    with pytest.raises(CategorizedError):
        wrapper.reprovision(_SYSTEM, _PROFILE)


def test_inner_reprovision_reaps_existing_domain_and_re_mints_same_name() -> None:
    inventory = FaultInjectInventory()
    inner = FaultInjectProvisioning(inventory)
    expected = f"fault-inject-{_SYSTEM}"
    inner.provision(_SYSTEM, _PROFILE)
    # A mid-op cancel left the prior domain orphan-flagged; reprovision must clear it by
    # forgetting the existing domain keyed on this system before re-minting.
    inventory.flag_orphan(expected)
    assert inventory.is_orphaned(expected) is True

    domain = inner.reprovision(_SYSTEM, _PROFILE)

    assert domain == expected
    assert inventory.is_orphaned(expected) is False


def test_install_fail_draw_raises_categorized_error() -> None:
    engine = _seed_that_fails(FaultPlane.INSTALL)
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=lambda _s: None)
    with pytest.raises(CategorizedError) as exc:
        wrapper.install(_INSTALL_REQUEST)
    assert exc.value.category in {ErrorCategory.INSTALL_FAILURE, ErrorCategory.BOOT_TIMEOUT}


def test_install_latency_sleeps_for_the_engine_computed_delay() -> None:
    engine = _seed_that_never_fails(FaultPlane.INSTALL, max_latency_s=1000.0)
    recorded: list[float] = []
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=recorded.append)
    wrapper.install(_INSTALL_REQUEST)
    expected = engine.decide(system_id=_SYSTEM, plane=FaultPlane.INSTALL, attempt=1).latency_s
    assert recorded == [expected]
    assert expected > 0.0


def test_boot_uses_the_boot_plane() -> None:
    engine = _seed_that_fails(FaultPlane.BOOT)
    wrapper = FaultedInstall(FaultInjectInstall(), engine, sleep_s=lambda _s: None)
    with pytest.raises(CategorizedError) as exc:
        wrapper.boot(_SYSTEM)
    assert exc.value.category in {ErrorCategory.READINESS_FAILURE, ErrorCategory.BOOT_TIMEOUT}


def test_fresh_system_id_each_call_is_independent() -> None:
    engine = _seed_that_never_fails(FaultPlane.PROVISION, max_latency_s=1000.0)
    a: list[float] = []
    b: list[float] = []
    sid_a, sid_b = uuid4(), uuid4()
    _provision(engine, sleep_s=a.append).provision(sid_a, _PROFILE)
    _provision(engine, sleep_s=b.append).provision(sid_b, _PROFILE)
    assert a != b  # the draw is keyed on system_id


def test_apply_sleeps_for_sub_second_latency() -> None:
    recorded: list[float] = []
    _apply(FaultDecision(fail=False, category=None, latency_s=0.5), recorded.append)
    assert recorded == [0.5]


def test_apply_fail_message_names_the_drawn_category() -> None:
    decision = FaultDecision(fail=True, category=ErrorCategory.PROVISIONING_FAILURE, latency_s=0.0)
    with pytest.raises(CategorizedError) as exc:
        _apply(decision, _noop_sleep)
    assert str(exc.value) == "fault-inject drew a provisioning_failure failure"


def test_fail_without_category_message_is_exact() -> None:
    decision = FaultDecision(fail=True, category=None, latency_s=0.0)
    with pytest.raises(RuntimeError) as exc:
        _apply(decision, _noop_sleep)
    assert str(exc.value) == "fault engine returned a failing decision without a category"


def test_provision_threads_exact_args_to_engine_and_inner() -> None:
    engine = _SpyEngine()
    inner = _SpyProvisioningInner()
    attempt_seen: list[UUID] = []
    wrapper = FaultedProvisioning(
        cast(FaultInjectProvisioning, inner),
        cast(FaultEngine, engine),
        attempt_for=lambda sid: attempt_seen.append(sid) or 4,
        sleep_s=_noop_sleep,
    )

    result = wrapper.provision(_SYSTEM, _PROFILE)

    assert result == "spy-domain"
    assert inner.provision_calls == [(_SYSTEM, _PROFILE)]
    assert attempt_seen == [_SYSTEM]
    assert engine.calls == [{"system_id": _SYSTEM, "plane": FaultPlane.PROVISION, "attempt": 4}]


def test_reprovision_threads_exact_args_to_engine_and_inner() -> None:
    engine = _SpyEngine()
    inner = _SpyProvisioningInner()
    wrapper = FaultedProvisioning(
        cast(FaultInjectProvisioning, inner),
        cast(FaultEngine, engine),
        attempt_for=lambda _sid: 4,
        sleep_s=_noop_sleep,
    )

    wrapper.reprovision(_SYSTEM, _PROFILE)

    assert inner.reprovision_calls == [(_SYSTEM, _PROFILE)]
    assert engine.calls == [{"system_id": _SYSTEM, "plane": FaultPlane.PROVISION, "attempt": 4}]


def test_teardown_passes_domain_name_through_unchanged() -> None:
    engine = _SpyEngine()
    inner = _SpyProvisioningInner()
    wrapper = FaultedProvisioning(
        cast(FaultInjectProvisioning, inner), cast(FaultEngine, engine), sleep_s=_noop_sleep
    )

    wrapper.teardown("fault-inject-host")

    assert inner.teardown_calls == ["fault-inject-host"]
    assert engine.calls == []  # teardown never draws


def test_install_threads_exact_args_to_engine_and_inner() -> None:
    engine = _SpyEngine()
    inner = _SpyInstallInner()
    attempt_seen: list[UUID] = []
    wrapper = FaultedInstall(
        cast(FaultInjectInstall, inner),
        cast(FaultEngine, engine),
        attempt_for=lambda sid: attempt_seen.append(sid) or 9,
        sleep_s=_noop_sleep,
    )

    wrapper.install(_INSTALL_REQUEST)

    assert inner.install_calls == [_INSTALL_REQUEST]
    assert attempt_seen == [_SYSTEM]
    assert engine.calls == [{"system_id": _SYSTEM, "plane": FaultPlane.INSTALL, "attempt": 9}]


def test_boot_threads_exact_args_to_engine_and_inner() -> None:
    engine = _SpyEngine()
    inner = _SpyInstallInner()
    wrapper = FaultedInstall(
        cast(FaultInjectInstall, inner),
        cast(FaultEngine, engine),
        attempt_for=lambda _sid: 9,
        sleep_s=_noop_sleep,
    )

    wrapper.boot(_SYSTEM)

    assert inner.boot_calls == [_SYSTEM]
    assert engine.calls == [{"system_id": _SYSTEM, "plane": FaultPlane.BOOT, "attempt": 9}]

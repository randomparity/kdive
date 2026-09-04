"""Provider-neutral external-boot port contract, run against every bound provider.

Generalizes the fault-inject-only seed in ``tests/providers/ports/test_external_boot.py``
past one provider. The assertions below import nothing from any provider package: a
provider joins by adding a module under ``contract/bindings/``, and admitting one must
never require an edit here (ADR-0583, ADR-0584; #2199, consumed by #2200).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from kdive.providers.ports.external_boot import (
    ExternalBootMaterialization,
    ExternalBootPorts,
    RecoveryPoint,
    RunningKernelObservation,
)
from tests.providers.contract.registry import ProviderBinding, discover

_PORT_MODULE = "kdive.providers.ports.external_boot"
_OPERATIONS = ("materialize", "prepare", "activate", "observe", "recover", "cleanup")


def _refusal(call: Callable[[], object]) -> Exception | None:
    """Return the exception a call refused with, or ``None`` when it did not refuse.

    The contract is that the call refuses; which exception type a provider chooses is its
    own business and must not leak into a shared assertion.
    """
    try:
        call()
    except Exception as error:  # noqa: BLE001 - refusal is the contract, not the type
        return error
    return None


def _activated(
    binding: ProviderBinding,
) -> tuple[ExternalBootPorts, ExternalBootMaterialization, RecoveryPoint]:
    """Drive materialize -> prepare -> activate and return the resulting values."""
    provider = binding.build()
    authority = binding.authority()
    materialization = provider.materialize(binding.plan(), authority)
    recovery = provider.prepare(materialization, binding.activation(materialization), authority)
    provider.activate(recovery, authority)
    return provider, materialization, recovery


def _providers() -> list[ProviderBinding]:
    return discover()


def _ids(candidates: list[ProviderBinding]) -> list[str]:
    return [candidate.name for candidate in candidates]


pytestmark = pytest.mark.parametrize("binding", _providers(), ids=_ids(_providers()))


def test_binding_exposes_the_six_provider_neutral_operations(binding: ProviderBinding) -> None:
    provider = binding.build()

    ports: ExternalBootPorts = provider
    assert ports is provider
    for operation in _OPERATIONS:
        assert callable(getattr(provider, operation)), operation


def test_materialize_carries_the_plan_identity_and_ownership(binding: ProviderBinding) -> None:
    provider = binding.build()
    plan = binding.plan()

    materialization = provider.materialize(plan, binding.authority())

    assert materialization.plan_identity == plan.identity
    assert materialization.ownership.system_id == plan.ownership.system_id
    assert materialization.ownership.run_id == plan.ownership.run_id
    assert materialization.architecture == plan.architecture


def test_prepare_binds_the_recovery_point_to_the_activation(binding: ProviderBinding) -> None:
    provider = binding.build()
    authority = binding.authority()
    materialization = provider.materialize(binding.plan(), authority)
    activation = binding.activation(materialization)

    recovery = provider.prepare(materialization, activation, authority)

    assert recovery.binding == activation
    assert recovery.plan_identity == materialization.plan_identity
    assert recovery.materialization_identity == materialization.identity
    assert recovery.source_state != recovery.target_state


def test_activate_then_observe_reports_the_materialized_kernel(
    binding: ProviderBinding,
) -> None:
    provider, materialization, recovery = _activated(binding)

    observation = provider.observe(recovery, binding.authority())

    assert observation.identity == materialization.kernel_observation
    assert observation.cmdline == observation.expected_cmdline


def test_recover_then_cleanup_completes_the_activation(binding: ProviderBinding) -> None:
    provider, _, recovery = _activated(binding)
    authority = binding.authority()

    provider.recover(recovery, authority)
    provider.cleanup(recovery, authority)

    assert _refusal(lambda: provider.observe(recovery, authority)) is not None


def test_every_returned_value_is_a_provider_neutral_port_type(
    binding: ProviderBinding,
) -> None:
    provider, materialization, recovery = _activated(binding)

    observation = provider.observe(recovery, binding.authority())

    for value in (materialization, recovery, observation):
        assert type(value).__module__ == _PORT_MODULE, type(value)
    assert isinstance(materialization, ExternalBootMaterialization)
    assert isinstance(recovery, RecoveryPoint)
    assert isinstance(observation, RunningKernelObservation)


def test_prepare_refuses_an_activation_owned_by_another_system(
    binding: ProviderBinding,
) -> None:
    provider = binding.build()
    authority = binding.authority()
    materialization = provider.materialize(binding.plan(), authority)
    foreign = binding.activation(materialization).model_copy(
        update={"system_id": "00000000-0000-0000-0000-0000000000ff"}
    )

    assert _refusal(lambda: provider.prepare(materialization, foreign, authority)) is not None

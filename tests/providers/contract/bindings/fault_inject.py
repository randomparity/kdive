"""Fault-inject's registration in the shared external-boot contract suite."""

from __future__ import annotations

from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPorts,
    OpaqueProviderRef,
)
from tests.providers.contract.plans import ACTIVATION_ID, sample_plan_data
from tests.providers.contract.registry import ProviderBinding


def _build() -> ExternalBootPorts:
    return FaultInjectExternalBoot()


def _plan() -> ExternalBootPlan:
    return ExternalBootPlan.model_validate(sample_plan_data())


def _activation(
    materialization: ExternalBootMaterialization,
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=materialization.ownership.system_id,
        run_id=materialization.ownership.run_id,
        activation_id=ACTIVATION_ID,
    )


def _authority() -> OpaqueProviderRef:
    return OpaqueProviderRef(ref="authority/current")


BINDING = ProviderBinding(
    name="fault-inject",
    build=_build,
    plan=_plan,
    activation=_activation,
    authority=_authority,
)

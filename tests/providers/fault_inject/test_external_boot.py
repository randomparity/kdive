"""Fault-inject preparation receipts (ADR-0595)."""

from __future__ import annotations

from typing import Literal

import pytest

from kdive.providers.fault_inject.lifecycle.external_boot import (
    FaultInjectExternalBoot,
    PreparationInterrupted,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPreparationRequest,
    OpaqueProviderRef,
)
from tests.jobs.handlers.external_boot.vehicle import synthetic_plan


def _request(*, phase: Literal["materialize", "prepare"]) -> ExternalBootPreparationRequest:
    from uuid import uuid4

    system_id, run_id, activation_id = uuid4(), uuid4(), uuid4()
    plan = synthetic_plan(system_id=system_id, run_id=run_id)
    return ExternalBootPreparationRequest(
        phase=phase,
        plan=plan,
        binding=ExternalBootActivationBinding(
            system_id=str(system_id), run_id=str(run_id), activation_id=str(activation_id)
        ),
        authority=OpaqueProviderRef(ref="authority/current"),
        operation_identity=f"{phase}-1",
    )


def test_materialize_receipt_survives_interruption_and_prevents_second_mutation() -> None:
    provider = FaultInjectExternalBoot()
    request = _request(phase="materialize")
    provider.interrupt_after_receipt("materialize")

    with pytest.raises(PreparationInterrupted):
        provider.execute_preparation(request)

    observed = provider.observe_preparation(request)
    replayed = provider.execute_preparation(request)
    assert observed == replayed
    assert observed.state == "materialized"
    assert provider.preparation_mutations == {"materialize": 1, "prepare": 0}


def test_prepare_receipt_survives_interruption_and_prevents_second_mutation() -> None:
    provider = FaultInjectExternalBoot()
    materialize = _request(phase="materialize")
    provider.execute_preparation(materialize)
    prepare = materialize.model_copy(update={"phase": "prepare", "operation_identity": "prepare-1"})
    provider.interrupt_after_receipt("prepare")

    with pytest.raises(PreparationInterrupted):
        provider.execute_preparation(prepare)

    observed = provider.observe_preparation(prepare)
    replayed = provider.execute_preparation(prepare)
    assert observed == replayed
    assert observed.state == "prepared"
    assert provider.preparation_mutations == {"materialize": 1, "prepare": 1}

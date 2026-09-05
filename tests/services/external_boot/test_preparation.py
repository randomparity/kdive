"""Server-owned external-boot preparation re-entry (ADR-0595)."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from kdive.db.external_boot_activations import ExternalBootActivationRepository
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.providers.fault_inject.lifecycle.external_boot import (
    FaultInjectExternalBoot,
    PreparationInterrupted,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.services.external_boot.preparation import prepare_external_boot
from tests.jobs.handlers.external_boot.vehicle import synthetic_plan
from tests.mcp.lifecycle import runs_support
from tests.services.external_boot.conftest import seed_activation


@pytest.mark.parametrize("interrupted_phase", ["materialize", "prepare"])
def test_post_provider_pre_commit_loss_converges_without_second_mutation(
    migrated_url: str, interrupted_phase: str
) -> None:
    asyncio.run(_assert_interrupted_preparation_reenters(migrated_url, interrupted_phase))


async def _assert_interrupted_preparation_reenters(
    migrated_url: str, interrupted_phase: str
) -> None:
    provider = FaultInjectExternalBoot()
    async with runs_support.pool(migrated_url) as pool:
        async with pool.connection() as conn:
            seeded = await seed_activation(
                conn, state=ExternalBootActivationState.PREPARING, ready_reservation=True
            )
            plan = synthetic_plan(system_id=seeded.system_id, run_id=seeded.run_id)
            await conn.execute(
                "UPDATE external_boot_activations SET plan_identity = %s WHERE id = %s",
                (plan.identity, seeded.activation.id),
            )
        provider.interrupt_after_receipt(interrupted_phase)
        arguments = {
            "pool": pool,
            "repository": ExternalBootActivationRepository(),
            "ports": provider,
            "plan": plan,
            "activation_id": seeded.activation.id,
            "system_id": seeded.system_id,
            "operation_owner_id": seeded.activation.operation_owner_id,
            "authority_generation": seeded.activation.authority_generation,
            "authority": OpaqueProviderRef(ref="authority/current"),
            "materialize_operation_identity": f"materialize-{uuid4()}",
            "prepare_operation_identity": f"prepare-{uuid4()}",
        }

        with pytest.raises(PreparationInterrupted):
            await prepare_external_boot(**arguments)
        prepared = await prepare_external_boot(**arguments)

        assert prepared.state is ExternalBootActivationState.PREPARED
        assert prepared.materialization is not None
        assert prepared.recovery_point is not None
        assert provider.preparation_mutations == {"materialize": 1, "prepare": 1}

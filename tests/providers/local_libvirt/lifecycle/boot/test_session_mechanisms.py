"""Host mechanisms for the local external-boot session factory (ADR-0591, #2211)."""

from __future__ import annotations

from uuid import UUID

import pytest

from kdive.providers.local_libvirt.lifecycle.boot.session import OperationOwnership
from kdive.providers.local_libvirt.lifecycle.boot.session_mechanisms import (
    LocalOperationLane,
    LocalOperationLease,
)
from kdive.providers.ports.external_boot import ExternalBootActivationBinding

SYSTEM_ID = UUID("11111111-1111-1111-1111-111111111111")
BINDING = ExternalBootActivationBinding(
    system_id=str(SYSTEM_ID),
    run_id="22222222-2222-2222-2222-222222222222",
    activation_id="33333333-3333-3333-3333-333333333333",
)


def _lease() -> LocalOperationLease:
    return LocalOperationLease(system_id=SYSTEM_ID, binding=BINDING)


class TestOperationLane:
    def test_pin_refuses_a_foreign_lease(self) -> None:
        with pytest.raises(TypeError, match="foreign operation lease"):
            LocalOperationLane().pin(object())  # ty: ignore[invalid-argument-type]

    def test_pin_refuses_a_structural_impostor(self) -> None:
        # Nominal, not structural: an object carrying the right attribute names is still
        # not a lease. `LocalExternalBootOperationLease` is a Protocol, so a structural
        # check here would accept this and the lane would pin an identity nobody issued.
        class Impostor:
            system_id = SYSTEM_ID
            binding = BINDING

        # No `ty: ignore` here, and that is the point: `Impostor` satisfies the Protocol
        # structurally, so the type checker accepts this call. Only the runtime isinstance
        # check refuses it.
        with pytest.raises(TypeError, match="foreign operation lease"):
            LocalOperationLane().pin(Impostor())

    def test_pin_refuses_a_released_lease(self) -> None:
        lease = _lease()
        lease.release()
        with pytest.raises(RuntimeError, match="operation lease is released"):
            LocalOperationLane().pin(lease)

    def test_release_is_refused_while_a_pin_is_outstanding(self) -> None:
        lease = _lease()
        pinned = LocalOperationLane().pin(lease)
        with pytest.raises(RuntimeError, match="operation lease is pinned"):
            lease.release()
        pinned._pin.close()
        lease.release()
        assert lease.released is True

    def test_pin_returns_the_exact_lease_identity(self) -> None:
        lease = _lease()
        pinned = LocalOperationLane().pin(lease)
        assert pinned.ownership == OperationOwnership(SYSTEM_ID, BINDING)
        # The binding is carried through, not rebuilt: a reconstructed equal value would
        # satisfy the equality above while losing the identity the lease actually issued.
        assert pinned.ownership.binding is lease.binding

    def test_lane_cannot_mint_its_own_lease(self) -> None:
        # ADR-0587 assigns lease issuance to the serialization-lane context (#2212). A lane
        # that could issue one would be the synthetic identity the rejected #2126 attempt
        # reached for, so the absence of an issuing method is part of the contract.
        assert not hasattr(LocalOperationLane, "issue")

    def test_a_second_pin_keeps_the_lease_held(self) -> None:
        # Closing one pin must not release a lease another pin still holds.
        lane, lease = LocalOperationLane(), _lease()
        first, second = lane.pin(lease), lane.pin(lease)
        first._pin.close()
        with pytest.raises(RuntimeError, match="operation lease is pinned"):
            lease.release()
        second._pin.close()
        lease.release()
        assert lease.released is True

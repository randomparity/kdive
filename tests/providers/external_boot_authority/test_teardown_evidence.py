"""Closed teardown evidence uses proved absence and the actual reservation (ADR-0620)."""

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.protocol import (
    AuthorityOperation,
    AuthorityTeardownMutationRequestV1,
)
from kdive.providers.external_boot_authority.service_teardown import teardown_proof
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef
from tests.providers.external_boot_authority.service_support import _takeover


def _request() -> AuthorityTeardownMutationRequestV1:
    values = _takeover().model_dump(mode="json", by_alias=True)
    values.update(
        schema="external-boot-authority-teardown-request-v1",
        purpose="teardown",
        operation=AuthorityOperation.TEARDOWN,
        attempt_id=str(uuid4()),
    )
    return AuthorityTeardownMutationRequestV1.model_validate(values)


def _facts(
    disposition: Literal["pending", "ready", "released"] = "ready",
) -> AuthoritySystemTeardownFacts:
    return AuthoritySystemTeardownFacts(
        intent_identity="sha256:" + "a" * 64,
        domain_absent=True,
        overlay_absent=True,
        baseline_absent=True,
        recovery_absent=True,
        quarantine_retained=False,
        completed_at=datetime(2026, 9, 6, tzinfo=UTC),
        reservation=AuthorityTeardownReservationV1(
            disposition=disposition,
            store_identity=OpaqueProviderRef(ref="store-a"),
            owner_key=OpaqueProviderRef(ref="owner-a"),
            reserved_bytes=4096,
        ),
    )


def test_ready_proof_uses_persisted_capacity_and_prefixed_release_identity() -> None:
    request = _request()
    proof = teardown_proof(request, _facts())
    assert proof.disposition == "complete_ready"
    assert proof.release_evidence.reserved_bytes == 4096
    assert proof.release_evidence.owner_key == OpaqueProviderRef(ref="owner-a")
    assert proof.release_evidence.objects == ()
    assert proof.release_identity == proof.release_evidence.identity
    assert proof.cleanup_evidence.release_identity == proof.release_identity
    assert proof.teardown_evidence.observed_at == _facts().completed_at
    assert teardown_proof(request, _facts()) == proof


def test_pending_proof_has_no_creditable_release() -> None:
    proof = teardown_proof(_request(), _facts("pending"))
    assert proof.disposition == "complete_pending"
    assert proof.cleanup_evidence.release_identity is None
    assert proof.cleanup_evidence.mode == "pending_system_teardown"


@pytest.mark.parametrize(
    "changes",
    [
        {"domain_absent": False},
        {"overlay_absent": False},
        {"baseline_absent": False},
        {"recovery_absent": False},
        {"quarantine_retained": True},
        {"completed_at": None},
    ],
)
def test_incomplete_facts_never_produce_absence_evidence(changes: dict[str, Any]) -> None:
    proof = teardown_proof(_request(), _facts().model_copy(update=changes))
    assert proof.disposition == "retained_quarantine"


def test_released_proof_does_not_reissue_release_or_cleanup() -> None:
    proof = teardown_proof(_request(), _facts("released"))
    assert proof.disposition == "complete_released"
    assert set(type(proof).model_fields) == {"disposition", "teardown_evidence"}

"""Construct teardown evidence from authenticated ownership and host facts (ADR-0620)."""

from kdive.domain.external_boot_activation import (
    ExternalBootCleanupEvidenceV1,
    ExternalBootReleaseEvidenceV1,
    ExternalBootTeardownEvidenceV1,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityTeardownCompletePendingV1,
    AuthorityTeardownCompleteReadyV1,
    AuthorityTeardownCompleteReleasedV1,
    AuthorityTeardownMutationRequestV1,
    AuthorityTeardownProofV1,
    AuthorityTeardownRetainedQuarantineV1,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
)


def teardown_proof(
    request: AuthorityTeardownMutationRequestV1, facts: AuthoritySystemTeardownFacts
) -> AuthorityTeardownProofV1:
    """Never infer capacity credit from a partial observation or synthesize its timestamp."""
    if not facts.complete or facts.completed_at is None or facts.reservation is None:
        return AuthorityTeardownRetainedQuarantineV1(disposition="retained_quarantine")
    reservation = facts.reservation
    teardown = ExternalBootTeardownEvidenceV1(
        system_id=request.system_id, observed_at=facts.completed_at
    )
    if reservation.disposition == "released":
        return AuthorityTeardownCompleteReleasedV1(
            disposition="complete_released", teardown_evidence=teardown
        )
    if reservation.disposition == "pending":
        return AuthorityTeardownCompletePendingV1(
            disposition="complete_pending",
            teardown_evidence=teardown,
            cleanup_evidence=ExternalBootCleanupEvidenceV1(
                activation_id=request.activation_id,
                system_id=request.system_id,
                mode="pending_system_teardown",
                teardown_identity=teardown.identity,
                completed_at=facts.completed_at,
            ),
        )
    release = ExternalBootReleaseEvidenceV1(
        activation_id=request.activation_id,
        system_id=request.system_id,
        store_identity=reservation.store_identity,
        owner_key=reservation.owner_key,
        reserved_bytes=reservation.reserved_bytes,
        objects=(),
        verified_at=facts.completed_at,
    )
    return AuthorityTeardownCompleteReadyV1(
        disposition="complete_ready",
        teardown_evidence=teardown,
        release_evidence=release,
        release_identity=release.identity,
        cleanup_evidence=ExternalBootCleanupEvidenceV1(
            activation_id=request.activation_id,
            system_id=request.system_id,
            mode="system_teardown",
            release_identity=release.identity,
            teardown_identity=teardown.identity,
            completed_at=facts.completed_at,
        ),
    )

"""Closed contracts for authority-owned System provider ports."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.system_authority.protocol import (
    AuthoritySystemAbsenceFacts,
    AuthoritySystemJournalPhase,
    AuthoritySystemJournalRecordV1,
    AuthoritySystemMarkerV1,
    AuthoritySystemOperation,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemProvisionFacts,
    AuthoritySystemProvisionReadyV1,
    AuthoritySystemTakeoverRequestV1,
    canonical_system_authority_bytes,
    canonical_system_record_payload,
)

_DIGEST = "sha256:" + "a" * 64


def _marker() -> AuthoritySystemMarkerV1:
    return AuthoritySystemMarkerV1(
        system_id=uuid4(),
        allocation_id=uuid4(),
        resource_id=uuid4(),
        provider_kind="local-libvirt",
        resource_name="local-a",
        authority_instance="authority-a",
        profile_identity=_DIGEST,
        root_identity=_DIGEST,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-a",
    )


def test_marker_is_closed_canonical_and_path_free() -> None:
    marker = _marker()
    encoded = canonical_system_authority_bytes(marker)
    assert encoded.startswith(b'{"allocation_id"')
    assert b"path" not in encoded
    with pytest.raises(ValidationError):
        AuthoritySystemMarkerV1.model_validate({**marker.model_dump(), "path": "/private"})


def test_attempt_requires_explicit_bootstrap_and_attempt_binding() -> None:
    marker = _marker()
    value = {
        **marker.model_dump(exclude={"schema_"}),
        "authority_id": uuid4(),
        "generation": 1,
        "attempt_id": uuid4(),
        "operation_digest": _DIGEST,
        "bootstrap_identity": _DIGEST,
    }
    request = AuthoritySystemTakeoverRequestV1.model_validate(value)
    assert request.bootstrap_identity == _DIGEST
    with pytest.raises(ValidationError):
        AuthoritySystemTakeoverRequestV1.model_validate(
            {key: item for key, item in value.items() if key != "bootstrap_identity"}
        )


@pytest.mark.parametrize(
    "operation",
    [AuthoritySystemOperation.PROVISION, AuthoritySystemOperation.PREACTIVATION_TEARDOWN],
)
def test_terminal_never_began_is_valid_for_both_operations(
    operation: AuthoritySystemOperation,
) -> None:
    marker = _marker().model_copy(update={"operation": operation})
    fields = {
        **marker.model_dump(mode="python", exclude={"schema_"}),
        "schema": "authority-system-journal-v1",
        "authority_id": uuid4(),
        "generation": 1,
        "attempt_id": uuid4(),
        "operation_digest": _DIGEST,
        "bootstrap_identity": _DIGEST,
        "sequence": 1,
        "previous_digest": _DIGEST,
        "phase": AuthoritySystemJournalPhase.TERMINAL,
        "observation": None,
        "outcome": "never-began",
    }
    record = AuthoritySystemJournalRecordV1.model_validate(
        {**fields, "canonical_record": canonical_system_record_payload(fields)}
    )
    assert record.observation is None


@pytest.mark.parametrize("kind", ["provision", "absence"])
def test_provider_facts_require_literal_complete_shape(kind: str) -> None:
    common = {
        "intent_identity": _DIGEST,
        "quarantine_retained": False,
        "completed_at": datetime(2026, 9, 6, tzinfo=UTC),
    }
    if kind == "provision":
        facts = AuthoritySystemProvisionFacts(
            **common,
            domain_owned=True,
            root_storage_owned=True,
            boot_ready=True,
            bootstrap_ready=True,
        )
        malformed = facts.model_dump()
        malformed["boot_ready"] = False
        model = AuthoritySystemProvisionFacts
    else:
        facts = AuthoritySystemAbsenceFacts(
            **common,
            domain_absent=True,
            root_storage_absent=True,
            baseline_absent=True,
            private_intent_absent=True,
        )
        malformed = facts.model_dump()
        malformed["private_intent_absent"] = False
        model = AuthoritySystemAbsenceFacts
    assert facts.complete
    with pytest.raises(ValidationError):
        model.model_validate(malformed)


def test_provider_facts_reject_naive_completion_time() -> None:
    with pytest.raises(ValidationError):
        AuthoritySystemProvisionFacts(
            intent_identity=_DIGEST,
            domain_owned=True,
            root_storage_owned=True,
            boot_ready=True,
            bootstrap_ready=True,
            quarantine_retained=False,
            completed_at=datetime(2026, 9, 6),
        )


def test_terminal_proof_disposition_is_bound_to_operation() -> None:
    marker = _marker()
    binding = {
        **marker.model_dump(exclude={"schema_"}),
        "authority_id": uuid4(),
        "generation": 1,
        "attempt_id": uuid4(),
        "operation_digest": _DIGEST,
        "bootstrap_identity": _DIGEST,
        "intent_identity": _DIGEST,
        "domain_owned": True,
        "root_storage_owned": True,
        "boot_ready": True,
        "bootstrap_ready": True,
        "quarantine_retained": False,
        "completed_at": datetime(2026, 9, 6, tzinfo=UTC),
        "disposition": "provision-ready",
    }
    assert AuthoritySystemProvisionReadyV1.model_validate(binding)
    with pytest.raises(ValidationError):
        AuthoritySystemProvisionReadyV1.model_validate(
            {**binding, "operation": "preactivation-teardown"}
        )
    with pytest.raises(ValidationError):
        AuthoritySystemPreactivationAbsentV1.model_validate(binding)

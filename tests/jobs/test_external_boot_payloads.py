"""Payload contracts for authority-marked boot and teardown jobs (ADR-0593).

The marker rides one optional field named exactly ``external_boot_authority_v1``, because that
literal is what ``0122_external_boot_authority.sql`` and
``0127_reopen_external_boot_claim_lane.sql`` test with ``payload ? '…'``. These tests pin the key
name, the cross-field rules, and the two halves of charter criterion 1 — in the model registry and
on the wire.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from kdive.domain.capacity.state import JobState
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.jobs.payloads import (
    _ACTIVE_PAYLOAD_MODELS,
    ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS,
    BootPayload,
    PayloadValidationError,
    TeardownPayload,
    dump_payload,
    load_payload,
    run_id_from_payload,
)

MARKER_KEY = "external_boot_authority_v1"
_DIGEST = "sha256:" + "a" * 64


def _marker(
    *,
    activation_id: UUID | None = None,
    run_id: UUID | None = None,
    system_id: UUID | None = None,
    purpose: str = "activate",
    operation: str = "activate",
    plan_identity: str = _DIGEST,
) -> dict[str, Any]:
    return {
        "activation_id": str(activation_id or uuid4()),
        "run_id": str(run_id or uuid4()),
        "system_id": str(system_id or uuid4()),
        "plan_identity": plan_identity,
        "purpose": purpose,
        "provider_kind": "local-libvirt",
        "authority_instance": "provider-1",
        "operation": operation,
        "operation_identity": f"{operation}-1",
    }


def _job(kind: JobKind, payload: dict[str, Any]) -> Job:
    now = datetime.now(UTC)
    return Job(
        id=uuid4(),
        created_at=now,
        updated_at=now,
        kind=kind,
        payload=payload,
        state=JobState.QUEUED,
        max_attempts=3,
        authorizing={"principal": "alice", "agent_session": None, "project": "kernel-team"},
        dedup_key=f"{uuid4()}:{kind.value}",
    )


def test_unmarked_boot_payload_round_trips_unchanged() -> None:
    """An unmarked payload is byte-identical on the wire, so persisted jobs still decode."""
    run_id = uuid4()

    dumped = dump_payload(JobKind.BOOT, {"run_id": str(run_id)})

    assert dumped == {"run_id": str(run_id)}
    decoded = load_payload(_job(JobKind.BOOT, dumped), BootPayload)
    assert decoded.run_id == str(run_id)
    assert decoded.external_boot_authority_v1 is None


def test_unmarked_teardown_payload_round_trips_unchanged() -> None:
    system_id = uuid4()

    dumped = dump_payload(JobKind.TEARDOWN, {"system_id": str(system_id)})

    assert dumped == {"system_id": str(system_id)}
    decoded = load_payload(_job(JobKind.TEARDOWN, dumped), TeardownPayload)
    assert decoded.system_id == str(system_id)
    assert decoded.external_boot_authority_v1 is None


def test_marked_boot_payload_round_trips_unchanged() -> None:
    run_id = uuid4()
    marker = _marker(run_id=run_id)

    dumped = dump_payload(JobKind.BOOT, {"run_id": str(run_id), MARKER_KEY: marker})

    assert set(dumped) == {"run_id", MARKER_KEY}
    decoded = load_payload(_job(JobKind.BOOT, dumped), BootPayload)
    assert decoded.external_boot_authority_v1 is not None
    assert decoded.external_boot_authority_v1.run_id == run_id
    assert dump_payload(JobKind.BOOT, decoded) == dumped


def test_marked_teardown_payload_round_trips_unchanged() -> None:
    system_id = uuid4()
    marker = _marker(system_id=system_id, purpose="teardown", operation="teardown")

    dumped = dump_payload(JobKind.TEARDOWN, {"system_id": str(system_id), MARKER_KEY: marker})

    assert set(dumped) == {"system_id", MARKER_KEY}
    decoded = load_payload(_job(JobKind.TEARDOWN, dumped), TeardownPayload)
    assert decoded.external_boot_authority_v1 is not None
    assert decoded.external_boot_authority_v1.system_id == system_id
    assert dump_payload(JobKind.TEARDOWN, decoded) == dumped


def test_marked_payload_rejects_extra_field() -> None:
    run_id = uuid4()

    with pytest.raises(PayloadValidationError, match="Extra inputs"):
        dump_payload(
            JobKind.BOOT,
            {"run_id": str(run_id), MARKER_KEY: _marker(run_id=run_id), "surprise": 1},
        )


def test_marker_rejects_extra_field() -> None:
    run_id = uuid4()
    marker = _marker(run_id=run_id) | {"surprise": 1}

    with pytest.raises(PayloadValidationError, match="Extra inputs"):
        dump_payload(JobKind.BOOT, {"run_id": str(run_id), MARKER_KEY: marker})


def test_marker_run_id_must_match_payload_run_id() -> None:
    with pytest.raises(PayloadValidationError, match="run_id"):
        dump_payload(JobKind.BOOT, {"run_id": str(uuid4()), MARKER_KEY: _marker()})


def test_marker_system_id_must_match_payload_system_id() -> None:
    with pytest.raises(PayloadValidationError, match="system_id"):
        dump_payload(
            JobKind.TEARDOWN,
            {
                "system_id": str(uuid4()),
                MARKER_KEY: _marker(purpose="teardown", operation="teardown"),
            },
        )


def test_teardown_marker_requires_teardown_purpose() -> None:
    system_id = uuid4()

    with pytest.raises(PayloadValidationError, match="teardown"):
        dump_payload(
            JobKind.TEARDOWN,
            {
                "system_id": str(system_id),
                MARKER_KEY: _marker(system_id=system_id, purpose="release", operation="release"),
            },
        )


def test_boot_marker_rejects_teardown_purpose() -> None:
    run_id = uuid4()

    with pytest.raises(PayloadValidationError, match="teardown"):
        dump_payload(
            JobKind.BOOT,
            {
                "run_id": str(run_id),
                MARKER_KEY: _marker(run_id=run_id, purpose="teardown", operation="teardown"),
            },
        )


@pytest.mark.parametrize(
    ("purpose", "operation"),
    [("activate", "release"), ("release", "activate"), ("activate", "cleanup")],
)
def test_marker_operation_must_be_permitted_for_purpose(purpose: str, operation: str) -> None:
    run_id = uuid4()

    with pytest.raises(PayloadValidationError, match="not permitted"):
        dump_payload(
            JobKind.BOOT,
            {
                "run_id": str(run_id),
                MARKER_KEY: _marker(run_id=run_id, purpose=purpose, operation=operation),
            },
        )


@pytest.mark.parametrize(
    ("purpose", "operation"),
    [("activate", "deadline"), ("recover", "recovery-attempt"), ("activate", "fail")],
)
def test_marker_operation_must_be_enqueueable(purpose: str, operation: str) -> None:
    """``deadline``, ``recovery-attempt``, and ``fail`` are permitted commit points, not admissions.

    Each is permitted for its purpose by ``_PURPOSE_OPERATIONS``, so this rejection is the
    enqueueable-set check and not the purpose/operation one.
    """
    run_id = uuid4()

    with pytest.raises(PayloadValidationError, match=operation):
        dump_payload(
            JobKind.BOOT,
            {
                "run_id": str(run_id),
                MARKER_KEY: _marker(run_id=run_id, purpose=purpose, operation=operation),
            },
        )


def test_enqueueable_operations_are_exactly_these_six() -> None:
    """The only place the six names are pinned, and it is pinned against a literal.

    Every other check compares the constant against something the constant already gates —
    ``ExternalBootOperations.register`` refuses outside it and the payload validator rejects a
    marker outside it — so a ``registered_operations() == CONSTANT`` assertion compares the
    constant to itself. Drop ``cleanup`` from the constant and that assertion stays green while
    criterion 4's "cleanup … ha[s] a registered handler" silently stops holding. This literal is
    what turns that rename red.
    """
    assert (
        frozenset({"activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"})
        == ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS
    )


def test_run_id_from_payload_returns_run_for_boot_and_none_for_teardown() -> None:
    run_id = uuid4()
    system_id = uuid4()

    boot = dump_payload(JobKind.BOOT, {"run_id": str(run_id), MARKER_KEY: _marker(run_id=run_id)})
    teardown = dump_payload(
        JobKind.TEARDOWN,
        {
            "system_id": str(system_id),
            MARKER_KEY: _marker(system_id=system_id, purpose="teardown", operation="teardown"),
        },
    )

    assert run_id_from_payload(JobKind.BOOT, boot) == run_id
    assert run_id_from_payload(JobKind.TEARDOWN, teardown) is None


def test_every_marked_payload_kind_is_boot_or_teardown() -> None:
    """Charter criterion 1 in the registry: ``0122…sql:465`` pins the kind to boot/teardown."""
    marked = {
        kind for kind, model in _ACTIVE_PAYLOAD_MODELS.items() if MARKER_KEY in model.model_fields
    }

    assert marked == {JobKind.BOOT, JobKind.TEARDOWN}


@pytest.mark.parametrize("kind", [JobKind.PROVISION, JobKind.FORCE_CRASH])
def test_a_marked_payload_cannot_be_dumped_under_another_kind(kind: JobKind) -> None:
    """Charter criterion 1 on the wire, which is where the hole is.

    ``TeardownPayload`` *is* a ``SystemPayload``, so ``dump_payload``'s
    ``isinstance(payload, model_class)`` accepts one for a kind whose registry entry is still bare
    ``SystemPayload``, and ``model_dump(exclude_none=True)`` then emits the marker key. Such a job
    is claimable, routes to the ordinary handler, and is then unreapable. The registry-shaped test
    above stays green with this hole open, so it does not substitute for this one.
    """
    system_id = uuid4()
    payload = TeardownPayload(
        system_id=str(system_id),
        external_boot_authority_v1=ExternalBootAuthorityMarkerV1.model_validate(
            _marker(system_id=system_id, purpose="teardown", operation="teardown")
        ),
    )

    with pytest.raises(PayloadValidationError, match=MARKER_KEY):
        dump_payload(kind, payload)

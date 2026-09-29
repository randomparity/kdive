"""Real-Postgres proofs for taking over an interrupted System teardown (#2884, ADR-0620)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    JournalPhase,
    JournalRecordV1,
    record_digest,
)
from tests.db.external_boot_authority_support import (
    _PLAN,
    _allocate,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)
from tests.db.test_external_boot_authority_journal_migration import (
    _DIGEST,
    _advance_raw,
    _payload,
    _promote,
    _record,
)

_OPERATION_PHASES = (
    JournalPhase.ADMITTED,
    JournalPhase.MUTATION_STARTED,
    JournalPhase.PROVIDER_RETURNED,
    JournalPhase.OBSERVED,
)


def _teardown_changes(case: _AuthorityCase, phase: JournalPhase) -> dict[str, object]:
    changes: dict[str, object] = {
        "attempt_id": uuid5(NAMESPACE_URL, case.operation_identity),
        "expected_source_identity": None,
        "intended_target_identity": None,
        "recovery_objects": (),
    }
    if phase is JournalPhase.OBSERVED:
        changes["observation"] = {
            "observation_id": str(uuid4()),
            "category": "absent",
            "composite_state": _DIGEST,
        }
    return changes


def _drive_to(
    migrated_url: str,
    role_dsns: _RoleDsns,
    case: _AuthorityCase,
    authority: Any,
    phase: JournalPhase,
    previous: JournalRecordV1 | None = None,
) -> JournalRecordV1:
    """Take over as ``authority`` and run its teardown up to and including ``phase``."""
    sequence = 0 if previous is None else previous.sequence
    digest = GENESIS_DIGEST if previous is None else record_digest(previous)
    watermark = _record(
        case,
        authority,
        sequence + 1,
        digest,
        JournalPhase.WATERMARK_INSTALLED,
        attempt_id=authority.authority_id,
    )
    acknowledgement = _record(
        case,
        authority,
        sequence + 2,
        record_digest(watermark),
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
        attempt_id=authority.authority_id,
        watermark_sequence=watermark.sequence,
        watermark_digest=record_digest(watermark),
    )
    head = previous
    with psycopg.connect(role_dsns("kdive_provider_authority")) as provider:
        for record in (watermark, acknowledgement):
            assert (
                _advance_raw(
                    provider,
                    case,
                    authority,
                    sequence,
                    digest,
                    _payload(record),
                )
                == "advanced"
            )
            sequence, digest, head = record.sequence, record_digest(record), record
    _promote(migrated_url, case, authority, acknowledgement)
    with psycopg.connect(role_dsns("kdive_provider_authority")) as provider:
        for step in _OPERATION_PHASES[: _OPERATION_PHASES.index(phase) + 1]:
            record = _record(
                case, authority, sequence + 1, digest, step, **_teardown_changes(case, step)
            )
            assert (
                _advance_raw(provider, case, authority, sequence, digest, _payload(record))
                == "advanced"
            )
            sequence, digest, head = record.sequence, record_digest(record), record
    assert head is not None
    return head


def _reclaim(migrated_url: str, case: _AuthorityCase) -> _AuthorityCase:
    """Charge the job one more attempt, as a lease-expiry reclaim and a new claim would."""
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "UPDATE jobs SET attempt = attempt + 1, max_attempts = attempt + 3, "
            "lease_expires_at = now() + interval '5 minutes' WHERE id = %s RETURNING attempt",
            (case.job_id,),
        ).fetchone()
    assert row is not None
    return replace(case, attempt=row[0])


def _head_row(migrated_url: str, case: _AuthorityCase) -> tuple[Any, ...]:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT sequence, phase, suspended_operation FROM "
            "external_boot_authority_journal_heads WHERE system_id = %s",
            (case.system_id,),
        ).fetchone()
    assert row is not None
    return row


def test_migration_0161_is_registered() -> None:
    assert "0161" in [item.version for item in migrate.discover_migrations()]


@pytest.mark.parametrize("phase", _OPERATION_PHASES, ids=lambda phase: phase.value)
def test_teardown_takeover_watermark_anchors_over_unresolved_head(
    migrated_url: str, authority_role_dsns: _RoleDsns, phase: JournalPhase
) -> None:
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown", worker_suffix="w")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        first = _allocate(worker, case)
    head = _drive_to(migrated_url, authority_role_dsns, case, first, phase)
    successor_case = _reclaim(migrated_url, case)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        successor = _allocate(worker, successor_case)
    watermark = _record(
        successor_case,
        successor,
        head.sequence + 1,
        record_digest(head),
        JournalPhase.WATERMARK_INSTALLED,
        attempt_id=successor.authority_id,
    )

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        status = _advance_raw(
            provider,
            successor_case,
            successor,
            head.sequence,
            record_digest(head),
            _payload(watermark),
        )

    assert status == "advanced"
    sequence, head_phase, suspended = _head_row(migrated_url, case)
    assert (sequence, head_phase) == (watermark.sequence, "watermark-installed")
    assert suspended["authority_id"] == str(first.authority_id)
    assert suspended["phase"] == phase.value
    assert suspended["attempt_id"] == str(uuid5(NAMESPACE_URL, case.operation_identity))


def test_operation_phase_record_with_changed_attempt_is_still_conflict(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown", worker_suffix="x")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    head = _drive_to(
        migrated_url, authority_role_dsns, case, authority, JournalPhase.MUTATION_STARTED
    )
    returned = _record(
        case,
        authority,
        head.sequence + 1,
        record_digest(head),
        JournalPhase.PROVIDER_RETURNED,
        **(_teardown_changes(case, JournalPhase.PROVIDER_RETURNED) | {"attempt_id": uuid4()}),
    )

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        status = _advance_raw(
            provider, case, authority, head.sequence, record_digest(head), _payload(returned)
        )

    assert status == "conflict"
    assert _head_row(migrated_url, case)[:2] == (head.sequence, "mutation-started")


def _teardown_job(migrated_url: str, case: _AuthorityCase) -> _AuthorityCase:
    """Add a teardown job for ``case``'s activation, as a public ``systems.teardown`` would."""
    job_id = uuid4()
    teardown = replace(
        case,
        job_id=job_id,
        attempt=1,
        purpose="teardown",
        operation="teardown",
        operation_identity=f"teardown-{uuid4()}",
    )
    marker = {
        "activation_id": str(case.activation_id),
        "run_id": str(case.run_id),
        "system_id": str(case.system_id),
        "plan_identity": _PLAN,
        "purpose": "teardown",
        "provider_kind": case.provider_kind,
        "authority_instance": case.authority_instance,
        "operation": "teardown",
        "operation_identity": teardown.operation_identity,
    }
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'teardown', %s, 'running', 1, 9, %s, now() + interval '5 minutes', now(), "
            "%s, %s)",
            (
                job_id,
                Jsonb({"external_boot_authority_v1": marker}),
                case.worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                f"{case.system_id}:teardown",
            ),
        )
    return teardown


def _allocation_status(role_dsns: _RoleDsns, case: _AuthorityCase) -> str:
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT status FROM allocate_external_boot_authority("
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                case.activation_id,
                case.run_id,
                case.system_id,
                _PLAN,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.operation_identity,
            ),
        ).fetchone()
    assert row is not None
    return row[0]


def _set_state(migrated_url: str, authority: Any, state: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE external_boot_authorities SET state = %s, "
            "acknowledged_at = coalesce(acknowledged_at, now()), "
            "superseded_at = CASE WHEN %s = 'superseded' THEN now() END, "
            "retired_at = CASE WHEN %s = 'retired' THEN now() END WHERE id = %s",
            (state, state, state, authority.authority_id),
        )


@pytest.mark.parametrize(
    "fence", ["allocating", "current", "unresolved-head", "suspended", "resolved"]
)
def test_activate_allocation_is_fenced_by_teardown(
    migrated_url: str, authority_role_dsns: _RoleDsns, fence: str
) -> None:
    with psycopg.connect(migrated_url) as seed:
        activate = _seed_case(seed, worker_suffix="f")
    teardown = _teardown_job(migrated_url, activate)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, teardown)
    if fence == "current":
        _set_state(migrated_url, authority, "current")
    elif fence in {"unresolved-head", "suspended", "resolved"}:
        head = _drive_to(
            migrated_url, authority_role_dsns, teardown, authority, JournalPhase.MUTATION_STARTED
        )
        if fence == "suspended":
            teardown = _reclaim(migrated_url, teardown)
            with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
                authority = _allocate(worker, teardown)
            watermark = _record(
                teardown,
                authority,
                head.sequence + 1,
                record_digest(head),
                JournalPhase.WATERMARK_INSTALLED,
                attempt_id=authority.authority_id,
            )
            with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
                assert (
                    _advance_raw(
                        provider,
                        teardown,
                        authority,
                        head.sequence,
                        record_digest(head),
                        _payload(watermark),
                    )
                    == "advanced"
                )
        if fence == "resolved":
            previous = head
            with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
                for phase in (
                    JournalPhase.PROVIDER_RETURNED,
                    JournalPhase.OBSERVED,
                    JournalPhase.TERMINAL,
                ):
                    changes = _teardown_changes(teardown, JournalPhase.OBSERVED)
                    if phase is JournalPhase.PROVIDER_RETURNED:
                        changes.pop("observation")
                    if phase is JournalPhase.TERMINAL:
                        changes["outcome"] = "absent"
                    record = _record(
                        teardown,
                        authority,
                        previous.sequence + 1,
                        record_digest(previous),
                        phase,
                        **changes,
                    )
                    assert (
                        _advance_raw(
                            provider,
                            teardown,
                            authority,
                            previous.sequence,
                            record_digest(previous),
                            _payload(record),
                        )
                        == "advanced"
                    )
                    previous = record
            _set_state(migrated_url, authority, "retired")
        else:
            _set_state(migrated_url, authority, "superseded")

    expected = "allocated" if fence == "resolved" else "superseded"
    assert _allocation_status(authority_role_dsns, activate) == expected
    if fence != "resolved":
        assert _allocation_status(authority_role_dsns, _reclaim(migrated_url, teardown)) == (
            "allocated"
        )

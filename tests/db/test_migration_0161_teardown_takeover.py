"""Real-Postgres proofs for taking over an interrupted System teardown (#2884, ADR-0620)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.payloads import TeardownPayload
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    AuthorityCommitContextV1,
    AuthorityTeardownMutationRequestV1,
    JournalPhase,
    JournalRecordV1,
    canonical_teardown_proof_bytes,
    record_digest,
    teardown_proof_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.external_boot_authority.teardown import (
    AuthoritySystemTeardownFacts,
    AuthorityTeardownReservationV1,
)
from kdive.providers.ports.external_boot import OpaqueProviderRef
from tests.db.external_boot_authority_support import (
    _PLAN,
    _allocate,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_journal_support import (
    _DIGEST,
    _advance_raw,
    _database_repository,
    _make_ready_prepared,
    _payload,
    _promote,
    _proof,
    _record,
    _takeover_request,
)
from tests.providers.external_boot_authority.service_support import _Adapter

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


class _ContextAdapter(_Adapter):
    """Record the journal sequence of every teardown context; fail execution on demand."""

    def __init__(self) -> None:
        super().__init__()
        self.interrupt = False
        self.observe_failures = 0
        self.executed: list[int] = []
        self.observed: list[int] = []
        self.facts = AuthoritySystemTeardownFacts(
            intent_identity="sha256:" + "a" * 64,
            domain_absent=True,
            overlay_absent=True,
            baseline_absent=True,
            recovery_absent=True,
            quarantine_retained=False,
            completed_at=datetime(2026, 9, 28, tzinfo=UTC),
            reservation=AuthorityTeardownReservationV1(
                disposition="ready",
                store_identity=OpaqueProviderRef(ref="store/private"),
                owner_key=OpaqueProviderRef(ref="owner/private"),
                reserved_bytes=4096,
            ),
        )

    async def execute_system_teardown(
        self,
        request: AuthorityTeardownMutationRequestV1,
        context: AuthorityCommitContextV1,
        reservation: AuthorityTeardownReservationV1,
    ) -> AuthoritySystemTeardownFacts:
        self.executed.append(context.journal_sequence)
        if self.interrupt:
            raise RuntimeError("injected host interruption after mutation-started")
        return self.facts

    async def observe_system_teardown(
        self, request: AuthorityTeardownMutationRequestV1, context: AuthorityCommitContextV1
    ) -> AuthoritySystemTeardownFacts:
        self.observed.append(context.journal_sequence)
        if self.observe_failures:
            self.observe_failures -= 1
            raise RuntimeError("injected host observation failure")
        return self.facts


def _ready_teardown_case(migrated_url: str, suffix: str) -> _AuthorityCase:
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown", worker_suffix=suffix)
        _make_ready_prepared(seed, case)
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,'ready',now())",
            (case.activation_id,),
        )
    return case


def _teardown_request(case: _AuthorityCase, takeover: Any) -> AuthorityTeardownMutationRequestV1:
    return AuthorityTeardownMutationRequestV1.model_validate(
        takeover.model_dump(mode="json", by_alias=True)
        | {
            "schema": "external-boot-authority-teardown-request-v1",
            "attempt_id": str(uuid5(NAMESPACE_URL, case.operation_identity)),
        }
    )


@pytest.mark.anyio
async def test_teardown_takeover_recovers_and_proves_each_generation_through_real_cas(
    tmp_path: Path, migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """Three generations share one teardown identity; each proof binds its own anchor."""
    case = _ready_teardown_case(migrated_url, "g")
    adapter = _ContextAdapter()
    service = ExternalBootAuthorityService(
        repository=_database_repository(authority_role_dsns("kdive_provider_authority")),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    peer = AuthenticatedPeer(case.worker_id)
    try:
        for generation in (1, 2, 3):
            if generation > 1:
                case = _reclaim(migrated_url, case)
            with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
                authority = _allocate(worker, case)
            takeover = _takeover_request(case, authority)
            await service.acknowledge_takeover(peer, takeover)
            request = _teardown_request(case, takeover)
            adapter.interrupt = generation < 3
            if adapter.interrupt:
                with pytest.raises(AuthorityServiceError, match="provider_conflict"):
                    await service.execute_teardown(peer, request)
            else:
                response = await service.execute_teardown(peer, request)
    finally:
        await service.close()

    first, second, third = adapter.executed
    assert adapter.observed == [first, second, third, third]
    assert response.proof.disposition == "complete_ready"
    assert _head_row(migrated_url, case)[:2] == (response.journal_sequence, "terminal")


@pytest.mark.anyio
@pytest.mark.parametrize("interrupted", ["mutation-started", "provider-returned"])
async def test_takeover_after_an_interrupted_teardown_takeover_recovers_it(
    tmp_path: Path, migrated_url: str, authority_role_dsns: _RoleDsns, interrupted: str
) -> None:
    """A takeover that fails before acknowledging leaves recovery to its successor (#2884)."""
    case = _ready_teardown_case(migrated_url, "n")
    adapter = _ContextAdapter()
    service = ExternalBootAuthorityService(
        repository=_database_repository(authority_role_dsns("kdive_provider_authority")),
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=adapter,
    )
    peer = AuthenticatedPeer(case.worker_id)
    try:
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            authority = _allocate(worker, case)
        takeover = _takeover_request(case, authority)
        await service.acknowledge_takeover(peer, takeover)
        adapter.interrupt = interrupted == "mutation-started"
        adapter.observe_failures = 0 if adapter.interrupt else 1
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await service.execute_teardown(peer, _teardown_request(case, takeover))
        assert _head_row(migrated_url, case)[1] == interrupted

        case = _reclaim(migrated_url, case)
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            authority = _allocate(worker, case)
        adapter.interrupt, adapter.observe_failures = False, 1
        with pytest.raises(AuthorityServiceError, match="provider_conflict"):
            await service.acknowledge_takeover(peer, _takeover_request(case, authority))
        assert _head_row(migrated_url, case)[1] == "watermark-installed"

        case = _reclaim(migrated_url, case)
        with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
            authority = _allocate(worker, case)
        takeover = _takeover_request(case, authority)
        await service.acknowledge_takeover(peer, takeover)
        response = await service.execute_teardown(peer, _teardown_request(case, takeover))
    finally:
        await service.close()

    assert response.proof.disposition == "complete_ready"
    sequence, phase, suspended = _head_row(migrated_url, case)
    assert (sequence, phase, suspended) == (response.journal_sequence, "terminal", None)


def _make_current(
    conn: psycopg.Connection, case: _AuthorityCase, authority: Any, proof: Any, sequence: int
) -> str:
    """Acknowledge ``authority`` and point the head at its terminal teardown record."""
    digest = "sha256:" + f"{sequence:x}" * 64
    conn.execute(
        "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
        "WHERE id = %s",
        (authority.authority_id,),
    )
    conn.execute(
        "INSERT INTO external_boot_authority_acknowledgements "
        "(authority_id, system_id, generation, authority_instance, operation_identity, "
        "operation_digest, journal_sequence, journal_digest, positive_quiescence_digest) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            authority.authority_id,
            case.system_id,
            authority.generation,
            case.authority_instance,
            case.operation_identity,
            authority.operation_digest,
            sequence - 1,
            _DIGEST,
            _DIGEST,
        ),
    )
    conn.execute(
        "INSERT INTO external_boot_authority_journal_heads "
        "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
        "operation_identity, head_record) VALUES (%s,%s,%s,%s,'terminal',%s,%s,%s,%s) "
        "ON CONFLICT (authority_instance, system_id) DO UPDATE SET sequence = EXCLUDED.sequence, "
        "digest = EXCLUDED.digest, authority_id = EXCLUDED.authority_id, "
        "generation = EXCLUDED.generation, head_record = EXCLUDED.head_record",
        (
            case.authority_instance,
            case.system_id,
            sequence,
            digest,
            authority.authority_id,
            authority.generation,
            case.operation_identity,
            Jsonb(
                {
                    "observation": {
                        "category": "absent",
                        "composite_state": teardown_proof_digest(proof),
                    }
                }
            ),
        ),
    )
    return digest


def _finalize(
    role_dsns: _RoleDsns,
    case: _AuthorityCase,
    authority: Any,
    proof: Any,
    sequence: int,
    digest: str,
) -> str:
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        row = worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                authority.authority_id,
                authority.generation,
                sequence,
                digest,
                canonical_teardown_proof_bytes(proof),
            ),
        ).fetchone()
    assert row is not None
    return row[0]


@pytest.mark.anyio
async def test_recycled_teardown_job_credits_once(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """#2884: a recycled attempt number cannot revive its failed predecessor's authority."""
    case = _ready_teardown_case(migrated_url, "c")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        old = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        old_digest = _make_current(conn, case, old, proof, 2)
        conn.execute(
            "UPDATE jobs SET state = 'failed', attempt = max_attempts, "
            "error_category = 'conflict', worker_id = NULL, lease_expires_at = NULL, "
            "authorizing = authorizing || '{\"agent_session\": null}' WHERE id = %s",
            (case.job_id,),
        )
        marker = conn.execute(
            "SELECT payload -> 'external_boot_authority_v1' FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone()
    assert marker is not None
    async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
        recycled = await queue.enqueue(
            conn,
            JobKind.TEARDOWN,
            TeardownPayload.model_validate(
                {"system_id": str(case.system_id), "external_boot_authority_v1": marker[0]}
            ),
            {"principal": "p", "agent_session": None, "project": "proj"},
            f"external-authority-{case.job_id}",
            recycle=queue.JobRecyclePolicy.TERMINAL,
        )
    assert (recycled.id, recycled.attempt) == (case.job_id, 0)

    # A different incarnation claims the recycled job, as claim_worker_job would.
    successor = replace(
        case, worker_id=f"docker:external-authority-c2-{uuid4()}", credential=b"z" * 32
    )
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
            "VALUES (%s, 'docker', '{}'::jsonb, %s, 4)",
            (successor.worker_id, successor.credential),
        )
        conn.execute(
            "UPDATE jobs SET state = 'running', attempt = 1, worker_id = %s, "
            "lease_expires_at = now() + interval '5 minutes', heartbeat_at = now() WHERE id = %s",
            (successor.worker_id, case.job_id),
        )
    assert _finalize(authority_role_dsns, case, old, proof, 2, old_digest) == "superseded"

    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        new = _allocate(worker, successor)
    with psycopg.connect(migrated_url) as conn:
        rows = conn.execute(
            "SELECT id, state FROM external_boot_authorities WHERE job_id = %s AND job_attempt = 1 "
            "ORDER BY generation",
            (case.job_id,),
        ).fetchall()
        new_digest = _make_current(conn, successor, new, proof, 4)
    assert rows == [(old.authority_id, "superseded"), (new.authority_id, "allocating")]
    assert _finalize(authority_role_dsns, case, old, proof, 2, old_digest) == "superseded"
    assert _finalize(authority_role_dsns, successor, new, proof, 4, new_digest) == "applied"
    assert _finalize(authority_role_dsns, successor, new, proof, 4, new_digest) == "applied"
    with psycopg.connect(migrated_url) as conn:
        released = conn.execute(
            "SELECT count(*) FROM external_boot_reservation_releases WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone()
        reservations = conn.execute(
            "SELECT count(*) FROM external_boot_reservations WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone()
        job = conn.execute("SELECT state FROM jobs WHERE id = %s", (case.job_id,)).fetchone()
    assert (released, reservations, job) == ((1,), (0,), ("succeeded",))

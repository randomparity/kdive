"""Real-Postgres proofs of receipt-bound exhausted authority System discharge (ADR-0754, #3143)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kdive.db import migrate
from kdive.mcp.tools.lifecycle.allocations.lifecycle import release_allocation
from kdive.mcp.tools.lifecycle.systems.admin import teardown_system
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemJournalPhase,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemObservationV1,
    AuthoritySystemPreactivationAbsentV1,
    AuthoritySystemTakeoverRequestV1,
    canonical_system_authority_bytes,
    make_authority_system_record,
    system_authority_digest,
)
from kdive.reconciler.repairs.jobs import repair_terminal_authority_system_attempts
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.external_boot.admission import (
    AUTHORITY_PREACTIVATION_DENIAL_REASON,
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from tests.db.external_boot_authority_support import _apply_through, _apply_version, _RoleDsns
from tests.db.test_migration_0149_authority_system_provisioning import (
    _seed_direct_authority_case,
    _seed_direct_terminal_attempt,
)

_DISCHARGE = "SELECT discharge_exhausted_authority_system_provisions(%s::integer)"
_PROOF = "SELECT authority_system_preactivation_absence_proven(%s)"
_COUNT = "SELECT count_claimable_worker_jobs(ARRAY['default'])"
_CLAIM = "SELECT id FROM claim_worker_job(%s,%s,interval '1 minute',ARRAY['default'])"
_BOOTSTRAP_IDENTITY = "sha256:" + hashlib.sha256(b"ssh-ed25519 YWFhYQ== kdive-system").hexdigest()
_ADMIN = RequestContext("p", "discharge", ("proj",), {"proj": Role.ADMIN})
_CONTRIBUTOR = RequestContext("p", "discharge", ("proj",), {"proj": Role.CONTRIBUTOR})


class _NoArtifacts:
    def delete_retired_key_batch(self, key: str, limit: int) -> bool:
        return True


def _advance(
    authority: psycopg.Connection,
    worker: str,
    request: AuthoritySystemTakeoverRequestV1 | AuthoritySystemMutationRequestV1,
    head: tuple[int, str],
    phase: AuthoritySystemJournalPhase,
    *,
    receipt: bytes | None = None,
    observation: AuthoritySystemObservationV1 | None = None,
    outcome: str | None = None,
) -> tuple[int, str]:
    sequence, digest = head
    record = make_authority_system_record(
        request,
        sequence=sequence + 1,
        previous_digest=digest,
        phase=phase,
        observation=observation,
        outcome=outcome,
    )
    row = authority.execute(
        "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,%s,%s,%s,%s)",
        (
            worker,
            request.authority_id,
            request.generation,
            sequence,
            digest,
            Jsonb(record.model_dump(mode="json", by_alias=True)),
            receipt,
        ),
    ).fetchone()
    assert row is not None and row[0] == "advanced", row
    return int(row[1]), str(row[2])


def _take_over(
    role_dsns: _RoleDsns,
    case: dict[str, Any],
    job_id: UUID,
    job_attempt: int,
    marker: dict[str, Any],
    head: tuple[int, str],
) -> tuple[AuthoritySystemTakeoverRequestV1, tuple[int, str]]:
    """Allocate, journal and acknowledge one genuine authority attempt for a claimed job."""
    request_id = uuid4()
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = worker.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,%s,%s)",
            (case["credential"], job_id, job_attempt, request_id),
        ).fetchone()
    assert allocated is not None and allocated[0] == "allocated", allocated
    request = AuthoritySystemTakeoverRequestV1.model_validate_json(
        json.dumps(
            {
                **marker,
                "schema": "authority-system-attempt-v1",
                "authority_id": allocated[1],
                "generation": allocated[2],
                "attempt_id": request_id,
                "operation_digest": allocated[3],
                "bootstrap_identity": _BOOTSTRAP_IDENTITY,
            },
            default=str,
        )
    )
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as authority:
        head = _advance(
            authority,
            case["worker"],
            request,
            head,
            AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
        )
        head = _advance(
            authority,
            case["worker"],
            request,
            head,
            AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        ack = worker.execute(
            "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case["credential"],
                job_id,
                job_attempt,
                request.authority_id,
                request.generation,
                request_id,
                head[0],
                head[1],
                "sha256:" + "d" * 64,
            ),
        ).fetchone()
    assert ack is not None and ack[0] == "acknowledged", ack
    return request, head


def _exhausted_provision(migrated_url: str, role_dsns: _RoleDsns) -> dict[str, Any]:
    """Reproduce #3143: an acknowledged final provision attempt whose worker died."""
    with psycopg.connect(migrated_url) as admin:
        case: dict[str, Any] = _seed_direct_authority_case(
            admin, operation="provision", ownership_state="provisioning", attempt=3
        )
    _request, head = _take_over(
        role_dsns, case, case["job_id"], 3, case["marker"], (0, GENESIS_DIGEST)
    )
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 minute' WHERE id=%s",
            (case["job_id"],),
        )
    return {**case, "head": head}


def _queue_teardown(pool_dsn: str, system_id: UUID) -> None:
    async def run() -> None:
        async with AsyncConnectionPool(pool_dsn, min_size=1, max_size=1, open=False) as pool:
            response = await teardown_system(pool, _ADMIN, str(system_id))
        assert response.status == "queued", response

    asyncio.run(run())


def _publish_absence(
    migrated_url: str, role_dsns: _RoleDsns, case: dict[str, Any]
) -> dict[str, Any]:
    """Run the supported teardown through a genuine preactivation-absence receipt."""
    _queue_teardown(role_dsns("kdive_server"), case["system_id"])
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        claimed = worker.execute(_CLAIM, (case["worker"], case["credential"])).fetchone()
    with psycopg.connect(migrated_url) as admin:
        teardown = admin.execute(
            "SELECT id,attempt,payload->'authority_system_v1' FROM jobs WHERE dedup_key=%s",
            (f"{case['system_id']}:teardown",),
        ).fetchone()
    assert teardown is not None and claimed == (teardown[0],)
    request, head = _take_over(role_dsns, case, teardown[0], teardown[1], teardown[2], case["head"])
    mutation = AuthoritySystemMutationRequestV1.model_validate(
        request.model_dump(mode="python", by_alias=True)
    )
    proof = AuthoritySystemPreactivationAbsentV1.model_validate(
        {
            **mutation.model_dump(mode="python", by_alias=True, exclude={"schema_"}),
            "disposition": "preactivation-absent",
            "intent_identity": "sha256:" + "f" * 64,
            "domain_absent": True,
            "root_storage_absent": True,
            "baseline_absent": True,
            "private_intent_absent": True,
            "quarantine_retained": False,
            "completed_at": datetime(2026, 10, 12, tzinfo=UTC),
        }
    )
    receipt = canonical_system_authority_bytes(proof)
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as authority:
        head = _advance(
            authority, case["worker"], mutation, head, AuthoritySystemJournalPhase.ADMITTED
        )
        head = _advance(
            authority,
            case["worker"],
            mutation,
            head,
            AuthoritySystemJournalPhase.TERMINAL,
            receipt=receipt,
            observation=AuthoritySystemObservationV1(
                category="absent", composite_state=system_authority_digest(proof)
            ),
            outcome="preactivation-absent",
        )
    return {
        "job_id": teardown[0],
        "job_attempt": teardown[1],
        "request": request,
        "head": head,
        "receipt": receipt,
        "receipt_digest": system_authority_digest(proof),
    }


def _finalize_by_worker(role_dsns: _RoleDsns, case: dict[str, Any], absence: dict[str, Any]):
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        return worker.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case["credential"],
                absence["job_id"],
                absence["job_attempt"],
                absence["request"].authority_id,
                absence["request"].generation,
                absence["head"][0],
                absence["head"][1],
                absence["receipt"],
            ),
        ).fetchone()


def _assert_unclaimable(role_dsns: _RoleDsns, case: dict[str, Any]) -> None:
    """Claim and queue-depth counting both still refuse the exhausted job."""
    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        assert worker.execute(_COUNT).fetchone() == (0,)
        assert worker.execute(_CLAIM, (case["worker"], case["credential"])).fetchone() is None


def _scalar(dsn: str, query: LiteralString, params: tuple[object, ...]) -> Any:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _job(migrated_url: str, job_id: object) -> tuple[object, ...]:
    with psycopg.connect(migrated_url) as admin:
        row = admin.execute(
            "SELECT state,attempt,max_attempts,worker_id,lease_expires_at IS NULL,result_ref "
            "FROM jobs WHERE id=%s",
            (job_id,),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _release(server_dsn: str, allocation_id: object, ctx: RequestContext = _CONTRIBUTOR) -> Any:
    async def run() -> Any:
        async with AsyncConnectionPool(server_dsn, min_size=1, max_size=1, open=False) as pool:
            return await release_allocation(pool, ctx, str(allocation_id))

    return asyncio.run(run())


def _reconcile(reconciler_dsn: str) -> int:
    async def run() -> int:
        async with await psycopg.AsyncConnection.connect(reconciler_dsn) as conn:
            return await repair_terminal_authority_system_attempts(conn, cast(Any, _NoArtifacts()))

    return asyncio.run(run())


def test_migration_0171_is_registered_once() -> None:
    assert [
        (migration.version, migration.filename)
        for migration in migrate.discover_migrations()
        if migration.version == "0171"
    ] == [("0171", "0171_authority_system_discharge.sql")]


def test_worker_absence_receipt_discharges_exhausted_provision_and_admits_release(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _exhausted_provision(migrated_url, authority_role_dsns)
    server, reconciler = (
        authority_role_dsns("kdive_server"),
        authority_role_dsns("kdive_reconciler"),
    )
    stuck = ("running", 3, 3, case["worker"], False, None)
    assert _job(migrated_url, case["job_id"]) == stuck
    assert _reconcile(reconciler) == 0
    _assert_unclaimable(authority_role_dsns, case)
    assert _scalar(server, _PROOF, (case["system_id"],)) is False
    denied = _release(server, case["allocation_id"])
    assert denied.status == "error"
    assert AUTHORITY_PREACTIVATION_DENIAL_REASON in str(denied.data)

    absence = _publish_absence(migrated_url, authority_role_dsns, case)
    assert _reconcile(reconciler) == 0
    assert _scalar(server, _PROOF, (case["system_id"],)) is False
    assert _job(migrated_url, case["job_id"]) == stuck

    assert _finalize_by_worker(authority_role_dsns, case, absence) == (
        "applied",
        "succeeded",
        "torn_down",
    )
    assert _scalar(server, _PROOF, (case["system_id"],)) is True
    assert _scalar(reconciler, _PROOF, (case["system_id"],)) is True
    _assert_unclaimable(authority_role_dsns, case)

    assert _reconcile(reconciler) == 1
    discharged = ("canceled", 3, 3, None, True, absence["receipt_digest"])
    assert _job(migrated_url, case["job_id"]) == discharged
    assert _reconcile(reconciler) == 0
    assert _job(migrated_url, case["job_id"]) == discharged
    _assert_unclaimable(authority_role_dsns, case)

    released = _release(server, case["allocation_id"])
    assert released.status == "released", released
    assert (
        _scalar(migrated_url, "SELECT state FROM allocations WHERE id=%s", (case["allocation_id"],))
        == "released"
    )


def test_reconciler_absence_finalization_discharges_in_the_same_pass(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _exhausted_provision(migrated_url, authority_role_dsns)
    absence = _publish_absence(migrated_url, authority_role_dsns, case)
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (absence["job_id"],),
        )

    assert _reconcile(authority_role_dsns("kdive_reconciler")) == 2

    assert _job(migrated_url, absence["job_id"])[0] == "succeeded"
    assert _job(migrated_url, case["job_id"]) == (
        "canceled",
        3,
        3,
        None,
        True,
        absence["receipt_digest"],
    )
    released = _release(authority_role_dsns("kdive_server"), case["allocation_id"])
    assert released.status == "released", released


def _discharged_residue(conn: psycopg.Connection) -> dict[str, Any]:
    """Seed an already-consumed absence receipt beside its stranded exhausted provision job."""
    case: dict[str, Any] = _seed_direct_authority_case(
        conn, operation="preactivation-teardown", ownership_state="teardown-requested"
    )
    provision_id = uuid4()
    marker = {**case["marker"], "operation": "provision", "operation_identity": "prior-provision"}
    conn.execute(
        "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
        "payload,authorizing,dedup_key,created_at) VALUES (%s,'provision','running',3,3,%s,"
        "clock_timestamp()-interval '1 minute',%s,'{}',%s,clock_timestamp()-interval '1 hour')",
        (provision_id, case["worker"], Jsonb({"authority_system_v1": marker}), str(provision_id)),
    )
    conn.execute(
        "INSERT INTO authority_system_attempts (system_id,generation,operation,job_id,"
        "job_attempt,worker_incarnation,request_attempt_id,operation_identity,operation_digest,"
        "state,superseded_at) VALUES (%s,1,'provision',%s,3,%s,%s,'prior-provision',%s,"
        "'superseded',clock_timestamp())",
        (case["system_id"], provision_id, case["worker"], uuid4(), "sha256:" + "c" * 64),
    )
    authority_id, _request, receipt, head, digest = _seed_direct_terminal_attempt(
        conn, case, disposition="preactivation-absent", generation=2
    )
    finalized = conn.execute(
        "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,2,2,%s,%s)",
        (case["credential"], case["job_id"], authority_id, head, receipt),
    ).fetchone()
    assert finalized == ("applied", "succeeded", "torn_down")
    return {**case, "provision_id": provision_id, "receipt_digest": digest}


# Forged tuples bypass the immutability triggers and foreign keys that keep them unreachable.
_UNFREEZE: LiteralString = "SET session_replication_role = replica"
_REFREEZE: LiteralString = "SET session_replication_role = DEFAULT"


def _forged(statement: LiteralString) -> tuple[LiteralString, ...]:
    return (_UNFREEZE, statement, _REFREEZE)


def _owner(assignment: LiteralString) -> tuple[LiteralString, ...]:
    return _forged(
        "UPDATE authority_system_ownership SET " + assignment + " WHERE system_id=%(system)s"
    )


def _receipt(assignment: LiteralString) -> tuple[LiteralString, ...]:
    return _forged(
        "UPDATE authority_system_attempts SET "
        + assignment
        + " WHERE system_id=%(system)s AND operation='preactivation-teardown'"
    )


def _marker(field: LiteralString) -> tuple[LiteralString, ...]:
    return (
        "UPDATE jobs SET payload=jsonb_set(payload,'{authority_system_v1,"
        + field
        + "}',to_jsonb(gen_random_uuid()::text)) WHERE id=%(job)s",
    )


_REFUSALS: dict[str, tuple[LiteralString, ...]] = {
    "live-lease": (
        "UPDATE jobs SET lease_expires_at=clock_timestamp()+interval '5 minutes' WHERE id=%(job)s",
    ),
    "budget-remains": ("UPDATE jobs SET attempt=2 WHERE id=%(job)s",),
    "not-running": (
        "UPDATE jobs SET state='queued',worker_id=NULL,lease_expires_at=NULL WHERE id=%(job)s",
    ),
    "enqueued-after-receipt": (
        "UPDATE jobs SET created_at=clock_timestamp()+interval '1 minute' WHERE id=%(job)s",
    ),
    "unmarked": ("UPDATE jobs SET payload=payload-'authority_system_v1' WHERE id=%(job)s",),
    "foreign-identity": (
        "UPDATE jobs SET payload=jsonb_set(payload,"
        "'{authority_system_v1,resource_name}','\"other-host\"') WHERE id=%(job)s",
    ),
    "foreign-system": (
        "UPDATE jobs SET payload=jsonb_set(payload,"
        "'{authority_system_v1,system_id}',to_jsonb(gen_random_uuid()::text)) WHERE id=%(job)s",
    ),
    "teardown-operation": (
        "UPDATE jobs SET payload=jsonb_set(payload,"
        "'{authority_system_v1,operation}','\"preactivation-teardown\"') WHERE id=%(job)s",
    ),
    "teardown-kind": ("UPDATE jobs SET kind='teardown' WHERE id=%(job)s",),
    "active-attempt": (
        "INSERT INTO authority_system_attempts (system_id,generation,operation,job_id,"
        "job_attempt,worker_incarnation,request_attempt_id,operation_identity,operation_digest) "
        "SELECT system_id,3,'provision',%(job)s,3,worker_incarnation,gen_random_uuid(),"
        "'prior-provision',operation_digest FROM authority_system_attempts WHERE job_id=%(job)s",
    ),
    "head-moved": (
        "UPDATE authority_system_ownership SET journal_digest='sha256:'||repeat('9',64) "
        "WHERE system_id=%(system)s",
    ),
    "unconsumed-receipt": _receipt("consumed_at=NULL"),
    "foreign-attempt": _forged(
        "UPDATE authority_system_attempts SET system_id=%(other)s WHERE job_id=%(job)s"
    ),
    "terminal-own-attempt": _forged(
        "UPDATE authority_system_attempts AS own SET state='terminal',superseded_at=NULL,"
        "ack_sequence=r.ack_sequence,ack_digest=r.ack_digest,"
        "quiescence_digest=r.quiescence_digest,acknowledged_at=r.acknowledged_at,"
        "ack_head_sequence=r.ack_head_sequence,ack_head_digest=r.ack_head_digest,"
        "terminal_head_sequence=r.terminal_head_sequence,"
        "terminal_head_digest=r.terminal_head_digest,receipt_bytes=r.receipt_bytes,"
        "receipt_digest=r.receipt_digest,receipt_disposition='retained-quarantine',"
        "receipt_at=r.receipt_at,consumed_at=r.consumed_at "
        "FROM authority_system_attempts AS r WHERE own.job_id=%(job)s "
        "AND r.system_id=%(system)s AND r.operation='preactivation-teardown'"
    ),
    "foreign-allocation": _marker("allocation_id"),
    "foreign-resource": _marker("resource_id"),
    "foreign-provider": (
        "UPDATE jobs SET payload=jsonb_set(payload,'{authority_system_v1,provider_kind}',"
        "'\"remote-libvirt\"') WHERE id=%(job)s",
    ),
    "foreign-authority": _marker("authority_instance"),
    "foreign-profile": _marker("profile_identity"),
    "foreign-root": _marker("root_identity"),
    "owner-not-torn-down": _owner("state='teardown-requested'"),
    "owner-activated": _owner("first_activation_id=gen_random_uuid()"),
    "head-not-terminal": _owner("journal_phase='observed'"),
    "head-sequence-moved": _owner("journal_sequence=journal_sequence+1"),
    "system-not-torn-down": _forged("UPDATE systems SET state='failed' WHERE id=%(system)s"),
    "provision-receipt": _receipt("operation='provision'"),
    "quarantine-receipt": _receipt("receipt_disposition='retained-quarantine'"),
}
_PROOF_REFUSALS = {
    "active-attempt",
    "head-moved",
    "unconsumed-receipt",
    "owner-not-torn-down",
    "owner-activated",
    "head-not-terminal",
    "head-sequence-moved",
    "system-not-torn-down",
    "provision-receipt",
    "quarantine-receipt",
}


@pytest.mark.parametrize("refusal", sorted(_REFUSALS))
def test_discharge_and_release_proof_refuse_each_unproven_tuple(
    migrated_url: str, authority_role_dsns: _RoleDsns, refusal: str
) -> None:
    with psycopg.connect(migrated_url) as admin:
        residue = _discharged_residue(admin)
        other = _seed_direct_authority_case(
            admin, operation="provision", ownership_state="provisioning"
        )
        for statement in _REFUSALS[refusal]:
            admin.execute(
                statement,
                {
                    "job": residue["provision_id"],
                    "system": residue["system_id"],
                    "other": other["system_id"],
                },
            )
    before = _job(migrated_url, residue["provision_id"])
    reconciler = authority_role_dsns("kdive_reconciler")
    with psycopg.connect(reconciler) as conn:
        assert conn.execute(_DISCHARGE, (100,)).fetchone() == (0,)
        proven = conn.execute(_PROOF, (residue["system_id"],)).fetchone()
    assert proven == (refusal not in _PROOF_REFUSALS,)
    assert _job(migrated_url, residue["provision_id"]) == before


def test_proof_refuses_an_unconsumed_absence_receipt(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        case: dict[str, Any] = _seed_direct_authority_case(
            admin, operation="preactivation-teardown", ownership_state="teardown-requested"
        )
        _seed_direct_terminal_attempt(admin, case, disposition="preactivation-absent")
    with psycopg.connect(authority_role_dsns("kdive_server")) as server:
        assert server.execute(_PROOF, (case["system_id"],)).fetchone() == (False,)
        assert server.execute(_PROOF, (uuid4(),)).fetchone() == (False,)


@pytest.mark.parametrize(
    ("role", "allowed"),
    [
        ("kdive_server", {_PROOF}),
        ("kdive_reconciler", {_PROOF, _DISCHARGE}),
        ("kdive_worker", set()),
        ("kdive_provider_authority", set()),
    ],
)
def test_functions_are_restricted_to_their_control_roles(
    authority_role_dsns: _RoleDsns, role: str, allowed: set[str]
) -> None:
    with psycopg.connect(authority_role_dsns(role), autocommit=True) as conn:
        for statement, argument in ((_PROOF, uuid4()), (_DISCHARGE, 1)):
            if statement in allowed:
                conn.execute(statement, (argument,))
                continue
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement, (argument,))


@pytest.mark.parametrize("limit", [0, 101])
def test_discharge_limit_is_bounded(authority_role_dsns: _RoleDsns, limit: int) -> None:
    with (
        psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn,
        pytest.raises(psycopg.errors.InvalidParameterValue, match="repair limit"),
    ):
        conn.execute(_DISCHARGE, (limit,))


def test_ineligible_prefix_cannot_starve_eligible_residue(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        residue = _discharged_residue(admin)
        foreign = {
            **residue["marker"],
            "operation": "provision",
            "operation_identity": "prior-provision",
            "resource_name": "other-host",
        }
        for index in range(101):
            job_id = UUID(int=index + 1)
            admin.execute(
                "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,"
                "lease_expires_at,payload,authorizing,dedup_key,created_at) VALUES "
                "(%s,'provision','running',3,3,%s,clock_timestamp()-interval '1 minute',%s,"
                "'{}',%s,clock_timestamp()-interval '1 hour')",
                (job_id, residue["worker"], Jsonb({"authority_system_v1": foreign}), str(job_id)),
            )
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_DISCHARGE, (1,)).fetchone() == (1,)
    assert _job(migrated_url, residue["provision_id"])[0] == "canceled"
    assert (
        _scalar(
            migrated_url,
            "SELECT count(*) FROM jobs WHERE state='running' AND id<=%s",
            (UUID(int=101),),
        )
        == 101
    )


_CONTENTIONS: dict[str, LiteralString] = {
    "system-lock": "SELECT pg_advisory_xact_lock("
    "hashtextextended('kdive:system:' || %(system)s::text,2125))",
    "job-row": "SELECT 1 FROM jobs WHERE id=%(job)s FOR UPDATE",
    "ownership-row": "SELECT 1 FROM authority_system_ownership WHERE system_id=%(system)s "
    "FOR UPDATE",
}


@pytest.mark.parametrize("contention", sorted(_CONTENTIONS))
def test_locked_candidate_defers_without_waiting(
    migrated_url: str, authority_role_dsns: _RoleDsns, contention: str
) -> None:
    with psycopg.connect(migrated_url) as admin:
        residue = _discharged_residue(admin)
    with psycopg.connect(migrated_url) as holder:
        holder.execute(
            _CONTENTIONS[contention],
            {"job": residue["provision_id"], "system": residue["system_id"]},
        )
        with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
            conn.execute("SET statement_timeout='5s'")
            assert conn.execute(_DISCHARGE, (100,)).fetchone() == (0,)
        assert _job(migrated_url, residue["provision_id"])[0] == "running"
    with psycopg.connect(authority_role_dsns("kdive_reconciler")) as conn:
        assert conn.execute(_DISCHARGE, (100,)).fetchone() == (1,)


def test_release_exception_is_limited_to_allocation_release_of_proven_systems(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        residue = _discharged_residue(admin)
    server = authority_role_dsns("kdive_server")

    async def admit(operation: ExternalBootOperation) -> None:
        async with await psycopg.AsyncConnection.connect(server) as conn:
            await check_external_boot_admission(
                conn, residue["system_id"], operation, project="proj"
            )

    asyncio.run(admit(ExternalBootOperation.ALLOCATION_RELEASE))
    for operation in (
        ExternalBootOperation.SYSTEM_POWER,
        ExternalBootOperation.SYSTEM_REPROVISION,
        ExternalBootOperation.SYSTEM_AUTHORIZE_SSH_KEY,
    ):
        with pytest.raises(ExternalBootDenied) as denied:
            asyncio.run(admit(operation))
        assert denied.value.details["reason"] == AUTHORITY_PREACTIVATION_DENIAL_REASON

    foreign = RequestContext("q", "discharge", ("other",), {"other": Role.ADMIN})
    assert _release(server, residue["allocation_id"], foreign).status == "error"
    with psycopg.connect(migrated_url) as admin:
        sibling = _seed_direct_authority_case(
            admin, operation="provision", ownership_state="provisioning"
        )
        admin.execute(
            "UPDATE systems SET allocation_id=%s WHERE id=%s",
            (residue["allocation_id"], sibling["system_id"]),
        )
    denied = _release(server, residue["allocation_id"])
    assert denied.status == "error"
    assert str(sibling["system_id"]) in str(denied.data)
    assert (
        _scalar(
            migrated_url, "SELECT state FROM allocations WHERE id=%s", (residue["allocation_id"],)
        )
        == "active"
    )


def test_0171_repairs_residue_discharged_before_the_upgrade(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0170")
    residue = _discharged_residue(pg_conn)
    _apply_version(pg_conn, "0171")
    assert pg_conn.execute(_DISCHARGE, (100,)).fetchone() == (1,)
    assert pg_conn.execute(
        "SELECT state,result_ref FROM jobs WHERE id=%s", (residue["provision_id"],)
    ).fetchone() == ("canceled", residue["receipt_digest"])

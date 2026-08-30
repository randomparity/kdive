"""Migration 0123 trusted authority journal-head tests (ADR-0584)."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from kdive.db.external_boot_authority_journal import (
    advance_journal_head,
    read_journal_head,
    resolve_allocating_authority_binding,
)
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    canonical_record_bytes,
    record_digest,
)
from tests.db.test_external_boot_authority_migration import (
    _allocate,
    _RoleDsns,
    _seed_case,
)
from tests.db.test_external_boot_authority_migration import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)

_FUNCTIONS = {
    "resolve_allocating_external_boot_authority(text,uuid,bigint)",
    "resolve_current_external_boot_authority(text,uuid,bigint,bigint,text)",
    "read_external_boot_authority_journal_head(text,uuid,bigint,text)",
    "advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)",
}

_DIGEST = "sha256:" + "d" * 64


def _record(
    case: Any,
    authority: Any,
    sequence: int,
    previous_digest: str,
    phase: JournalPhase,
    **changes: object,
) -> JournalRecordV1:
    values: dict[str, object] = {
        "authority_id": authority.authority_id,
        "generation": authority.generation,
        "system_id": case.system_id,
        "activation_id": case.activation_id,
        "run_id": case.run_id,
        "plan_identity": "sha256:" + "a" * 64,
        "purpose": case.purpose,
        "provider_kind": case.provider_kind,
        "authority_instance": case.authority_instance,
        "operation_identity": case.operation_identity,
        "operation_digest": authority.operation_digest,
        "sequence": sequence,
        "previous_digest": previous_digest,
        "phase": phase,
        "attempt_id": case.job_id,
    }
    if phase not in {
        JournalPhase.WATERMARK_INSTALLED,
        JournalPhase.TAKEOVER_SUPERSEDED,
        JournalPhase.TAKEOVER_ACKNOWLEDGED,
    }:
        values |= {
            "expected_source_identity": "source-a",
            "intended_target_identity": "target-a",
            "recovery_objects": (),
        }
    values.update(changes)
    return JournalRecordV1.model_validate(values)


def _payload(record: JournalRecordV1) -> dict[str, object]:
    return record.model_dump(mode="json", by_alias=True) | {
        "canonical_record": canonical_record_bytes(record).decode()
    }


def _canonicalize(payload: dict[str, object]) -> None:
    canonical = dict(payload)
    canonical.pop("canonical_record", None)
    payload["canonical_record"] = json.dumps(
        canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


def _advance_raw(
    conn: psycopg.Connection,
    case: Any,
    authority: Any,
    expected_sequence: int,
    expected_digest: str,
    payload: dict[str, object],
) -> str:
    row = conn.execute(
        "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
        (
            case.worker_id,
            authority.authority_id,
            authority.generation,
            expected_sequence,
            expected_digest,
            Jsonb(payload),
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def _head(conn: psycopg.Connection, case: Any, authority: Any) -> tuple[object, ...] | None:
    return conn.execute(
        "SELECT sequence,digest,phase,authority_id,generation,operation_identity,"
        "pending_takeover,suspended_operation FROM read_external_boot_authority_journal_head("
        "%s,%s,%s,%s)",
        (
            case.worker_id,
            authority.authority_id,
            authority.generation,
            case.authority_instance,
        ),
    ).fetchone()


def _seed_allocated(migrated_url: str, role_dsns: _RoleDsns, suffix: str) -> tuple[Any, Any]:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix=suffix)
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    return case, authority


def _promote(
    migrated_url: str, case: Any, authority: Any, acknowledgement: JournalRecordV1
) -> None:
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE external_boot_authorities SET state='current', acknowledged_at=now() "
            "WHERE id=%s",
            (authority.authority_id,),
        )
        conn.execute(
            "INSERT INTO external_boot_authority_acknowledgements "
            "(authority_id,system_id,generation,authority_instance,operation_identity,"
            "operation_digest,journal_sequence,journal_digest,positive_quiescence_digest) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                authority.authority_id,
                case.system_id,
                authority.generation,
                case.authority_instance,
                case.operation_identity,
                authority.operation_digest,
                acknowledgement.sequence,
                record_digest(acknowledgement),
                _DIGEST,
            ),
        )


def _allocate_successor(migrated_url: str, role_dsns: _RoleDsns, case: Any) -> tuple[Any, Any]:
    successor_job = uuid4()
    successor_identity = f"successor-{uuid4()}"
    with psycopg.connect(migrated_url) as conn:
        marker_row = conn.execute(
            "SELECT payload->'external_boot_authority_v1' FROM jobs WHERE id=%s",
            (case.job_id,),
        ).fetchone()
        assert marker_row is not None
        marker = dict(marker_row[0])
        marker["operation_identity"] = successor_identity
        conn.execute(
            "INSERT INTO jobs (id,kind,payload,state,attempt,max_attempts,worker_id,"
            "lease_expires_at,heartbeat_at,authorizing,dedup_key) VALUES "
            "(%s,'boot',%s,'running',1,3,%s,now()+interval '5 minutes',now(),%s,%s)",
            (
                successor_job,
                Jsonb({"external_boot_authority_v1": marker}),
                case.worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                str(successor_job),
            ),
        )
    successor_case = replace(case, job_id=successor_job, operation_identity=successor_identity)
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        successor = _allocate(worker, successor_case)
    return successor_case, successor


def test_migration_0123_is_the_unique_inventory_tail() -> None:
    migrations = migrate.discover_migrations()
    assert (migrations[-1].version, migrations[-1].filename) == (
        "0123",
        "0123_external_boot_authority_journal.sql",
    )


def test_journal_head_has_bounded_continuations(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    columns = {
        row[0]: row[1]
        for row in pg_conn.execute(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema='public' "
            "AND table_name='external_boot_authority_journal_heads'"
        ).fetchall()
    }
    assert columns["pending_takeover"] == "YES"
    assert columns["suspended_operation"] == "YES"
    constraints = {
        row[0]
        for row in pg_conn.execute(
            "SELECT conname FROM pg_constraint WHERE conrelid = "
            "'external_boot_authority_journal_heads'::regclass"
        ).fetchall()
    }
    assert {
        "external_boot_journal_sequence_positive",
        "external_boot_journal_generation_positive",
        "external_boot_journal_pending_bounded",
        "external_boot_journal_suspended_bounded",
    } <= constraints


def test_only_authority_role_can_execute_journal_functions(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for function in _FUNCTIONS:
        for role in (
            "kdive_server",
            "kdive_worker",
            "kdive_reconciler",
            "kdive_lifecycle_witness",
        ):
            assert pg_conn.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')", (role, function)
            ).fetchone() == (False,)
        assert pg_conn.execute(
            "SELECT has_function_privilege('kdive_provider_authority', %s, 'EXECUTE')",
            (function,),
        ).fetchone() == (True,)


def test_runtime_roles_have_no_direct_journal_table_access(pg_conn: psycopg.Connection) -> None:
    migrate.apply_migrations(pg_conn)
    for role in (
        "kdive_server",
        "kdive_worker",
        "kdive_reconciler",
        "kdive_lifecycle_witness",
        "kdive_provider_authority",
    ):
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert pg_conn.execute(
                "SELECT has_table_privilege(%s, 'external_boot_authority_journal_heads', %s)",
                (role, privilege),
            ).fetchone() == (False,)


def test_journal_functions_are_security_definer_with_pinned_search_path(
    pg_conn: psycopg.Connection,
) -> None:
    migrate.apply_migrations(pg_conn)
    rows = pg_conn.execute(
        "SELECT p.oid::regprocedure::text, p.prosecdef, p.proconfig "
        "FROM pg_proc AS p JOIN pg_namespace AS n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname LIKE '%external_boot_authority%journal%'"
    ).fetchall()
    assert {row[0] for row in rows} == {
        "read_external_boot_authority_journal_head(text,uuid,bigint,text)",
        "advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)",
    }
    assert all(row[1] and row[2] == ['search_path=""'] for row in rows)


def test_allocating_binding_can_create_and_read_exact_genesis_head(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix="j")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider_authority:
        binding = provider_authority.execute(
            "SELECT * FROM resolve_allocating_external_boot_authority(%s, %s, %s)",
            (case.worker_id, authority.authority_id, authority.generation),
        ).fetchone()
        assert binding is not None
        record = JournalRecordV1.model_validate(
            {
                "authority_id": authority.authority_id,
                "generation": authority.generation,
                "system_id": case.system_id,
                "activation_id": case.activation_id,
                "run_id": case.run_id,
                "plan_identity": "sha256:" + "a" * 64,
                "purpose": case.purpose,
                "provider_kind": case.provider_kind,
                "authority_instance": case.authority_instance,
                "operation_identity": case.operation_identity,
                "operation_digest": authority.operation_digest,
                "sequence": 1,
                "previous_digest": GENESIS_DIGEST,
                "phase": JournalPhase.WATERMARK_INSTALLED,
                "attempt_id": case.job_id,
            }
        )
        payload = record.model_dump(mode="json", by_alias=True) | {
            "canonical_record": canonical_record_bytes(record).decode("utf-8")
        }
        assert provider_authority.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
            (
                case.worker_id,
                authority.authority_id,
                authority.generation,
                0,
                GENESIS_DIGEST,
                Jsonb(payload),
            ),
        ).fetchone() == ("advanced",)
        head = provider_authority.execute(
            "SELECT sequence, digest, phase FROM read_external_boot_authority_journal_head("
            "%s,%s,%s,%s)",
            (
                case.worker_id,
                authority.authority_id,
                authority.generation,
                case.authority_instance,
            ),
        ).fetchone()
        assert head == (1, record_digest(record), "watermark-installed")
        advance_parameters = (
            case.worker_id,
            authority.authority_id,
            authority.generation,
            0,
            GENESIS_DIGEST,
        )
        assert provider_authority.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
            (*advance_parameters, Jsonb(payload)),
        ).fetchone() == ("advanced",)
        noncanonical = payload | {"canonical_record": " " + payload["canonical_record"]}
        assert provider_authority.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
            (*advance_parameters, Jsonb(noncanonical)),
        ).fetchone() == ("conflict",)
        extra = payload | {"unexpected": "field"}
        extra["canonical_record"] = extra["canonical_record"][:-1] + ',"unexpected":"field"}'
        assert provider_authority.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,%s,%s,%s)",
            (*advance_parameters, Jsonb(extra)),
        ).fetchone() == ("conflict",)
        assert provider_authority.execute(
            "SELECT sequence, digest FROM read_external_boot_authority_journal_head(%s,%s,%s,%s)",
            (
                case.worker_id,
                authority.authority_id,
                authority.generation,
                case.authority_instance,
            ),
        ).fetchone() == (1, record_digest(record))


@pytest.mark.parametrize(
    "mismatch",
    ["authority", "generation", "system", "activation", "run", "peer"],
)
def test_cross_binding_genesis_changes_no_head(
    migrated_url: str, authority_role_dsns: _RoleDsns, mismatch: str
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, mismatch[0])
    record = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
    changed_case = case
    changed_authority = authority
    if mismatch in {"system", "activation", "run"}:
        record = record.model_copy(update={f"{mismatch}_id": uuid4()})
    elif mismatch == "authority":
        changed_authority = replace(authority, authority_id=uuid4())
    elif mismatch == "generation":
        changed_authority = replace(authority, generation=authority.generation + 1)
    elif mismatch == "peer":
        changed_case = replace(case, worker_id="docker:foreign-peer")
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider_authority:
        assert (
            _advance_raw(
                provider_authority,
                changed_case,
                changed_authority,
                0,
                GENESIS_DIGEST,
                _payload(record),
            )
            == "superseded"
        )
        assert _head(provider_authority, case, authority) is None


@pytest.mark.parametrize(
    "mutation",
    ["noncanonical", "extra", "null_uuid", "overflow", "uppercase_digest", "oversized"],
)
def test_malformed_genesis_is_conflict_without_a_write(
    migrated_url: str, authority_role_dsns: _RoleDsns, mutation: str
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, mutation[0])
    record = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
    payload = _payload(record)
    if mutation == "noncanonical":
        payload["canonical_record"] = " " + str(payload["canonical_record"])
    elif mutation == "extra":
        payload["extra"] = "field"
    elif mutation == "null_uuid":
        payload["activation_id"] = None
    elif mutation == "overflow":
        payload["sequence"] = 9_223_372_036_854_775_808
    elif mutation == "uppercase_digest":
        payload["operation_digest"] = "sha256:" + "A" * 64
    else:
        payload["authority_instance"] = "x" * 256
    if mutation != "noncanonical":
        canonical = dict(payload)
        canonical.pop("canonical_record", None)
        payload["canonical_record"] = json.dumps(
            canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider_authority:
        assert (
            _advance_raw(provider_authority, case, authority, 0, GENESIS_DIGEST, payload)
            == "conflict"
        )
        assert _head(provider_authority, case, authority) is None


def test_concurrent_identical_genesis_is_idempotent(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, "r")
    record = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)

    def advance() -> str:
        with psycopg.connect(
            authority_role_dsns("kdive_provider_authority"), autocommit=True
        ) as connection:
            return _advance_raw(connection, case, authority, 0, GENESIS_DIGEST, _payload(record))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: advance(), range(2)))
    assert results == ["advanced", "advanced"]


def test_continuation_constraints_reject_malformed_retained_json(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, "c")
    record = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider_authority:
        assert (
            _advance_raw(provider_authority, case, authority, 0, GENESIS_DIGEST, _payload(record))
            == "advanced"
        )
    suspended = {
        "authority_id": str(authority.authority_id),
        "generation": authority.generation,
        "activation_id": str(case.activation_id),
        "operation_identity": case.operation_identity,
        "attempt_id": str(case.job_id),
        "purpose": case.purpose,
        "request_digest": authority.operation_digest,
        "phase": "admitted",
        "source_identity": "source-a",
        "target_identity": "target-a",
        "ownership_digest": _DIGEST,
    }
    with psycopg.connect(migrated_url, autocommit=True) as admin:
        before = admin.execute(
            "SELECT pending_takeover,suspended_operation FROM "
            "external_boot_authority_journal_heads WHERE system_id=%s",
            (case.system_id,),
        ).fetchone()
        assert before is not None
        with pytest.raises(psycopg.DataError):
            admin.execute(
                "UPDATE external_boot_authority_journal_heads SET pending_takeover="
                "jsonb_set(pending_takeover, '{authority_id}', '\"not-a-uuid\"') "
                "WHERE system_id=%s",
                (case.system_id,),
            )
        suspended["attempt_id"] = "not-a-uuid"
        with pytest.raises(psycopg.DataError):
            admin.execute(
                "UPDATE external_boot_authority_journal_heads SET suspended_operation=%s "
                "WHERE system_id=%s",
                (Jsonb(suspended), case.system_id),
            )
        after = admin.execute(
            "SELECT pending_takeover,suspended_operation FROM "
            "external_boot_authority_journal_heads WHERE system_id=%s",
            (case.system_id,),
        ).fetchone()
        assert after == before


def test_full_current_mutation_phase_sequence_and_rejections(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, "m")
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        watermark = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
        assert (
            _advance_raw(connection, case, authority, 0, GENESIS_DIGEST, _payload(watermark))
            == "advanced"
        )
        acknowledgement = _record(
            case,
            authority,
            2,
            record_digest(watermark),
            JournalPhase.TAKEOVER_ACKNOWLEDGED,
            watermark_sequence=1,
            watermark_digest=record_digest(watermark),
        )
        assert (
            _advance_raw(
                connection,
                case,
                authority,
                1,
                record_digest(watermark),
                _payload(acknowledgement),
            )
            == "advanced"
        )
    _promote(migrated_url, case, authority, acknowledgement)
    phases = [
        JournalPhase.ADMITTED,
        JournalPhase.MUTATION_STARTED,
        JournalPhase.PROVIDER_RETURNED,
        JournalPhase.OBSERVED,
        JournalPhase.TERMINAL,
    ]
    previous = acknowledgement
    owned = RecoveryObjectBindingV1(
        system_id=case.system_id,
        activation_id=case.activation_id,
        reference="current-object-a",
    )
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        for sequence, phase in enumerate(phases, start=3):
            changes: dict[str, object] = {}
            if phase in {JournalPhase.OBSERVED, JournalPhase.TERMINAL}:
                changes["observation"] = {
                    "observation_id": str(uuid4()),
                    "category": "source",
                    "composite_state": _DIGEST,
                }
            if phase is JournalPhase.TERMINAL:
                changes["outcome"] = "source"
            record = _record(
                case,
                authority,
                sequence,
                record_digest(previous),
                phase,
                recovery_objects=(owned,),
                **changes,
            )
            before = _head(connection, case, authority)
            skipped = record.model_copy(update={"sequence": sequence + 1})
            assert (
                _advance_raw(
                    connection,
                    case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    _payload(skipped),
                )
                == "conflict"
            )
            assert _head(connection, case, authority) == before
            invalid_attempt = _payload(record)
            invalid_attempt["attempt_id"] = "not-a-uuid"
            _canonicalize(invalid_attempt)
            assert (
                _advance_raw(
                    connection,
                    case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    invalid_attempt,
                )
                == "conflict"
            )
            assert _head(connection, case, authority) == before
            if phase is not JournalPhase.ADMITTED:
                replacements = (
                    {"attempt_id": uuid4()},
                    {"expected_source_identity": "source-b"},
                    {"intended_target_identity": "target-b"},
                    {
                        "recovery_objects": (
                            RecoveryObjectBindingV1(
                                system_id=case.system_id,
                                activation_id=case.activation_id,
                                reference="current-object-b",
                            ),
                        )
                    },
                )
                for replacement in replacements:
                    drifted = record.model_copy(update=replacement)
                    assert (
                        _advance_raw(
                            connection,
                            case,
                            authority,
                            sequence - 1,
                            record_digest(previous),
                            _payload(drifted),
                        )
                        == "conflict"
                    )
                    assert _head(connection, case, authority) == before
                malformed_payloads: list[dict[str, object]] = []
                for field, value in (
                    ("expected_source_identity", ""),
                    ("intended_target_identity", "x" * 1025),
                ):
                    malformed = _payload(record)
                    malformed[field] = value
                    _canonicalize(malformed)
                    malformed_payloads.append(malformed)
                foreign_object = _payload(record)
                recovery = cast(list[dict[str, object]], foreign_object["recovery_objects"])
                recovery[0]["system_id"] = str(uuid4())
                _canonicalize(foreign_object)
                malformed_payloads.append(foreign_object)
                if phase in {JournalPhase.OBSERVED, JournalPhase.TERMINAL}:
                    bad_observation = _payload(record)
                    observation = cast(dict[str, object], bad_observation["observation"])
                    observation["category"] = "foreign"
                    _canonicalize(bad_observation)
                    malformed_payloads.append(bad_observation)
                if phase is JournalPhase.TERMINAL:
                    bad_outcome = _payload(record)
                    bad_outcome["observation"] = None
                    _canonicalize(bad_outcome)
                    malformed_payloads.append(bad_outcome)
                for malformed in malformed_payloads:
                    assert (
                        _advance_raw(
                            connection,
                            case,
                            authority,
                            sequence - 1,
                            record_digest(previous),
                            malformed,
                        )
                        == "conflict"
                    )
                    assert _head(connection, case, authority) == before
            assert (
                _advance_raw(
                    connection,
                    case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    _payload(record),
                )
                == "advanced"
            )
            previous = record


@pytest.mark.anyio
async def test_repository_round_trips_typed_binding_and_head(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, "t")
    async with await psycopg.AsyncConnection.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        binding = await resolve_allocating_authority_binding(
            connection,
            peer_incarnation_id=case.worker_id,
            authority_id=authority.authority_id,
            generation=authority.generation,
        )
        assert binding is not None and isinstance(binding.authority_id, UUID)
        record = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
        assert (
            await advance_journal_head(
                connection,
                binding=binding,
                expected_sequence=0,
                expected_digest=GENESIS_DIGEST,
                record=record,
            )
            == "advanced"
        )
        head = await read_journal_head(connection, binding=binding)
        assert head is not None
        assert head.sequence == 1 and head.phase is JournalPhase.WATERMARK_INSTALLED


def test_successor_takeover_has_exact_supersede_watermark_ack_order(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    first_case, first = _seed_allocated(migrated_url, authority_role_dsns, "g")
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        first_watermark = _record(
            first_case, first, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED
        )
        assert (
            _advance_raw(
                connection, first_case, first, 0, GENESIS_DIGEST, _payload(first_watermark)
            )
            == "advanced"
        )
    successor_case, successor = _allocate_successor(migrated_url, authority_role_dsns, first_case)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        skipped = _record(
            successor_case,
            successor,
            2,
            record_digest(first_watermark),
            JournalPhase.WATERMARK_INSTALLED,
        )
        before = _head(connection, successor_case, successor)
        assert (
            _advance_raw(
                connection,
                successor_case,
                successor,
                1,
                record_digest(first_watermark),
                _payload(skipped),
            )
            == "conflict"
        )
        assert _head(connection, successor_case, successor) == before
        superseded = _record(
            successor_case,
            successor,
            2,
            record_digest(first_watermark),
            JournalPhase.TAKEOVER_SUPERSEDED,
            predecessor_generation=first.generation,
            watermark_sequence=1,
            watermark_digest=record_digest(first_watermark),
        )
        assert (
            _advance_raw(
                connection,
                successor_case,
                successor,
                1,
                record_digest(first_watermark),
                _payload(superseded),
            )
            == "advanced"
        )
        pending = _head(connection, successor_case, successor)
        assert pending is not None
        pending_takeover = pending[6]
        assert isinstance(pending_takeover, dict)
        pending_takeover = cast(dict[str, object], pending_takeover)
        assert pending_takeover["authority_id"] == str(successor.authority_id)
        successor_watermark = _record(
            successor_case,
            successor,
            3,
            record_digest(superseded),
            JournalPhase.WATERMARK_INSTALLED,
        )
        assert (
            _advance_raw(
                connection,
                successor_case,
                successor,
                2,
                record_digest(superseded),
                _payload(successor_watermark),
            )
            == "advanced"
        )
        acknowledgement = _record(
            successor_case,
            successor,
            4,
            record_digest(successor_watermark),
            JournalPhase.TAKEOVER_ACKNOWLEDGED,
            watermark_sequence=3,
            watermark_digest=record_digest(successor_watermark),
        )
        assert (
            _advance_raw(
                connection,
                successor_case,
                successor,
                3,
                record_digest(successor_watermark),
                _payload(acknowledgement),
            )
            == "advanced"
        )
        final = _head(connection, successor_case, successor)
        assert final is not None and final[6] is None and final[7] is None


@pytest.mark.parametrize("anchored_phase", [JournalPhase.ADMITTED, JournalPhase.MUTATION_STARTED])
def test_successor_inherits_and_completes_exact_older_operation(
    migrated_url: str, authority_role_dsns: _RoleDsns, anchored_phase: JournalPhase
) -> None:
    case, authority = _seed_allocated(migrated_url, authority_role_dsns, anchored_phase.value[0])
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        watermark = _record(case, authority, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
        assert (
            _advance_raw(connection, case, authority, 0, GENESIS_DIGEST, _payload(watermark))
            == "advanced"
        )
        acknowledgement = _record(
            case,
            authority,
            2,
            record_digest(watermark),
            JournalPhase.TAKEOVER_ACKNOWLEDGED,
            watermark_sequence=1,
            watermark_digest=record_digest(watermark),
        )
        assert (
            _advance_raw(
                connection, case, authority, 1, record_digest(watermark), _payload(acknowledgement)
            )
            == "advanced"
        )
    _promote(migrated_url, case, authority, acknowledgement)
    previous = acknowledgement
    sequence = 3
    owned = RecoveryObjectBindingV1(
        system_id=case.system_id,
        activation_id=case.activation_id,
        reference="recovery-a",
    )
    admitted = _record(
        case,
        authority,
        sequence,
        record_digest(previous),
        JournalPhase.ADMITTED,
        recovery_objects=(owned,),
    )
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        assert (
            _advance_raw(
                connection,
                case,
                authority,
                sequence - 1,
                record_digest(previous),
                _payload(admitted),
            )
            == "advanced"
        )
        previous = admitted
        if anchored_phase is JournalPhase.MUTATION_STARTED:
            sequence += 1
            started = _record(
                case,
                authority,
                sequence,
                record_digest(previous),
                JournalPhase.MUTATION_STARTED,
                recovery_objects=(owned,),
            )
            assert (
                _advance_raw(
                    connection,
                    case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    _payload(started),
                )
                == "advanced"
            )
            previous = started
    successor_case, successor = _allocate_successor(migrated_url, authority_role_dsns, case)
    sequence += 1
    successor_watermark = _record(
        successor_case,
        successor,
        sequence,
        record_digest(previous),
        JournalPhase.WATERMARK_INSTALLED,
    )
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        assert (
            _advance_raw(
                connection,
                successor_case,
                successor,
                sequence - 1,
                record_digest(previous),
                _payload(successor_watermark),
            )
            == "advanced"
        )
        retained = _head(connection, successor_case, successor)
        assert retained is not None
        suspended = retained[7]
        assert isinstance(suspended, dict)
        assert cast(dict[str, object], suspended)["phase"] == anchored_phase.value
        completion_phases = (
            [JournalPhase.TERMINAL]
            if anchored_phase is JournalPhase.ADMITTED
            else [JournalPhase.PROVIDER_RETURNED, JournalPhase.OBSERVED, JournalPhase.TERMINAL]
        )
        previous = successor_watermark
        for phase in completion_phases:
            sequence += 1
            changes: dict[str, object] = {}
            if phase is JournalPhase.TERMINAL and anchored_phase is JournalPhase.ADMITTED:
                changes["outcome"] = "never-began"
            elif phase in {JournalPhase.OBSERVED, JournalPhase.TERMINAL}:
                changes["observation"] = {
                    "observation_id": str(uuid4()),
                    "category": "source",
                    "composite_state": _DIGEST,
                }
                if phase is JournalPhase.TERMINAL:
                    changes["outcome"] = "source"
            completion = _record(
                case,
                authority,
                sequence,
                record_digest(previous),
                phase,
                recovery_objects=(owned,),
                **changes,
            )
            before = _head(connection, successor_case, successor)
            wrong_owner = RecoveryObjectBindingV1(
                system_id=case.system_id,
                activation_id=case.activation_id,
                reference="recovery-b",
            )
            mismatches = [
                (case, completion.model_copy(update={"attempt_id": uuid4()})),
                (case, completion.model_copy(update={"operation_digest": _DIGEST})),
                (case, completion.model_copy(update={"recovery_objects": (wrong_owner,)})),
                (replace(case, worker_id="docker:foreign-peer"), completion),
            ]
            for mismatch_case, mismatch_record in mismatches:
                assert _advance_raw(
                    connection,
                    mismatch_case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    _payload(mismatch_record),
                ) in {"superseded", "conflict"}
                assert _head(connection, successor_case, successor) == before
            assert (
                _advance_raw(
                    connection,
                    case,
                    authority,
                    sequence - 1,
                    record_digest(previous),
                    _payload(completion),
                )
                == "advanced"
            )
            previous = completion
        completed = _head(connection, successor_case, successor)
        assert completed is not None and completed[7] is None and completed[6] is not None


def test_concurrent_successors_only_allow_newest_takeover_progress(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    first_case, first = _seed_allocated(migrated_url, authority_role_dsns, "c")
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        watermark = _record(first_case, first, 1, GENESIS_DIGEST, JournalPhase.WATERMARK_INSTALLED)
        assert (
            _advance_raw(connection, first_case, first, 0, GENESIS_DIGEST, _payload(watermark))
            == "advanced"
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(_allocate_successor, migrated_url, authority_role_dsns, first_case)
            for _ in range(2)
        ]
        successors = [future.result() for future in futures]
    older_case, older = min(successors, key=lambda item: item[1].generation)
    newest_case, newest = max(successors, key=lambda item: item[1].generation)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        older_record = _record(
            older_case,
            older,
            2,
            record_digest(watermark),
            JournalPhase.TAKEOVER_SUPERSEDED,
            predecessor_generation=first.generation,
            watermark_sequence=1,
            watermark_digest=record_digest(watermark),
        )
        before = _head(connection, newest_case, newest)
        assert (
            _advance_raw(
                connection,
                older_case,
                older,
                1,
                record_digest(watermark),
                _payload(older_record),
            )
            == "superseded"
        )
        assert _head(connection, newest_case, newest) == before
        winner = _record(
            newest_case,
            newest,
            2,
            record_digest(watermark),
            JournalPhase.TAKEOVER_SUPERSEDED,
            predecessor_generation=first.generation,
            watermark_sequence=1,
            watermark_digest=record_digest(watermark),
        )
        assert (
            _advance_raw(
                connection,
                newest_case,
                newest,
                1,
                record_digest(watermark),
                _payload(winner),
            )
            == "advanced"
        )

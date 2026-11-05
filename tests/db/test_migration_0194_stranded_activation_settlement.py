"""Real-Postgres contracts for stranded activation settlement (#3550, ADR-0816)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from kdive.db.external_boot_activations import (
    ExternalBootActivationRepository,
    SettlementStatus,
)
from kdive.domain.external_boot_activation import (
    ExternalBootActivation,
    ExternalBootReservationRelease,
)
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import PlatformRole
from kdive.serialization import validate_stored
from kdive.services.external_boot.settlement import settle_stranded_activation
from tests.db.external_boot_authority_support import (
    _allocate,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)

_DOMAIN = "kdive-stranded"
_COMPOSITE = "sha256:" + "e" * 64
_AUTHORITY_RECORD = "SELECT record_stranded_activation_authority_absence(%s,%s,%s,%s,%s)"
_WORKER_RECORD = "SELECT record_stranded_activation_worker_ownership(%s,%s,%s,%s,%s)"
_SETTLE = "SELECT settle_stranded_external_boot_activation(%s,%s,%s,%s,%s)"


@dataclass(frozen=True, slots=True)
class _Stranded:
    case: _AuthorityCase
    observer_job: UUID


def _strand(migrated_url: str, role_dsns: _RoleDsns, *, suffix: str = "s") -> _Stranded:
    """Build the #3138 shape: a failed activate job over an unprepared activation."""
    with psycopg.connect(migrated_url) as conn:
        case = _seed_case(conn, worker_suffix=suffix)
        conn.execute(
            "UPDATE external_boot_activations SET state = 'preparing', materialization = NULL, "
            "recovery_point = NULL WHERE id = %s",
            (case.activation_id,),
        )
        conn.execute("UPDATE systems SET domain_name = %s WHERE id = %s", (_DOMAIN, case.system_id))
        conn.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state) "
            "VALUES (%s, 'store/private', 'owner/private', 4096, 'pending')",
            (case.activation_id,),
        )
        conn.execute(
            "INSERT INTO remote_module_attempt_obligations (system_id, run_id, operation_nonce) "
            "VALUES (%s, %s, %s)",
            (case.system_id, case.run_id, "1" * 32),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        _allocate(worker, case)
    observer_job = uuid4()
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE jobs SET state = 'failed', worker_id = NULL, lease_expires_at = NULL, "
            "heartbeat_at = NULL, error_category = 'conflict' WHERE id = %s",
            (case.job_id,),
        )
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'teardown', %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), "
            "%s, %s)",
            (
                observer_job,
                Jsonb({"system_id": str(case.system_id)}),
                case.worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                f"observer-{observer_job}",
            ),
        )
    return _Stranded(case, observer_job)


def _record_authority(
    role_dsns: _RoleDsns,
    stranded: _Stranded,
    *,
    domain: str = _DOMAIN,
    objects: tuple[str, ...] = (),
) -> str:
    case = stranded.case
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as conn:
        row = conn.execute(
            _AUTHORITY_RECORD,
            (case.activation_id, case.authority_instance, domain, _COMPOSITE, list(objects)),
        ).fetchone()
    assert row is not None
    return row[0]


def _record_worker(
    role_dsns: _RoleDsns,
    stranded: _Stranded,
    *,
    credential: bytes | None = None,
    job_id: UUID | None = None,
    domain: str = _DOMAIN,
) -> str:
    case = stranded.case
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as conn:
        row = conn.execute(
            _WORKER_RECORD,
            (
                case.credential if credential is None else credential,
                stranded.observer_job if job_id is None else job_id,
                1,
                case.activation_id,
                domain,
            ),
        ).fetchone()
    assert row is not None
    return row[0]


def _settle(role_dsns: _RoleDsns, activation_id: UUID, *, reason: str = "stranded by #3138") -> str:
    with psycopg.connect(role_dsns("kdive_server"), autocommit=True) as conn:
        row = conn.execute(
            _SETTLE, (activation_id, "admin", "session", "operator-cli", reason)
        ).fetchone()
    assert row is not None
    return row[0]


def _activation_row(migrated_url: str, activation_id: UUID) -> dict[str, Any]:
    with psycopg.connect(migrated_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        row = cur.execute(
            "SELECT * FROM external_boot_activations WHERE id = %s", (activation_id,)
        ).fetchone()
    assert row is not None
    return row


def _audit_rows(migrated_url: str, activation_id: UUID) -> int:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM platform_audit_log WHERE scope = %s",
            (f"settled:{activation_id}",),
        ).fetchone()
    assert row is not None
    return row[0]


def test_applied_settles_and_audits(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    assert _record_authority(authority_role_dsns, stranded, objects=("b/two", "a/one")) == "applied"
    assert _record_worker(authority_role_dsns, stranded) == "applied"

    assert _settle(authority_role_dsns, case.activation_id) == "applied"

    row = _activation_row(migrated_url, case.activation_id)
    activation = validate_stored(ExternalBootActivation, row)
    assert activation.state.value == "abandoned"
    assert activation.cleanup_complete
    assert activation.terminal_evidence is not None
    assert [item.ref for item in activation.terminal_evidence.objects] == ["a/one", "b/two"]
    assert activation.cleanup_evidence is not None
    with psycopg.connect(migrated_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        release = cur.execute(
            "SELECT * FROM external_boot_reservation_releases WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone()
        assert release is not None
        assert (
            validate_stored(ExternalBootReservationRelease, release).release_identity
            == activation.cleanup_evidence.release_identity
        )
        assert cur.execute(
            "SELECT count(*) AS n FROM external_boot_reservations WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone() == {"n": 0}
        assert cur.execute(
            "SELECT count(*) AS n FROM external_boot_authorities "
            "WHERE activation_id = %s AND state IN ('allocating', 'current')",
            (case.activation_id,),
        ).fetchone() == {"n": 0}
        assert cur.execute(
            "SELECT count(*) AS n FROM external_boot_authorities "
            "WHERE activation_id = %s AND state = 'superseded'",
            (case.activation_id,),
        ).fetchone() == {"n": 1}
        assert cur.execute(
            "SELECT mutation_discharge_reason AS reason "
            "FROM remote_module_attempt_obligations WHERE system_id = %s",
            (case.system_id,),
        ).fetchone() == {"reason": "terminal_escape"}
        settlement = cur.execute(
            "SELECT principal, reason, settled_at IS NOT NULL AS settled "
            "FROM external_boot_activation_settlements WHERE activation_id = %s",
            (case.activation_id,),
        ).fetchone()
    assert settlement == {"principal": "admin", "reason": "stranded by #3138", "settled": True}
    assert _audit_rows(migrated_url, case.activation_id) == 1

    assert _settle(authority_role_dsns, case.activation_id) == "replayed"
    assert _audit_rows(migrated_url, case.activation_id) == 1


def test_settled_activation_no_longer_restricts_through_the_service(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    _record_authority(authority_role_dsns, stranded)
    _record_worker(authority_role_dsns, stranded)
    ctx = RequestContext(
        principal="admin",
        agent_session=None,
        projects=(),
        platform_roles=frozenset({PlatformRole.PLATFORM_ADMIN}),
    )

    async def settle() -> tuple[SettlementStatus, ExternalBootActivation | None]:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_server"), autocommit=True
        ) as conn:
            status = await settle_stranded_activation(
                conn, ctx, activation_id=case.activation_id, actor="agent", reason="stranded"
            )
            repository = ExternalBootActivationRepository()
            return status, await repository.get_restricting_for_system(conn, case.system_id)

    assert asyncio.run(settle()) == (SettlementStatus.APPLIED, None)


def test_authority_owned_system_cannot_settle(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    assert _record_authority(authority_role_dsns, stranded) == "applied"
    assert _record_worker(authority_role_dsns, stranded) == "applied"
    digest = "sha256:" + "f" * 64
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO authority_system_ownership (system_id, allocation_id, resource_id, "
            "provider_kind, resource_name, authority_instance, profile_identity, root_identity) "
            "SELECT %s, id, resource_id, 'local-libvirt', 'resource', %s, %s, %s "
            "FROM allocations WHERE id = %s",
            (case.system_id, case.authority_instance, digest, digest, case.allocation_id),
        )
    before = _activation_row(migrated_url, case.activation_id)

    assert _settle(authority_role_dsns, case.activation_id) == "authority_owned"

    assert _activation_row(migrated_url, case.activation_id) == before
    assert _audit_rows(migrated_url, case.activation_id) == 0


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record_authority, "worker_evidence_missing"),
        (_record_worker, "authority_evidence_missing"),
    ],
)
def test_one_owner_is_not_evidence_for_the_other(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    record: Callable[[_RoleDsns, _Stranded], str],
    expected: str,
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    assert record(authority_role_dsns, stranded) == "applied"
    assert _settle(authority_role_dsns, stranded.case.activation_id) == expected


_SET_ABANDONED = (
    "UPDATE external_boot_activations SET state = 'abandoned', terminal_evidence = "
    "jsonb_build_object('schema', 'external-boot-terminal-evidence-v1', "
    "'activation_id', id::text, 'system_id', system_id::text, 'outcome', 'abandoned') "
    "WHERE id = {activation}"
)
_SET_SYSTEM_ENDED = "UPDATE systems SET state = 'torn_down' WHERE id = {system}"
_SET_JOB_LIVE = "UPDATE jobs SET state = 'queued' WHERE id = {job}"
_SET_STALE = (
    "UPDATE external_boot_activation_settlements "
    "SET worker_recorded_at = now() - interval '2 hours' WHERE activation_id = {activation}"
)
_SET_DOMAIN_CHANGED = "UPDATE systems SET domain_name = 'renamed' WHERE id = {system}"
_SET_NO_RESERVATION = "DELETE FROM external_boot_reservations WHERE activation_id = {activation}"
_SET_UNPROVEN = (
    "UPDATE external_boot_activations SET materialization = jsonb_build_object("
    "'schema', 'external-boot-materialization-v1', "
    "'ownership', jsonb_build_object('system_id', system_id::text, 'run_id', run_id::text), "
    "'plan_identity', plan_identity, "
    "'artifacts', jsonb_build_object('kernel', jsonb_build_object('ref', 'held/kernel'))), "
    "pre_recovery_evidence = jsonb_build_object("
    "'schema', 'external-boot-pre-recovery-evidence-v1', 'activation_id', id::text, "
    "'system_id', system_id::text, 'run_id', run_id::text, 'plan_identity', plan_identity, "
    "'recovery_object', jsonb_build_object('ref', 'held/recovery')) "
    "WHERE id = {activation}"
)
_SET_JOURNAL_MOVED = (
    "INSERT INTO external_boot_authority_journal_heads (authority_instance, system_id, sequence, "
    "digest, phase, authority_id, generation, operation_identity, head_record) "
    "SELECT authority_instance, system_id, 1, 'sha256:' || repeat('a', 64), 'admitted', id, "
    "generation, operation_identity, '{{}}'::jsonb FROM external_boot_authorities "
    "WHERE activation_id = {activation}"
)


@pytest.mark.parametrize(
    ("statement", "expected"),
    [
        (_SET_ABANDONED, "not_preparing"),
        (_SET_SYSTEM_ENDED, "system_ended"),
        (_SET_JOB_LIVE, "job_live"),
        (_SET_STALE, "evidence_stale"),
        (_SET_DOMAIN_CHANGED, "evidence_stale"),
        (_SET_JOURNAL_MOVED, "authority_journal_moved"),
        (_SET_NO_RESERVATION, "reservation_missing"),
        (_SET_UNPROVEN, "objects_unproven"),
    ],
)
def test_refusals_change_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns, statement: str, expected: str
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    assert _record_authority(authority_role_dsns, stranded) == "applied"
    assert _record_worker(authority_role_dsns, stranded) == "applied"
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            cast(
                LiteralString,
                statement.format(
                    activation=f"'{case.activation_id}'",
                    system=f"'{case.system_id}'",
                    job=f"'{case.job_id}'",
                ),
            )
        )
    before = _activation_row(migrated_url, case.activation_id)

    assert _settle(authority_role_dsns, case.activation_id) == expected

    assert _activation_row(migrated_url, case.activation_id) == before
    assert _audit_rows(migrated_url, case.activation_id) == 0
    with psycopg.connect(migrated_url) as conn:
        settled = conn.execute(
            "SELECT count(*) FROM external_boot_activation_settlements "
            "WHERE activation_id = %s AND settled_at IS NOT NULL",
            (case.activation_id,),
        ).fetchone()
    assert settled == (0,)


def test_one_uncovered_object_of_several_refuses(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            cast(LiteralString, _SET_UNPROVEN.format(activation=f"'{case.activation_id}'"))
        )
    _record_authority(authority_role_dsns, stranded, objects=("held/kernel",))
    _record_worker(authority_role_dsns, stranded)

    assert _settle(authority_role_dsns, case.activation_id) == "objects_unproven"


def test_absent_objects_cover_every_object_the_activation_names(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            cast(LiteralString, _SET_UNPROVEN.format(activation=f"'{case.activation_id}'"))
        )
    assert (
        _record_authority(authority_role_dsns, stranded, objects=("held/kernel", "held/recovery"))
        == "applied"
    )
    assert _record_worker(authority_role_dsns, stranded) == "applied"

    assert _settle(authority_role_dsns, case.activation_id) == "applied"


def test_unknown_activation_is_not_found(authority_role_dsns: _RoleDsns) -> None:
    assert _settle(authority_role_dsns, uuid4()) == "not_found"


@pytest.mark.parametrize(
    ("role", "statement", "digest_args"),
    [
        ("kdive_server", _AUTHORITY_RECORD, True),
        ("kdive_worker", _AUTHORITY_RECORD, True),
        ("kdive_reconciler", _AUTHORITY_RECORD, True),
        ("kdive_server", _WORKER_RECORD, False),
        ("kdive_provider_authority", _WORKER_RECORD, False),
        ("kdive_reconciler", _WORKER_RECORD, False),
        ("kdive_worker", _SETTLE, None),
        ("kdive_provider_authority", _SETTLE, None),
        ("kdive_reconciler", _SETTLE, None),
    ],
)
def test_role_separation(
    authority_role_dsns: _RoleDsns, role: str, statement: str, digest_args: bool | None
) -> None:
    args: tuple[object, ...]
    if digest_args is None:
        args = (uuid4(), "p", None, "agent", "reason")
    elif digest_args:
        args = (uuid4(), "authority", _DOMAIN, _COMPOSITE, [])
    else:
        args = (b"x" * 32, uuid4(), 1, uuid4(), _DOMAIN)
    with (
        psycopg.connect(authority_role_dsns(role), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(cast(LiteralString, statement), args)


@pytest.mark.parametrize("forged", ["credential", "lease", "marked", "other_system", "domain"])
def test_worker_record_requires_its_own_live_unmarked_job(
    migrated_url: str, authority_role_dsns: _RoleDsns, forged: str
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    updates = {
        "lease": "UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
        "marked": (
            "UPDATE jobs SET payload = payload || "
            "'{\"external_boot_authority_v1\": {}}'::jsonb WHERE id = %s"
        ),
        "other_system": (
            "UPDATE jobs SET payload = jsonb_build_object('system_id', gen_random_uuid()) "
            "WHERE id = %s"
        ),
    }
    if forged in updates:
        with psycopg.connect(migrated_url) as conn:
            conn.execute(cast(LiteralString, updates[forged]), (stranded.observer_job,))

    status = _record_worker(
        authority_role_dsns,
        stranded,
        credential=b"z" * 32 if forged == "credential" else None,
        domain="other-domain" if forged == "domain" else _DOMAIN,
    )

    assert status == "superseded"


def test_authority_record_requires_the_systems_domain_and_route(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    case = stranded.case
    assert _record_authority(authority_role_dsns, stranded, domain="other") == "superseded"
    with psycopg.connect(authority_role_dsns("kdive_provider_authority"), autocommit=True) as conn:
        row = conn.execute(
            _AUTHORITY_RECORD, (case.activation_id, "another-authority", _DOMAIN, _COMPOSITE, [])
        ).fetchone()
    assert row == ("superseded",)


@pytest.mark.parametrize(
    "ref", ["vol:x", "owner@host", "back\\slash", "/x", "a//b", "a/../b", "a/.", 'a"b', "é", ""]
)
def test_authority_record_refuses_a_ref_outside_the_canonical_grammar(
    migrated_url: str, authority_role_dsns: _RoleDsns, ref: str
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _record_authority(authority_role_dsns, stranded, objects=(ref,))


def test_authority_record_refuses_an_oversize_object_list(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    objects = tuple(f"object/{index}" for index in range(33))
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _record_authority(authority_role_dsns, stranded, objects=objects)


def test_the_largest_object_list_settles(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    objects = tuple(f"{index:02d}/" + "o" * 1021 for index in range(32))
    assert _record_authority(authority_role_dsns, stranded, objects=objects) == "applied"
    assert _record_worker(authority_role_dsns, stranded) == "applied"

    assert _settle(authority_role_dsns, stranded.case.activation_id) == "applied"


@pytest.mark.parametrize("reason", ["", "   ", "\n\t", "x" * 1025])
def test_invalid_reason_is_rejected(
    migrated_url: str, authority_role_dsns: _RoleDsns, reason: str
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    with pytest.raises(psycopg.errors.InvalidParameterValue):
        _settle(authority_role_dsns, stranded.case.activation_id, reason=reason)


@pytest.mark.parametrize("role", ["kdive_server", "kdive_worker", "kdive_reconciler"])
def test_settled_is_readable_by_each_role(
    migrated_url: str, authority_role_dsns: _RoleDsns, role: str
) -> None:
    stranded = _strand(migrated_url, authority_role_dsns)
    activation_id = stranded.case.activation_id

    async def is_settled() -> bool:
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns(role), autocommit=True
        ) as conn:
            return await ExternalBootActivationRepository().is_settled(conn, activation_id)

    assert asyncio.run(is_settled()) is False
    _record_authority(authority_role_dsns, stranded)
    assert asyncio.run(is_settled()) is False
    _record_worker(authority_role_dsns, stranded)
    assert _settle(authority_role_dsns, activation_id) == "applied"
    assert asyncio.run(is_settled()) is True

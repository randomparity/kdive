"""Migration 0179 pre-stop System-holder recheck tests (ADR-0583, #3353)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.db.external_boot_authority_support import _AuthorityCase, _RoleDsns, _seed_case
from tests.support.external_boot_plan import external_boot_plan

_ACK_DIGEST = "sha256:" + "b" * 64
_SQL = "SELECT external_boot_prestop_system_holders(%s,%s,%s,1,%s)"


@dataclass(frozen=True, slots=True)
class _Prepare:
    case: _AuthorityCase
    authority_id: UUID
    generation: int


def _current_prepare_authority(migrated_url: str, role_dsns: _RoleDsns) -> _Prepare:
    """Seed one acknowledged, current activate authority whose job is the activation's own."""
    with psycopg.connect(migrated_url) as admin:
        case = _seed_case(admin, worker_suffix="h", provider_kind="remote-libvirt")
        plan = external_boot_plan(case.system_id, case.run_id)
        marker = admin.execute(
            "SELECT payload->'external_boot_authority_v1' FROM jobs WHERE id = %s", (case.job_id,)
        ).fetchone()
        assert marker is not None
        admin.execute(
            "UPDATE external_boot_activations SET state = 'preparing', plan_identity = %s, "
            "materialization = NULL, recovery_point = NULL WHERE id = %s",
            (plan.identity, case.activation_id),
        )
        admin.execute(
            "UPDATE jobs SET payload = %s WHERE id = %s",
            (
                Jsonb(
                    {
                        # A real boot job names its System and Run, so it must exclude itself.
                        "system_id": str(case.system_id),
                        "run_id": str(case.run_id),
                        "external_boot_authority_v1": marker[0] | {"plan_identity": plan.identity},
                        "external_boot_plan_v1": plan.model_dump(mode="json", by_alias=True),
                    }
                ),
                case.job_id,
            ),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = worker.execute(
            "SELECT status, authority_id, generation, operation_digest "
            "FROM allocate_external_boot_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                case.activation_id,
                case.run_id,
                case.system_id,
                plan.identity,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.operation_identity,
            ),
        ).fetchone()
    assert allocated is not None and allocated[0] == "allocated"
    _status, authority_id, generation, root_digest = allocated
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "INSERT INTO external_boot_authority_acknowledgements "
            "(authority_id, system_id, generation, authority_instance, operation_identity, "
            "operation_digest, journal_sequence, journal_digest, positive_quiescence_digest) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
            (
                authority_id,
                case.system_id,
                generation,
                case.authority_instance,
                case.operation_identity,
                root_digest,
                _ACK_DIGEST,
                "sha256:" + "c" * 64,
            ),
        )
        admin.execute(
            "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
            "WHERE id = %s",
            (authority_id,),
        )
    return _Prepare(case, authority_id, generation)


def _holders(role_dsns: _RoleDsns, prepare: _Prepare, **overrides: object) -> str | None:
    arguments = {
        "worker_id": prepare.case.worker_id,
        "generation": prepare.generation,
        "digest": _ACK_DIGEST,
    } | overrides
    with psycopg.connect(role_dsns("kdive_provider_authority"), autocommit=True) as provider:
        row = provider.execute(
            _SQL,
            (
                arguments["worker_id"],
                prepare.authority_id,
                arguments["generation"],
                arguments["digest"],
            ),
        ).fetchone()
    assert row is not None
    return row[0]


def _insert_session(url: str, run_id: UUID, state: str) -> None:
    with psycopg.connect(url) as admin:
        admin.execute(
            "INSERT INTO debug_sessions (run_id, state, transport, principal, project) "
            "VALUES (%s, %s, 'gdbstub', 'p', 'proj')",
            (run_id, state),
        )


def _insert_job(url: str, system_id: UUID | None, run_id: UUID | None, state: str) -> None:
    payload = {}
    if system_id is not None:
        payload["system_id"] = str(system_id)
    if run_id is not None:
        payload["run_id"] = str(run_id)
    running = state == "running"
    with psycopg.connect(url) as admin:
        admin.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'power', %s, %s, 1, 3, NULL, NULL, NULL, %s, %s)"
            if not running
            else "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, 'power', %s, %s, 1, 3, 'other-worker', now() + interval '5 minutes', now(), "
            "%s, %s)",
            (
                uuid4(),
                Jsonb(payload),
                state,
                Jsonb({"principal": "p", "project": "proj"}),
                f"holder-{uuid4()}",
            ),
        )


def test_a_system_with_only_the_activations_own_job_is_clear(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    assert _holders(authority_role_dsns, prepare) == "clear"


@pytest.mark.parametrize("state", ["attach", "live"])
def test_an_attaching_or_live_session_on_the_system_holds_it(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: str
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    _insert_session(migrated_url, prepare.case.run_id, state)
    assert _holders(authority_role_dsns, prepare) == "session_active"


def test_a_detached_session_does_not_hold_the_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    _insert_session(migrated_url, prepare.case.run_id, "detached")
    assert _holders(authority_role_dsns, prepare) == "clear"


@pytest.mark.parametrize("state", ["queued", "running"])
@pytest.mark.parametrize("by", ["system_id", "run_id"])
def test_another_active_job_for_the_system_holds_it(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: str, by: str
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    _insert_job(
        migrated_url,
        prepare.case.system_id if by == "system_id" else None,
        prepare.case.run_id if by == "run_id" else None,
        state,
    )
    assert _holders(authority_role_dsns, prepare) == "job_active"


def test_a_settled_job_or_another_systems_job_does_not_hold_the_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    _insert_job(migrated_url, prepare.case.system_id, prepare.case.run_id, "succeeded")
    _insert_job(migrated_url, uuid4(), uuid4(), "queued")
    assert _holders(authority_role_dsns, prepare) == "clear"


def test_a_session_outranks_a_job(migrated_url: str, authority_role_dsns: _RoleDsns) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    _insert_job(migrated_url, prepare.case.system_id, None, "queued")
    _insert_session(migrated_url, prepare.case.run_id, "live")
    assert _holders(authority_role_dsns, prepare) == "session_active"


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 99},
        {"digest": "sha256:" + "e" * 64},
        {"worker_id": "docker:someone-else"},
    ],
    ids=["generation", "acknowledgement", "peer"],
)
def test_an_unbound_authority_answers_nothing_even_with_a_holder_present(
    migrated_url: str, authority_role_dsns: _RoleDsns, overrides: dict[str, object]
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    assert _holders(authority_role_dsns, prepare, **overrides) is None


def test_an_expired_job_lease_answers_nothing(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
            (prepare.case.job_id,),
        )
    assert _holders(authority_role_dsns, prepare) is None


@pytest.mark.parametrize("role", ["kdive_server", "kdive_worker", "kdive_reconciler"])
def test_only_the_provider_authority_role_may_call_it(
    migrated_url: str, authority_role_dsns: _RoleDsns, role: str
) -> None:
    prepare = _current_prepare_authority(migrated_url, authority_role_dsns)
    with (
        psycopg.connect(authority_role_dsns(role), autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(_SQL, (prepare.case.worker_id, prepare.authority_id, 1, _ACK_DIGEST))

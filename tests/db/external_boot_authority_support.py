"""Shared fixtures for external-boot authority migration tests (ADR-0584)."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from psycopg.types.json import Jsonb

from kdive.db import migrate

_LOGIN_PASSWORD = "external-boot-authority-test"  # pragma: allowlist secret
_PLAN = "sha256:" + "a" * 64
_JOURNAL = "sha256:" + "b" * 64
_QUIESCENCE = "sha256:" + "c" * 64
_EVIDENCE_DIGEST = "sha256:" + "d" * 64
_OBSERVED_AT = "2026-08-29T00:00:00Z"
_ALLOCATE_SIGNATURE = (
    "allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)"
)
_ACKNOWLEDGE_SIGNATURE = (
    "acknowledge_external_boot_authority(uuid,bigint,uuid,uuid,uuid,uuid,text,uuid,integer,"
    "text,text,text,text,text,text,text,bigint,text,text)"
)
_COMMIT_SIGNATURE = (
    "commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,"
    "text,text,text,text,text,text,bigint,text,text,jsonb)"
)


@dataclass(frozen=True, slots=True)
class _RoleDsns:
    parameters: dict[str, str]
    logins: dict[str, str]

    def __call__(self, role: str) -> str:
        parameters = {
            **self.parameters,
            "user": self.logins[role],
            "password": _LOGIN_PASSWORD,
        }
        return make_conninfo(**parameters)


@dataclass(frozen=True, slots=True)
class _AuthorityCase:
    allocation_id: UUID
    system_id: UUID
    run_id: UUID
    activation_id: UUID
    job_id: UUID
    attempt: int
    worker_id: str
    credential: bytes
    purpose: str
    provider_kind: str
    authority_instance: str
    operation: str
    operation_identity: str


@dataclass(frozen=True, slots=True)
class _Allocated:
    authority_id: UUID
    generation: int
    operation_digest: str


def _apply_through(conn: psycopg.Connection, version: str) -> None:
    for migration in migrate.discover_migrations():
        if migration.version <= version:
            conn.execute(migration.sql.encode())


def _apply_version(conn: psycopg.Connection, version: str) -> None:
    migration = next(item for item in migrate.discover_migrations() if item.version == version)
    conn.execute(migration.sql.encode())


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Iterator[_RoleDsns]:
    """Create unique LOGIN principals for the migration's non-login roles."""
    with psycopg.connect(migrated_url, autocommit=True) as conn:
        suffix = uuid4().hex[:16]
        logins = {
            role: f"kdive_eba_{role.removeprefix('kdive_')}_{suffix}"
            for role in (
                "kdive_server",
                "kdive_worker",
                "kdive_reconciler",
                "kdive_provider_authority",
            )
        }
        for role, login in logins.items():
            conn.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {} IN ROLE {}").format(
                    Identifier(login), Literal(_LOGIN_PASSWORD), Identifier(role)
                )
            )
        try:
            yield _RoleDsns(dict(conn.info.get_parameters()), logins)
        finally:
            for login in logins.values():
                conn.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))


def _activation_evidence(system_id: UUID, run_id: UUID, activation_id: UUID) -> tuple[Jsonb, Jsonb]:
    ownership = {"system_id": str(system_id), "run_id": str(run_id)}
    return (
        Jsonb(
            {
                "schema": "external-boot-materialization-v1",
                "ownership": ownership,
                "plan_identity": _PLAN,
            }
        ),
        Jsonb(
            {
                "schema": "external-boot-recovery-v1",
                "binding": {
                    **ownership,
                    "activation_id": str(activation_id),
                },
                "plan_identity": _PLAN,
            }
        ),
    )


def _seed_case(
    conn: psycopg.Connection,
    *,
    purpose: str = "activate",
    operation: str | None = None,
    worker_protocol: int = 4,
    worker_suffix: str = "a",
    legacy_recovery_point: bool = False,
) -> _AuthorityCase:
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    investigation_id, run_id, activation_id, job_id = uuid4(), uuid4(), uuid4(), uuid4()
    worker_id = f"docker:external-authority-{worker_suffix}-{uuid4()}"
    credential = worker_suffix.encode() * 32
    provider_kind = "local-libvirt"
    authority_instance = f"authority-{worker_suffix}"
    operation = operation or purpose
    operation_identity = f"operation-{worker_suffix}-{uuid4()}"
    job_kind = "teardown" if purpose == "teardown" else "boot"

    conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id, "failed" if purpose == "teardown" else "ready"),
    )
    conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'active')",
        (investigation_id,),
    )
    conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES (%s, %s, %s, 'local-libvirt', %s, '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id, "failed" if purpose == "teardown" else "succeeded"),
    )
    materialization, recovery_point = _activation_evidence(system_id, run_id, activation_id)
    if legacy_recovery_point:
        recovery_point = Jsonb(
            {
                "schema": "external-boot-recovery-v1",
                "ownership": {"system_id": str(system_id), "run_id": str(run_id)},
                "plan_identity": _PLAN,
            }
        )
    if purpose == "teardown":
        attempt_id = uuid4()
        conn.execute(
            "INSERT INTO external_boot_activations "
            "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
            "state, materialization, pre_recovery_evidence, current_attempt_id) VALUES "
            "(%s, %s, %s, %s, %s, 1, 'recovery_failed', %s, %s, %s)",
            (
                activation_id,
                system_id,
                run_id,
                _PLAN,
                uuid4(),
                materialization,
                Jsonb(
                    {
                        "schema": "external-boot-pre-recovery-evidence-v1",
                        "activation_id": str(activation_id),
                        "system_id": str(system_id),
                        "run_id": str(run_id),
                        "plan_identity": _PLAN,
                    }
                ),
                attempt_id,
            ),
        )
        conn.execute(
            "INSERT INTO external_boot_recovery_attempts "
            "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
            "state, terminal_evidence) VALUES (%s, 1, %s, 1, 'pre_recovery', 'failed', %s)",
            (
                activation_id,
                attempt_id,
                Jsonb(
                    {
                        "schema": "external-boot-terminal-evidence-v1",
                        "activation_id": str(activation_id),
                        "outcome": "recovery_failed",
                    }
                ),
            ),
        )
    else:
        conn.execute(
            "INSERT INTO external_boot_activations "
            "(id, system_id, run_id, plan_identity, operation_owner_id, authority_generation, "
            "state, materialization, recovery_point) "
            "VALUES (%s, %s, %s, %s, %s, 1, 'prepared', %s, %s)",
            (activation_id, system_id, run_id, _PLAN, uuid4(), materialization, recovery_point),
        )
    if worker_protocol < 4:
        conn.execute(
            "ALTER TABLE worker_incarnations DISABLE TRIGGER "
            "worker_incarnations_capture_protocol_floor"
        )
    try:
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation, authority_kind, authority_binding, credential_hash, fence_protocol) "
            "VALUES (%s, 'docker', '{}'::jsonb, %s, %s)",
            (worker_id, credential, worker_protocol),
        )
    finally:
        if worker_protocol < 4:
            conn.execute(
                "ALTER TABLE worker_incarnations ENABLE TRIGGER "
                "worker_incarnations_capture_protocol_floor"
            )
    marker = {
        "activation_id": str(activation_id),
        "run_id": str(run_id),
        "system_id": str(system_id),
        "plan_identity": _PLAN,
        "purpose": purpose,
        "provider_kind": provider_kind,
        "authority_instance": authority_instance,
        "operation": operation,
        "operation_identity": operation_identity,
    }
    if worker_protocol < 4:
        conn.execute("ALTER TABLE jobs DISABLE TRIGGER jobs_current_worker_fence_protocol")
    try:
        conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "lease_expires_at, heartbeat_at, authorizing, dedup_key) VALUES "
            "(%s, %s, %s, 'running', 1, 3, %s, now() + interval '5 minutes', now(), %s, %s)",
            (
                job_id,
                job_kind,
                Jsonb({"external_boot_authority_v1": marker}),
                worker_id,
                Jsonb({"principal": "p", "project": "proj"}),
                f"external-authority-{job_id}",
            ),
        )
    finally:
        if worker_protocol < 4:
            conn.execute("ALTER TABLE jobs ENABLE TRIGGER jobs_current_worker_fence_protocol")
    return _AuthorityCase(
        allocation_id=allocation_id,
        system_id=system_id,
        run_id=run_id,
        activation_id=activation_id,
        job_id=job_id,
        attempt=1,
        worker_id=worker_id,
        credential=credential,
        purpose=purpose,
        provider_kind=provider_kind,
        authority_instance=authority_instance,
        operation=operation,
        operation_identity=operation_identity,
    )


def _allocate(worker: psycopg.Connection, case: _AuthorityCase) -> _Allocated:
    row = worker.execute(
        "SELECT status, authority_id, generation, operation_digest "
        "FROM allocate_external_boot_authority(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
    assert row is not None and row[0] == "allocated"
    return _Allocated(authority_id=row[1], generation=row[2], operation_digest=row[3])

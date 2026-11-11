"""The stranded-activation fixture shared by the settlement and observation tests (ADR-0816)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from tests.db.external_boot_authority_support import (
    _allocate,
    _Allocated,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)

_DOMAIN = "kdive-stranded"
_SET_ALL_NAMED_OBJECTS = (
    "UPDATE external_boot_activations SET materialization = jsonb_build_object("
    "'schema', 'external-boot-materialization-v1', "
    "'ownership', jsonb_build_object('system_id', system_id::text, 'run_id', run_id::text), "
    "'plan_identity', plan_identity, 'artifacts', jsonb_build_object("
    "'kernel', jsonb_build_object('ref', 'held/kernel'), "
    "'modules', jsonb_build_object('ref', 'held/modules'), "
    "'initrd', jsonb_build_object('ref', 'held/initrd'), "
    "'debuginfo', jsonb_build_object('ref', 'held/debuginfo'))), "
    "recovery_point = jsonb_build_object('schema', 'external-boot-recovery-v1', "
    "'binding', jsonb_build_object('system_id', system_id::text, 'run_id', run_id::text, "
    "'activation_id', id::text), 'plan_identity', plan_identity, "
    "'recovery_ref', jsonb_build_object('ref', 'held/recovery-point')), "
    "pre_recovery_evidence = jsonb_build_object("
    "'schema', 'external-boot-pre-recovery-evidence-v1', 'activation_id', id::text, "
    "'system_id', system_id::text, 'run_id', run_id::text, 'plan_identity', plan_identity, "
    "'recovery_object', jsonb_build_object('ref', 'held/pre-recovery')) "
    "WHERE id = {activation}"
)
_ALL_NAMED_OBJECTS = (
    "held/kernel",
    "held/modules",
    "held/initrd",
    "held/debuginfo",
    "held/recovery-point",
    "held/pre-recovery",
)
_SET_ABANDONED = (
    "UPDATE external_boot_activations SET state = 'abandoned', terminal_evidence = "
    "jsonb_build_object('schema', 'external-boot-terminal-evidence-v1', "
    "'activation_id', id::text, 'system_id', system_id::text, 'outcome', 'abandoned') "
    "WHERE id = {activation}"
)


@dataclass(frozen=True, slots=True)
class _Stranded:
    case: _AuthorityCase
    observer_job: UUID
    authority: _Allocated | None


def _strand(
    migrated_url: str, role_dsns: _RoleDsns, *, suffix: str = "s", allocate: bool = True
) -> _Stranded:
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
    authority = None
    if allocate:
        with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
            authority = _allocate(worker, case)
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
    return _Stranded(case, observer_job, authority)


def _mark_authority_owned(migrated_url: str, case: _AuthorityCase) -> None:
    digest = "sha256:" + "f" * 64
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO authority_system_ownership (system_id, allocation_id, resource_id, "
            "provider_kind, resource_name, authority_instance, profile_identity, root_identity) "
            "SELECT %s, id, resource_id, 'local-libvirt', 'resource', %s, %s, %s "
            "FROM allocations WHERE id = %s",
            (case.system_id, case.authority_instance, digest, digest, case.allocation_id),
        )

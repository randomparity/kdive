"""Seed and read helpers for the remote module cleanup-evidence read tests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import psycopg
from psycopg.types.json import Jsonb

from tests.db.external_boot_authority_support import (
    _JOURNAL,
    _PLAN,
    _QUIESCENCE,
    _allocate,
    _Allocated,
    _AuthorityCase,
    _RoleDsns,
    _seed_case,
)

_DIGEST = "sha256:" + "d" * 64
_OTHER_DIGEST = "sha256:" + "e" * 64
_NONCE = "0" * 32


def _identity(document: Mapping[str, object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return (
        "sha256:"
        + hashlib.sha256(str(document["protocol"]).encode() + b"\0" + canonical).hexdigest()
    )


def _stored(document: Mapping[str, object]) -> dict[str, object]:
    """The form the worker stores: the identity's canonical document plus explicit nulls."""
    return {**document, "capture_absent": None}


def _recovery(case: _AuthorityCase) -> dict[str, object]:
    return {
        "protocol": "remote-module-recovery-ref-v2",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": _NONCE,
        "operation_identity": _DIGEST,
        "result_identity": _DIGEST,
        "source_capacity_bytes": 4096,
    }


def _insert_obligation(admin: psycopg.Connection, case: _AuthorityCase) -> dict[str, object]:
    recovery = _recovery(case)
    result = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "installed",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": _NONCE,
    }
    terminal_operation = {
        "protocol": "remote-module-operation-v1",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": _NONCE,
    }
    restored_operation = {**terminal_operation, "operation": "restore"}
    restored_result = {**result, "phase": "restored"}
    admin.execute(
        "INSERT INTO remote_module_attempt_obligations "
        "(system_id,run_id,operation_nonce,terminal_operation,terminal_operation_identity,"
        "terminal_result,terminal_result_identity,baseline_operation_identity,"
        "baseline_result_identity,installed_entry_count,installed_content_bytes,"
        "recovery_reference,reap_opened_at,restored_operation,"
        "restored_operation_identity,restored_result,restored_result_identity) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,now(),%s,%s,%s,%s)",
        (
            case.system_id,
            case.run_id,
            _NONCE,
            Jsonb(_stored(terminal_operation)),
            _identity(terminal_operation),
            Jsonb(_stored(result)),
            _identity(result),
            _DIGEST,
            _DIGEST,
            Jsonb(recovery),
            Jsonb(_stored(restored_operation)),
            _identity(restored_operation),
            Jsonb(_stored(restored_result)),
            _identity(restored_result),
        ),
    )
    return recovery


class _Seeded:
    def __init__(
        self, case: _AuthorityCase, allocated: _Allocated, recovery: dict[str, object]
    ) -> None:
        self.case = case
        self.allocated = allocated
        self.recovery = recovery


def _seed(
    migrated_url: str, dsns: _RoleDsns, *, purpose: str = "release", operation: str = "release"
) -> _Seeded:
    """An acknowledged, current authority row with restored evidence and an open reap."""
    with psycopg.connect(migrated_url) as admin:
        case = _seed_case(
            admin,
            purpose=purpose,
            operation=operation,
            worker_suffix="z",
            provider_kind="remote-libvirt",
        )
        if purpose in {"recover", "release"}:
            admin.execute(
                "UPDATE external_boot_activations SET state='active', terminal_evidence=%s, "
                "activation_readiness_deadline=now() WHERE id=%s",
                (
                    Jsonb(
                        {
                            "schema": "external-boot-terminal-evidence-v1",
                            "activation_id": str(case.activation_id),
                            "system_id": str(case.system_id),
                            "outcome": "active",
                        }
                    ),
                    case.activation_id,
                ),
            )
    with psycopg.connect(dsns("kdive_worker"), autocommit=True) as worker:
        allocated = _allocate(worker, case)
    with psycopg.connect(dsns("kdive_provider_authority")) as provider:
        provider.execute(
            "SELECT * FROM acknowledge_external_boot_authority("
            "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                allocated.authority_id,
                allocated.generation,
                case.allocation_id,
                case.activation_id,
                case.run_id,
                case.system_id,
                _PLAN,
                case.job_id,
                case.attempt,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.worker_id,
                case.operation,
                case.operation_identity,
                allocated.operation_digest,
                1,
                _JOURNAL,
                _QUIESCENCE,
            ),
        ).fetchone()
        provider.commit()
    with psycopg.connect(migrated_url) as admin:
        recovery = _insert_obligation(admin, case)
    return _Seeded(case, allocated, recovery)


def _derived(migrated_url: str, seeded: _Seeded, operation: str = "cleanup") -> tuple[str, str]:
    """The phase identity and digest 0143 derives from the root authority row."""
    with psycopg.connect(migrated_url) as admin:
        row = admin.execute(
            "SELECT d.operation_identity, d.operation_digest FROM external_boot_authorities a "
            "CROSS JOIN LATERAL derive_external_boot_release_phase_binding(jsonb_build_object("
            "'authority_id', a.id, 'generation', a.generation, 'system_id', a.system_id, "
            "'activation_id', a.activation_id, 'run_id', a.run_id, "
            "'plan_identity', a.plan_identity, 'provider_kind', a.provider_kind, "
            "'authority_instance', a.authority_instance, "
            "'worker_incarnation', a.worker_incarnation, "
            "'root_operation_identity', a.operation_identity, "
            "'root_operation_digest', a.operation_digest), %s) d WHERE a.id = %s",
            (operation, seeded.allocated.authority_id),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


_FIELDS = (
    "peer",
    "authority_id",
    "generation",
    "sequence",
    "ack_digest",
    "system_id",
    "activation_id",
    "run_id",
    "plan",
    "purpose",
    "operation",
    "provider_kind",
    "authority_instance",
    "operation_identity",
    "operation_digest",
    "nonce",
)


def _read(
    provider: psycopg.Connection,
    seeded: _Seeded,
    identity: str,
    digest: str,
    **override: object,
) -> tuple | None:
    case, allocated = seeded.case, seeded.allocated
    values: dict[str, object] = {
        "peer": case.worker_id,
        "authority_id": allocated.authority_id,
        "generation": allocated.generation,
        "sequence": 1,
        "ack_digest": _JOURNAL,
        "system_id": case.system_id,
        "activation_id": case.activation_id,
        "run_id": case.run_id,
        "plan": _PLAN,
        "purpose": "release",
        "operation": "cleanup",
        "provider_kind": "remote-libvirt",
        "authority_instance": case.authority_instance,
        "operation_identity": identity,
        "operation_digest": digest,
        "nonce": _NONCE,
    } | override
    return provider.execute(
        "SELECT cleanup_state, recovery_reference "
        "FROM read_authorized_remote_module_cleanup_evidence("
        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        tuple(values[field] for field in _FIELDS),
    ).fetchone()


def _refused(
    provider: psycopg.Connection, seeded: _Seeded, identity: str, digest: str, **override: object
) -> bool:
    """True when the read returns nothing or rejects the facts outright."""
    try:
        return _read(provider, seeded, identity, digest, **override) is None
    except psycopg.errors.InvalidParameterValue:
        provider.rollback()
        return True

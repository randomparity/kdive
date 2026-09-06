"""Authority cleanup reads only exact worker-owned restored/reap evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

import psycopg
import pytest
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
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)

_DIGEST = "sha256:" + "d" * 64
_NONCE = "0" * 32


def _read(
    provider: psycopg.Connection,
    case: _AuthorityCase,
    allocated: _Allocated,
    *,
    authority_instance: str | None = None,
) -> tuple | None:
    return provider.execute(
        "SELECT cleanup_state, recovery_reference "
        "FROM read_authorized_remote_module_cleanup_evidence("
        "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            case.worker_id,
            allocated.authority_id,
            allocated.generation,
            1,
            _JOURNAL,
            case.system_id,
            case.activation_id,
            case.run_id,
            _PLAN,
            case.purpose,
            case.operation,
            "remote-libvirt",
            authority_instance or case.authority_instance,
            case.operation_identity,
            allocated.operation_digest,
            _NONCE,
        ),
    ).fetchone()


def _identity(document: Mapping[str, object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return (
        "sha256:"
        + hashlib.sha256(str(document["protocol"]).encode() + b"\0" + canonical).hexdigest()
    )


@pytest.mark.parametrize("operation", ["recover", "teardown"])
def test_cleanup_evidence_follows_restored_open_then_discharged_order(
    migrated_url: str, authority_role_dsns: _RoleDsns, operation: str
) -> None:
    with psycopg.connect(migrated_url) as admin:
        case = _seed_case(
            admin, purpose=operation, worker_suffix="z", provider_kind="remote-libvirt"
        )
        if operation == "recover":
            admin.execute(
                "UPDATE external_boot_activations SET state='active', "
                "terminal_evidence=%s, activation_readiness_deadline=now() WHERE id=%s",
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
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = _allocate(worker, case)
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
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
    recovery = {
        "protocol": "remote-module-recovery-ref-v2",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": _NONCE,
        "operation_identity": _DIGEST,
        "result_identity": _DIGEST,
        "source_capacity_bytes": 4096,
    }
    result = {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "restored",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": _NONCE,
    }
    terminal_operation = {
        "protocol": "remote-module-operation-v1",
        "system_id": str(case.system_id),
        "run_id": str(case.run_id),
        "operation_nonce": _NONCE,
    }
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "INSERT INTO remote_module_attempt_obligations "
            "(system_id,run_id,operation_nonce,terminal_operation,terminal_operation_identity,"
            "terminal_result,terminal_result_identity,baseline_operation_identity,"
            "baseline_result_identity,installed_entry_count,installed_content_bytes,"
            "recovery_reference,reap_opened_at) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,now())",
            (
                case.system_id,
                case.run_id,
                _NONCE,
                Jsonb(terminal_operation),
                _identity(terminal_operation),
                Jsonb(result),
                _identity(result),
                _DIGEST,
                _DIGEST,
                Jsonb(recovery),
            ),
        )
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, case, allocated) == ("open", recovery)
        assert _read(provider, case, allocated, authority_instance="foreign") is None
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (case.job_id,),
        )
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, case, allocated) is None
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=now()+interval '5 minutes' WHERE id=%s",
            (case.job_id,),
        )
        admin.execute(
            "UPDATE remote_module_attempt_obligations SET reap_discharged_at=now() "
            "WHERE system_id=%s AND run_id=%s AND operation_nonce=%s",
            (case.system_id, case.run_id, _NONCE),
        )
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, case, allocated) == ("discharged", recovery)

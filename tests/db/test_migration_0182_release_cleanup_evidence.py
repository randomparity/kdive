"""The cleanup-evidence read admits a derived release CLEANUP against the root release row."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from uuid import uuid4

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

_DIGEST = "sha256:" + "d" * 64
_OTHER_DIGEST = "sha256:" + "e" * 64
_NONCE = "0" * 32


def _identity(document: Mapping[str, object]) -> str:
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return (
        "sha256:"
        + hashlib.sha256(str(document["protocol"]).encode() + b"\0" + canonical).hexdigest()
    )


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
            Jsonb(terminal_operation),
            _identity(terminal_operation),
            Jsonb(result),
            _identity(result),
            _DIGEST,
            _DIGEST,
            Jsonb(recovery),
            Jsonb(restored_operation),
            _identity(restored_operation),
            Jsonb(restored_result),
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


def test_a_derived_release_cleanup_matches_the_root_release_row(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, seeded, identity, digest) == ("open", seeded.recovery)
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE remote_module_attempt_obligations SET reap_discharged_at = now() "
            "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
            (seeded.case.system_id, seeded.case.run_id, _NONCE),
        )
    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(provider, seeded, identity, digest) == ("discharged", seeded.recovery)


@pytest.mark.parametrize(
    "override",
    [
        {"peer": "docker:another-worker"},
        {"authority_id": uuid4()},
        {"generation": 99},
        {"sequence": 2},
        {"ack_digest": "sha256:" + "a" * 64},
        {"system_id": uuid4()},
        {"activation_id": uuid4()},
        {"run_id": uuid4()},
        {"plan": "sha256:" + "b" * 64},
        {"provider_kind": "local-libvirt"},
        {"authority_instance": "foreign"},
        {"nonce": "1" * 32},
    ],
    ids=lambda value: next(iter(value)),
)
def test_a_derived_release_cleanup_still_needs_every_binding_field(
    migrated_url: str, authority_role_dsns: _RoleDsns, override: dict[str, object]
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest, **override)


def test_the_phase_identity_and_digest_must_be_the_derived_cleanup_pair(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)
    recover_identity, recover_digest = _derived(migrated_url, seeded, "recover")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, recover_identity, recover_digest)
        assert _refused(provider, seeded, identity, recover_digest)
        assert _refused(provider, seeded, recover_identity, digest)
        assert _refused(provider, seeded, identity, _OTHER_DIGEST)
        assert _refused(provider, seeded, "sha256:" + "1" * 64, digest)
        assert _refused(provider, seeded, seeded.case.operation_identity, digest)
        assert _refused(
            provider, seeded, seeded.case.operation_identity, seeded.allocated.operation_digest
        )


def test_a_derived_release_cleanup_needs_a_live_current_release_authority(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded)
    case = seeded.case

    def refused() -> bool:
        with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
            return _refused(provider, seeded, identity, digest)

    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
            (case.job_id,),
        )
    assert refused()
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at = now() + interval '5 minutes' WHERE id = %s",
            (case.job_id,),
        )
        admin.execute(
            "UPDATE external_boot_authorities SET state = 'retired', retired_at = now() "
            "WHERE id = %s",
            (seeded.allocated.authority_id,),
        )
    assert refused()


def test_a_release_root_that_is_not_the_release_operation_is_not_a_phase_root(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, operation="fail")
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest)


@pytest.mark.parametrize("purpose", ["recover", "teardown"])
def test_the_other_admitted_pairs_never_accept_a_derived_request(
    migrated_url: str, authority_role_dsns: _RoleDsns, purpose: str
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, purpose=purpose, operation=purpose)
    identity, digest = _derived(migrated_url, seeded)

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest)
        assert _refused(provider, seeded, identity, digest, purpose=purpose, operation=purpose)


def test_a_release_recover_is_still_not_an_admitted_pair(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns)
    identity, digest = _derived(migrated_url, seeded, "recover")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _refused(provider, seeded, identity, digest, operation="recover")


def test_a_standalone_cleanup_root_still_matches_exactly(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    seeded = _seed(migrated_url, authority_role_dsns, purpose="release", operation="cleanup")

    with psycopg.connect(authority_role_dsns("kdive_provider_authority")) as provider:
        assert _read(
            provider,
            seeded,
            seeded.case.operation_identity,
            seeded.allocated.operation_digest,
        ) == ("open", seeded.recovery)
        assert _refused(provider, seeded, seeded.case.operation_identity, _OTHER_DIGEST)

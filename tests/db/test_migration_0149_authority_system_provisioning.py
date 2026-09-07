"""Real-Postgres contracts for authority-owned System provisioning (ADR-0623)."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import TypeAdapter

from kdive.db import migrate
from kdive.domain.external_boot_activation import ExternalBootTeardownEvidenceV1
from kdive.providers.external_boot_authority.protocol import (
    AuthorityTeardownProofV1,
    canonical_teardown_proof_bytes,
    teardown_proof_digest,
)
from kdive.providers.system_authority.protocol import (
    GENESIS_DIGEST,
    AuthoritySystemJournalPhase,
    AuthoritySystemMutationRequestV1,
    AuthoritySystemObservationV1,
    AuthoritySystemOperation,
    AuthoritySystemProvisionReadyV1,
    AuthoritySystemTakeoverRequestV1,
    canonical_system_authority_bytes,
    make_authority_system_record,
    system_authority_digest,
)
from kdive.security.audit import args_digest
from tests.db.external_boot_authority_support import (
    _allocate,
    _RoleDsns,
    _seed_case,
    authority_role_dsns,  # noqa: F401
)


def _wait_for_advisory_wait(
    observer: psycopg.Connection, future: Future[object], backend_pid: int
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if (
            observer.execute(
                "SELECT 1 FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted",
                (backend_pid,),
            ).fetchone()
            is not None
        ):
            return
        if future.done():
            future.result()
            raise AssertionError("authority contender completed before waiting on the System lock")
        time.sleep(0.01)
    raise AssertionError("authority contender did not wait on the System lock")


def test_migration_0149_is_registered_last() -> None:
    migrations = migrate.discover_migrations()
    assert (migrations[-1].version, migrations[-1].filename) == (
        "0149",
        "0149_authority_owned_system_provisioning.sql",
    )


def test_0149_installs_two_private_tables_and_exact_function_grants(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    with psycopg.connect(migrated_url) as admin:
        tables = {
            row[0]
            for row in admin.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name LIKE 'authority_system_%'"
            )
        }
        assert tables == {"authority_system_ownership", "authority_system_attempts"}

        functions = {
            row[0]
            for row in admin.execute(
                "SELECT p.proname FROM pg_proc AS p "
                "JOIN pg_namespace AS n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND p.proname LIKE '%authority_system%'"
            )
        }
        assert functions >= {
            "register_authority_system_ownership",
            "request_authority_system_preactivation_teardown",
            "resolve_authority_system_server_binding",
            "claim_authority_system_first_activation",
            "allocate_authority_system_attempt",
            "acknowledge_authority_system_attempt",
            "finalize_authority_system_attempt",
            "resolve_allocating_authority_system_attempt",
            "resolve_current_authority_system_attempt",
            "read_authority_system_journal_head",
            "advance_authority_system_journal_head",
            "list_authority_system_journal_heads",
            "repair_terminal_authority_system_attempts",
            "list_authority_system_teardown_repairs",
            "finalize_authority_system_teardown_repair",
        }

        allowed = {
            "register_authority_system_ownership(uuid,uuid,text,text,text,text,text)": {
                "kdive_server"
            },
            "request_authority_system_preactivation_teardown(uuid,uuid,text)": {"kdive_server"},
            "resolve_authority_system_server_binding(uuid)": {"kdive_server"},
            "claim_authority_system_first_activation(uuid,uuid)": {"kdive_server"},
            "allocate_authority_system_attempt(bytea,uuid,integer,uuid)": {"kdive_worker"},
            (
                "acknowledge_authority_system_attempt(bytea,uuid,integer,uuid,bigint,uuid,"
                "bigint,text,text)"
            ): {"kdive_worker"},
            "finalize_authority_system_attempt(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)": {
                "kdive_worker"
            },
            "resolve_allocating_authority_system_attempt(text,uuid,bigint)": {
                "kdive_provider_authority"
            },
            "resolve_current_authority_system_attempt(text,uuid,bigint,bigint,text)": {
                "kdive_provider_authority"
            },
            "read_authority_system_journal_head(text,uuid,bigint,text)": {
                "kdive_provider_authority"
            },
            "advance_authority_system_journal_head(text,uuid,bigint,bigint,text,jsonb,bytea)": {
                "kdive_provider_authority"
            },
            "list_authority_system_journal_heads(text)": {"kdive_provider_authority"},
            "repair_terminal_authority_system_attempts(integer)": {"kdive_reconciler"},
            "list_authority_system_teardown_repairs(integer)": {"kdive_reconciler"},
            ("finalize_authority_system_teardown_repair(uuid,bigint,uuid,bigint,text,text)"): {
                "kdive_reconciler"
            },
        }
        for signature, roles in allowed.items():
            for role, login in role_dsns.logins.items():
                assert admin.execute(
                    "SELECT has_function_privilege(%s,%s,'EXECUTE')", (login, signature)
                ).fetchone() == (role in roles,)

    for role in role_dsns.logins:
        with psycopg.connect(role_dsns(role)) as restricted:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                restricted.execute("SELECT * FROM authority_system_ownership").fetchall()
            restricted.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                restricted.execute("SELECT * FROM authority_system_attempts").fetchall()


def test_0149_ownership_head_is_global_and_binding_is_immutable(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        resource_id, allocation_id, system_id, image_id = (uuid4() for _ in range(4))
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
            "VALUES (%s,'local-libvirt','host-a','default','standard','available','qemu:///system')",
            (resource_id,),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (allocation_id, resource_id),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning','{}','p','proj')",
            (system_id, allocation_id),
        )
        conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id,source_image_id,project,architecture,image_digest,root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,'{}')",
            (system_id, image_id, "sha256:" + "a" * 64),
        )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity) "
            "VALUES (%s,%s,%s,'local-libvirt','host-a','auth-a',%s,%s)",
            (system_id, allocation_id, resource_id, "sha256:" + "b" * 64, "sha256:" + "a" * 64),
        )
        row = conn.execute(
            "SELECT journal_sequence,journal_digest,journal_phase,journal_record "
            "FROM authority_system_ownership WHERE system_id=%s",
            (system_id,),
        ).fetchone()
        assert row == (0, "sha256:" + "0" * 64, None, None)
        with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
            conn.execute(
                "UPDATE authority_system_ownership SET resource_name='host-b' WHERE system_id=%s",
                (system_id,),
            )


def test_0149_attempt_ack_and_receipt_tuples_are_closed(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        constraints = {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid IN "
                "('authority_system_ownership'::regclass,'authority_system_attempts'::regclass)"
            )
        }
        terminal_job_index = conn.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' "
            "AND indexname='authority_system_attempt_terminal_job_repair'"
        ).fetchone()
    assert "authority_system_ownership_journal_shape" in constraints
    assert "authority_system_attempts_ack_shape" in constraints
    assert "authority_system_attempts_receipt_shape" in constraints
    assert "authority_system_attempts_terminal_shape" in constraints
    assert terminal_job_index is not None
    assert "(job_id) WHERE ((state = 'terminal'" in terminal_job_index[0]


def test_0149_repair_definition_follows_owned_terminal_attempt(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        row = conn.execute(
            "SELECT pg_get_functiondef("
            "'repair_terminal_authority_system_attempts(integer)'::regprocedure)"
        ).fetchone()
    assert row is not None
    definition = row[0]
    assert "ownership.current_attempt_id = attempt.id" in definition
    assert "attempt.state = 'current'" not in definition
    assert "attempt.state = 'terminal'" in definition
    assert "attempt.receipt_disposition='provision-ready'" in definition


def test_0149_reconciler_consumes_terminal_row_selected_by_ownership(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    resource_id, allocation_id, system_id, image_id, job_id = (uuid4() for _ in range(5))
    authority_id, request_attempt_id = uuid4(), uuid4()
    worker = f"docker:authority-system-{uuid4()}"
    digest = "sha256:" + "a" * 64
    receipt = b'{"disposition":"provision-ready"}'
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
            "VALUES (%s,'local-libvirt','host-r','default','standard','available','qemu:///system')",
            (resource_id,),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (allocation_id, resource_id),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning','{}','p','proj')",
            (system_id, allocation_id),
        )
        conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id,source_image_id,project,architecture,image_digest,root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,'{}')",
            (system_id, image_id, digest),
        )
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash,state) "
            "VALUES (%s,'docker','{}',4,%s,'active')",
            (worker, b"w" * 32),
        )
        conn.execute(
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
            "payload,authorizing,dedup_key) VALUES "
            "(%s,'provision','running',1,3,%s,clock_timestamp()-interval '1 second','{}',%s,%s)",
            (
                job_id,
                worker,
                Jsonb({"principal": "p", "agent_session": "repair-session", "project": "proj"}),
                f"authority-system-repair-{job_id}",
            ),
        )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity,journal_sequence,journal_digest,journal_phase,"
            "journal_record) VALUES "
            "(%s,%s,%s,'local-libvirt','host-r','auth-r',%s,%s,2,%s,'terminal','{}')",
            (system_id, allocation_id, resource_id, digest, digest, digest),
        )
        conn.execute(
            "INSERT INTO authority_system_attempts "
            "(id,system_id,generation,operation,job_id,job_attempt,worker_incarnation,"
            "request_attempt_id,operation_identity,operation_digest,state,ack_sequence,"
            "ack_digest,quiescence_digest,acknowledged_at,ack_head_sequence,ack_head_digest,"
            "terminal_head_sequence,terminal_head_digest,receipt_bytes,receipt_digest,"
            "receipt_disposition,receipt_at) VALUES "
            "(%s,%s,1,'provision',%s,1,%s,%s,'provision-r',%s,'terminal',1,%s,%s,"
            "clock_timestamp(),0,%s,2,%s,%s,%s,'provision-ready',clock_timestamp())",
            (
                authority_id,
                system_id,
                job_id,
                worker,
                request_attempt_id,
                digest,
                digest,
                digest,
                "sha256:" + "0" * 64,
                digest,
                receipt,
                digest,
            ),
        )
        conn.execute(
            "UPDATE authority_system_ownership SET current_attempt_id=%s WHERE system_id=%s",
            (authority_id, system_id),
        )
        conn.commit()

    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT repair_terminal_authority_system_attempts(1)"
        ).fetchone() == (1,)

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute("SELECT state FROM systems WHERE id=%s", (system_id,)).fetchone() == (
            "ready",
        )
        assert conn.execute(
            "SELECT consumed_at IS NOT NULL FROM authority_system_attempts WHERE id=%s",
            (authority_id,),
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT active_started_at IS NOT NULL FROM allocations WHERE id=%s",
            (allocation_id,),
        ).fetchone() == (True,)
        assert conn.execute(
            "SELECT principal,agent_session,project,tool,object_kind,transition,args_digest "
            "FROM audit_log WHERE object_id=%s",
            (system_id,),
        ).fetchone() == (
            "p",
            "repair-session",
            "proj",
            "systems.provision",
            "systems",
            "provisioning->ready",
            args_digest({"system_id": str(system_id)}),
        )


def test_0149_reconciler_cleans_before_terminalizing_preactivation_absence(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    resource_id, allocation_id, system_id, job_id = (uuid4() for _ in range(4))
    investigation_id, run_id, authority_id, request_attempt_id = (uuid4() for _ in range(4))
    worker = f"docker:authority-system-teardown-{uuid4()}"
    head_digest = "sha256:" + "a" * 64
    receipt_digest = "sha256:" + "b" * 64
    operation_digest = "sha256:" + "c" * 64
    receipt = b'{"disposition":"preactivation-absent"}'
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
            "VALUES (%s,'local-libvirt','host-t','default','standard','available','qemu:///system')",
            (resource_id,),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (allocation_id, resource_id),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning','{}','p','proj')",
            (system_id, allocation_id),
        )
        conn.execute(
            "INSERT INTO investigations (id,principal,project,title,state) "
            "VALUES (%s,'p','proj','t','active')",
            (investigation_id,),
        )
        conn.execute(
            "INSERT INTO runs "
            "(id,investigation_id,system_id,target_kind,state,build_profile,principal,project) "
            "VALUES (%s,%s,%s,'local-libvirt','created','{}','p','proj')",
            (run_id, investigation_id, system_id),
        )
        conn.execute(
            "INSERT INTO snapshots "
            "(system_id,name,include_memory,state,principal,project) "
            "VALUES (%s,'pre-teardown',false,'available','p','proj')",
            (system_id,),
        )
        conn.execute(
            "INSERT INTO system_bootstrap_keys (system_id,private_key,public_key) "
            "VALUES (%s,'private','public')",
            (system_id,),
        )
        conn.execute(
            "INSERT INTO remote_module_attempt_obligations (system_id,run_id,operation_nonce) "
            "VALUES (%s,%s,%s)",
            (system_id, run_id, "1" * 32),
        )
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash,state) "
            "VALUES (%s,'docker','{}',4,%s,'active')",
            (worker, b"t" * 32),
        )
        conn.execute(
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
            "payload,authorizing,dedup_key) VALUES "
            "(%s,'teardown','running',1,3,%s,clock_timestamp()-interval '1 second',%s,%s,%s)",
            (
                job_id,
                worker,
                Jsonb({"authority_system_v1": {"system_id": str(system_id)}}),
                Jsonb({"principal": "p", "project": "proj"}),
                f"authority-system-teardown-repair-{job_id}",
            ),
        )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity,state,journal_sequence,journal_digest,journal_phase,"
            "journal_record) VALUES "
            "(%s,%s,%s,'local-libvirt','host-t','auth-t',%s,%s,'teardown-requested',2,%s,"
            "'terminal','{}')",
            (
                system_id,
                allocation_id,
                resource_id,
                operation_digest,
                operation_digest,
                head_digest,
            ),
        )
        conn.execute(
            "INSERT INTO authority_system_attempts "
            "(id,system_id,generation,operation,job_id,job_attempt,worker_incarnation,"
            "request_attempt_id,operation_identity,operation_digest,state,ack_sequence,ack_digest,"
            "quiescence_digest,acknowledged_at,ack_head_sequence,ack_head_digest,"
            "terminal_head_sequence,terminal_head_digest,receipt_bytes,receipt_digest,"
            "receipt_disposition,receipt_at) VALUES "
            "(%s,%s,1,'preactivation-teardown',%s,1,%s,%s,'teardown-r',%s,'terminal',1,%s,%s,"
            "clock_timestamp(),0,%s,2,%s,%s,%s,'preactivation-absent',clock_timestamp())",
            (
                authority_id,
                system_id,
                job_id,
                worker,
                request_attempt_id,
                operation_digest,
                operation_digest,
                operation_digest,
                "sha256:" + "0" * 64,
                head_digest,
                receipt,
                receipt_digest,
            ),
        )
        conn.execute(
            "UPDATE authority_system_ownership SET current_attempt_id=%s WHERE system_id=%s",
            (authority_id, system_id),
        )

    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT repair_terminal_authority_system_attempts(100)"
        ).fetchone() == (0,)
        with pytest.raises(psycopg.errors.InvalidParameterValue, match="repair limit"):
            reconciler.execute("SELECT * FROM list_authority_system_teardown_repairs(101)")
        reconciler.rollback()
        assert reconciler.execute(
            "SELECT * FROM list_authority_system_teardown_repairs(100)"
        ).fetchall() == [(authority_id, 1, system_id, 2, head_digest, receipt_digest)]
        finalize_args = (authority_id, 1, system_id, 2, head_digest, receipt_digest)
        assert reconciler.execute(
            "SELECT finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            (*finalize_args[:-1], "sha256:" + "f" * 64),
        ).fetchone() == ("superseded",)
        assert reconciler.execute(
            "SELECT finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            finalize_args,
        ).fetchone() == ("cleanup-required",)
        reconciler.rollback()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        assert (
            worker_conn.execute(
                "SELECT id FROM claim_worker_job(%s,%s,interval '1 minute',ARRAY['default'])",
                (worker, b"t" * 32),
            ).fetchone()
            is None
        )

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state,consumed_at FROM authority_system_attempts WHERE id=%s",
            (authority_id,),
        ).fetchone() == ("terminal", None)
        assert conn.execute(
            "SELECT state FROM authority_system_ownership WHERE system_id=%s", (system_id,)
        ).fetchone() == ("teardown-requested",)
        conn.execute("DELETE FROM system_bootstrap_keys WHERE system_id=%s", (system_id,))
        conn.execute("DELETE FROM snapshots WHERE system_id=%s", (system_id,))

    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            finalize_args,
        ).fetchone() == ("cleanup-required",)
        reconciler.rollback()

    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE remote_module_attempt_obligations SET mutation_discharged_at=now(),"
            "mutation_discharge_reason='terminal_escape' WHERE system_id=%s",
            (system_id,),
        )

    with psycopg.connect(role_dsns("kdive_reconciler")) as reconciler:
        assert reconciler.execute(
            "SELECT finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            finalize_args,
        ).fetchone() == ("applied",)
        reconciler.commit()
        assert reconciler.execute(
            "SELECT finalize_authority_system_teardown_repair(%s,%s,%s,%s,%s,%s)",
            finalize_args,
        ).fetchone() == ("applied",)
        assert (
            reconciler.execute(
                "SELECT * FROM list_authority_system_teardown_repairs(100)"
            ).fetchall()
            == []
        )

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT ownership.state,attempt.consumed_at IS NOT NULL,system.state,job.state "
            "FROM authority_system_ownership AS ownership "
            "JOIN authority_system_attempts AS attempt ON attempt.id=ownership.current_attempt_id "
            "JOIN systems AS system ON system.id=ownership.system_id "
            "JOIN jobs AS job ON job.id=attempt.job_id WHERE ownership.system_id=%s",
            (system_id,),
        ).fetchone() == ("torn-down", True, "torn_down", "succeeded")


def test_0149_worker_authority_journal_and_exact_receipt_replay(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    resource_id, allocation_id, system_id, image_id, job_id = (uuid4() for _ in range(5))
    request_attempt_id = uuid4()
    worker = f"docker:authority-flow-{uuid4()}"
    other_worker = f"docker:authority-flow-{uuid4()}"
    credential = b"f" * 32
    other_credential = b"g" * 32
    profile_digest = "sha256:" + "a" * 64
    root_digest = "sha256:" + "b" * 64
    bootstrap_key = "ssh-ed25519 YWFhYQ== kdive-system"
    bootstrap_digest = "sha256:" + hashlib.sha256(bootstrap_key.encode()).hexdigest()
    marker = {
        "schema": "authority-system-marker-v1",
        "system_id": str(system_id),
        "allocation_id": str(allocation_id),
        "resource_id": str(resource_id),
        "provider_kind": "local-libvirt",
        "resource_name": "host-flow",
        "authority_instance": "auth-flow",
        "profile_identity": profile_digest,
        "root_identity": root_digest,
        "operation": "provision",
        "operation_identity": "provision-flow",
    }
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
            "VALUES (%s,'local-libvirt','host-flow','default','standard','available',"
            "'qemu:///system')",
            (resource_id,),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (allocation_id, resource_id),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning','{}','p','proj')",
            (system_id, allocation_id),
        )
        conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id,source_image_id,project,architecture,image_digest,root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,'{}')",
            (system_id, image_id, root_digest),
        )
        conn.execute(
            "INSERT INTO system_bootstrap_keys (system_id,private_key,public_key) "
            "VALUES (%s,'private',%s)",
            (system_id, bootstrap_key),
        )
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash) "
            "VALUES (%s,'docker','{}',4,%s)",
            (worker, credential),
        )
        conn.execute(
            "INSERT INTO worker_incarnations "
            "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash) "
            "VALUES (%s,'docker','{}',4,%s)",
            (other_worker, other_credential),
        )
        conn.execute(
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
            "payload,authorizing,dedup_key) VALUES "
            "(%s,'provision','running',1,3,%s,clock_timestamp()+interval '5 minutes',%s,%s,%s)",
            (
                job_id,
                worker,
                Jsonb({"authority_system_v1": marker}),
                Jsonb({"principal": "p", "agent_session": "flow-session", "project": "proj"}),
                f"flow-{job_id}",
            ),
        )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity) VALUES "
            "(%s,%s,%s,'local-libvirt','host-flow','auth-flow',%s,%s)",
            (system_id, allocation_id, resource_id, profile_digest, root_digest),
        )
        conn.commit()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        allocated = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (credential, job_id, request_attempt_id),
        ).fetchone()
        assert allocated is not None and allocated[0] == "allocated"
        authority_id, generation, operation_digest = allocated[1:]
        worker_conn.commit()

    takeover = AuthoritySystemTakeoverRequestV1(
        system_id=system_id,
        allocation_id=allocation_id,
        resource_id=resource_id,
        provider_kind="local-libvirt",
        resource_name="host-flow",
        authority_instance="auth-flow",
        profile_identity=profile_digest,
        root_identity=root_digest,
        operation=AuthoritySystemOperation.PROVISION,
        operation_identity="provision-flow",
        authority_id=authority_id,
        generation=generation,
        attempt_id=request_attempt_id,
        operation_digest=operation_digest,
        bootstrap_identity=bootstrap_digest,
    )
    with psycopg.connect(role_dsns("kdive_provider_authority")) as authority:
        allocating = authority.execute(
            "SELECT authority_id,bootstrap_identity FROM "
            "resolve_allocating_authority_system_attempt(%s,%s,%s)",
            (worker, authority_id, generation),
        ).fetchone()
        assert allocating == (authority_id, bootstrap_digest)
        watermark = make_authority_system_record(
            takeover,
            sequence=1,
            previous_digest=GENESIS_DIGEST,
            phase=AuthoritySystemJournalPhase.WATERMARK_INSTALLED,
        )
        first = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,0,%s,%s,NULL)",
            (
                worker,
                authority_id,
                generation,
                GENESIS_DIGEST,
                Jsonb(watermark.model_dump(mode="json", by_alias=True)),
            ),
        ).fetchone()
        assert first is not None and first[0] == "advanced"
        takeover_ack = make_authority_system_record(
            takeover,
            sequence=2,
            previous_digest=first[2],
            phase=AuthoritySystemJournalPhase.TAKEOVER_ACKNOWLEDGED,
        )
        second = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,1,%s,%s,NULL)",
            (
                worker,
                authority_id,
                generation,
                first[2],
                Jsonb(takeover_ack.model_dump(mode="json", by_alias=True)),
            ),
        ).fetchone()
        assert second is not None and second[0] == "advanced"
        authority.commit()

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT attempt.state,ownership.current_attempt_id FROM authority_system_attempts "
            "AS attempt JOIN authority_system_ownership AS ownership USING (system_id) "
            "WHERE attempt.id=%s",
            (authority_id,),
        ).fetchone() == ("allocating", None)

    quiescence = "sha256:" + "d" * 64
    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        ack = worker_conn.execute(
            "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,1,%s,%s,%s,2,%s,%s)",
            (
                credential,
                job_id,
                authority_id,
                generation,
                request_attempt_id,
                second[2],
                quiescence,
            ),
        ).fetchone()
        assert ack is not None and ack[0] == "acknowledged"
        worker_conn.commit()
        replay_ack = worker_conn.execute(
            "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,1,%s,%s,%s,2,%s,%s)",
            (
                credential,
                job_id,
                authority_id,
                generation,
                request_attempt_id,
                second[2],
                quiescence,
            ),
        ).fetchone()
        assert replay_ack == ("replay", ack[1])
        for wrong_credential, wrong_job, wrong_attempt in (
            (other_credential, job_id, 1),
            (credential, uuid4(), 1),
            (credential, job_id, 2),
        ):
            denied = worker_conn.execute(
                "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,%s,%s,%s,%s,2,%s,%s)",
                (
                    wrong_credential,
                    wrong_job,
                    wrong_attempt,
                    authority_id,
                    generation,
                    request_attempt_id,
                    second[2],
                    quiescence,
                ),
            ).fetchone()
            assert denied == ("superseded", None)

    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT attempt.state,ownership.current_attempt_id FROM authority_system_attempts "
            "AS attempt JOIN authority_system_ownership AS ownership USING (system_id) "
            "WHERE attempt.id=%s",
            (authority_id,),
        ).fetchone() == ("current", authority_id)

    mutation = AuthoritySystemMutationRequestV1.model_validate(
        takeover.model_dump(mode="python", by_alias=True)
    )
    with psycopg.connect(role_dsns("kdive_provider_authority")) as authority:
        current = authority.execute(
            "SELECT authority_id,bootstrap_identity FROM "
            "resolve_current_authority_system_attempt(%s,%s,%s,2,%s)",
            (worker, authority_id, generation, second[2]),
        ).fetchone()
        assert current == (authority_id, bootstrap_digest)
        sequence, digest = 2, second[2]
        for phase in (
            AuthoritySystemJournalPhase.ADMITTED,
            AuthoritySystemJournalPhase.MUTATION_STARTED,
            AuthoritySystemJournalPhase.PROVIDER_RETURNED,
        ):
            record = make_authority_system_record(
                mutation, sequence=sequence + 1, previous_digest=digest, phase=phase
            )
            advanced = authority.execute(
                "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,%s,%s,%s,NULL)",
                (
                    worker,
                    authority_id,
                    generation,
                    sequence,
                    digest,
                    Jsonb(record.model_dump(mode="json", by_alias=True)),
                ),
            ).fetchone()
            assert advanced is not None and advanced[0] == "advanced"
            sequence, digest = advanced[1], advanced[2]
        facts_digest = "sha256:" + "e" * 64
        observed = make_authority_system_record(
            mutation,
            sequence=sequence + 1,
            previous_digest=digest,
            phase=AuthoritySystemJournalPhase.OBSERVED,
            observation=AuthoritySystemObservationV1(
                category="owned", composite_state=facts_digest
            ),
        )
        advanced = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,%s,%s,%s,NULL)",
            (
                worker,
                authority_id,
                generation,
                sequence,
                digest,
                Jsonb(observed.model_dump(mode="json", by_alias=True)),
            ),
        ).fetchone()
        assert advanced is not None and advanced[0] == "advanced"
        proof = AuthoritySystemProvisionReadyV1.model_validate(
            {
                **mutation.model_dump(mode="python", by_alias=True, exclude={"schema_"}),
                "disposition": "provision-ready",
                "intent_identity": "sha256:" + "f" * 64,
                "domain_owned": True,
                "root_storage_owned": True,
                "boot_ready": True,
                "bootstrap_ready": True,
                "quarantine_retained": False,
                "completed_at": datetime(2026, 9, 6, tzinfo=UTC),
            }
        )
        receipt = canonical_system_authority_bytes(proof)
        sequence, digest = advanced[1], advanced[2]
        terminal = make_authority_system_record(
            mutation,
            sequence=sequence + 1,
            previous_digest=digest,
            phase=AuthoritySystemJournalPhase.TERMINAL,
            observation=AuthoritySystemObservationV1(
                category="owned", composite_state=system_authority_digest(proof)
            ),
            outcome="provision-ready",
        )
        completed = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,%s,%s,%s,%s)",
            (
                worker,
                authority_id,
                generation,
                sequence,
                digest,
                Jsonb(terminal.model_dump(mode="json", by_alias=True)),
                receipt,
            ),
        ).fetchone()
        assert completed is not None and completed[0] == "advanced"
        authority.commit()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        finalized = worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,%s,7,%s,%s)",
            (credential, job_id, authority_id, generation, completed[2], receipt),
        ).fetchone()
        assert finalized == ("applied", "succeeded", "ready")
        replay = worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,%s,7,%s,%s)",
            (credential, job_id, authority_id, generation, completed[2], receipt),
        ).fetchone()
        assert replay == ("applied", "succeeded", "ready")
        for wrong_credential, wrong_job, wrong_attempt in (
            (other_credential, job_id, 1),
            (credential, uuid4(), 1),
            (credential, job_id, 2),
        ):
            denied = worker_conn.execute(
                "SELECT status FROM finalize_authority_system_attempt(%s,%s,%s,%s,%s,7,%s,%s)",
                (
                    wrong_credential,
                    wrong_job,
                    wrong_attempt,
                    authority_id,
                    generation,
                    completed[2],
                    receipt,
                ),
            ).fetchone()
            assert denied == ("superseded",)

    with psycopg.connect(migrated_url) as conn:
        stored = conn.execute(
            "SELECT journal_sequence,receipt_bytes,consumed_at IS NOT NULL "
            "FROM authority_system_ownership JOIN authority_system_attempts "
            "ON current_attempt_id=authority_system_attempts.id "
            "WHERE authority_system_ownership.system_id=%s",
            (system_id,),
        ).fetchone()
        allocation_started = conn.execute(
            "SELECT active_started_at IS NOT NULL FROM allocations WHERE id=%s",
            (allocation_id,),
        ).fetchone()
        audit = conn.execute(
            "SELECT principal,agent_session,project,tool,object_kind,transition,args_digest "
            "FROM audit_log WHERE object_id=%s",
            (system_id,),
        ).fetchone()
    assert stored == (7, receipt, True)
    assert allocation_started == (True,)
    assert audit == (
        "p",
        "flow-session",
        "proj",
        "systems.provision",
        "systems",
        "provisioning->ready",
        args_digest({"system_id": str(system_id)}),
    )
    assert json.loads(receipt)["disposition"] == "provision-ready"


def test_0149_external_boot_teardown_finalizes_matching_system_ownership(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    journal_digest = "sha256:" + "b" * 64
    quiescence_digest = "sha256:" + "c" * 64
    profile_digest = "sha256:" + "d" * 64
    root_digest = "sha256:" + "e" * 64
    with psycopg.connect(migrated_url) as seed:
        case = _seed_case(seed, purpose="teardown")
        resource_row = seed.execute(
            "SELECT resource_id FROM allocations WHERE id=%s", (case.allocation_id,)
        ).fetchone()
        assert resource_row is not None
        resource_id = resource_row[0]
        seed.execute("UPDATE resources SET name='host-owned' WHERE id=%s", (resource_id,))
        seed.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity,state,first_activation_id) VALUES "
            "(%s,%s,%s,%s,'host-owned',%s,%s,%s,'activated',%s)",
            (
                case.system_id,
                case.allocation_id,
                resource_id,
                case.provider_kind,
                case.authority_instance,
                profile_digest,
                root_digest,
                case.activation_id,
            ),
        )
        seed.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id,store_identity,owner_key,reserved_bytes,state,ready_at) "
            "VALUES (%s,'store/private','owner/private',4096,'pending',NULL)",
            (case.activation_id,),
        )

    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)

    teardown = {
        "schema": "external-boot-teardown-evidence-v1",
        "system_id": str(case.system_id),
        "system_state": "torn_down",
        "observed_at": "2026-09-06T00:00:00Z",
    }
    teardown_identity = ExternalBootTeardownEvidenceV1.model_validate(teardown).identity
    proof = TypeAdapter(AuthorityTeardownProofV1).validate_python(
        {
            "disposition": "complete_pending",
            "teardown_evidence": teardown,
            "cleanup_evidence": {
                "schema": "external-boot-cleanup-evidence-v1",
                "activation_id": str(case.activation_id),
                "system_id": str(case.system_id),
                "mode": "pending_system_teardown",
                "teardown_identity": teardown_identity,
                "completed_at": "2026-09-06T00:00:00Z",
            },
        }
    )
    with psycopg.connect(migrated_url) as seed:
        seed.execute(
            "UPDATE external_boot_authorities SET state='current',acknowledged_at=now() "
            "WHERE id=%s",
            (authority.authority_id,),
        )
        seed.execute(
            "INSERT INTO external_boot_authority_acknowledgements "
            "(authority_id,system_id,generation,authority_instance,operation_identity,"
            "operation_digest,journal_sequence,journal_digest,positive_quiescence_digest) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s)",
            (
                authority.authority_id,
                case.system_id,
                authority.generation,
                case.authority_instance,
                case.operation_identity,
                authority.operation_digest,
                journal_digest,
                quiescence_digest,
            ),
        )
        seed.execute(
            "INSERT INTO external_boot_authority_journal_heads "
            "(authority_instance,system_id,sequence,digest,phase,authority_id,generation,"
            "operation_identity,head_record) VALUES (%s,%s,2,%s,'terminal',%s,%s,%s,%s)",
            (
                case.authority_instance,
                case.system_id,
                journal_digest,
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

    with psycopg.connect(role_dsns("kdive_worker")) as worker:
        assert worker.execute(
            "SELECT finalize_external_boot_authority_teardown(%s,%s,%s,%s,%s,2,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                authority.authority_id,
                authority.generation,
                journal_digest,
                canonical_teardown_proof_bytes(proof),
            ),
        ).fetchone() == ("applied",)

    with psycopg.connect(migrated_url) as seed:
        assert seed.execute(
            "SELECT state FROM authority_system_ownership WHERE system_id=%s",
            (case.system_id,),
        ).fetchone() == ("torn-down",)


def test_0149_allocation_supersedes_only_dead_unacknowledged_candidate(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    resource_id, allocation_id, system_id, image_id = (uuid4() for _ in range(4))
    first_job, second_job, first_request, second_request = (uuid4() for _ in range(4))
    first_worker, second_worker = (
        f"docker:authority-candidate-{uuid4()}",
        f"docker:authority-candidate-{uuid4()}",
    )
    first_credential, second_credential = b"1" * 32, b"2" * 32
    profile_digest = "sha256:" + "a" * 64
    root_digest = "sha256:" + "b" * 64

    def marker(operation_identity: str) -> dict[str, str]:
        return {
            "schema": "authority-system-marker-v1",
            "system_id": str(system_id),
            "allocation_id": str(allocation_id),
            "resource_id": str(resource_id),
            "provider_kind": "local-libvirt",
            "resource_name": "host-candidate",
            "authority_instance": "auth-candidate",
            "profile_identity": profile_digest,
            "root_identity": root_digest,
            "operation": "provision",
            "operation_identity": operation_identity,
        }

    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
            "VALUES (%s,'local-libvirt','host-candidate','default','standard','available',"
            "'qemu:///system')",
            (resource_id,),
        )
        conn.execute(
            "INSERT INTO allocations (id,resource_id,state,principal,project) "
            "VALUES (%s,%s,'active','p','proj')",
            (allocation_id, resource_id),
        )
        conn.execute(
            "INSERT INTO systems (id,allocation_id,state,provisioning_profile,principal,project) "
            "VALUES (%s,%s,'provisioning','{}','p','proj')",
            (system_id, allocation_id),
        )
        conn.execute(
            "INSERT INTO system_root_provenance "
            "(system_id,source_image_id,project,architecture,image_digest,root_spec) "
            "VALUES (%s,%s,'proj','x86_64',%s,'{}')",
            (system_id, image_id, root_digest),
        )
        conn.execute(
            "INSERT INTO system_bootstrap_keys (system_id,private_key,public_key) "
            "VALUES (%s,'private','ssh-ed25519 YWFhYQ== kdive-system')",
            (system_id,),
        )
        for worker, credential in (
            (first_worker, first_credential),
            (second_worker, second_credential),
        ):
            conn.execute(
                "INSERT INTO worker_incarnations "
                "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash) "
                "VALUES (%s,'docker','{}',4,%s)",
                (worker, credential),
            )
        for job_id, worker, payload in (
            (first_job, first_worker, marker("candidate-one")),
            (second_job, second_worker, marker("candidate-two")),
        ):
            conn.execute(
                "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
                "payload,authorizing,dedup_key) VALUES "
                "(%s,'provision','running',1,3,%s,clock_timestamp()+interval '5 minutes',"
                "%s,'{}',%s)",
                (job_id, worker, Jsonb({"authority_system_v1": payload}), f"candidate-{job_id}"),
            )
        conn.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity) VALUES "
            "(%s,%s,%s,'local-libvirt','host-candidate','auth-candidate',%s,%s)",
            (system_id, allocation_id, resource_id, profile_digest, root_digest),
        )
        conn.commit()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        first = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (first_credential, first_job, first_request),
        ).fetchone()
        assert first is not None and first[0] == "allocated"
        first_authority = first[1]
        worker_conn.commit()

        holder = psycopg.connect(migrated_url)
        contender = psycopg.connect(role_dsns("kdive_worker"))
        contender.execute("SET statement_timeout='5s'")

        def acknowledge_while_locked() -> object:
            return contender.execute(
                "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,1,%s,%s,%s,1,%s,%s)",
                (
                    first_credential,
                    first_job,
                    first_authority,
                    first[2],
                    first_request,
                    profile_digest,
                    root_digest,
                ),
            ).fetchone()

        try:
            holder.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,2125))",
                (f"kdive:system:{system_id}",),
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(acknowledge_while_locked)
                _wait_for_advisory_wait(holder, future, contender.info.backend_pid)
                assert holder.execute(
                    "SELECT id FROM authority_system_attempts WHERE id=%s FOR UPDATE NOWAIT",
                    (first_authority,),
                ).fetchone() == (first_authority,)
                holder.rollback()
                assert future.result(timeout=5) == ("superseded", None)
        finally:
            holder.rollback()
            contender.rollback()
            contender.close()
            holder.close()

        assert (
            worker_conn.execute(
                "SELECT state FROM complete_worker_job(%s,%s,1,'forbidden')",
                (first_job, first_credential),
            ).fetchone()
            is None
        )
        assert (
            worker_conn.execute(
                "SELECT state FROM fail_worker_job(%s,%s,1,'provider_error',%s,true)",
                (first_job, first_credential, Jsonb({})),
            ).fetchone()
            is None
        )

        assert worker_conn.execute(
            "SELECT status FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (second_credential, second_job, second_request),
        ).fetchone() == ("busy",)
        worker_conn.rollback()

    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
            (first_job,),
        )
        admin.commit()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        rolled_back = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (second_credential, second_job, second_request),
        ).fetchone()
        assert rolled_back is not None and rolled_back[0] == "allocated"
        worker_conn.rollback()

    with psycopg.connect(migrated_url) as admin:
        assert admin.execute(
            "SELECT state FROM authority_system_attempts WHERE id=%s", (first_authority,)
        ).fetchone() == ("allocating",)
        assert admin.execute(
            "SELECT count(*) FROM authority_system_attempts WHERE job_id=%s", (second_job,)
        ).fetchone() == (0,)

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        successor = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (second_credential, second_job, second_request),
        ).fetchone()
        assert successor is not None and successor[0] == "allocated"
        worker_conn.commit()
        replay = worker_conn.execute(
            "SELECT * FROM allocate_authority_system_attempt(%s,%s,1,%s)",
            (second_credential, second_job, second_request),
        ).fetchone()
        assert replay == successor

    with psycopg.connect(migrated_url) as admin:
        states = admin.execute(
            "SELECT id,state FROM authority_system_attempts WHERE system_id=%s ORDER BY generation",
            (system_id,),
        ).fetchall()
    assert states == [(first_authority, "superseded"), (successor[1], "allocating")]

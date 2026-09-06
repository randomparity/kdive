"""Real-Postgres contracts for authority-owned System provisioning (ADR-0623)."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _RoleDsns,
    authority_role_dsns,  # noqa: F401
)


def test_migration_0148_is_registered_last() -> None:
    migrations = migrate.discover_migrations()
    assert (migrations[-1].version, migrations[-1].filename) == (
        "0148",
        "0148_authority_owned_system_provisioning.sql",
    )


def test_0148_installs_two_private_tables_and_exact_function_grants(
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
        }

    for role in role_dsns.logins:
        with psycopg.connect(role_dsns(role)) as restricted:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                restricted.execute("SELECT * FROM authority_system_ownership").fetchall()
            restricted.rollback()
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                restricted.execute("SELECT * FROM authority_system_attempts").fetchall()


def test_0148_ownership_head_is_global_and_binding_is_immutable(migrated_url: str) -> None:
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


def test_0148_attempt_ack_and_receipt_tuples_are_closed(migrated_url: str) -> None:
    with psycopg.connect(migrated_url) as conn:
        constraints = {
            row[0]
            for row in conn.execute(
                "SELECT conname FROM pg_constraint WHERE conrelid IN "
                "('authority_system_ownership'::regclass,'authority_system_attempts'::regclass)"
            )
        }
    assert "authority_system_ownership_journal_shape" in constraints
    assert "authority_system_attempts_ack_shape" in constraints
    assert "authority_system_attempts_receipt_shape" in constraints
    assert "authority_system_attempts_terminal_shape" in constraints


def test_0148_repair_definition_follows_owned_terminal_attempt(migrated_url: str) -> None:
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


def test_0148_reconciler_consumes_terminal_row_selected_by_ownership(
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
            "(%s,'provision','running',1,3,%s,clock_timestamp()-interval '1 second','{}','{}',%s)",
            (job_id, worker, f"authority-system-repair-{job_id}"),
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


def test_0148_worker_authority_journal_and_exact_receipt_replay(
    migrated_url: str, request: pytest.FixtureRequest
) -> None:
    role_dsns: _RoleDsns = request.getfixturevalue("authority_role_dsns")
    resource_id, allocation_id, system_id, image_id, job_id = (uuid4() for _ in range(5))
    request_attempt_id = uuid4()
    worker = f"docker:authority-flow-{uuid4()}"
    credential = b"f" * 32
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
            "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
            "payload,authorizing,dedup_key) VALUES "
            "(%s,'provision','running',1,3,%s,clock_timestamp()+interval '5 minutes',%s,'{}',%s)",
            (job_id, worker, Jsonb({"authority_system_v1": marker}), f"flow-{job_id}"),
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
        ack = worker_conn.execute(
            "SELECT * FROM acknowledge_authority_system_attempt(%s,%s,1,%s,%s,%s,1,%s,%s)",
            (
                credential,
                job_id,
                authority_id,
                generation,
                request_attempt_id,
                "sha256:" + "c" * 64,
                "sha256:" + "d" * 64,
            ),
        ).fetchone()
        assert ack is not None and ack[0] == "acknowledged"
        worker_conn.commit()

    genesis = "sha256:" + "0" * 64
    with psycopg.connect(role_dsns("kdive_provider_authority")) as authority:
        current = authority.execute(
            "SELECT authority_id,bootstrap_identity FROM "
            "resolve_current_authority_system_attempt(%s,%s,%s,1,%s)",
            (worker, authority_id, generation, "sha256:" + "c" * 64),
        ).fetchone()
        assert current == (authority_id, bootstrap_digest)
        started = {
            "authority_id": str(authority_id),
            "generation": generation,
            "system_id": str(system_id),
            "sequence": 1,
            "previous_digest": genesis,
            "operation_digest": operation_digest,
            "phase": "mutation-started",
        }
        advanced = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,0,%s,%s,NULL)",
            (worker, authority_id, generation, genesis, Jsonb(started)),
        ).fetchone()
        assert advanced is not None and advanced[0] == "advanced"
        receipt = b'{"disposition":"provision-ready"}'
        receipt_digest = (
            "sha256:" + hashlib.sha256(b"kdive-authority-system-proof-v1\0" + receipt).hexdigest()
        )
        terminal = {
            **started,
            "sequence": 2,
            "previous_digest": advanced[2],
            "phase": "terminal",
            "observation": {"composite_state": receipt_digest},
        }
        completed = authority.execute(
            "SELECT * FROM advance_authority_system_journal_head(%s,%s,%s,1,%s,%s,%s)",
            (worker, authority_id, generation, advanced[2], Jsonb(terminal), receipt),
        ).fetchone()
        assert completed is not None and completed[0] == "advanced"
        authority.commit()

    with psycopg.connect(role_dsns("kdive_worker")) as worker_conn:
        finalized = worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,%s,2,%s,%s)",
            (credential, job_id, authority_id, generation, completed[2], receipt),
        ).fetchone()
        assert finalized == ("applied", "succeeded", "ready")
        replay = worker_conn.execute(
            "SELECT * FROM finalize_authority_system_attempt(%s,%s,1,%s,%s,2,%s,%s)",
            (credential, job_id, authority_id, generation, completed[2], receipt),
        ).fetchone()
        assert replay == ("applied", "succeeded", "ready")

    with psycopg.connect(migrated_url) as conn:
        stored = conn.execute(
            "SELECT journal_sequence,receipt_bytes,consumed_at IS NOT NULL "
            "FROM authority_system_ownership JOIN authority_system_attempts "
            "ON current_attempt_id=authority_system_attempts.id "
            "WHERE authority_system_ownership.system_id=%s",
            (system_id,),
        ).fetchone()
    assert stored == (2, receipt, True)
    assert json.loads(receipt) == {"disposition": "provision-ready"}

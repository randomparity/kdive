"""Direct-seeded authority System ownership cases for the DB-backed authority tests."""

from __future__ import annotations

import json
from typing import cast
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from kdive.providers.system_authority.protocol import GENESIS_DIGEST


def _seed_direct_authority_case(
    conn: psycopg.Connection,
    *,
    operation: str,
    ownership_state: str,
    attempt: int = 1,
    max_attempts: int = 3,
) -> dict[str, object]:
    resource_id, allocation_id, system_id, image_id, job_id = (uuid4() for _ in range(5))
    worker = f"docker:direct-authority-{uuid4()}"
    credential = uuid4().bytes + uuid4().bytes
    profile_digest = "sha256:" + "a" * 64
    root_digest = "sha256:" + "b" * 64
    marker = {
        "schema": "authority-system-marker-v1",
        "system_id": str(system_id),
        "allocation_id": str(allocation_id),
        "resource_id": str(resource_id),
        "provider_kind": "local-libvirt",
        "resource_name": f"host-{system_id}",
        "authority_instance": f"authority-{system_id}",
        "profile_identity": profile_digest,
        "root_identity": root_digest,
        "operation": operation,
        "operation_identity": f"{operation}-{job_id}",
    }
    conn.execute(
        "INSERT INTO resources (id,kind,name,pool,cost_class,status,host_uri) "
        "VALUES (%s,'local-libvirt',%s,'default','standard','available','qemu:///system')",
        (resource_id, marker["resource_name"]),
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
    conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation,authority_kind,authority_binding,fence_protocol,credential_hash,state) "
        "VALUES (%s,'docker','{}',4,%s,'active')",
        (worker, credential),
    )
    conn.execute(
        "INSERT INTO jobs (id,kind,state,attempt,max_attempts,worker_id,lease_expires_at,"
        "heartbeat_at,payload,authorizing,dedup_key,error_category,failure_context) VALUES "
        "(%s,%s,'running',%s,%s,%s,clock_timestamp()+interval '5 minutes',clock_timestamp(),"
        "%s,%s,%s,'conflict',%s)",
        (
            job_id,
            "provision" if operation == "provision" else "teardown",
            attempt,
            max_attempts,
            worker,
            Jsonb({"authority_system_v1": marker}),
            Jsonb({"principal": "p", "agent_session": "authority-session", "project": "proj"}),
            f"direct-authority-{job_id}",
            Jsonb({"reason": "prior-attempt"}),
        ),
    )
    conn.execute(
        "INSERT INTO authority_system_ownership "
        "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
        "profile_identity,root_identity,state) VALUES "
        "(%s,%s,%s,'local-libvirt',%s,%s,%s,%s,%s)",
        (
            system_id,
            allocation_id,
            resource_id,
            marker["resource_name"],
            marker["authority_instance"],
            profile_digest,
            root_digest,
            ownership_state,
        ),
    )
    return {
        "resource_id": resource_id,
        "allocation_id": allocation_id,
        "system_id": system_id,
        "job_id": job_id,
        "worker": worker,
        "credential": credential,
        "marker": marker,
    }


def _seed_direct_terminal_attempt(
    conn: psycopg.Connection,
    case: dict[str, object],
    *,
    disposition: str,
    generation: int = 1,
) -> tuple[object, object, bytes, str, str]:
    authority_id, request_attempt_id = uuid4(), uuid4()
    operation_digest = "sha256:" + "c" * 64
    head_digest = "sha256:" + "d" * 64
    receipt_digest = "sha256:" + "e" * 64
    receipt = json.dumps({"disposition": disposition}, separators=(",", ":")).encode()
    marker = cast(dict[str, object], case["marker"])
    job_row = conn.execute("SELECT attempt FROM jobs WHERE id=%s", (case["job_id"],)).fetchone()
    assert job_row is not None
    conn.execute(
        "UPDATE authority_system_ownership SET next_generation=%s,journal_sequence=2,"
        "journal_digest=%s,journal_phase='terminal',journal_record='{}' WHERE system_id=%s",
        (generation + 1, head_digest, case["system_id"]),
    )
    conn.execute(
        "INSERT INTO authority_system_attempts "
        "(id,system_id,generation,operation,job_id,job_attempt,worker_incarnation,"
        "request_attempt_id,operation_identity,operation_digest,state,ack_sequence,ack_digest,"
        "quiescence_digest,acknowledged_at,ack_head_sequence,ack_head_digest,"
        "terminal_head_sequence,terminal_head_digest,receipt_bytes,receipt_digest,"
        "receipt_disposition,receipt_at) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'terminal',1,%s,%s,clock_timestamp(),0,%s,2,%s,"
        "%s,%s,%s,clock_timestamp())",
        (
            authority_id,
            case["system_id"],
            generation,
            marker["operation"],
            case["job_id"],
            job_row[0],
            case["worker"],
            request_attempt_id,
            marker["operation_identity"],
            operation_digest,
            operation_digest,
            operation_digest,
            GENESIS_DIGEST,
            head_digest,
            receipt,
            receipt_digest,
            disposition,
        ),
    )
    conn.execute(
        "UPDATE authority_system_ownership SET current_attempt_id=%s WHERE system_id=%s",
        (authority_id, case["system_id"]),
    )
    return authority_id, request_attempt_id, receipt, head_digest, receipt_digest

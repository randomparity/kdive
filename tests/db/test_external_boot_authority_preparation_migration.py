"""Migration 0135 authority-owned preparation tests (ADR-0608)."""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from tests.db.external_boot_authority_support import (
    _PLAN,
    _RoleDsns,
    _seed_case,
)
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.support.external_boot_plan import external_boot_materialization, external_boot_plan


def test_preparing_allocation_and_phase_resolution_are_exact(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    with psycopg.connect(migrated_url) as admin:
        case = _seed_case(admin, worker_suffix="p")
        plan = external_boot_plan(case.system_id, case.run_id)
        marker = admin.execute(
            "SELECT payload->'external_boot_authority_v1' FROM jobs WHERE id = %s",
            (case.job_id,),
        ).fetchone()
        assert marker is not None
        marker_value = marker[0] | {"plan_identity": plan.identity}
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
                        "external_boot_authority_v1": marker_value,
                        "external_boot_plan_v1": plan.model_dump(mode="json", by_alias=True),
                    }
                ),
                case.job_id,
            ),
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
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
        assert row is not None and row[0] == "allocated"
        authority_id, generation, root_digest = row[1:]

    ack_digest = "sha256:" + "b" * 64
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
                ack_digest,
                "sha256:" + "c" * 64,
            ),
        )
        admin.execute(
            "UPDATE external_boot_authorities SET state = 'current', acknowledged_at = now() "
            "WHERE id = %s",
            (authority_id,),
        )

    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider:
        materialize = provider.execute(
            "SELECT operation, operation_identity, operation_digest, preparation_plan "
            "FROM resolve_current_external_boot_preparation_authority(%s,%s,%s,1,%s,%s)",
            (case.worker_id, authority_id, generation, ack_digest, "materialize"),
        ).fetchone()
        repeated = provider.execute(
            "SELECT operation, operation_identity, operation_digest, preparation_plan "
            "FROM resolve_current_external_boot_preparation_authority(%s,%s,%s,1,%s,%s)",
            (case.worker_id, authority_id, generation, ack_digest, "materialize"),
        ).fetchone()
        prepared = provider.execute(
            "SELECT operation_identity, operation_digest "
            "FROM resolve_current_external_boot_preparation_authority(%s,%s,%s,1,%s,%s)",
            (case.worker_id, authority_id, generation, ack_digest, "prepare"),
        ).fetchone()

    assert materialize == repeated
    assert materialize is not None and materialize[0] == "materialize"
    assert materialize[3] == plan.model_dump(mode="json", by_alias=True)
    assert prepared is not None and prepared != materialize[1:3]
    assert plan.identity != _PLAN

    journal_digest = "sha256:" + "d" * 64
    receipt = external_boot_materialization(plan)
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "INSERT INTO external_boot_authority_journal_heads "
            "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
            "operation_identity, head_record) VALUES (%s,%s,2,%s,'terminal',%s,%s,%s,%s)",
            (
                case.authority_instance,
                case.system_id,
                journal_digest,
                authority_id,
                generation,
                materialize[1],
                Jsonb(
                    {
                        "operation": "materialize",
                        "operation_identity": materialize[1],
                        "operation_digest": materialize[2],
                    }
                ),
            ),
        )
    arguments = (
        case.credential,
        case.job_id,
        case.attempt,
        authority_id,
        generation,
        "materialize",
        materialize[1],
        materialize[2],
        2,
        journal_digest,
        plan.identity,
        Jsonb(receipt.model_dump(mode="json", by_alias=True)),
    )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT commit_external_boot_preparation_result(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            arguments,
        ).fetchone() == ("applied",)
        assert worker.execute(
            "SELECT commit_external_boot_preparation_result(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            arguments,
        ).fetchone() == ("applied",)
    with psycopg.connect(migrated_url) as admin:
        state = admin.execute(
            "SELECT e.state, e.materialization, j.state, a.state "
            "FROM external_boot_activations e JOIN jobs j ON j.id=%s "
            "JOIN external_boot_authorities a ON a.id=%s WHERE e.id=%s",
            (case.job_id, authority_id, case.activation_id),
        ).fetchone()
    assert state == (
        "preparing",
        receipt.model_dump(mode="json", by_alias=True),
        "running",
        "current",
    )

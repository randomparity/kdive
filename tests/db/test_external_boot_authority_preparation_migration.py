"""Migration 0135 authority-owned preparation tests (ADR-0608)."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.providers.external_boot_authority.protocol import (
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
    record_digest,
)
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    ExternalBootPreparationObservation,
    OpaqueProviderRef,
)
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
        case = _seed_case(admin, worker_suffix="p", provider_kind="remote-libvirt")
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

    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        resumed = worker.execute(
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
    assert resumed == ("allocated", authority_id, generation, root_digest)

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
    operation_attempt_id = uuid4()
    materialization_receipt = external_boot_materialization(plan)
    receipt = ExternalBootPreparationObservation(
        state="materialized",
        binding=ExternalBootActivationBinding(
            system_id=str(case.system_id),
            run_id=str(case.run_id),
            activation_id=str(case.activation_id),
        ),
        plan_identity=plan.identity,
        authority=OpaqueProviderRef(
            ref=f"authority/{authority_id}/{generation}/{operation_attempt_id}"
        ),
        operation_identity=materialize[1],
        materialization=materialization_receipt,
    )
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
                        "attempt_id": str(operation_attempt_id),
                        "operation_identity": materialize[1],
                        "operation_digest": materialize[2],
                        "observation": {"composite_state": receipt.identity},
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
        operation_attempt_id,
        materialize[1],
        materialize[2],
        2,
        journal_digest,
        plan.identity,
        Jsonb(receipt.model_dump(mode="json", by_alias=True)),
    )
    commit_sql = (
        "SELECT commit_external_boot_preparation_result(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
    )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            commit_sql,
            arguments,
        ).fetchone() == ("applied",)
        receipt_json = receipt.model_dump(mode="json", by_alias=True)
        for path in (
            ("binding", "system_id"),
            ("authority", "ref"),
            ("materialization", "ownership"),
        ):
            malformed = deepcopy(receipt_json)
            owner = malformed
            for key in path[:-1]:
                nested = owner[key]
                assert isinstance(nested, dict)
                owner = nested
            del owner[path[-1]]
            rejected = arguments[:-1] + (Jsonb(malformed),)
            assert worker.execute(
                commit_sql,
                rejected,
            ).fetchone() == ("conflict",)
        assert worker.execute(
            commit_sql,
            arguments[:6] + (uuid4(),) + arguments[7:],
        ).fetchone() == ("superseded",)
        with pytest.raises(psycopg.errors.InvalidParameterValue):
            worker.execute(
                commit_sql,
                arguments[:6] + (None,) + arguments[7:],
            )
        assert worker.execute(
            commit_sql,
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
        materialization_receipt.model_dump(mode="json", by_alias=True),
        "running",
        "current",
    )
    prepare_attempt_id = uuid4()
    prepare_record = JournalRecordV1(
        authority_id=authority_id,
        generation=generation,
        system_id=case.system_id,
        activation_id=case.activation_id,
        run_id=case.run_id,
        plan_identity=plan.identity,
        purpose="activate",
        operation="prepare",
        provider_kind=case.provider_kind,
        authority_instance=case.authority_instance,
        operation_identity=prepared[0],
        operation_digest=prepared[1],
        sequence=3,
        previous_digest=journal_digest,
        phase=JournalPhase.ADMITTED,
        attempt_id=prepare_attempt_id,
        expected_source_identity="source-a",
        intended_target_identity="target-a",
    )
    payload = prepare_record.model_dump(mode="json", by_alias=True)
    payload["canonical_record"] = canonical_record_bytes(prepare_record).decode()
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider:
        assert provider.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,2,%s,%s)",
            (case.worker_id, authority_id, generation, journal_digest, Jsonb(payload)),
        ).fetchone() == ("advanced",)
        admitted_digest = record_digest(prepare_record)
        prepare_record = prepare_record.model_copy(
            update={
                "sequence": 4,
                "previous_digest": admitted_digest,
                "phase": JournalPhase.MUTATION_STARTED,
            }
        )
        payload = prepare_record.model_dump(mode="json", by_alias=True)
        payload["canonical_record"] = canonical_record_bytes(prepare_record).decode()
        assert provider.execute(
            "SELECT advance_external_boot_authority_journal_head(%s,%s,%s,3,%s,%s)",
            (case.worker_id, authority_id, generation, admitted_digest, Jsonb(payload)),
        ).fetchone() == ("advanced",)
        opened = provider.execute(
            "SELECT open_external_boot_remote_module_attempt(%s,%s,%s,1,%s,%s,%s,%s)",
            (
                case.worker_id,
                authority_id,
                generation,
                ack_digest,
                prepare_attempt_id,
                prepared[0],
                prepared[1],
            ),
        ).fetchone()
        assert opened is not None
        assert opened[0]["module_attempt_obligation"] == {
            "schema": "module-attempt-obligation-receipt-v1",
            "system_id": str(case.system_id),
            "run_id": str(case.run_id),
            "operation_nonce": prepare_attempt_id.hex,
        }
        assert (
            provider.execute(
                "SELECT open_external_boot_remote_module_attempt(%s,%s,%s,1,%s,%s,%s,%s)",
                (
                    case.worker_id,
                    authority_id,
                    generation,
                    ack_digest,
                    prepare_attempt_id,
                    prepared[0],
                    prepared[1],
                ),
            ).fetchone()
            == opened
        )
        assert provider.execute(
            "SELECT open_external_boot_remote_module_attempt(%s,%s,%s,1,%s,%s,%s,%s)",
            (
                case.worker_id,
                authority_id,
                generation,
                ack_digest,
                uuid4(),
                prepared[0],
                prepared[1],
            ),
        ).fetchone() == (None,)
    open_arguments = (
        case.worker_id,
        authority_id,
        generation,
        ack_digest,
        prepare_attempt_id,
        prepared[0],
        prepared[1],
    )
    with (
        psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute(
            "SELECT open_external_boot_remote_module_attempt(%s,%s,%s,1,%s,%s,%s,%s)",
            open_arguments,
        )
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE jobs SET lease_expires_at=now()-interval '1 second' WHERE id=%s",
            (case.job_id,),
        )
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as provider:
        assert provider.execute(
            "SELECT open_external_boot_remote_module_attempt(%s,%s,%s,1,%s,%s,%s,%s)",
            open_arguments,
        ).fetchone() == (None,)

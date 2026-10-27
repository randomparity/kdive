"""Migration 0180: an ACTIVATE boot job's PREP terminal evidence is bound by durable records.

An ACTIVATE boot job is admitted before its module attempt exists, so its payload never carries
``remote_module_attempt_v1``; the commit used to refuse every remote Run boot's terminal evidence
(#3373). These tests drive the real repository against the real function with the payload shape
admission produces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import LiteralString, cast
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttemptWorkerWriteContext,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from tests.db.external_boot_authority_support import _AuthorityCase, _RoleDsns, _seed_case
from tests.db.remote_module_attempt_obligations_support import _attempt, _evidence, _real_evidence

_REPO = RemoteModuleAttemptObligationRepository()


@dataclass(frozen=True, slots=True)
class _Activate:
    case: _AuthorityCase
    context: ModuleAttemptWorkerWriteContext


def _seed_activate(
    migrated_url: str, role_dsns: _RoleDsns, suffix: str = "m", *, preparing: bool = True
) -> _Activate:
    """An activation in PREP whose boot job is its latest takeover, as admission builds it."""
    with psycopg.connect(migrated_url) as admin:
        case = _seed_case(admin, worker_suffix=suffix, provider_kind="remote-libvirt")
        marker = admin.execute(
            "SELECT payload->'external_boot_authority_v1' FROM jobs WHERE id = %s", (case.job_id,)
        ).fetchone()
        assert marker is not None
        if preparing:
            admin.execute(
                "UPDATE external_boot_activations SET state = 'preparing', "
                "materialization = NULL, recovery_point = NULL WHERE id = %s",
                (case.activation_id,),
            )
        admin.execute(
            "UPDATE jobs SET payload = %s WHERE id = %s",
            (
                Jsonb(
                    {
                        "run_id": str(case.run_id),
                        "external_boot_authority_v1": marker[0],
                    }
                ),
                case.job_id,
            ),
        )
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = worker.execute(
            "SELECT status FROM allocate_external_boot_authority(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                case.activation_id,
                case.run_id,
                case.system_id,
                "sha256:" + "a" * 64,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.operation_identity,
            ),
        ).fetchone()
    assert allocated is not None and allocated[0] == "allocated"
    preparation = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": case.system_id,
                "run_id": case.run_id,
                "operation_nonce": "0" * 32,
            }
        }
    )
    context = ModuleAttemptWorkerWriteContext(
        case.job_id, case.attempt, SecretStr(f"activate-worker-credential-{suffix}"), preparation
    )
    with psycopg.connect(migrated_url) as admin:
        admin.execute(
            "UPDATE worker_incarnations SET credential_hash = %s WHERE incarnation = %s",
            (context.credential_hash, case.worker_id),
        )
    return _Activate(case, context)


def _commit(
    migrated_url: str,
    role_dsns: _RoleDsns,
    activate: _Activate,
    *,
    context: ModuleAttemptWorkerWriteContext | None = None,
    action: str = "terminal",
) -> bool:
    async def run() -> bool:
        attempt = _attempt(activate.case.system_id, activate.case.run_id)
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            await _REPO.open_mutation_obligation(admin, attempt)
            await admin.commit()
        async with await psycopg.AsyncConnection.connect(role_dsns("kdive_worker")) as worker:
            chosen = context or activate.context
            if action == "terminal":
                recorded = await _REPO.worker_record_terminal_evidence(
                    worker, chosen, attempt, _evidence(attempt)
                )
            else:
                _terminal, restored = _real_evidence(attempt)
                recorded = await _REPO.worker_record_restored_evidence(
                    worker, chosen, attempt, restored
                )
            await worker.commit()
            return recorded

    return asyncio.run(run())


def _terminal_recorded(migrated_url: str, activate: _Activate) -> bool:
    with psycopg.connect(migrated_url) as admin:
        row = admin.execute(
            "SELECT terminal_result IS NOT NULL FROM remote_module_attempt_obligations "
            "WHERE system_id = %s AND run_id = %s",
            (activate.case.system_id, activate.case.run_id),
        ).fetchone()
    return row is not None and row[0] is True


def test_an_activate_job_without_the_receipt_records_its_terminal_evidence(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    activate = _seed_activate(migrated_url, authority_role_dsns)
    with psycopg.connect(migrated_url) as admin:
        payload = admin.execute(
            "SELECT payload FROM jobs WHERE id = %s", (activate.case.job_id,)
        ).fetchone()
    assert payload is not None and "remote_module_attempt_v1" not in payload[0]

    assert _commit(migrated_url, authority_role_dsns, activate) is True
    assert _terminal_recorded(migrated_url, activate)


def _mutate(migrated_url: str, statement: str, *args: object) -> None:
    with psycopg.connect(migrated_url) as admin:
        admin.execute(cast(LiteralString, statement), args)


@pytest.mark.parametrize(
    ("name", "mutation"),
    [
        (
            "a later takeover by another job attempt supersedes this job",
            lambda url, a: _mutate(
                url,
                "INSERT INTO external_boot_authority_audit "
                "(authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, "
                "job_id, job_attempt, worker_incarnation, prior_generation, generation, purpose, "
                "provider_kind, authority_instance, operation, operation_identity, "
                "operation_digest, outcome) "
                "SELECT authority_id, system_id, allocation_id, activation_id, run_id, "
                "plan_identity, job_id, job_attempt + 1, worker_incarnation, generation, "
                "generation + 1, purpose, provider_kind, authority_instance, operation, "
                "operation_identity, operation_digest, 'takeover_allocated' "
                "FROM external_boot_authority_audit WHERE activation_id = %s LIMIT 1",
                a.case.activation_id,
            ),
        ),
        (
            "the payload marker names a different activation",
            lambda url, a: _mutate(
                url,
                "UPDATE jobs SET payload = jsonb_set(payload, "
                "'{external_boot_authority_v1,activation_id}', to_jsonb(%s::text)) WHERE id = %s",
                str(uuid4()),
                a.case.job_id,
            ),
        ),
        (
            "the payload marker names a different run",
            lambda url, a: _mutate(
                url,
                "UPDATE jobs SET payload = jsonb_set(payload, "
                "'{external_boot_authority_v1,run_id}', to_jsonb(%s::text)) WHERE id = %s",
                str(uuid4()),
                a.case.job_id,
            ),
        ),
        (
            "the payload marker names a different system",
            lambda url, a: _mutate(
                url,
                "UPDATE jobs SET payload = jsonb_set(payload, "
                "'{external_boot_authority_v1,system_id}', to_jsonb(%s::text)) WHERE id = %s",
                str(uuid4()),
                a.case.job_id,
            ),
        ),
        (
            "the payload marker is not an activate purpose",
            lambda url, a: _mutate(
                url,
                "UPDATE jobs SET payload = jsonb_set(payload, "
                "'{external_boot_authority_v1,purpose}', '\"release\"') WHERE id = %s",
                a.case.job_id,
            ),
        ),
        (
            "the job lease has lapsed",
            lambda url, a: _mutate(
                url,
                "UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s",
                a.case.job_id,
            ),
        ),
    ],
)
def test_stale_or_foreign_activate_jobs_are_still_refused(
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    name: str,
    mutation: Callable[[str, _Activate], None],
) -> None:
    del name
    activate = _seed_activate(migrated_url, authority_role_dsns)
    mutation(migrated_url, activate)

    assert _commit(migrated_url, authority_role_dsns, activate) is False
    assert not _terminal_recorded(migrated_url, activate)


def test_an_activation_that_is_no_longer_preparing_is_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    activate = _seed_activate(migrated_url, authority_role_dsns, preparing=False)

    assert _commit(migrated_url, authority_role_dsns, activate) is False
    assert not _terminal_recorded(migrated_url, activate)


def test_another_jobs_evidence_cannot_bind_to_this_activation(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    ours = _seed_activate(migrated_url, authority_role_dsns, "m")
    foreign = _seed_activate(migrated_url, authority_role_dsns, "n")
    hijack = ModuleAttemptWorkerWriteContext(
        foreign.case.job_id,
        foreign.case.attempt,
        foreign.context.incarnation_credential,
        ours.context.preparation,
    )

    assert _commit(migrated_url, authority_role_dsns, ours, context=hijack) is False
    assert not _terminal_recorded(migrated_url, ours)


def test_a_job_attempt_that_is_not_the_leased_attempt_is_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    activate = _seed_activate(migrated_url, authority_role_dsns)
    wrong = ModuleAttemptWorkerWriteContext(
        activate.case.job_id,
        2,
        activate.context.incarnation_credential,
        activate.context.preparation,
    )

    assert _commit(migrated_url, authority_role_dsns, activate, context=wrong) is False


def test_restored_evidence_still_requires_the_payload_receipt(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    activate = _seed_activate(migrated_url, authority_role_dsns)
    assert _commit(migrated_url, authority_role_dsns, activate) is True

    assert _commit(migrated_url, authority_role_dsns, activate, action="restored") is False


def test_a_payload_that_carries_a_different_receipt_is_still_refused(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    activate = _seed_activate(migrated_url, authority_role_dsns)
    other = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": activate.case.system_id,
                "run_id": activate.case.run_id,
                "operation_nonce": "1" * 32,
            }
        }
    )
    _mutate(
        migrated_url,
        "UPDATE jobs SET payload = payload || "
        "jsonb_build_object('remote_module_attempt_v1', %s::jsonb) WHERE id = %s",
        Jsonb(other.model_dump(mode="json", by_alias=True)),
        activate.case.job_id,
    )

    assert _commit(migrated_url, authority_role_dsns, activate) is False


def test_the_helper_is_not_callable_by_the_worker_directly(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    del migrated_url
    with (
        psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        worker.execute(
            "SELECT public.remote_module_activate_job_owns_attempt(%s, 1, '{}'::jsonb, %s, %s)",
            (UUID(int=1), UUID(int=2), UUID(int=3)),
        )

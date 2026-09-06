"""Authority-role quarantine publication over real PostgreSQL (#2204)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Generator
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.providers.external_boot_authority.journal import record_digest
from kdive.providers.external_boot_authority.protocol import (
    AuthorityObservationV1,
    AuthorityOperation,
    JournalPhase,
    JournalRecordV1,
)
from kdive.providers.external_boot_authority.repository import DatabaseAuthorityRepository
from kdive.providers.external_boot_authority.service import AuthenticatedPeer
from kdive.providers.ports.external_boot import (
    ExternalBootActivationBinding,
    OpaqueProviderRef,
    RecoveryObjectBinding,
    RecoveryObjectObservation,
)
from tests.db.conftest import _migrated_db as _migrated_db  # noqa: F401
from tests.db.conftest import migrated_url as migrated_url  # noqa: F401
from tests.db.conftest import postgres_url as postgres_url  # noqa: F401
from tests.db.external_boot_authority_support import authority_role_dsns as _role_dsns
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


@pytest.fixture
def authority_role_dsns(migrated_url: str) -> Generator[Callable[[str], str]]:
    fixture = cast(Any, _role_dsns).__wrapped__(migrated_url)
    yield next(fixture)
    fixture.close()


@asynccontextmanager
async def _authority_connection(dsn: str) -> AsyncIterator[psycopg.AsyncConnection]:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        yield conn


def _digest(letter: str) -> str:
    return "sha256:" + letter * 64


def test_authority_publication_replays_conflicts_and_fences_release_cleanup(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    async def run() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed, vehicle, purpose="release", operation="release", activation_state="active"
            )
            authority_id = uuid4()
            generation = 1
            root_identity = _digest("a")
            root_digest = _digest("b")
            await seed.execute(
                "INSERT INTO external_boot_authorities "
                "(id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
                "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, "
                "operation, operation_identity, operation_digest, generation, state, "
                "acknowledged_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,1,'release','local-libvirt','authority-vehicle',"
                "%s,'release',%s,%s,%s,'current',now())",
                (
                    authority_id,
                    vehicle.system_id,
                    case.allocation_id,
                    vehicle.activation_id,
                    vehicle.run_id,
                    vehicle.plan_identity,
                    case.job_id,
                    case.worker_incarnation,
                    root_identity,
                    root_digest,
                    generation,
                ),
            )
            root = {
                "authority_id": str(authority_id),
                "generation": generation,
                "system_id": str(vehicle.system_id),
                "activation_id": str(vehicle.activation_id),
                "run_id": str(vehicle.run_id),
                "plan_identity": vehicle.plan_identity,
                "provider_kind": "local-libvirt",
                "authority_instance": "authority-vehicle",
                "worker_incarnation": case.worker_incarnation,
                "root_operation_identity": root_identity,
                "root_operation_digest": root_digest,
            }
            row = await (
                await seed.execute(
                    "SELECT operation_identity, operation_digest "
                    "FROM derive_external_boot_release_phase_binding(%s, 'cleanup')",
                    (Jsonb(root),),
                )
            ).fetchone()
            assert row is not None
            cleanup_identity, cleanup_digest = row
            binding = AuthorityBinding(
                peer_incarnation_id=case.worker_incarnation,
                authority_id=authority_id,
                generation=generation,
                system_id=vehicle.system_id,
                activation_id=vehicle.activation_id,
                run_id=vehicle.run_id,
                plan_identity=vehicle.plan_identity,
                purpose="release",
                operation=AuthorityOperation.CLEANUP,
                provider_kind="local-libvirt",
                authority_instance="authority-vehicle",
                operation_identity=cleanup_identity,
                operation_digest=cleanup_digest,
                state="current",
            )
            terminal = JournalRecordV1(
                authority_id=authority_id,
                generation=generation,
                system_id=vehicle.system_id,
                activation_id=vehicle.activation_id,
                run_id=vehicle.run_id,
                plan_identity=vehicle.plan_identity,
                purpose="release",
                operation=AuthorityOperation.CLEANUP,
                provider_kind="local-libvirt",
                authority_instance="authority-vehicle",
                operation_identity=cleanup_identity,
                operation_digest=cleanup_digest,
                sequence=9,
                previous_digest=_digest("c"),
                phase=JournalPhase.TERMINAL,
                attempt_id=uuid4(),
                expected_source_identity=_digest("d"),
                intended_target_identity=_digest("e"),
                observation=AuthorityObservationV1(
                    observation_id=uuid4(), category="absent", composite_state=_digest("f")
                ),
                outcome="absent",
            )
            await seed.execute(
                "INSERT INTO external_boot_authority_journal_heads "
                "(authority_instance, system_id, sequence, digest, phase, authority_id, "
                "generation, operation_identity, head_record) "
                "VALUES (%s,%s,%s,%s,'terminal',%s,%s,%s,'{}'::jsonb)",
                (
                    binding.authority_instance,
                    binding.system_id,
                    terminal.sequence,
                    record_digest(terminal),
                    authority_id,
                    generation,
                    cleanup_identity,
                ),
            )
            observation = RecoveryObjectObservation(
                binding=RecoveryObjectBinding(
                    record_id=str(uuid4()),
                    binding=ExternalBootActivationBinding(
                        system_id=str(vehicle.system_id),
                        run_id=str(vehicle.run_id),
                        activation_id=str(vehicle.activation_id),
                    ),
                    kind="recovery-record",
                    reference=OpaqueProviderRef(ref="private/recovery-record"),
                    ownership_digest=_digest("1"),
                    operation_identity=cleanup_identity,
                    attempt_id=str(terminal.attempt_id),
                    mutation_journal_sequence=8,
                    mutation_journal_digest=_digest("2"),
                    reserved_bytes=0,
                ),
                present=True,
                managed=False,
                observed_digest=_digest("3"),
            )
            repository = DatabaseAuthorityRepository(
                lambda: _authority_connection(authority_role_dsns("kdive_provider_authority"))
            )
            peer = AuthenticatedPeer(case.worker_incarnation)
            await repository.publish_cleanup_quarantine(peer, binding, terminal, (observation,))
            await repository.publish_cleanup_quarantine(peer, binding, terminal, (observation,))
            saved = await (
                await seed.execute("SELECT count(*) FROM external_boot_recovery_quarantine")
            ).fetchone()
            assert saved == (1,)
            conflicting = observation.model_copy(update={"observed_digest": _digest("4")})
            with pytest.raises(psycopg.Error):
                await repository.publish_cleanup_quarantine(peer, binding, terminal, (conflicting,))
            await seed.execute(
                "UPDATE worker_incarnations SET state = 'terminated', terminated_at = now(), "
                "outcome = 'killed' WHERE incarnation = %s",
                (case.worker_incarnation,),
            )
            with pytest.raises(psycopg.Error):
                await repository.publish_cleanup_quarantine(peer, binding, terminal, (observation,))

    asyncio.run(run())

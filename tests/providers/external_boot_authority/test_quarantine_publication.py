"""Authority-role quarantine publication over real PostgreSQL (#2204)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db.external_boot_authority_journal import AuthorityBinding
from kdive.providers.external_boot_authority.journal import record_digest
from kdive.providers.external_boot_authority.orphan import _Selection
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
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle


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


async def _insert_release_authority(
    seed: psycopg.AsyncConnection,
    vehicle: Any,
    case: Any,
    *,
    generation: int,
    state: str,
    acknowledged: bool = True,
) -> tuple[UUID, str, str]:
    """Insert one release authority generation; return its id and derived cleanup binding."""
    authority_id = uuid4()
    root_identity = _digest(str(generation))
    root_digest = _digest("b")
    superseded = "now()" if state == "superseded" else "NULL"
    acknowledged_at = "now()" if acknowledged else "NULL"
    await seed.execute(
        "INSERT INTO external_boot_authorities "
        "(id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
        "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, "
        "operation, operation_identity, operation_digest, generation, state, "
        f"acknowledged_at, superseded_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,1,'release','local-libvirt','authority-vehicle',"
        f"%s,'release',%s,%s,%s,%s,{acknowledged_at},{superseded})",
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
            state,
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
    return authority_id, row[0], row[1]


def test_publication_admits_an_earlier_release_cleanup_identity(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """A retried release publishes the adopted cleanup's receipt under its own identity (#3434)."""

    async def run() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(
                seed, vehicle, purpose="release", operation="release", activation_state="active"
            )
            _first_id, adopted_identity, _ = await _insert_release_authority(
                seed, vehicle, case, generation=1, state="superseded"
            )
            _unacknowledged_id, unacknowledged_identity, _ = await _insert_release_authority(
                seed, vehicle, case, generation=2, state="superseded", acknowledged=False
            )
            authority_id, cleanup_identity, cleanup_digest = await _insert_release_authority(
                seed, vehicle, case, generation=3, state="current"
            )
            other_vehicle = build_vehicle()
            other_case = await seed_case(
                seed,
                other_vehicle,
                purpose="release",
                operation="release",
                activation_state="active",
            )
            _other_id, other_lineage_identity, _ = await _insert_release_authority(
                seed, other_vehicle, other_case, generation=1, state="superseded"
            )
            binding = AuthorityBinding(
                peer_incarnation_id=case.worker_incarnation,
                authority_id=authority_id,
                generation=3,
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
                generation=3,
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
                sequence=12,
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
                    3,
                    cleanup_identity,
                ),
            )
            adopted_attempt = uuid4()
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
                    operation_identity=adopted_identity,
                    attempt_id=str(adopted_attempt),
                    mutation_journal_sequence=3,
                    mutation_journal_digest=_digest("2"),
                    reserved_bytes=0,
                ),
                present=True,
                managed=False,
                observed_digest=_digest("3"),
            )
            authority_dsn = authority_role_dsns("kdive_provider_authority")
            repository = DatabaseAuthorityRepository(lambda: _authority_connection(authority_dsn))
            peer = AuthenticatedPeer(case.worker_incarnation)

            await repository.publish_cleanup_quarantine(peer, binding, terminal, (observation,))
            await repository.publish_cleanup_quarantine(peer, binding, terminal, (observation,))

            rows = await (
                await seed.execute(
                    "SELECT q.id, q.system_id, q.activation_id, s.run_id, q.authority_instance, "
                    "q.object_kind, q.object_reference, q.ownership_digest, q.observed_digest, "
                    "q.operation_identity, q.attempt_id, q.mutation_journal_sequence, "
                    "q.mutation_journal_digest, q.reserved_bytes "
                    "FROM external_boot_recovery_quarantine AS q "
                    "JOIN external_boot_activations AS s ON s.id = q.activation_id"
                )
            ).fetchall()
            (row,) = rows
            selection = _Selection(
                object_id=row[0],
                system_id=row[1],
                activation_id=row[2],
                run_id=row[3],
                disposition="delete",
                authority_instance=row[4],
                kind=row[5],
                reference=row[6],
                ownership_digest=row[7],
                observed_digest=row[8],
                operation_identity=row[9],
                attempt_id=row[10],
                journal_sequence=row[11],
                journal_digest=row[12],
                reserved_bytes=row[13],
                deadline=datetime.now(UTC),
            )
            assert row[9] == adopted_identity
            assert selection.binding() == observation.binding

            for refused in (_digest("7"), unacknowledged_identity, other_lineage_identity):
                foreign = observation.model_copy(
                    update={
                        "binding": observation.binding.model_copy(
                            update={"record_id": str(uuid4()), "operation_identity": refused}
                        )
                    }
                )
                with pytest.raises(psycopg.Error, match="not admissible"):
                    await repository.publish_cleanup_quarantine(peer, binding, terminal, (foreign,))

            keyless = {
                "id": str(uuid4()),
                "object_identity": _digest("8"),
                "object_kind": "recovery-record",
                "object_reference": "private/keyless-record",
                "ownership_digest": _digest("1"),
                "observed_digest": _digest("3"),
                "attempt_id": str(uuid4()),
                "mutation_journal_sequence": 4,
                "mutation_journal_digest": _digest("4"),
                "reserved_bytes": 0,
            }
            async with _authority_connection(authority_dsn) as conn:
                await conn.execute(
                    "SELECT publish_external_boot_recovery_quarantine_authority"
                    "(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        case.worker_incarnation,
                        authority_id,
                        3,
                        cleanup_identity,
                        terminal.sequence,
                        record_digest(terminal),
                        Jsonb([keyless]),
                    ),
                )
            stored = await (
                await seed.execute(
                    "SELECT operation_identity FROM external_boot_recovery_quarantine "
                    "WHERE id = %s",
                    (keyless["id"],),
                )
            ).fetchone()
            assert stored == (cleanup_identity,)

            not_release = replace(binding, purpose="cleanup")
            with pytest.raises(ValueError, match="does not match current authority"):
                await repository.publish_cleanup_quarantine(
                    peer, not_release, terminal, (observation,)
                )

    asyncio.run(run())

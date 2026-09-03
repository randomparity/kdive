"""Durable remote-module attempt obligation proofs (ADR-0588, migration 0126)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptObligationError,
    ModuleAttemptTerminalEvidence,
    MutationDischargeReason,
    RemoteModuleAttemptObligationRepository,
    RetainedModuleAttempt,
)

_PLAN = "sha256:" + "a" * 64
_DIGEST = "sha256:" + "b" * 64
_MANIFEST = "sha256:" + "c" * 64
_TERMINAL_OPERATION_IDENTITY = "sha256:" + "1" * 64
_TERMINAL_RESULT_IDENTITY = "sha256:" + "2" * 64
_BASELINE_OPERATION_IDENTITY = "sha256:" + "3" * 64
_BASELINE_RESULT_IDENTITY = "sha256:" + "4" * 64


async def _seed(conn: psycopg.AsyncConnection) -> tuple[UUID, UUID]:
    """Insert the resource/allocation/system/investigation/run spine one attempt hangs off."""
    resource_id, allocation_id = uuid4(), uuid4()
    system_id, investigation_id, run_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'remote-libvirt', 'default', 'standard', 'available', 'qemu+tls://host/system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES "
        "(%s, %s, %s, 'remote-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )
    return system_id, run_id


def _attempt(system_id: UUID, run_id: UUID, nonce: str = "0" * 32) -> ModuleAttempt:
    return ModuleAttempt(system_id=system_id, run_id=run_id, operation_nonce=nonce)


def _operation(attempt: ModuleAttempt) -> dict[str, Any]:
    return {
        "protocol": "remote-module-operation-v1",
        "operation": "restore",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "release": "6.12.0",
        "root_volume": {"key": "kdive-module-root", "identity": _DIGEST},
        "source_manifest": _MANIFEST,
        "installed_manifest": _MANIFEST,
        "capture_absent": True,
        "appliance_image_digest": _DIGEST,
    }


def _result(attempt: ModuleAttempt) -> dict[str, Any]:
    return {
        "protocol": "remote-module-result-v1",
        "status": "success",
        "phase": "restored",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "appliance_image_digest": _DIGEST,
    }


def _recovery_reference(attempt: ModuleAttempt) -> dict[str, Any]:
    return {
        "protocol": "remote-module-recovery-ref-v1",
        "system_id": str(attempt.system_id),
        "run_id": str(attempt.run_id),
        "plan_identity": _PLAN,
        "operation_nonce": attempt.operation_nonce,
        "pool": {"ref": "pools/modules"},
        "root_volume": {"ref": "volumes/root"},
        "source_volume": {"ref": "volumes/source"},
        "scratch_volume": {"ref": "volumes/scratch"},
        "operation_identity": _TERMINAL_OPERATION_IDENTITY,
        "result_identity": _TERMINAL_RESULT_IDENTITY,
        "installed_entry_count": 42,
        "installed_content_bytes": 4096,
        "appliance_image_digest": _DIGEST,
        "authority_identity": _DIGEST,
    }


def _evidence(attempt: ModuleAttempt) -> ModuleAttemptTerminalEvidence:
    return ModuleAttemptTerminalEvidence(
        terminal_operation=_operation(attempt),
        terminal_operation_identity=_TERMINAL_OPERATION_IDENTITY,
        terminal_result=_result(attempt),
        terminal_result_identity=_TERMINAL_RESULT_IDENTITY,
        baseline_operation_identity=_BASELINE_OPERATION_IDENTITY,
        baseline_result_identity=_BASELINE_RESULT_IDENTITY,
        installed_entry_count=42,
        installed_content_bytes=4096,
        recovery_reference=_recovery_reference(attempt),
    )


def test_mutation_obligation_opens_once_and_discharges_once(migrated_url: str) -> None:
    """Opening replays cleanly, and the first discharge reason is the one that stands."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)

            assert await repo.open_mutation_obligation(conn, attempt) is True
            assert await repo.open_mutation_obligation(conn, attempt) is False
            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(attempt, mutation_retained=True, reap_retained=False),
            )

            assert (
                await repo.discharge_mutation_obligation(conn, attempt, reason="restored") is True
            )
            assert (
                await repo.discharge_mutation_obligation(conn, attempt, reason="baseline_committed")
                is False
            )
            row = await (
                await conn.execute(
                    "SELECT mutation_discharge_reason FROM remote_module_attempt_obligations "
                    "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                    attempt.key,
                )
            ).fetchone()
            assert row is not None and row[0] == "restored"
            assert await repo.retained_owners(conn) == ()

    asyncio.run(_run())


@pytest.mark.parametrize("reason", ["restored", "baseline_committed", "terminal_escape"], ids=str)
def test_every_mutation_discharge_reason_releases_the_attempt(
    migrated_url: str, reason: MutationDischargeReason
) -> None:
    """All three ADR-0585 discharge events drop the attempt out of the retained set."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            assert await repo.discharge_mutation_obligation(conn, attempt, reason=reason) is True
            assert await repo.retained_owners(conn) == ()

    asyncio.run(_run())


def test_discharging_the_mutation_obligation_leaves_an_open_reap_obligation(
    migrated_url: str,
) -> None:
    """The journal volumes survive the mutation discharge that reclaims the scratch volume.

    This is the arm ADR-0588 names: under one shared rule the sweep would delete the marker the
    crash-resume path calls authoritative, because the journal volumes are created strictly after
    `restored` — the event that discharges the mutation obligation.
    """

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            await repo.record_terminal_evidence(conn, attempt, _evidence(attempt))
            assert await repo.open_reap_obligation(conn, attempt) is True

            await repo.discharge_mutation_obligation(conn, attempt, reason="restored")
            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(attempt, mutation_retained=False, reap_retained=True),
            )

            assert await repo.discharge_reap_obligation(conn, attempt) is True
            assert await repo.discharge_reap_obligation(conn, attempt) is False
            assert await repo.retained_owners(conn) == ()

    asyncio.run(_run())


def test_retained_read_excludes_discharged_attempts_and_keeps_the_others(
    migrated_url: str,
) -> None:
    """Only the attempts with an un-discharged obligation come back, each with its own flags.

    ``reaped_only`` is the arm that separates a *discharged* reap obligation from an *opened* one:
    it still appears, because its mutation obligation stands, so its ``reap_retained`` flag has to
    say the journals are reclaimable while its ``source.ext4`` and ``scratch.ext4`` are not.
    """

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            open_mutation = _attempt(system_id, run_id, "a" * 32)
            open_reap = _attempt(system_id, run_id, "b" * 32)
            fully_discharged = _attempt(system_id, run_id, "c" * 32)
            reaped_only = _attempt(system_id, run_id, "e" * 32)

            for attempt in (open_mutation, open_reap, fully_discharged, reaped_only):
                await repo.open_mutation_obligation(conn, attempt)

            for attempt in (open_reap, fully_discharged, reaped_only):
                await repo.record_terminal_evidence(conn, attempt, _evidence(attempt))
                await repo.open_reap_obligation(conn, attempt)
            for attempt in (open_reap, fully_discharged):
                await repo.discharge_mutation_obligation(conn, attempt, reason="restored")
            for attempt in (fully_discharged, reaped_only):
                await repo.discharge_reap_obligation(conn, attempt)

            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(open_mutation, mutation_retained=True, reap_retained=False),
                RetainedModuleAttempt(open_reap, mutation_retained=False, reap_retained=True),
                RetainedModuleAttempt(reaped_only, mutation_retained=True, reap_retained=False),
            )

    asyncio.run(_run())


def test_terminal_evidence_round_trips_and_is_write_once(migrated_url: str) -> None:
    """Every field the discarded `attempt-reap` element carried survives the round trip."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            assert await repo.read_terminal_evidence(conn, attempt) is None

            evidence = _evidence(attempt)
            await repo.record_terminal_evidence(conn, attempt, evidence)
            assert await repo.read_terminal_evidence(conn, attempt) == evidence

            await repo.record_terminal_evidence(conn, attempt, evidence)
            assert await repo.read_terminal_evidence(conn, attempt) == evidence

            changed = _operation(attempt) | {"release": "6.13.0"}
            with pytest.raises(psycopg.errors.RaiseException, match="terminal evidence"):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE remote_module_attempt_obligations SET terminal_operation = %s "
                        "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                        (Jsonb(changed), *attempt.key),
                    )
            assert await repo.read_terminal_evidence(conn, attempt) == evidence

    asyncio.run(_run())


def test_kdive_server_can_drive_the_row_without_execute_on_the_trigger_function(
    migrated_url: str,
) -> None:
    """The write-once trigger still fires for the role that owns none of it.

    The migration revokes the trigger function's default EXECUTE-to-PUBLIC, because the authority
    host's readiness check counts an unlisted PUBLIC grant as excess privilege. That is only safe
    if PostgreSQL checks EXECUTE when the trigger is *created* rather than when it runs, so this
    exercises the whole row under ``SET ROLE kdive_server`` — which has SELECT/INSERT/UPDATE on the
    table and no privilege at all on the function.
    """

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            row = await (
                await conn.execute(
                    "SELECT has_function_privilege('kdive_server', "
                    "'public.reject_remote_module_attempt_rewrite()', 'EXECUTE')"
                )
            ).fetchone()
            assert row is not None and row[0] is False

            await conn.execute("SET ROLE kdive_server")
            evidence = _evidence(attempt)
            await repo.open_mutation_obligation(conn, attempt)
            await repo.record_terminal_evidence(conn, attempt, evidence)
            assert await repo.read_terminal_evidence(conn, attempt) == evidence

            with pytest.raises(psycopg.errors.RaiseException, match="terminal evidence"):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE remote_module_attempt_obligations SET terminal_operation = %s "
                        "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                        (Jsonb(_operation(attempt) | {"release": "6.13.0"}), *attempt.key),
                    )
            await conn.execute("RESET ROLE")

    asyncio.run(_run())


def test_reap_obligation_requires_terminal_evidence(migrated_url: str) -> None:
    """A reap obligation cannot open before the source its readers depend on exists."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            with pytest.raises(ModuleAttemptObligationError, match="no terminal evidence"):
                await repo.open_reap_obligation(conn, attempt)

            with pytest.raises(psycopg.errors.CheckViolation, match="reap_needs_evidence"):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE remote_module_attempt_obligations SET reap_opened_at = now() "
                        "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                        attempt.key,
                    )
            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(attempt, mutation_retained=True, reap_retained=False),
            )

    asyncio.run(_run())


def test_obligation_calls_against_a_missing_row_fail_loudly(migrated_url: str) -> None:
    """N5 makes the row precede the volumes, so an absent row is a caller ordering error."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            for call in (
                repo.discharge_mutation_obligation(conn, attempt, reason="restored"),
                repo.record_terminal_evidence(conn, attempt, _evidence(attempt)),
                repo.open_reap_obligation(conn, attempt),
                repo.discharge_reap_obligation(conn, attempt),
            ):
                with pytest.raises(ModuleAttemptObligationError, match="no obligation row"):
                    await call
            assert await repo.read_terminal_evidence(conn, attempt) is None

            await repo.open_mutation_obligation(conn, attempt)
            await repo.record_terminal_evidence(conn, attempt, _evidence(attempt))
            with pytest.raises(ModuleAttemptObligationError, match="never opened"):
                await repo.discharge_reap_obligation(conn, attempt)

    asyncio.run(_run())


@pytest.mark.parametrize(
    "document", ["terminal_operation", "terminal_result", "recovery_reference"]
)
@pytest.mark.parametrize("field", ["system_id", "run_id", "operation_nonce"])
def test_database_rejects_evidence_belonging_to_another_attempt(
    migrated_url: str, document: str, field: str
) -> None:
    """Each document is checked against the key on each of the three attempt fields.

    Parametrized across the whole grid because the three arms of the ownership constraint are
    independent: with only one document mutated on only one field, disabling the other eight is
    invisible.
    """

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)

            foreign = str(uuid4()) if field.endswith("_id") else "d" * 32
            evidence = replace(
                _evidence(attempt),
                **{document: getattr(_evidence(attempt), document) | {field: foreign}},
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="evidence_ownership"):
                async with conn.transaction():
                    await repo.record_terminal_evidence(conn, attempt, evidence)
            assert await repo.read_terminal_evidence(conn, attempt) is None

    asyncio.run(_run())


@pytest.mark.parametrize(
    "document", ["terminal_operation", "terminal_result", "recovery_reference"]
)
def test_database_rejects_a_document_of_the_wrong_protocol(
    migrated_url: str, document: str
) -> None:
    """Each column holds one named document, so a foreign payload never reaches a reader."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)

            evidence = replace(
                _evidence(attempt),
                **{document: getattr(_evidence(attempt), document) | {"protocol": "other-v1"}},
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="evidence_schema"):
                async with conn.transaction():
                    await repo.record_terminal_evidence(conn, attempt, evidence)
            assert await repo.read_terminal_evidence(conn, attempt) is None

    asyncio.run(_run())


def test_database_rejects_a_recovery_reference_disagreeing_on_the_terminal_digests(
    migrated_url: str,
) -> None:
    """The row must have one answer for the digests its readers verify the payloads against."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)

            mismatched = replace(
                _evidence(attempt), terminal_operation_identity="sha256:" + "9" * 64
            )
            with pytest.raises(psycopg.errors.CheckViolation, match="evidence_identity"):
                async with conn.transaction():
                    await repo.record_terminal_evidence(conn, attempt, mismatched)
            assert await repo.read_terminal_evidence(conn, attempt) is None

    asyncio.run(_run())


def test_evidence_columns_cannot_be_written_piecemeal(migrated_url: str) -> None:
    """A reader that finds a terminal operation finds every field beside it, or none at all."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            with pytest.raises(psycopg.errors.CheckViolation, match="evidence_group"):
                async with conn.transaction():
                    await conn.execute(
                        "UPDATE remote_module_attempt_obligations SET terminal_operation = %s "
                        "WHERE system_id = %s AND run_id = %s AND operation_nonce = %s",
                        (Jsonb(_operation(attempt)), *attempt.key),
                    )
            assert await repo.read_terminal_evidence(conn, attempt) is None

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("assignment", "value", "constraint"),
    [
        ("reap_discharged_at = now()", None, "reap_discharge"),
        ("mutation_discharged_at = now()", None, "mutation_discharge"),
        ("mutation_discharge_reason = %s", "restored", "mutation_discharge"),
        (
            "mutation_discharged_at = now(), mutation_discharge_reason = %s",
            "abandoned",
            "mutation_reason",
        ),
    ],
)
def test_database_rejects_a_half_written_obligation(
    migrated_url: str, assignment: str, value: str | None, constraint: str
) -> None:
    """An obligation is discharged with its reason, and never discharged before it is opened.

    No Python-side check stands in front of these: they exist so a writer reaching the table
    directly cannot leave the sweep a row it would read as a discharge that never happened.
    """

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            params = ((value,) if value is not None else ()) + attempt.key
            with pytest.raises(psycopg.errors.CheckViolation, match=constraint):
                async with conn.transaction():
                    await conn.execute(
                        (
                            "UPDATE remote_module_attempt_obligations SET "
                            + assignment
                            + " WHERE system_id = %s AND run_id = %s AND operation_nonce = %s"
                        ).encode(),
                        params,
                    )
            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(attempt, mutation_retained=True, reap_retained=False),
            )

    asyncio.run(_run())


def test_database_rejects_a_key_that_is_not_a_module_operation_nonce(migrated_url: str) -> None:
    """The nonce in the key is the one the sweep parses out of a volume name, so its shape holds."""

    async def _run() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            with pytest.raises(psycopg.errors.CheckViolation, match="attempt_nonce"):
                await conn.execute(
                    "INSERT INTO remote_module_attempt_obligations "
                    "(system_id, run_id, operation_nonce) VALUES (%s, %s, %s)",
                    (system_id, run_id, "not-a-nonce"),
                )

    asyncio.run(_run())


def test_discharge_rejects_a_reason_outside_the_three_adr_0585_events(migrated_url: str) -> None:
    """The caller gets the list of events, not a constraint name, when it names another."""

    async def _run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as conn:
            system_id, run_id = await _seed(conn)
            attempt = _attempt(system_id, run_id)
            await repo.open_mutation_obligation(conn, attempt)
            with pytest.raises(ValueError, match="baseline_committed"):
                await repo.discharge_mutation_obligation(
                    conn,
                    attempt,
                    reason="abandoned",  # ty: ignore[invalid-argument-type]
                )
            assert await repo.retained_owners(conn) == (
                RetainedModuleAttempt(attempt, mutation_retained=True, reap_retained=False),
            )

    asyncio.run(_run())


def test_attempt_rejects_a_nonce_that_is_not_a_module_operation_nonce() -> None:
    """The key is the tuple ADR-0588 encodes into the volume name, so its shape is fixed."""
    for nonce in ("", "0" * 31, "0" * 33, "A" * 32, "g" * 32):
        with pytest.raises(ValueError, match="operation_nonce"):
            ModuleAttempt(system_id=uuid4(), run_id=uuid4(), operation_nonce=nonce)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("terminal_operation_identity", "sha256:zz"),
        ("baseline_result_identity", "b" * 64),
        ("installed_entry_count", 200_001),
        ("installed_content_bytes", -1),
    ],
)
def test_terminal_evidence_rejects_out_of_range_scalars(field: str, value: object) -> None:
    """The baseline counts and digests carry the bounds the recovery reference already places."""
    base = _evidence(_attempt(uuid4(), uuid4()))
    with pytest.raises(ValueError, match=field):
        replace(base, **{field: value})

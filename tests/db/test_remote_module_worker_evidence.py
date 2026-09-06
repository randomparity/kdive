"""Worker-fenced remote-module evidence proofs (ADR-0609, migration 0134)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.errors import InsufficientPrivilege
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    ModuleAttemptRestoredEvidence,
    ModuleAttemptTerminalEvidence,
    ModuleAttemptWorkerWriteContext,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.remote_module_attempt_preparation import ModuleAttemptPreparationRequestV1
from kdive.providers.ports.external_boot import OpaqueProviderRef
from kdive.providers.remote_libvirt.lifecycle.rootfs.remote_module_documents import (
    RemoteModuleOperationV1,
    RemoteModuleRecoveryRefV2,
    RemoteModuleResultV1,
    identity_for,
)
from tests.db.external_boot_authority_support import _RoleDsns
from tests.db.external_boot_authority_support import (
    authority_role_dsns as authority_role_dsns,  # noqa: F401
)
from tests.db.remote_module_attempt_obligations_support import _attempt, _evidence, _seed


async def _seed_worker_job(
    conn: psycopg.AsyncConnection,
    context: ModuleAttemptWorkerWriteContext,
    *,
    state: str = "running",
) -> None:
    receipt = context.preparation.module_attempt_obligation
    await conn.execute(
        "INSERT INTO worker_incarnations "
        "(incarnation, authority_kind, authority_binding, fence_protocol, credential_hash) "
        "VALUES ('module-worker', 'docker', %s, 4, %s)",
        (Jsonb({"container_id": "a" * 64}), context.credential_hash),
    )
    await conn.execute(
        "INSERT INTO jobs "
        "(id, kind, payload, state, attempt, max_attempts, worker_id, lease_expires_at, "
        "authorizing, dedup_key) "
        "VALUES (%s, 'boot', %s, %s, %s, 3, 'module-worker', now() + interval '5 minutes', "
        "%s, %s)",
        (
            context.job_id,
            Jsonb(
                {
                    "run_id": str(receipt.run_id),
                    "remote_module_attempt_v1": context.preparation.model_dump(
                        mode="json", by_alias=True
                    ),
                }
            ),
            state,
            context.job_attempt,
            Jsonb({"principal": "phase-test", "project": "phase-test"}),
            f"module-{context.job_id}",
        ),
    )
    await conn.commit()


def _context(system_id: UUID, run_id: UUID) -> ModuleAttemptWorkerWriteContext:
    preparation = ModuleAttemptPreparationRequestV1.model_validate(
        {
            "module_attempt_obligation": {
                "system_id": system_id,
                "run_id": run_id,
                "operation_nonce": "0" * 32,
            }
        }
    )
    return ModuleAttemptWorkerWriteContext(
        uuid4(), 1, SecretStr("module-worker-credential"), preparation
    )


def _real_evidence(
    attempt: ModuleAttempt,
) -> tuple[ModuleAttemptTerminalEvidence, ModuleAttemptRestoredEvidence]:
    system_id = str(attempt.system_id)
    run_id = str(attempt.run_id)
    digest = "sha256:" + "a" * 64
    manifest = "sha256:" + "b" * 64
    capture = RemoteModuleOperationV1(
        operation="capture_install",
        system_id=system_id,
        run_id=run_id,
        plan_identity=digest,
        operation_nonce=attempt.operation_nonce,
        release="6.12.0",
        root_volume={"key": "system-root", "identity": digest},
        source_manifest=manifest,
        appliance_image_digest=digest,
    )
    installed = RemoteModuleResultV1(
        status="success",
        phase="installed",
        system_id=system_id,
        run_id=run_id,
        plan_identity=digest,
        operation_nonce=attempt.operation_nonce,
        appliance_image_digest=digest,
        release="6.12.0",
        root_volume_key="system-root",
        root_volume_identity=digest,
        source_manifest=manifest,
        installed_manifest=manifest,
        capture_absent=True,
        entry_count=1,
        content_bytes=4096,
    )
    installed.validate_for(capture)
    authority = OpaqueProviderRef(ref="authority/test")
    recovery = RemoteModuleRecoveryRefV2(
        system_id=system_id,
        run_id=run_id,
        plan_identity=digest,
        operation_nonce=attempt.operation_nonce,
        pool=OpaqueProviderRef(ref="modules"),
        root_volume=OpaqueProviderRef(ref="system-root"),
        source_volume=OpaqueProviderRef(ref="source.ext4"),
        scratch_volume=OpaqueProviderRef(ref="scratch.ext4"),
        operation_identity=identity_for(capture),
        result_identity=identity_for(installed),
        installed_entry_count=1,
        installed_content_bytes=4096,
        appliance_image_digest=digest,
        authority_identity=RemoteModuleRecoveryRefV2.identity_for_authority(authority),
        source_capacity_bytes=4096,
    )
    terminal = ModuleAttemptTerminalEvidence(
        terminal_operation=capture.model_dump(mode="json"),
        terminal_operation_identity=identity_for(capture),
        terminal_result=installed.model_dump(mode="json"),
        terminal_result_identity=identity_for(installed),
        baseline_operation_identity=identity_for(capture),
        baseline_result_identity=identity_for(installed),
        installed_entry_count=1,
        installed_content_bytes=4096,
        recovery_reference=recovery.model_dump(mode="json"),
    )
    restore = RemoteModuleOperationV1(
        operation="restore",
        system_id=system_id,
        run_id=run_id,
        plan_identity=digest,
        operation_nonce=attempt.operation_nonce,
        release="6.12.0",
        root_volume={"key": "system-root", "identity": digest},
        source_manifest=manifest,
        installed_manifest=manifest,
        capture_absent=True,
        appliance_image_digest=digest,
    )
    restored = installed.model_copy(
        update={"phase": "restored", "entry_count": None, "content_bytes": None}
    )
    restored.validate_for(restore)
    return terminal, ModuleAttemptRestoredEvidence(
        restored_operation=restore.model_dump(mode="json"),
        restored_operation_identity=identity_for(restore),
        restored_result=restored.model_dump(mode="json"),
        restored_result_identity=identity_for(restored),
    )


def test_worker_evidence_requires_exact_live_job_fence_and_preserves_select_only_grant(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
            attempt = _attempt(system_id, run_id)
            context = _context(system_id, run_id)
            await repo.open_mutation_obligation(admin, attempt)
            await _seed_worker_job(admin, context)

        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            with pytest.raises(InsufficientPrivilege):
                await worker.execute(
                    "UPDATE remote_module_attempt_obligations SET reap_opened_at = now()"
                )
            await worker.rollback()

            stale = ModuleAttemptWorkerWriteContext(
                context.job_id,
                context.job_attempt,
                SecretStr("stale-credential"),
                context.preparation,
            )
            assert (
                await repo.worker_record_terminal_evidence(
                    worker, stale, attempt, _evidence(attempt)
                )
                is False
            )
            assert (
                await repo.worker_record_terminal_evidence(
                    worker, context, attempt, _evidence(attempt)
                )
                is True
            )
            await worker.rollback()
            assert (
                await repo.worker_record_terminal_evidence(
                    worker, context, attempt, _evidence(attempt)
                )
                is True
            )
            await worker.commit()

        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            evidence = await repo.read_terminal_evidence(admin, attempt)
            assert evidence == _evidence(attempt)
            await admin.execute(
                "UPDATE jobs SET state = 'canceled' WHERE id = %s", (context.job_id,)
            )
            await admin.commit()

        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            assert await repo.worker_discharge_reap_obligation(worker, context, attempt) is False

    asyncio.run(run())


def test_worker_persists_immutable_prep_then_restore_before_cleanup(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
            attempt = _attempt(system_id, run_id)
            context = _context(system_id, run_id)
            terminal, restored = _real_evidence(attempt)
            await repo.open_mutation_obligation(admin, attempt)
            await _seed_worker_job(admin, context)

        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            assert await repo.worker_record_terminal_evidence(worker, context, attempt, terminal)
            await worker.commit()
            assert await repo.read_terminal_evidence(worker, attempt) == terminal
            assert await repo.read_restored_evidence(worker, attempt) is None
            assert await repo.reap_obligation_is_open(worker, attempt)

            with pytest.raises(
                psycopg.errors.RaiseException, match="terminal evidence is immutable"
            ):
                async with worker.transaction():
                    await repo.worker_record_terminal_evidence(
                        worker,
                        context,
                        attempt,
                        replace(terminal, installed_content_bytes=8192),
                    )

            mismatch = replace(
                restored,
                restored_operation={
                    **restored.restored_operation,
                    "source_manifest": "sha256:" + "c" * 64,
                },
            )
            with pytest.raises(psycopg.errors.CheckViolation):
                async with worker.transaction():
                    await repo.worker_record_restored_evidence(worker, context, attempt, mismatch)

            assert await repo.worker_record_restored_evidence(worker, context, attempt, restored)
            await worker.commit()
            assert await repo.worker_record_restored_evidence(worker, context, attempt, restored)
            await worker.commit()
            assert await repo.read_restored_evidence(worker, attempt) == restored
            assert await repo.read_terminal_evidence(worker, attempt) == terminal

            with pytest.raises(
                psycopg.errors.RaiseException, match="restored evidence is immutable"
            ):
                async with worker.transaction():
                    await repo.worker_record_restored_evidence(
                        worker,
                        context,
                        attempt,
                        replace(
                            restored,
                            restored_result_identity="sha256:" + "d" * 64,
                        ),
                    )

            assert await repo.worker_discharge_reap_obligation(worker, context, attempt)
            await worker.commit()
            assert not await repo.reap_obligation_is_open(worker, attempt)
            assert await repo.read_terminal_evidence(worker, attempt) == terminal
            assert await repo.read_restored_evidence(worker, attempt) == restored

    asyncio.run(run())


@pytest.mark.parametrize("mismatch", ["job", "attempt", "nonce", "system", "lease"], ids=str)
def test_worker_evidence_rejects_job_and_payload_mismatches(
    migrated_url: str, authority_role_dsns: _RoleDsns, mismatch: str
) -> None:
    async def run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
            attempt = _attempt(system_id, run_id)
            context = _context(system_id, run_id)
            await repo.open_mutation_obligation(admin, attempt)
            await _seed_worker_job(admin, context)
            if mismatch == "lease":
                await admin.execute(
                    "UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s",
                    (context.job_id,),
                )
                await admin.commit()

        receipt = context.preparation.module_attempt_obligation
        if mismatch == "nonce":
            receipt = receipt.model_copy(update={"operation_nonce": "f" * 32})
        elif mismatch == "system":
            receipt = receipt.model_copy(update={"system_id": uuid4()})
        preparation = ModuleAttemptPreparationRequestV1(module_attempt_obligation=receipt)
        candidate = ModuleAttemptWorkerWriteContext(
            uuid4() if mismatch == "job" else context.job_id,
            2 if mismatch == "attempt" else context.job_attempt,
            context.incarnation_credential,
            preparation,
        )
        candidate_attempt = attempt
        if mismatch in {"nonce", "system"}:
            candidate_attempt = _attempt(
                preparation.module_attempt_obligation.system_id,
                preparation.module_attempt_obligation.run_id,
                preparation.module_attempt_obligation.operation_nonce,
            )
        async with await psycopg.AsyncConnection.connect(
            authority_role_dsns("kdive_worker")
        ) as worker:
            assert (
                await repo.worker_record_terminal_evidence(
                    worker, candidate, candidate_attempt, _evidence(candidate_attempt)
                )
                is False
            )

    asyncio.run(run())


def test_worker_evidence_rechecks_lease_after_waiting_for_system_lock(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    async def run() -> None:
        repo = RemoteModuleAttemptObligationRepository()
        async with await psycopg.AsyncConnection.connect(migrated_url) as admin:
            system_id, run_id = await _seed(admin)
            attempt = _attempt(system_id, run_id)
            context = _context(system_id, run_id)
            await repo.open_mutation_obligation(admin, attempt)
            await _seed_worker_job(admin, context)
            await admin.execute(
                "UPDATE jobs SET lease_expires_at = now() + interval '200 milliseconds' "
                "WHERE id = %s",
                (context.job_id,),
            )
            await admin.commit()
            await admin.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || %s::text, 2125))",
                (system_id,),
            )

            async with await psycopg.AsyncConnection.connect(
                authority_role_dsns("kdive_worker")
            ) as worker:
                pending = asyncio.create_task(
                    repo.worker_record_terminal_evidence(
                        worker, context, attempt, _evidence(attempt)
                    )
                )
                await asyncio.sleep(0.3)
                async with await psycopg.AsyncConnection.connect(migrated_url) as observer:
                    row = await (
                        await observer.execute(
                            "SELECT pg_try_advisory_xact_lock(hashtextextended("
                            "'kdive:worker-incarnation:module-worker', 1803))"
                        )
                    ).fetchone()
                    assert row == (True,)
                    await observer.rollback()
                await admin.commit()
                assert await pending is False

    asyncio.run(run())

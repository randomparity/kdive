"""Unit tests for native authority carrier inputs; these are not native evidence."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.mcp.responses import ToolResponse
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle
from tests.live_vm.installed_local_authority_support import (
    CONFIG_ENV,
    NativeAuthorityConfig,
    NormalOperationJobs,
    OwnedResource,
    ResourceLedger,
    assert_root_release_completion,
    drive_normal_operations,
    load_config,
    provision_authority_fixture,
    require_deployed_revision,
    require_fault_barrier,
)


def _document(tmp_path: Path) -> Path:
    path = tmp_path / "carrier.json"
    path.write_text(
        json.dumps(
            {
                "installed_revision": "1" * 40,
                "system_id": str(uuid4()),
                "project": "kdive-2151-project",
                "ownership_prefix": "kdive-2151-" + "1" * 12 + "-" + "2" * 8,
                "authority_service": "kdive-external-boot-authority.service",
                "barrier_socket": "/run/kdive/provider-authority/test-barrier.sock",
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_absent_trigger_is_the_only_skip_state() -> None:
    assert load_config({}) is None


def test_complete_owner_only_config_loads(tmp_path: Path) -> None:
    path = _document(tmp_path)
    config = load_config({CONFIG_ENV: str(path)})
    assert config is not None
    assert config.ownership_prefix.startswith("kdive-2151-")


def test_set_trigger_with_unsafe_or_partial_config_fails(tmp_path: Path) -> None:
    path = _document(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe metadata"):
        load_config({CONFIG_ENV: str(path)})
    path.chmod(0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["system_id"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config({CONFIG_ENV: str(path)})


def test_ledger_rejects_unowned_and_cleans_exact_reverse_order() -> None:
    prefix = "kdive-2151-" + "1" * 12 + "-" + "2" * 8
    ledger = ResourceLedger(prefix)
    domain = OwnedResource(kind="domain", identity=f"{prefix}-domain")
    volume = OwnedResource(kind="volume", identity=f"{prefix}-volume")
    ledger.record(domain)
    ledger.record(volume)
    with pytest.raises(ValueError, match="outside"):
        ledger.record(OwnedResource(kind="domain", identity="unrelated-domain"))
    removed: list[OwnedResource] = []
    ledger.cleanup(removed.append)
    assert removed == [volume, domain]


def test_missing_fault_barrier_fails_loud(tmp_path: Path) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=tmp_path / "absent.sock",
    )
    with pytest.raises(RuntimeError, match="no deterministic provider-effect barrier"):
        require_fault_barrier(config)


def test_normal_driver_uses_public_tools_and_drains_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del tmp_path
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    calls: list[tuple[str, dict[str, object]]] = []
    drained: list[tuple[str, str]] = []

    class Client:
        async def call_tool(self, name: str, **args: object) -> ToolResponse:
            calls.append((name, args))
            ids = {
                "investigations.open": "11111111-1111-1111-1111-111111111111",
                "runs.create": "22222222-2222-2222-2222-222222222222",
                "runs.install": "33333333-3333-3333-3333-333333333333",
                "runs.boot": "44444444-4444-4444-4444-444444444444",
                "runs.release_external_boot": "55555555-5555-5555-5555-555555555555",
            }
            return ToolResponse.success(ids[name], "running")

    async def uploaded(_client: object, *, run_id: str, **_kwargs: object) -> None:
        calls.append(("upload-build", {"run_id": run_id}))

    async def drained_job(
        _client: object, phase_name: str, job_id: str, **_kwargs: object
    ) -> ToolResponse:
        drained.append((phase_name, job_id))
        return ToolResponse.success(job_id, "succeeded")

    monkeypatch.setattr(
        "tests.live_vm.installed_local_authority_support.build_and_upload_kernel", uploaded
    )
    monkeypatch.setattr("tests.live_vm.installed_local_authority_support.drain_job", drained_job)
    ledger = ResourceLedger(config.ownership_prefix)
    result = asyncio.run(drive_normal_operations(cast(Any, Client()), config, ledger))

    assert result == NormalOperationJobs(
        investigation_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        activate_job_id="44444444-4444-4444-4444-444444444444",
        release_job_id="55555555-5555-5555-5555-555555555555",
    )
    assert [name for name, _ in calls] == [
        "investigations.open",
        "runs.create",
        "upload-build",
        "runs.install",
        "runs.boot",
        "runs.release_external_boot",
    ]
    assert [phase for phase, _ in drained] == ["install", "activate", "release"]
    assert [resource.kind for resource in ledger.resources] == ["investigation", "run"]


def test_deployed_revision_uses_the_actual_active_fixed_worker_slot() -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    seen: list[str] = []

    def fetch(url: str) -> dict[str, object]:
        seen.append(url)
        return {"commit": config.installed_revision}

    require_deployed_revision(
        config,
        "http://127.0.0.1:8000/mcp",
        "kdive-live-worker@2.service loaded active running KDIVE retained live worker slot 2",
        fetch=fetch,
    )

    assert seen == ["http://127.0.0.1:9464/readyz", "http://127.0.0.1:9470/readyz"]


@pytest.mark.parametrize(
    ("target", "reported", "message"),
    [
        ("server", None, "deployed server revision"),
        ("worker", {"commit": "2" * 40}, "deployed worker slot 1 revision"),
    ],
)
def test_deployed_revision_rejects_unknown_or_mismatched_build(
    target: str,
    reported: dict[str, object] | None,
    message: str,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )

    def fetch(url: str) -> dict[str, object] | None:
        if target == "server" or url.endswith(":9465/readyz"):
            return reported
        return {"commit": config.installed_revision}

    with pytest.raises(AssertionError, match=message):
        require_deployed_revision(
            config,
            "http://127.0.0.1:8000/mcp",
            "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1",
            fetch=fetch,
        )


def test_native_carrier_checks_deployed_builds_before_fixture_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.live_vm import test_installed_local_authority as carrier

    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    fixture_called = False

    def output(*argv: str) -> str:
        if argv[:3] == ("sudo", "-n", "cat"):
            return config.installed_revision
        if argv[:2] == ("systemctl", "is-active"):
            return "active"
        return "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1"

    async def provision(_db_url: str, _config: NativeAuthorityConfig) -> None:
        nonlocal fixture_called
        fixture_called = True

    def reject_before_mutation(*_args: object) -> None:
        raise AssertionError("deployed worker slot 1 revision 'unknown' does not match")

    monkeypatch.setattr(carrier, "load_config", lambda: config)
    monkeypatch.setattr(carrier, "_output", output)
    monkeypatch.setattr(carrier, "require_issuer", lambda: "issuer")
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://127.0.0.1:8000/mcp")
    monkeypatch.setattr(carrier, "require_deployed_revision", reject_before_mutation)
    monkeypatch.setattr(carrier, "provision_authority_fixture", provision)

    with pytest.raises(AssertionError, match="deployed worker slot 1 revision"):
        carrier.test_installed_local_authority_normal_operations()
    assert not fixture_called


async def _completed_root_release(migrated_url: str) -> NormalOperationJobs:
    """Seed one exact, completed root release with its derived cleanup evidence."""
    vehicle = build_vehicle()
    activate_job_id = uuid4()
    root_authority_id = uuid4()
    digest = "sha256:" + "a" * 64
    cleanup_identity = "sha256:" + "b" * 64
    cleanup_digest = "sha256:" + "c" * 64
    absent_digest = "sha256:" + "d" * 64
    journal_digest = "sha256:" + "e" * 64
    async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
        case = await seed_case(
            conn,
            vehicle,
            purpose="release",
            operation="release",
            activation_state="recovered",
            attempt_state="recovered",
            with_release=True,
        )
        activate_marker = case.marker | {
            "purpose": "activate",
            "operation": "activate",
            "operation_identity": f"activate-{uuid4()}",
        }
        await conn.execute(
            "INSERT INTO jobs (id, kind, payload, state, attempt, max_attempts, worker_id, "
            "authorizing, dedup_key) VALUES (%s, 'boot', %s, 'succeeded', 1, 3, %s, %s, %s)",
            (
                activate_job_id,
                Jsonb(
                    {"run_id": str(vehicle.run_id), "external_boot_authority_v1": activate_marker}
                ),
                case.worker_incarnation,
                Jsonb({"principal": "p", "agent_session": None, "project": "proj"}),
                f"activate-{activate_job_id}",
            ),
        )
        await conn.execute("UPDATE jobs SET state = 'succeeded' WHERE id = %s", (case.job_id,))
        await conn.execute(
            "UPDATE external_boot_activations SET cleanup_complete = true, cleanup_evidence = %s "
            "WHERE id = %s",
            (
                Jsonb(
                    {
                        "schema": "external-boot-cleanup-evidence-v1",
                        "activation_id": str(vehicle.activation_id),
                        "system_id": str(vehicle.system_id),
                        "release_identity": digest,
                        "mode": "ordinary",
                        "completed_at": "2026-09-06T00:00:00Z",
                    }
                ),
                vehicle.activation_id,
            ),
        )
        await conn.execute(
            "INSERT INTO external_boot_authorities "
            "(id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id, "
            "job_attempt, purpose, provider_kind, authority_instance, worker_incarnation, "
            "operation, operation_identity, operation_digest, generation, state, "
            "acknowledged_at, retired_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, 'release', 'local-libvirt', "
            "'authority-vehicle', %s, 'release', 'release-root', %s, 1, 'retired', now(), now())",
            (
                root_authority_id,
                vehicle.system_id,
                case.allocation_id,
                vehicle.activation_id,
                vehicle.run_id,
                vehicle.plan_identity,
                case.job_id,
                case.worker_incarnation,
                digest,
            ),
        )
        await conn.execute(
            "INSERT INTO external_boot_authority_journal_heads "
            "(authority_instance, system_id, sequence, digest, phase, authority_id, generation, "
            "operation_identity, head_record) VALUES (%s, %s, 9, %s, 'terminal', %s, 1, %s, %s)",
            (
                "authority-vehicle",
                vehicle.system_id,
                journal_digest,
                root_authority_id,
                cleanup_identity,
                Jsonb(
                    {
                        "operation": "cleanup",
                        "operation_identity": cleanup_identity,
                        "operation_digest": cleanup_digest,
                        "observation": {"category": "absent", "composite_state": absent_digest},
                    }
                ),
            ),
        )
        await conn.execute(
            "INSERT INTO external_boot_release_cleanup_receipts "
            "(root_authority_id, job_id, job_attempt, activation_id, system_id, run_id, "
            "plan_identity, operation_identity, operation_digest, journal_sequence, "
            "journal_digest, "
            "observed_absent_digest, consumed, consumed_at) "
            "VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s, 9, %s, %s, true, now())",
            (
                root_authority_id,
                case.job_id,
                vehicle.activation_id,
                vehicle.system_id,
                vehicle.run_id,
                vehicle.plan_identity,
                cleanup_identity,
                cleanup_digest,
                journal_digest,
                absent_digest,
            ),
        )
    return NormalOperationJobs(
        investigation_id=str(case.investigation_id),
        run_id=str(vehicle.run_id),
        activate_job_id=str(activate_job_id),
        release_job_id=str(case.job_id),
    )


def test_completed_root_release_requires_exact_terminal_derived_evidence(migrated_url: str) -> None:
    operations = asyncio.run(_completed_root_release(migrated_url))
    asyncio.run(assert_root_release_completion(migrated_url, operations))


@pytest.mark.parametrize("fault", ["failed", "pending", "mismatched-journal"])
def test_completed_root_release_rejects_incomplete_or_mismatched_proof(
    migrated_url: str, fault: str
) -> None:
    async def run() -> None:
        operations = await _completed_root_release(migrated_url)
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            if fault == "failed":
                await conn.execute(
                    "UPDATE jobs SET state = 'failed' WHERE id = %s",
                    (operations.activate_job_id,),
                )
            elif fault == "pending":
                await conn.execute(
                    "UPDATE jobs SET state = 'running' WHERE id = %s",
                    (operations.release_job_id,),
                )
            else:
                await conn.execute(
                    "UPDATE external_boot_authority_journal_heads SET digest = %s",
                    ("sha256:" + "f" * 64,),
                )
        with pytest.raises(AssertionError, match="root release completion"):
            await assert_root_release_completion(migrated_url, operations)

    asyncio.run(run())


def test_fixture_provisioning_passes_only_durable_profile_to_exact_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )

    class Cursor:
        async def __aenter__(self) -> Cursor:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, query: str, params: object) -> None:
            assert "WHERE id = %s AND project = %s" in query
            assert params == (config.system_id, config.project)

        async def fetchone(self) -> tuple[dict[str, object]]:
            return ({"schema_version": 1},)

    class Connection:
        async def __aenter__(self) -> Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def cursor(self) -> Cursor:
            return Cursor()

    async def connect(_dsn: str) -> Connection:
        return Connection()

    seen: dict[str, object] = {}

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen.update(argv=argv, kwargs=kwargs)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
    monkeypatch.setattr(subprocess, "run", run)
    asyncio.run(provision_authority_fixture("postgresql://fixture", config))
    argv = cast(list[str], seen["argv"])
    assert argv[:3] == ["sudo", "-n", "/opt/kdive-provider-authority/.venv/bin/python"]
    assert argv[-1] == str(config.system_id)
    kwargs = cast(dict[str, object], seen["kwargs"])
    assert json.loads(cast(str, kwargs["input"])) == {"schema_version": 1}

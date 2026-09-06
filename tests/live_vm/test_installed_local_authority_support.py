"""Unit tests for native authority carrier inputs; these are not native evidence."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.mcp.responses import ToolResponse
from tests.live_vm.installed_local_authority_support import (
    CONFIG_ENV,
    NativeAuthorityConfig,
    OwnedResource,
    ResourceLedger,
    drive_normal_operations,
    load_config,
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

    assert result == (
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
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

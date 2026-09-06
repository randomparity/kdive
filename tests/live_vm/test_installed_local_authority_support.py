"""Unit tests for native authority carrier inputs; these are not native evidence."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from kdive.mcp.responses import ToolResponse
from tests.jobs.handlers.external_boot.seeding import seed_case
from tests.jobs.handlers.external_boot.vehicle import build_vehicle
from tests.live_vm import installed_local_authority_support as carrier
from tests.live_vm.installed_local_authority_support import (
    CONFIG_ENV,
    NativeAuthorityConfig,
    NormalOperationJobs,
    OwnedResource,
    ResourceLedger,
    RunningJobClaim,
    arm_fault_barrier,
    assert_root_release_completion,
    drive_normal_operations,
    hide_authority_journal_lane,
    load_config,
    provision_authority_fixture,
    release_fault_barrier,
    require_authority_artifact_confinement,
    require_deployed_revision,
    require_fault_barrier,
    require_installed_authority_routes,
    require_journal_inventory_refusal,
    restart_authority_after_fault,
    restore_after_journal_inventory_refusal,
    restore_authority_journal_lane,
    wait_for_fault_barrier,
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
                "barrier_socket": "/run/kdive/provider-authority/proof-control/control.sock",
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


def test_missing_fault_barrier_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=Path("/run/kdive/provider-authority/proof-control/control.sock"),
    )

    def refuse(*_argv: str) -> str:
        raise subprocess.CalledProcessError(1, "proof socket metadata")

    monkeypatch.setattr(carrier, "_output", refuse)
    with pytest.raises(RuntimeError, match="no deterministic provider-effect barrier"):
        require_fault_barrier(config)


def test_fault_barrier_metadata_is_checked_by_root_not_the_denied_control_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=Path("/run/kdive/provider-authority/proof-control/control.sock"),
    )
    calls: list[tuple[str, ...]] = []

    def denied_stat(*_args: object, **_kwargs: object) -> object:
        raise PermissionError("private authority directory")

    def output(*argv: str) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(Path, "stat", denied_stat)
    monkeypatch.setattr(carrier, "_output", output)
    assert require_fault_barrier(config) == config.barrier_socket
    assert len(calls) == 1
    assert calls[0][:4] == ("sudo", "-n", "/usr/bin/python3", "-c")


def test_fault_barrier_client_arms_and_releases_only_the_configured_system(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    socket_path = tmp_path / "control.sock"
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=Path("/run/kdive/provider-authority/proof-control/control.sock"),
    )
    requests: list[dict[str, str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        requests.append(json.loads(cast(bytes, kwargs["input"])))
        response = (
            b'{"state":"armed"}' if requests[-1]["action"] == "arm" else b'{"state":"released"}'
        )
        return subprocess.CompletedProcess(argv, 0, response, b"")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(carrier, "require_fault_barrier", lambda _config: socket_path)
    run_id = str(uuid4())
    arm_fault_barrier(config, run_id, "activate", "after-provider")
    release_fault_barrier(config)

    assert requests == [
        {
            "action": "arm",
            "system_id": str(config.system_id),
            "run_id": run_id,
            "operation": "activate",
            "checkpoint": "after-provider",
        },
        {"action": "release"},
    ]


def test_fault_barrier_config_refuses_caller_selected_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixed authority proof socket"):
        NativeAuthorityConfig(
            installed_revision="1" * 40,
            system_id=uuid4(),
            project="kdive-2151-project",
            ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
            authority_service="kdive-external-boot-authority.service",
            barrier_socket=tmp_path / "caller-selected.sock",
        )


def test_fault_barrier_waits_for_reached_state_then_restarts_only_configured_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    responses = iter(({"state": "armed"}, {"state": "reached"}))
    commands: list[tuple[str, ...]] = []

    monkeypatch.setattr(carrier, "fault_barrier_request", lambda *_args: next(responses))
    monkeypatch.setattr(carrier.time, "sleep", lambda _seconds: None)
    wait_for_fault_barrier(config)

    def output(*argv: str) -> str:
        commands.append(argv)
        return "active" if argv[:2] == ("systemctl", "is-active") else ""

    monkeypatch.setattr(carrier, "_output", output)
    restart_authority_after_fault(config)
    assert commands == [
        ("sudo", "-n", "systemctl", "restart", config.authority_service),
        ("systemctl", "is-active", config.authority_service),
    ]


def test_journal_loss_helper_moves_only_the_configured_lane_and_restores_exact_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    requests: list[dict[str, str]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert argv[:4] == ["sudo", "-n", "/usr/bin/python3", "-c"]
        request = json.loads(cast(bytes, kwargs["input"]))
        requests.append(request)
        response = (
            {"device": "1", "inode": "2", "size": "3", "digest": "sha256:" + "a" * 64}
            if request["action"] == "hide"
            else {"state": "restored"}
        )
        return subprocess.CompletedProcess(argv, 0, json.dumps(response).encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    lane = hide_authority_journal_lane(config)
    restore_authority_journal_lane(config, lane)

    assert requests == [
        {"action": "hide", "system_id": str(config.system_id)},
        {
            "action": "restore",
            "system_id": str(config.system_id),
            "device": "1",
            "inode": "2",
            "size": "3",
            "digest": "sha256:" + "a" * 64,
        },
    ]


def test_journal_lane_hold_is_outside_the_inventoried_journal_root() -> None:
    """The startup refusal must be caused by the absent lane, not an unknown hold file."""
    assert 'hold_root = "/var/lib/kdive/provider-authority"' in carrier._JOURNAL_LANE_CLIENT
    assert "dst_dir_fd=hold_fd" in carrier._JOURNAL_LANE_CLIENT
    assert "src_dir_fd=hold_fd, dst_dir_fd=root_fd" in carrier._JOURNAL_LANE_CLIENT
    assert "os.fsync(root_fd)\n        os.fsync(hold_fd)" in carrier._JOURNAL_LANE_CLIENT


def test_journal_loss_helper_refuses_an_unverified_or_unrestored_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, b'{"device":"1","inode":"2"}', b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AssertionError, match="journal lane proof is malformed"):
        hide_authority_journal_lane(config)


def test_journal_refusal_requires_the_inventory_mismatch_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, b"", b"other failure"),
    )
    monkeypatch.setattr(carrier, "_output", lambda *_argv: "other-failure")

    with pytest.raises(AssertionError, match="inventory-mismatch"):
        require_journal_inventory_refusal(config)


def test_journal_restoration_stops_a_failed_authority_before_restoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    proof = carrier.JournalLaneProof("1", "2", "3", "sha256:" + "a" * 64)
    events: list[str] = []

    monkeypatch.setattr(
        carrier, "require_journal_inventory_refusal", lambda _config: events.append("refused")
    )
    monkeypatch.setattr(
        carrier, "stop_authority_after_inventory_refusal", lambda _config: events.append("stopped")
    )

    def restore(_config: NativeAuthorityConfig, _proof: carrier.JournalLaneProof) -> None:
        assert events == ["refused", "stopped"], "restore raced the failed authority"
        events.append("restored")

    def restart(_config: NativeAuthorityConfig) -> None:
        assert events == ["refused", "stopped", "restored"], "restart preceded restoration"
        events.append("restarted")

    monkeypatch.setattr(
        carrier,
        "restore_authority_journal_lane",
        restore,
    )
    monkeypatch.setattr(
        carrier,
        "restart_authority_after_fault",
        restart,
    )

    restore_after_journal_inventory_refusal(config, proof)

    assert events == ["refused", "stopped", "restored", "restarted"]


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
        resolve=lambda commit: commit,
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
            resolve=lambda commit: commit,
        )


def test_deployed_revision_resolves_the_actual_checkout_abbreviation() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    config = NativeAuthorityConfig(
        installed_revision=head,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )

    require_deployed_revision(
        config,
        "http://127.0.0.1:8000/mcp",
        "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1",
        fetch=lambda _url: {"commit": head[:12]},
    )


def test_installed_route_preflight_requires_exact_active_worker_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    calls: list[tuple[str, ...]] = []

    def output(*argv: str) -> str:
        calls.append(argv)
        return "route-ok"

    monkeypatch.setattr(carrier, "_output", output)
    require_installed_authority_routes(
        config,
        "kdive-live-worker@2.service loaded active running KDIVE retained live worker slot 2",
    )

    assert calls[0][:4] == ("sudo", "-n", "/usr/bin/python3", "-c")
    assert calls[0][-1] == "2"
    assert 'source_root = Path("/opt/kdive")' in carrier._INSTALLED_ROUTE_PREFLIGHT
    assert 'python = source_root / ".venv/bin/python"' in carrier._INSTALLED_ROUTE_PREFLIGHT
    assert "resolved_python = python.resolve(strict=True)" in carrier._INSTALLED_ROUTE_PREFLIGHT
    assert "entry.stat().st_uid != owner_uid" in carrier._INSTALLED_ROUTE_PREFLIGHT


def test_installed_route_preflight_rejects_an_unexpected_root_helper_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    monkeypatch.setattr(carrier, "_output", lambda *_argv: "other")

    with pytest.raises(AssertionError, match="route preflight"):
        require_installed_authority_routes(
            config,
            "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1",
        )


def test_exact_worker_hold_requires_retained_invocation_and_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = RunningJobClaim(
        "local-systemd:kdive-live-worker@2.service:" + "a" * 32,
        1,
        datetime.now(UTC),
        datetime.now(UTC),
    )
    seen: list[tuple[str, ...]] = []
    monkeypatch.setattr(carrier, "_output", lambda *argv: seen.append(argv) or "stopped")

    carrier.set_exact_worker_hold(claim, "stop")

    assert seen[0][-2:] == (claim.worker_id, "stop")
    assert 'state.get("phase") != "started"' in carrier._WORKER_HOLD_CLIENT
    assert 'fields["InvocationID"] != state["invocation_id"]' in carrier._WORKER_HOLD_CLIENT
    assert "signal.pidfd_send_signal" in carrier._WORKER_HOLD_CLIENT


def test_identity_probe_limits_mutation_to_exact_authority_sentinels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit coverage mocks subprocesses; only the marked native carrier enforces uid ACLs."""
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    calls: list[tuple[list[str], list[dict[str, str]]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        entries = json.loads(cast(str, kwargs["input"]))
        calls.append((argv, entries))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    require_authority_artifact_confinement(
        config,
        "kdive-live-worker@2.service loaded active running KDIVE retained live worker slot 2",
    )

    assert [call[0][:4] for call in calls[:2]] == [
        ["sudo", "-n", "-u", "kdive-provider-authority"],
        ["sudo", "-n", "-u", "kdive-worker-2"],
    ]
    assert calls[2][0][:2] == ["/usr/bin/python3", "-c"]
    assert calls[3][0][:4] == ["sudo", "-n", "-u", "kdive-provider-authority"]
    for _argv, entries in calls:
        assert {entry["artifact"] for entry in entries} == {
            f"/var/lib/kdive/provider-authority/rootfs/{config.system_id}-overlay.qcow2",
            f"/var/lib/kdive/provider-authority/console/{config.system_id}.log",
        }
        for entry in entries:
            assert config.ownership_prefix in entry["sentinel"]
            assert config.ownership_prefix in entry["replacement"]


def test_identity_probe_fails_when_a_subordinate_identity_can_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    calls = 0

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            argv, 1 if calls == 2 else 0, "", "opened private artifact"
        )

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AssertionError, match="kdive-worker-1 bypass probe failed"):
        require_authority_artifact_confinement(
            config,
            "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1",
        )
    assert calls == 3  # authority creates and removes only its two exact sentinel names.


def test_native_carrier_checks_deployed_builds_before_fixture_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        carrier.run_installed_local_authority_normal_operations()
    assert not fixture_called


def test_native_carrier_probes_identities_after_fixture_before_public_mcp_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    events: list[str] = []

    def output(*argv: str) -> str:
        if argv[:3] == ("sudo", "-n", "cat"):
            return config.installed_revision
        if argv[:2] == ("systemctl", "is-active"):
            return "active"
        return "kdive-live-worker@1.service loaded active running KDIVE retained live worker slot 1"

    async def provision(_db_url: str, _config: NativeAuthorityConfig) -> None:
        events.append("fixture")

    def probe(_config: NativeAuthorityConfig, workers: str) -> None:
        assert "kdive-live-worker@1.service" in workers
        events.append("identity-probe")

    class StopBeforeMcpClient:
        @staticmethod
        def over_http(_base_url: str, _token: str) -> object:
            assert events == ["fixture", "identity-probe"]
            raise RuntimeError("stop before public MCP mutation")

    monkeypatch.setattr(carrier, "load_config", lambda: config)
    monkeypatch.setattr(carrier, "_output", output)
    monkeypatch.setattr(carrier, "require_issuer", lambda: "issuer")
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://127.0.0.1:8000/mcp")
    monkeypatch.setattr(carrier, "require_deployed_revision", lambda *_args: None)
    monkeypatch.setattr(carrier, "require_installed_authority_routes", lambda *_args: None)
    monkeypatch.setattr(carrier, "provision_authority_fixture", provision)
    monkeypatch.setattr(carrier, "require_authority_artifact_confinement", probe)
    monkeypatch.setattr(carrier, "LiveStackClient", StopBeforeMcpClient)
    monkeypatch.setattr(carrier, "mint_role_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://fixture")

    with pytest.raises(RuntimeError, match="stop before public MCP mutation"):
        carrier.run_installed_local_authority_normal_operations()


def test_native_route_preflight_fails_before_fixture_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    def reject_route(*_args: object) -> None:
        raise RuntimeError("installed server authority route is incomplete")

    monkeypatch.setattr(carrier, "load_config", lambda: config)
    monkeypatch.setattr(carrier, "_output", output)
    monkeypatch.setattr(carrier, "require_issuer", lambda: "issuer")
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://127.0.0.1:8000/mcp")
    monkeypatch.setattr(carrier, "require_deployed_revision", lambda *_args: None)
    monkeypatch.setattr(carrier, "require_installed_authority_routes", reject_route)
    monkeypatch.setattr(carrier, "provision_authority_fixture", provision)

    with pytest.raises(RuntimeError, match="authority route is incomplete"):
        carrier.run_installed_local_authority_normal_operations()
    assert not fixture_called


@pytest.mark.parametrize("restart_recovery", [False, True])
def test_native_carriers_supply_the_required_cleanup_summary(
    monkeypatch: pytest.MonkeyPatch, restart_recovery: bool
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
    )
    investigation_id = f"{config.ownership_prefix}-investigation"
    run_id = str(uuid4())
    cleanup_calls: list[tuple[str, dict[str, object]]] = []
    armed: list[tuple[NativeAuthorityConfig, str, str, str]] = []

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def call_tool(self, name: str, **kwargs: object) -> ToolResponse:
            cleanup_calls.append((name, kwargs))
            return ToolResponse.success(investigation_id, "closed")

    class ClientFactory:
        @staticmethod
        def over_http(_base_url: str, _token: str) -> Client:
            return Client()

    async def provision(_db_url: str, _config: NativeAuthorityConfig) -> None:
        return None

    async def completed(_db_url: str, _operations: NormalOperationJobs) -> None:
        return None

    async def normal(
        _client: object, _config: NativeAuthorityConfig, ledger: ResourceLedger
    ) -> NormalOperationJobs:
        ledger.record(OwnedResource(kind="investigation", identity=investigation_id))
        return NormalOperationJobs(investigation_id, run_id, str(uuid4()), str(uuid4()))

    async def activation(
        _client: object,
        _config: NativeAuthorityConfig,
        ledger: ResourceLedger,
        **_kwargs: object,
    ) -> carrier.ActivationJob:
        ledger.record(OwnedResource(kind="investigation", identity=investigation_id))
        before_activate = cast(Callable[[str], None], _kwargs["before_activate"])
        before_activate(run_id)
        return carrier.ActivationJob(investigation_id, run_id, str(uuid4()))

    async def success(*_args: object, **_kwargs: object) -> ToolResponse:
        return ToolResponse.success(str(uuid4()), "succeeded")

    monkeypatch.setattr(carrier, "load_config", lambda: config)
    monkeypatch.setattr(
        carrier,
        "_output",
        lambda *argv: (
            config.installed_revision
            if argv[:3] == ("sudo", "-n", "cat")
            else (
                "active"
                if argv[:2] == ("systemctl", "is-active")
                else "kdive-live-worker@1.service loaded active running KDIVE slot 1"
            )
        ),
    )
    monkeypatch.setattr(carrier, "require_issuer", lambda: "issuer")
    monkeypatch.setattr(carrier, "require_stack", lambda: "http://127.0.0.1:8000/mcp")
    monkeypatch.setattr(carrier, "require_deployed_revision", lambda *_args: None)
    monkeypatch.setattr(carrier, "require_installed_authority_routes", lambda *_args: None)
    monkeypatch.setattr(carrier, "mint_role_token", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(carrier, "provision_authority_fixture", provision)
    monkeypatch.setattr(carrier, "require_authority_artifact_confinement", lambda *_args: None)
    monkeypatch.setattr(carrier, "LiveStackClient", ClientFactory)
    monkeypatch.setattr(carrier, "assert_root_release_completion", completed)
    monkeypatch.setattr(carrier, "drive_normal_operations", normal)
    monkeypatch.setattr(carrier, "require_fault_barrier", lambda *_args: None)
    monkeypatch.setattr(
        carrier,
        "arm_fault_barrier",
        lambda *args: armed.append(cast(tuple[NativeAuthorityConfig, str, str, str], args)),
    )
    monkeypatch.setattr(carrier, "start_external_boot_activation", activation)
    monkeypatch.setattr(carrier, "wait_for_fault_barrier", lambda *_args: None)
    monkeypatch.setattr(carrier, "restart_authority_after_fault", lambda *_args: None)
    monkeypatch.setattr(carrier, "drain_job", success)
    monkeypatch.setattr(carrier, "scalar", success)
    monkeypatch.setenv("KDIVE_DATABASE_URL", "postgresql://fixture")

    if restart_recovery:
        carrier.run_installed_local_authority_restart_recovery()
    else:
        carrier.run_installed_local_authority_normal_operations()

    assert cleanup_calls == [
        (
            "investigations.close",
            {
                "investigation_id": investigation_id,
                "summary": "Native authority proof cleanup",
            },
        )
    ]
    assert armed == ([(config, run_id, "activate", "after-provider")] if restart_recovery else [])


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


def test_fixture_verification_uses_the_explicit_read_only_script_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = NativeAuthorityConfig(
        installed_revision="1" * 40,
        system_id=uuid4(),
        project="kdive-2151-project",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        authority_service="kdive-external-boot-authority.service",
        fixture_mode="verify-existing",
    )

    class Cursor:
        async def __aenter__(self) -> Cursor:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _query: str, _params: object) -> None:
            return None

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

    assert cast(list[str], seen["argv"])[-2:] == ["--verify-existing", str(config.system_id)]


def test_fixture_subprocess_snapshots_authority_uri_and_roots_before_provider_import() -> None:
    """Exercise the script import boundary; this does not provision a native fixture."""
    repository = Path(__file__).resolve().parents[2]
    script = repository / "scripts/live-vm/provision-authority-fixture.py"
    probe = f"""
import importlib.util
import json
import sys
import types
from uuid import UUID

seen = []
libvirt = types.ModuleType('libvirt')
libvirt.open = seen.append
libvirt.libvirtError = RuntimeError
libvirt.VIR_ERR_NO_DOMAIN = 42
sys.modules['libvirt'] = libvirt
spec = importlib.util.spec_from_file_location('fixture_probe', {str(script)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
provisioner = module.LocalLibvirtProvisioning.from_env()
provisioner._connect()
print(json.dumps({{
    'uri': seen[0],
    'rootfs': module.overlay_path(UUID('11111111-1111-1111-1111-111111111111')),
    'console': str(module.console_log_path(UUID('11111111-1111-1111-1111-111111111111'))),
}}))
"""
    env = os.environ | {
        "KDIVE_LIBVIRT_URI": "qemu:///wrong-before-fixture-import",
        "KDIVE_LIBVIRT_ROOTFS_ROOT": "/tmp/wrong-root-before-fixture-import",
        "KDIVE_LIBVIRT_CONSOLE_ROOT": "/tmp/wrong-console-before-fixture-import",
        "PYTHONPATH": str(repository),
    }
    result = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        cwd=repository,
        env=env,
    )

    assert json.loads(result.stdout) == {
        "uri": "qemu+unix:///session?socket=/run/kdive/provider-authority/libvirt/libvirt-sock",
        "rootfs": "/var/lib/kdive/provider-authority/rootfs/"
        "11111111-1111-1111-1111-111111111111-overlay.qcow2",
        "console": "/var/lib/kdive/provider-authority/console/"
        "11111111-1111-1111-1111-111111111111.log",
    }

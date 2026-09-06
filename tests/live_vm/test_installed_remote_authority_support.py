"""Unit tests for remote installed-authority proof inputs; not live evidence."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest

from tests.live_vm.installed_remote_authority_support import (
    CONFIG_ENV,
    RemoteAuthorityProofConfig,
    fault_barrier_request,
    load_config,
    restart_authority_after_fault,
)


def _document(tmp_path: Path, **overrides: object) -> Path:
    value: dict[str, object] = {
        "installed_revision": "1" * 40,
        "project": "kdive-2151-project",
        "resource_name": "remote-proof-x86",
        "authority_instance": "authority-remote-1",
        "ownership_prefix": "kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        "ssh_target": "authority-proof-host",
        "authority_service": "kdive-external-boot-authority.service",
        "barrier_socket": "/run/kdive/provider-authority/proof-control/control.sock",
    }
    value.update(overrides)
    path = tmp_path / "remote-authority.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_absent_trigger_is_the_only_skip_state() -> None:
    assert load_config({}) is None


def test_complete_owner_only_closed_config_loads(tmp_path: Path) -> None:
    config = load_config({CONFIG_ENV: str(_document(tmp_path))})
    assert config is not None
    assert config.resource_name == "remote-proof-x86"
    assert config.authority_instance == "authority-remote-1"


def test_unsafe_partial_or_open_config_fails(tmp_path: Path) -> None:
    path = _document(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="unsafe metadata"):
        load_config({CONFIG_ENV: str(path)})

    path = _document(tmp_path)
    value = json.loads(path.read_text(encoding="utf-8"))
    del value["resource_name"]
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ValueError):
        load_config({CONFIG_ENV: str(path)})

    with pytest.raises(ValueError):
        load_config({CONFIG_ENV: str(_document(tmp_path, connection_uri="qemu:///system"))})

    with pytest.raises(ValueError, match="fixed authority proof socket"):
        load_config({CONFIG_ENV: str(_document(tmp_path, barrier_socket="/tmp/control.sock"))})


def test_hardlinked_config_fails_owner_only_metadata_check(tmp_path: Path) -> None:
    path = _document(tmp_path)
    alias = tmp_path / "remote-authority-alias.json"
    alias.hardlink_to(path)
    with pytest.raises(ValueError, match="unsafe metadata"):
        load_config({CONFIG_ENV: str(path)})


@pytest.mark.parametrize("ssh_target", ["host name", "-oProxyCommand=bad", "host/other"])
def test_config_refuses_non_atomic_ssh_destinations(tmp_path: Path, ssh_target: str) -> None:
    with pytest.raises(ValueError):
        load_config({CONFIG_ENV: str(_document(tmp_path, ssh_target=ssh_target))})


def _config() -> RemoteAuthorityProofConfig:
    return RemoteAuthorityProofConfig(
        installed_revision="1" * 40,
        project="kdive-2151-project",
        resource_name="remote-proof-x86",
        authority_instance="authority-remote-1",
        ownership_prefix="kdive-2151-" + "1" * 12 + "-" + "2" * 8,
        ssh_target="authority-proof-host",
        authority_service="kdive-external-boot-authority.service",
        barrier_socket=Path("/run/kdive/provider-authority/proof-control/control.sock"),
    )


def test_fault_barrier_uses_only_fixed_bounded_remote_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((argv, kwargs))
        stdout = b"" if len(calls) == 1 else b'{"state":"armed"}'
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)
    response = fault_barrier_request(
        _config(),
        {
            "action": "arm",
            "system_id": "11111111-1111-1111-1111-111111111111",
            "run_id": "22222222-2222-2222-2222-222222222222",
            "operation": "activate",
            "checkpoint": "after-provider",
        },
    )
    assert response == {"state": "armed"}
    assert len(calls) == 2
    for argv, kwargs in calls:
        assert argv[:7] == [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            "authority-proof-host",
        ]
        assert argv[7].startswith("sudo -n /usr/bin/python3 -c ")
        assert kwargs["timeout"] == 15
        assert kwargs["capture_output"] is True
        assert kwargs["check"] is False
    request = json.loads(cast(bytes, calls[1][1]["input"]))
    assert request["operation"] == "activate"
    assert "/run/kdive/provider-authority/proof-control/control.sock" in calls[1][0][7]


def test_fault_barrier_rejects_oversized_requests_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("oversized input reached ssh"),
    )
    with pytest.raises(ValueError, match="byte limit"):
        fault_barrier_request(_config(), {"action": "arm", "padding": "x" * 1024})


@pytest.mark.parametrize("stdout", [b"not-json", b'[{"state":"armed"}]', b"x" * 1025])
def test_fault_barrier_rejects_malformed_or_oversized_responses(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    calls = 0

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(argv, 0, b"" if calls == 1 else stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(AssertionError):
        fault_barrier_request(_config(), {"action": "status"})


def test_restart_targets_only_the_fixed_authority_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        commands.append(argv[-1])
        stdout = b"active\n" if "is-active" in argv[-1] else b""
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(subprocess, "run", run)
    restart_authority_after_fault(_config())
    assert len(commands) == 2
    assert "systemctl restart kdive-external-boot-authority.service" in commands[0]
    assert "systemctl is-active kdive-external-boot-authority.service" in commands[1]

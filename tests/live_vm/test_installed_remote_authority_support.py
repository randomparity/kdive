"""Unit tests for remote installed-authority proof inputs; not live evidence."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.live_vm import installed_remote_authority_support as carrier
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
    calls: list[tuple[tuple[str, ...], bytes, float, int, int]] = []

    def run(
        argv: tuple[str, ...],
        *,
        payload: bytes,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
    ) -> tuple[bytes, bytes, int]:
        calls.append((argv, payload, timeout, stdout_limit, stderr_limit))
        return (b"" if len(calls) == 1 else b'{"state":"armed"}', b"", 0)

    monkeypatch.setattr(carrier, "_run_bounded_command", run)
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
    for argv, _payload, timeout, stdout_limit, stderr_limit in calls:
        assert argv[:7] == (
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "--",
            "authority-proof-host",
        )
        assert argv[7].startswith("sudo -n /usr/bin/python3 -c ")
        assert timeout == 15
        assert stdout_limit == 1024
        assert stderr_limit == 1024
    request = json.loads(calls[1][1])
    assert request["operation"] == "activate"
    assert "/run/kdive/provider-authority/proof-control/control.sock" in calls[1][0][7]


def test_fault_barrier_rejects_oversized_requests_without_ssh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        carrier,
        "_run_bounded_command",
        lambda *_args, **_kwargs: pytest.fail("oversized input reached ssh"),
    )
    with pytest.raises(ValueError, match="byte limit"):
        fault_barrier_request(_config(), {"action": "arm", "padding": "x" * 1024})


@pytest.mark.parametrize("stdout", [b"not-json", b'[{"state":"armed"}]', b"x" * 1025])
def test_fault_barrier_rejects_malformed_or_oversized_responses(
    monkeypatch: pytest.MonkeyPatch, stdout: bytes
) -> None:
    calls = 0

    def run(_argv: tuple[str, ...], **_kwargs: object) -> tuple[bytes, bytes, int]:
        nonlocal calls
        calls += 1
        return (b"" if calls == 1 else stdout, b"", 0)

    monkeypatch.setattr(carrier, "_run_bounded_command", run)
    with pytest.raises(AssertionError):
        fault_barrier_request(_config(), {"action": "status"})


def test_restart_targets_only_the_fixed_authority_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def run(argv: tuple[str, ...], **_kwargs: object) -> tuple[bytes, bytes, int]:
        commands.append(argv[-1])
        stdout = b"active\n" if "is-active" in argv[-1] else b""
        return stdout, b"", 0

    monkeypatch.setattr(carrier, "_run_bounded_command", run)
    restart_authority_after_fault(_config())
    assert len(commands) == 2
    assert "systemctl restart kdive-external-boot-authority.service" in commands[0]
    assert "systemctl is-active kdive-external-boot-authority.service" in commands[1]


def test_fault_barrier_client_reads_a_fragmented_header(tmp_path: Path) -> None:
    response = b'{"state":"armed"}'
    (tmp_path / "socket.py").write_text(
        """
AF_UNIX = 1
SOCK_STREAM = 1

class FakeSocket:
    def __init__(self):
        response = b'{"state":"armed"}'
        header = len(response).to_bytes(4, 'big')
        self.chunks = [header[:1], header[1:3], header[3:], response]
    def settimeout(self, _timeout): pass
    def connect(self, _path): pass
    def sendall(self, _payload): pass
    def recv(self, maximum):
        chunk = self.chunks.pop(0) if self.chunks else b''
        assert len(chunk) <= maximum
        return chunk
    def close(self): pass

def socket(_family, _kind):
    return FakeSocket()
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-c", carrier._FAULT_BARRIER_CLIENT],
        input=b'{"action":"status"}',
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        timeout=5,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.stdout == response


def _wait_for_process_exit(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    os.kill(pid, signal.SIGKILL)
    pytest.fail(f"bounded command left process {pid} running")


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_bounded_command_reaps_a_child_on_output_overflow(tmp_path: Path, stream: str) -> None:
    pid_file = tmp_path / "pid"
    program = (
        "import os,pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        f"sys.{stream}.buffer.write(b'x' * 2048); sys.{stream}.flush(); time.sleep(60)"
    )
    with pytest.raises(AssertionError, match=f"{stream}.*oversized"):
        carrier._run_bounded_command(
            (sys.executable, "-c", program, str(pid_file)),
            payload=b"",
            timeout=5,
            stdout_limit=1024,
            stderr_limit=1024,
        )
    _wait_for_process_exit(int(pid_file.read_text(encoding="utf-8")))


def test_bounded_command_timeout_terminates_and_reaps_the_process_group(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "pids"
    program = (
        "import os,pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}'); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        carrier._run_bounded_command(
            (sys.executable, "-c", program, str(pid_file)),
            payload=b"",
            timeout=1,
            stdout_limit=1024,
            stderr_limit=1024,
        )
    parent, child = (int(value) for value in pid_file.read_text(encoding="utf-8").split())
    _wait_for_process_exit(parent)
    _wait_for_process_exit(child)

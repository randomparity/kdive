"""Structural checks on the shipped systemd units.

`systemd-analyze verify` needs systemd and is environment-gated; these unit-file
assertions run everywhere and lock in the backend-retry contract (ADR-0114 §4).
"""

from __future__ import annotations

from pathlib import Path

import pytest

SYSTEM = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "system"
SERVICES = ("kdive-server", "kdive-worker", "kdive-reconciler")
LIVE_WORKER = SYSTEM / "kdive-live-worker@.service"
LIVE_SOCKET = SYSTEM / "kdive-live-worker-lifecycle.socket"
LIVE_SERVICE = SYSTEM / "kdive-live-worker-lifecycle@.service"
AUTHORITY_SERVICE = SYSTEM / "kdive-external-boot-authority.service"


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_has_retry_contract(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    assert "Restart=on-failure" in text
    assert "RestartSec=" in text
    assert "After=network-online.target" in text
    assert "EnvironmentFile=" in text
    assert "User=kdive" in text


@pytest.mark.parametrize("name", SERVICES)
def test_system_unit_exec_matches_process(name: str) -> None:
    text = (SYSTEM / f"{name}.service").read_text()
    process = name.removeprefix("kdive-")
    assert f"-m kdive {process}" in text


def test_live_worker_unit_pins_retained_slot_contract() -> None:
    text = LIVE_WORKER.read_text(encoding="utf-8")
    expected = (
        "User=kdive-worker-%i",
        "SupplementaryGroups=kdive-live-libvirt",
        "EnvironmentFile=/var/lib/kdive/live-workers/slots/%i/worker.env",
        "LoadCredential=worker-incarnation:"
        "/var/lib/kdive/live-workers/slots/%i/worker-incarnation.credential",
        "ExecStart=/usr/local/libexec/kdive-live-worker-gate %i",
        "Restart=no",
        "KillMode=control-group",
        "ExitType=cgroup",
        "RemainAfterExit=yes",
    )
    for directive in expected:
        assert directive in text
    assert "\nUMask=" not in text


def test_live_lifecycle_socket_accepts_two_root_control_connections() -> None:
    text = LIVE_SOCKET.read_text(encoding="utf-8")
    for directive in (
        "ListenStream=/run/kdive/live-worker-lifecycle.sock",
        "SocketUser=root",
        "SocketGroup=kdive-live-control",
        "SocketMode=0660",
        "Accept=yes",
        "MaxConnections=2",
    ):
        assert directive in text


def test_live_lifecycle_service_uses_only_installed_root_witness() -> None:
    text = LIVE_SERVICE.read_text(encoding="utf-8")
    expected = (
        "User=root",
        "Group=root",
        "EnvironmentFile=/etc/kdive/live-worker-lifecycle.conf",
        "LoadCredential=witness-dsn:/etc/kdive/credentials/live-worker-witness.dsn",
        "StandardInput=socket",
        "StandardOutput=socket",
        "ExecStart=/usr/local/libexec/kdive-live-worker-lifecycle",
    )
    for directive in expected:
        assert directive in text
    assert text.count("ExecStart=") == 1


def test_external_boot_authority_unit_is_isolated_and_supervised() -> None:
    text = AUTHORITY_SERVICE.read_text(encoding="utf-8")
    for directive in (
        "Type=notify",
        "NotifyAccess=main",
        "User=kdive-provider-authority",
        "Group=kdive-provider-authority",
        "EnvironmentFile=/etc/kdive/provider-authority.env",
        "LoadCredential=database-dsn:/etc/kdive/credentials/provider-authority/database-dsn",
        "LoadCredential=service-credential:/etc/kdive/credentials/provider-authority/service-credential",
        "LoadCredential=server-certificate:/etc/kdive/credentials/provider-authority/server-certificate",
        "LoadCredential=server-ca:/etc/kdive/credentials/provider-authority/server-ca",
        "LoadCredential=worker-client-ca:/etc/kdive/credentials/provider-authority/worker-client-ca",
        "LoadCredential=health-client-certificate:/etc/kdive/credentials/provider-authority/health-client-certificate",
        "LoadCredential=health-client-key:/etc/kdive/credentials/provider-authority/health-client-key",
        "ExecStart=/opt/kdive-provider-authority/.venv/bin/python "
        "-m kdive external-boot-authority-host",
        "Restart=on-failure",
        "RestartSec=5s",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateTmp=yes",
        "PrivateDevices=yes",
        "RestrictAddressFamilies=AF_UNIX",
        "ReadWritePaths=/var/lib/kdive/provider-authority/journal",
        "ReadWritePaths=/run/kdive/provider-authority/request",
    ):
        assert directive in text
    assert "service-credential:" in text
    assert "sentinel" not in text

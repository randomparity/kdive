"""Fail-closed readiness checks for the external-boot authority host."""

from __future__ import annotations

import asyncio
import fcntl
import os
import socket
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from kdive.db.external_boot_authority_journal import JournalHead
from kdive.providers.external_boot_authority import host
from kdive.providers.external_boot_authority import settings as authority_settings
from kdive.providers.external_boot_authority.host import (
    AuthorityHostConfig,
    HostReadinessError,
    check_probe_socket,
    restore_journal_inventory,
    run_authority_host,
    validate_credential_paths,
)
from kdive.providers.external_boot_authority.protocol import (
    AuthorityOperation,
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
    record_digest,
)


def _file(path: Path, *, mode: int, content: str = "value") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _config(tmp_path: Path) -> AuthorityHostConfig:
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    return AuthorityHostConfig(
        authority_instance="authority-a",
        authority_uid=os.geteuid(),
        journal_dir=journal,
        request_socket=tmp_path / "request" / "authority.sock",
        provider_socket=tmp_path / "libvirt.sock",
        database_dsn=_file(tmp_path / "database-dsn", mode=0o400),
        server_private_key=_file(tmp_path / "service-credential", mode=0o400),
        server_certificate=_file(tmp_path / "server-certificate", mode=0o400),
        server_ca=_file(tmp_path / "server-ca", mode=0o400),
        worker_client_ca=_file(tmp_path / "worker-client-ca", mode=0o400),
        health_client_certificate=_file(tmp_path / "health-client-certificate", mode=0o400),
        health_client_key=_file(tmp_path / "health-client-key", mode=0o400),
    )


def test_host_rejects_unsafe_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    validate_credential_paths(config)
    config.server_certificate.chmod(0o444)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)
    config.server_certificate.chmod(0o400)
    config.database_dsn.chmod(0o600)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)
    config.database_dsn.unlink()
    config.database_dsn.symlink_to(config.server_private_key)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)


def test_host_rejects_invalid_journal_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.journal_dir.chmod(0o777)
    with pytest.raises(HostReadinessError, match="journal: unsafe-tree"):
        restore_journal_inventory(config, ())


class _Cursor:
    def __init__(self, row: object) -> None:
        self.row = row

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object) -> None:
        return None

    async def fetchone(self) -> object:
        return self.row


class _Connection:
    def __init__(self, row: object) -> None:
        self.row = row

    def cursor(self) -> _Cursor:
        return _Cursor(self.row)


def test_host_rejects_privileged_database_role() -> None:
    privileged = (
        "kdive-provider-authority",
        True,
        True,
        True,
        False,
        False,
        False,
        False,
        ("kdive_provider_authority",),
        True,
        True,
        False,
    )
    with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
        asyncio.run(host.check_database_role(_Connection(privileged)))


def test_host_diagnostics_are_bounded_and_secret_free() -> None:
    unsafe = "sensitive-password sensitive-token"
    error = HostReadinessError(
        "database-role-with-an-unbounded-component" * 20,
        unsafe,
    )
    rendered = str(error)
    assert len(rendered.encode()) <= 192
    assert "sensitive-password" not in rendered
    assert "sensitive-token" not in rendered


def _head(system_id=None) -> JournalHead:
    return JournalHead(
        authority_instance="authority-a",
        system_id=system_id or uuid4(),
        sequence=1,
        digest="sha256:" + "1" * 64,
        phase=JournalPhase.WATERMARK_INSTALLED,
        authority_id=uuid4(),
        generation=1,
        operation_identity="operation-a",
        pending_takeover=None,
        suspended_operation=None,
    )


def test_host_rejects_missing_or_extra_lane(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(HostReadinessError, match="journal: inventory-mismatch"):
        restore_journal_inventory(config, (_head(),))
    extra = config.journal_dir / f"{uuid4()}.jsonl"
    extra.touch(mode=0o600)
    with pytest.raises(HostReadinessError, match="journal: inventory-mismatch"):
        restore_journal_inventory(config, ())


def test_host_requires_exact_terminal_journal_head(tmp_path: Path) -> None:
    config = _config(tmp_path)
    system_id = uuid4()
    record = JournalRecordV1(
        authority_id=uuid4(),
        generation=1,
        system_id=system_id,
        activation_id=uuid4(),
        run_id=uuid4(),
        plan_identity="sha256:" + "2" * 64,
        purpose="activate",
        operation=AuthorityOperation.ACTIVATE,
        provider_kind="local-libvirt",
        authority_instance=config.authority_instance,
        operation_identity="operation-a",
        operation_digest="sha256:" + "3" * 64,
        sequence=1,
        previous_digest="sha256:" + "0" * 64,
        phase=JournalPhase.WATERMARK_INSTALLED,
        attempt_id=uuid4(),
    )
    lane = config.journal_dir / f"{system_id}.jsonl"
    lane.write_bytes(canonical_record_bytes(record) + b"\n")
    lane.chmod(0o600)
    head = JournalHead(
        authority_instance=config.authority_instance,
        system_id=system_id,
        sequence=1,
        digest=record_digest(record),
        phase=record.phase,
        authority_id=record.authority_id,
        generation=record.generation,
        operation_identity=record.operation_identity,
        pending_takeover=None,
        suspended_operation=None,
    )
    restore_journal_inventory(config, (head,))
    with pytest.raises(HostReadinessError, match="journal: head-mismatch"):
        restore_journal_inventory(config, (replace(head, digest="sha256:" + "4" * 64),))


def test_host_exits_when_boundary_drifts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    listener = SimpleNamespace(
        start_serving=lambda: None, close=lambda: None, validate=lambda: None
    )
    checks = 0
    notices: list[str] = []

    async def serve(*_args: object, **_kwargs: object) -> object:
        async def start_serving() -> None:
            return None

        async def close() -> None:
            return None

        listener.start_serving = start_serving
        listener.close = close
        return listener

    async def check(*_args: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise HostReadinessError("journal", "inventory-mismatch")

    async def health(*_args: object) -> None:
        return None

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", check)
    monkeypatch.setattr(host, "check_authority_host", check)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", no_wait)

    with pytest.raises(HostReadinessError, match="inventory-mismatch"):
        asyncio.run(run_authority_host(config))
    assert notices == ["READY=1", "STOPPING=1"]


def test_host_notifies_readiness_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    notices: list[str] = []
    gate = asyncio.Event()

    class Listener:
        def validate(self) -> None:
            assert gate.is_set()

        async def start_serving(self) -> None:
            assert notices == []

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        assert gate.is_set()
        return Listener()

    async def static_check(*_args: object) -> None:
        assert notices == []
        gate.set()

    async def check(*_args: object) -> None:
        return None

    async def health(*_args: object) -> None:
        return None

    async def stop_after_ready(_delay: float) -> None:
        assert gate.is_set()
        raise asyncio.CancelledError

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", static_check)
    monkeypatch.setattr(host, "check_authority_host", check)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", stop_after_ready)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_authority_host(config))
    assert notices == ["READY=1", "STOPPING=1"]


def test_host_validates_listener_before_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    notices: list[str] = []

    class Listener:
        def validate(self) -> None:
            raise OSError("invalid listener")

        async def start_serving(self) -> None:
            pytest.fail("invalid listener must never begin serving")

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        return Listener()

    async def static_check(*_args: object) -> None:
        return None

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", static_check)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    with pytest.raises(HostReadinessError, match="invalid-evidence"):
        asyncio.run(run_authority_host(config))
    assert notices == ["STOPPING=1"]


def test_host_retracts_on_listener_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _config(tmp_path)
    notices: list[str] = []
    checks = 0

    class Listener:
        def validate(self) -> None:
            return None

        async def start_serving(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        return Listener()

    async def check(*_args: object) -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            raise HostReadinessError("listener", "invalid-evidence")

    async def health(*_args: object) -> None:
        return None

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", check)
    monkeypatch.setattr(host, "check_authority_host", check)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", no_wait)
    with pytest.raises(HostReadinessError, match="listener: invalid-evidence"):
        asyncio.run(run_authority_host(config))
    assert notices == ["READY=1", "STOPPING=1"]


def test_host_retracts_when_transport_certificate_expires(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    notices: list[str] = []
    handshakes = 0

    class Listener:
        def validate(self) -> None:
            return None

        async def start_serving(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        return Listener()

    async def check(*_args: object) -> None:
        return None

    async def health(*_args: object) -> None:
        nonlocal handshakes
        handshakes += 1
        if handshakes == 2:
            raise HostReadinessError("tls-health", "handshake-failed")

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", check)
    monkeypatch.setattr(host, "check_authority_host", check)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", no_wait)
    with pytest.raises(HostReadinessError, match="handshake-failed"):
        asyncio.run(run_authority_host(config))
    assert handshakes == 2
    assert notices == ["READY=1", "STOPPING=1"]


def test_probe_recovers_owned_stale_socket(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe = config.request_socket.with_name("authority-probe.sock")
    probe.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(probe))
    stale.close()
    check_probe_socket(probe, config.authority_uid)
    assert not probe.exists()


def test_probe_rejects_concurrent_or_foreign_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    probe = config.request_socket.with_name("authority-probe.sock")
    probe.parent.mkdir(mode=0o700)
    live = socket.socket(socket.AF_UNIX)
    live.bind(str(probe))
    live.listen()
    try:
        with pytest.raises(HostReadinessError, match="probe: live-socket"):
            check_probe_socket(probe, config.authority_uid)
    finally:
        live.close()
        probe.unlink()

    foreign = socket.socket(socket.AF_UNIX)
    foreign.bind(str(probe))
    foreign.close()
    real_stat = host.os.stat

    def foreign_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        status = real_stat(path, *args, **kwargs)
        if Path(path) == probe:
            return SimpleNamespace(
                st_mode=status.st_mode,
                st_uid=status.st_uid + 1,
                st_dev=status.st_dev,
                st_ino=status.st_ino,
            )
        return status

    monkeypatch.setattr(host.os, "stat", foreign_stat)
    with pytest.raises(HostReadinessError, match="probe: unsafe-socket"):
        check_probe_socket(probe, config.authority_uid)
    monkeypatch.setattr(host.os, "stat", real_stat)
    probe.unlink()

    target = tmp_path / "target"
    target.touch()
    probe.symlink_to(target)
    with pytest.raises(HostReadinessError, match="probe: unsafe-socket"):
        check_probe_socket(probe, config.authority_uid)

    lock = config.request_socket.with_name("authority-probe.lock")
    lock.touch(mode=0o600)
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (
            pytest.raises(HostReadinessError, match="probe: busy"),
            host.probe_lock(lock, config.authority_uid),
        ):
            pass
    finally:
        os.close(descriptor)


def test_authority_host_config_reads_fixed_registry_and_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kdive import config as kdive_config

    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", "authority-a")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", str(os.geteuid()))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    kdive_config.load()
    config = AuthorityHostConfig.from_environment()
    assert config.authority_instance == "authority-a"
    assert config.authority_uid == os.geteuid()
    assert config.journal_dir == Path("/var/lib/kdive/provider-authority/journal")
    assert config.request_socket == Path("/run/kdive/provider-authority/request/authority.sock")
    assert config.provider_socket == Path("/run/kdive/provider-authority/libvirt/libvirt-sock")
    assert config.database_dsn == tmp_path / "database-dsn"
    assert config.server_private_key == tmp_path / "service-credential"
    assert {setting.name for setting in authority_settings.SETTINGS} == {
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_UID",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_JOURNAL_DIR",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_PROVIDER_SOCKET",
    }


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", " "),
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", "0"),
    ],
)
def test_authority_host_config_rejects_invalid_required_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    from kdive import config as kdive_config

    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", "authority-a")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", str(os.geteuid()))
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv(name, value)
    kdive_config.load()
    with pytest.raises(HostReadinessError, match="configuration: invalid"):
        AuthorityHostConfig.from_environment()


def test_authority_host_cli_commands_parse() -> None:
    from kdive.__main__ import build_parser

    assert build_parser().parse_args(["external-boot-authority-host"]).command == (
        "external-boot-authority-host"
    )
    assert build_parser().parse_args(["check-external-boot-authority-host"]).command == (
        "check-external-boot-authority-host"
    )

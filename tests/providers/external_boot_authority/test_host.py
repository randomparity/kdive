"""Fail-closed readiness checks for the external-boot authority host."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest

from kdive.db.external_boot_authority_journal import JournalHead
from kdive.providers.external_boot_authority import host, transport
from kdive.providers.external_boot_authority import settings as authority_settings
from kdive.providers.external_boot_authority.host import (
    AuthorityHostConfig,
    HostReadinessError,
    restore_journal_inventory,
    run_authority_host,
    validate_credential_paths,
)
from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityOperation,
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
    record_digest,
)


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    """Keep trust-path tests below an owner-controlled, non-writable parent chain."""
    with tempfile.TemporaryDirectory(prefix="kdive-authority-test-", dir=Path.home()) as path:
        yield Path(path)


def _file(path: Path, *, mode: int, content: str = "value") -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def _install_remote_module_appliance(root: Path, architecture: str) -> None:
    directory = root / architecture
    (directory / "image").mkdir(parents=True)
    files = {
        "image/vmlinuz": b"kernel",
        "image/initramfs.cpio": b"initramfs",
    }
    for relative, content in files.items():
        (directory / relative).write_bytes(content)
    manifest = {
        "architecture": architecture,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            }
            for relative, content in files.items()
        ],
        "format": "kdive-remote-module-appliance-v1",
        "initramfs_files": [],
    }
    (directory / "manifest.json").write_bytes(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    )


def _config(tmp_path: Path) -> AuthorityHostConfig:
    journal = tmp_path / "journal"
    journal.mkdir(mode=0o700)
    return AuthorityHostConfig(
        authority_instance="authority-a",
        authority_uid=os.geteuid(),
        authority_gid=os.getegid(),
        authority_client_gid=os.getegid(),
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


def _access_boundary_config(tmp_path: Path) -> AuthorityHostConfig:
    config = _config(tmp_path)
    install = tmp_path / "install"
    credentials = tmp_path / "credential-source"
    state = tmp_path / "state"
    journal = state / "journal"
    remote_modules = state / "remote-module-preparations"
    remote_evidence = remote_modules / "evidence"
    remote_work = remote_modules / "work"
    remote_pool = state / "remote-libvirt-pool"
    runtime = tmp_path / "run" / "provider-authority"
    request = runtime / "request"
    provider = runtime / "libvirt"
    for path, mode in (
        (install, 0o700),
        (credentials, 0o700),
        (state, 0o700),
        (journal, 0o700),
        (remote_modules, 0o700),
        (remote_evidence, 0o700),
        (remote_work, 0o700),
        (remote_pool, 0o700),
        (runtime, 0o710),
        (request, 0o2750),
        (provider, 0o700),
    ):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(mode)
    return replace(
        config,
        install_dir=install,
        credentials_source_dir=credentials,
        state_dir=state,
        journal_dir=journal,
        request_socket=request / "authority.sock",
        provider_socket=provider / "libvirt.sock",
        denied_identities=("kdive-worker-1", "kdive"),
    )


def test_host_rejects_unsafe_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path)
    validate_credential_paths(config)
    config.server_certificate.chmod(0o444)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)


@pytest.mark.parametrize(
    ("membership", "allowed"),
    [("client", True), ("authority", False)],
)
def test_access_boundary_distinguishes_client_from_authority_group_membership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    membership: str,
    allowed: bool,
) -> None:
    config = _access_boundary_config(tmp_path)
    client_gid = config.authority_gid + 10_000
    config = replace(config, authority_client_gid=client_gid)
    denied_uid = config.authority_uid + 10_000
    denied_gid = client_gid + 10_000

    real_stat = host.os.stat
    client_group_paths = {config.request_socket.parent.parent, config.request_socket.parent}

    def distinct_group_stat(path: Any, *, follow_symlinks: bool = True) -> os.stat_result:
        opened = real_stat(path, follow_symlinks=follow_symlinks)
        if Path(path) not in client_group_paths:
            return opened
        fields = list(opened)
        fields[5] = client_gid
        return os.stat_result(fields)

    monkeypatch.setattr(
        host.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=denied_uid, pw_gid=denied_gid),
    )
    monkeypatch.setattr(host.os, "stat", distinct_group_stat)
    selected_gid = client_gid if membership == "client" else config.authority_gid
    monkeypatch.setattr(host.os, "getgrouplist", lambda _name, gid: [gid, selected_gid])
    if allowed:
        host._validate_access_boundary(config)  # noqa: SLF001
    else:
        with pytest.raises(HostReadinessError, match="access-boundary: denied-identity"):
            host._validate_access_boundary(config)  # noqa: SLF001


def test_access_boundary_rejects_acl_drift(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config = _access_boundary_config(tmp_path)
    denied_uid = config.authority_uid + 10_000
    denied_gid = config.authority_gid + 10_000

    monkeypatch.setattr(
        host.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(pw_uid=denied_uid, pw_gid=denied_gid),
    )
    monkeypatch.setattr(host.os, "getgrouplist", lambda _name, gid: [gid])
    real_listxattr = host.os.listxattr

    def named_acl(path: Any = None, *, follow_symlinks: bool = True) -> list[str]:
        if Path(path) == config.request_socket.parent:
            return ["system.posix_acl_access"]
        return real_listxattr(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(host.os, "listxattr", named_acl)
    with pytest.raises(HostReadinessError, match="access-boundary: unsafe-acl"):
        host._validate_access_boundary(config)  # noqa: SLF001


def test_access_boundary_rejects_world_traversable_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _access_boundary_config(tmp_path)
    monkeypatch.setattr(
        host.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(
            pw_uid=config.authority_uid + 10_000,
            pw_gid=config.authority_gid + 10_000,
        ),
    )
    monkeypatch.setattr(host.os, "getgrouplist", lambda _name, gid: [gid])
    config.request_socket.parent.parent.chmod(0o755)
    with pytest.raises(HostReadinessError, match="access-boundary: unsafe-path"):
        host._validate_access_boundary(config)  # noqa: SLF001
    config.server_certificate.chmod(0o400)
    config.database_dsn.chmod(0o600)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)
    config.database_dsn.unlink()
    config.database_dsn.symlink_to(config.server_private_key)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
        validate_credential_paths(config)


def _projected_credentials(tmp_path: Path) -> AuthorityHostConfig:
    config = _config(tmp_path)
    credentials = tmp_path / "run" / "credentials" / "authority.service"
    credentials.mkdir(parents=True, mode=0o700)
    projected = replace(
        config,
        database_dsn=_file(credentials / "database-dsn", mode=0o440),
        server_private_key=_file(credentials / "service-credential", mode=0o440),
        server_certificate=_file(credentials / "server-certificate", mode=0o440),
        server_ca=_file(credentials / "server-ca", mode=0o440),
        worker_client_ca=_file(credentials / "worker-client-ca", mode=0o440),
        health_client_certificate=_file(credentials / "health-client-certificate", mode=0o440),
        health_client_key=_file(credentials / "health-client-key", mode=0o440),
    )
    return projected


def _with_uid(status: os.stat_result, owner_uid: int) -> os.stat_result:
    fields = list(status)
    fields[4] = owner_uid
    return os.stat_result(fields)


def _mock_projection_ownership(monkeypatch: pytest.MonkeyPatch, credentials: Path) -> None:
    real_stat = host.os.stat
    real_fstat = host.os.fstat

    def root_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        status = real_stat(path, *args, **kwargs)
        candidate = Path(path)
        if (
            candidate == credentials
            or candidate in credentials.parents
            or credentials in candidate.parents
        ):
            return _with_uid(status, 0)
        return status

    def root_fstat(descriptor: int) -> Any:
        status = real_fstat(descriptor)
        target = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        owner = 0 if credentials in target.parents else status.st_uid
        return _with_uid(status, owner)

    monkeypatch.setattr(host.os, "stat", root_stat)
    monkeypatch.setattr(host.os, "fstat", root_fstat)


def test_host_accepts_systemd_projected_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _projected_credentials(tmp_path)
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, config.database_dsn.parent)
        validate_credential_paths(config)


def test_host_rejects_unsafe_systemd_projection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _projected_credentials(tmp_path)
    real_fstat = host.os.fstat

    config.server_ca.chmod(0o400)
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, config.database_dsn.parent)
        with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
            validate_credential_paths(config)
    config.server_ca.chmod(0o440)

    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, config.database_dsn.parent)
        patch.setattr(host.os, "fstat", real_fstat)
        with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
            validate_credential_paths(config)

    config.server_ca.unlink()
    config.server_ca.symlink_to(config.worker_client_ca)
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, config.database_dsn.parent)
        with pytest.raises(HostReadinessError, match="credentials: unsafe-file"):
            validate_credential_paths(config)


def test_host_rejects_systemd_projection_under_unsafe_ancestry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _projected_credentials(tmp_path)
    credentials = config.database_dsn.parent
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, credentials)
        real_stat = host.os.stat

        def foreign_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
            status = real_stat(path, *args, **kwargs)
            if Path(path) == credentials.parent:
                return _with_uid(status, 1234)
            return status

        patch.setattr(host.os, "stat", foreign_stat)
        with pytest.raises(HostReadinessError, match="credentials: unsafe-path"):
            validate_credential_paths(config)

    credentials.chmod(0o770)
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, credentials)
        with pytest.raises(HostReadinessError, match="credentials: unsafe-path"):
            validate_credential_paths(config)


def test_host_rejects_mixed_source_and_projected_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    projected = _projected_credentials(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir(mode=0o700)
    source = _config(source_root)
    mixed = replace(projected, database_dsn=source.database_dsn)
    with monkeypatch.context() as patch:
        _mock_projection_ownership(patch, projected.database_dsn.parent)
        with pytest.raises(HostReadinessError, match="credentials: mixed-profiles"):
            validate_credential_paths(mixed)


def test_host_rejects_untrusted_protected_parent_chains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)

    writable = tmp_path / "writable"
    writable.mkdir(mode=0o770)
    writable.chmod(0o770)
    writable_dsn = _file(writable / "database-dsn", mode=0o400)
    with pytest.raises(HostReadinessError, match="credentials: unsafe-path"):
        validate_credential_paths(replace(config, database_dsn=writable_dsn))

    real = tmp_path / "real"
    linked_journal = real / "journal"
    linked_journal.mkdir(parents=True, mode=0o700)
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(HostReadinessError, match="journal: unsafe-path"):
        restore_journal_inventory(replace(config, journal_dir=link / "journal"), ())

    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o700)
    real_stat = host.os.stat

    def foreign_stat(path: Any, *args: Any, **kwargs: Any) -> Any:
        status = real_stat(path, *args, **kwargs)
        if Path(path) == foreign:
            return SimpleNamespace(st_mode=status.st_mode, st_uid=status.st_uid + 1)
        return status

    monkeypatch.setattr(host.os, "stat", foreign_stat)
    with pytest.raises(HostReadinessError, match="provider-socket: unsafe-path"):
        asyncio.run(
            host._check_provider_socket(  # noqa: SLF001 - direct trust-boundary proof
                replace(config, provider_socket=foreign / "provider.sock")
            )
        )


def test_host_rejects_invalid_journal_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.journal_dir.chmod(0o777)
    with pytest.raises(HostReadinessError, match="journal: unsafe-tree"):
        restore_journal_inventory(config, ())


class _Cursor:
    def __init__(self, row: object, queries: list[str]) -> None:
        self.row = row
        self.queries = queries

    async def __aenter__(self) -> _Cursor:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object) -> None:
        self.queries.append(str(_args[0]))

    async def fetchone(self) -> object:
        return self.row


class _Connection:
    def __init__(self, row: object) -> None:
        self.row = row
        self.queries: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.row, self.queries)


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
        False,
        True,
        True,
        False,
    )
    with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
        asyncio.run(host.check_database_role(_Connection(privileged)))


def test_host_rejects_nested_effective_database_role() -> None:
    nested = (
        "kdive-provider-authority",
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        ("kdive_provider_authority", "nested_extra_role"),
        False,
        True,
        True,
        False,
    )
    connection = _Connection(nested)
    with pytest.raises(HostReadinessError, match="database-role: excessive-privilege"):
        asyncio.run(host.check_database_role(connection))
    assert "WITH RECURSIVE" in connection.queries[0]


def test_host_database_role_query_inventories_all_application_privileges() -> None:
    valid = (
        "kdive-provider-authority",
        True,
        True,
        False,
        False,
        False,
        False,
        False,
        ("kdive_provider_authority",),
        False,
        True,
        True,
        False,
    )
    connection = _Connection(valid)
    asyncio.run(host.check_database_role(connection))
    query = connection.queries[0]
    for catalog in (
        "pg_class",
        "pg_attribute",
        "pg_proc",
        "pg_type",
        "pg_namespace",
        "pg_database",
        "pg_foreign_data_wrapper",
        "pg_foreign_server",
        "pg_language",
        "pg_largeobject_metadata",
        "pg_parameter_acl",
        "pg_tablespace",
    ):
        assert catalog in query
    assert "aclexplode" in query
    assert "acldefault" in query
    assert "pg_shdepend" in query
    assert "resolve_current_external_boot_preparation_authority" in query
    assert "acl.grantee IN (0, role.oid, accepted_role.oid)" in query
    assert "accepted_public_function" in query
    for attribute in (
        "capability_role.rolcanlogin",
        "capability_role.rolinherit",
        "capability_role.rolsuper",
        "capability_role.rolcreatedb",
        "capability_role.rolcreaterole",
        "capability_role.rolreplication",
        "capability_role.rolbypassrls",
        "membership.admin_option",
        "membership.inherit_option",
        "membership.set_option",
    ):
        assert attribute in query


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


def test_journal_validation_runs_outside_the_transport_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    event_loop_thread = threading.get_ident()
    validation_threads: list[int] = []

    def validate(*_args: object, **_kwargs: object) -> None:
        validation_threads.append(threading.get_ident())

    monkeypatch.setattr(host, "_restore_journal_inventory", validate)
    asyncio.run(host.JournalInventoryValidator().validate(config, ()))
    assert validation_threads
    assert validation_threads[0] != event_loop_thread


def test_periodic_journal_validation_reuses_unchanged_lane_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
        sequence=record.sequence,
        digest=record_digest(record),
        phase=record.phase,
        authority_id=record.authority_id,
        generation=record.generation,
        operation_identity=record.operation_identity,
        pending_takeover=None,
        suspended_operation=None,
    )
    load_calls = 0
    original_load = FileAuthorityJournal.load

    def count_load(
        journal: FileAuthorityJournal, *, deadline: float | None = None
    ) -> tuple[JournalRecordV1, ...]:
        nonlocal load_calls
        load_calls += 1
        return original_load(journal, deadline=deadline)

    monkeypatch.setattr(FileAuthorityJournal, "load", count_load)
    validator = host.JournalInventoryValidator()

    async def validate_twice() -> None:
        await validator.validate(config, (head,))
        await validator.validate(config, (head,))

    asyncio.run(validate_twice())
    assert load_calls == 1


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
    assert notices == ["READY=1", "WATCHDOG=1", "STOPPING=1"]


def test_host_retracts_readiness_when_periodic_check_stalls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    notices: list[str] = []

    class Listener:
        def validate(self) -> None:
            return None

        async def start_serving(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        return Listener()

    async def static_check(*_args: object) -> None:
        return None

    async def stalled_check(*_args: object) -> None:
        await asyncio.Event().wait()

    async def health(*_args: object) -> None:
        return None

    async def no_wait(_delay: float) -> None:
        return None

    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", static_check)
    monkeypatch.setattr(host, "check_authority_host", stalled_check)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", no_wait)
    monkeypatch.setattr(host, "READINESS_CHECK_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HostReadinessError, match="readiness: timeout"):
        asyncio.run(run_authority_host(config))
    assert notices == ["READY=1", "WATCHDOG=1", "STOPPING=1"]


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
    assert notices == ["READY=1", "WATCHDOG=1", "STOPPING=1"]


def test_host_shares_one_mutation_service_between_listeners(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = replace(_config(tmp_path), network_address="127.0.0.1", network_port=9443)

    class Service:
        closed = False

        async def close(self) -> None:
            self.closed = True

    service = Service()
    received: list[object | None] = []

    class Listener:
        def validate(self) -> None:
            return None

        async def start_serving(self) -> None:
            return None

        async def close(self) -> None:
            return None

    async def serve(*_args: object, **kwargs: object) -> Listener:
        received.append(kwargs.get("service"))
        return Listener()

    async def ready(*_args: object) -> None:
        return None

    async def stop(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(host, "_build_mutation_service", lambda _config: service)
    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "serve_authority_network_transport", serve)
    monkeypatch.setattr(host, "_check_static_authority_host", ready)
    monkeypatch.setattr(host, "check_tls_health", ready)
    monkeypatch.setattr(host, "_notify_systemd", lambda _message: None)
    monkeypatch.setattr(host.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_authority_host(config))
    assert received == [service, service]
    assert service.closed


def test_host_keeps_identity_only_mode_without_local_recovery_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("KDIVE_LIBVIRT_RECOVERY_ROOT", raising=False)
    assert host._build_mutation_service(_config(tmp_path)) is None  # noqa: SLF001


def test_host_keeps_local_mutation_without_remote_module_dependencies(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import libvirt

    from kdive import config as kdive_config
    from kdive.providers.assembly import composition as provider_assembly
    from kdive.providers.external_boot_authority.orphan import RecoveryOrphanAuthorityService
    from kdive.providers.local_libvirt import composition

    recovery_root = tmp_path / "recovery"
    recovery_root.mkdir(mode=0o700)
    kdive_config.load({"KDIVE_LIBVIRT_RECOVERY_ROOT": str(recovery_root)})
    binding = SimpleNamespace(adapter=object(), provider=object())
    monkeypatch.setattr(provider_assembly, "object_store_from_env", object)
    monkeypatch.setattr(
        composition,
        "build_local_external_boot_authority",
        lambda _store, _socket: binding,
    )
    monkeypatch.setattr(
        libvirt,
        "open",
        lambda _uri: pytest.fail("disabled remote modules must not open libvirt"),
    )

    service = host._build_mutation_service(_access_boundary_config(tmp_path))  # noqa: SLF001

    assert service is not None
    assert service._adapter is binding.adapter  # noqa: SLF001
    assert service._remote_module_host is None  # noqa: SLF001
    assert isinstance(service._recovery_orphans, RecoveryOrphanAuthorityService)  # noqa: SLF001
    assert service._recovery_orphans._executor is binding.adapter  # noqa: SLF001


def test_host_constructs_mutation_chain_for_checked_provider_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import libvirt

    from kdive import config as kdive_config
    from kdive.providers.assembly import composition as provider_assembly
    from kdive.providers.local_libvirt import composition

    recovery_root = tmp_path / "recovery"
    recovery_root.mkdir(mode=0o700)
    kdive_config.load({"KDIVE_LIBVIRT_RECOVERY_ROOT": str(recovery_root)})
    store = object()
    captured: list[tuple[object, Path]] = []
    monkeypatch.setattr(provider_assembly, "object_store_from_env", lambda: store)
    monkeypatch.setattr(
        composition,
        "build_local_external_boot_authority",
        lambda actual_store, socket: (
            captured.append((actual_store, socket))
            or SimpleNamespace(adapter=object(), provider=object())
        ),
    )
    closed: list[bool] = []

    class Pool:
        def isActive(self) -> int:
            return 1

        def XMLDesc(self, _flags: int) -> str:
            return (
                "<pool type='dir'><name>authority-systems</name><target><path>"
                f"{config.remote_libvirt_pool_dir}</path></target></pool>"
            )

    class Connection:
        def storagePoolLookupByName(self, name: str) -> Pool:
            assert name == "authority-systems"
            return Pool()

        def close(self) -> None:
            closed.append(True)

    config = replace(
        _access_boundary_config(tmp_path),
        remote_module_enabled=True,
        remote_module_architectures=("x86_64",),
        remote_libvirt_storage_pool="authority-systems",
        remote_module_appliance_root=tmp_path / "appliance",
    )
    _install_remote_module_appliance(config.remote_module_appliance_root, "x86_64")
    opened: list[str] = []

    def open_connection(uri: str) -> Connection:
        opened.append(uri)
        return Connection()

    monkeypatch.setattr(libvirt, "open", open_connection)

    service = host._build_mutation_service(config)  # noqa: SLF001
    assert service is not None
    assert opened == ["qemu+unix:///session?socket=" + str(config.provider_socket)]
    assert captured == [(store, config.provider_socket)]
    remote_module_host = cast(Any, service._remote_module_host)  # noqa: SLF001
    assert remote_module_host._host._factory._pool_name == "authority-systems"  # noqa: SLF001
    operations = cast(Any, service._adapter)._coordinator._operations  # noqa: SLF001
    assert operations._pool_name == "authority-systems"  # noqa: SLF001
    assert operations._materializer._pool_name == "authority-systems"  # noqa: SLF001
    asyncio.run(service.close())
    assert closed == [True]


def test_remote_module_readiness_rejects_missing_selected_appliance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import libvirt

    closed: list[bool] = []
    config = replace(
        _access_boundary_config(tmp_path),
        remote_module_enabled=True,
        remote_module_architectures=("x86_64",),
        remote_libvirt_storage_pool="authority-systems",
        remote_module_appliance_root=tmp_path / "appliance",
    )

    class Pool:
        def isActive(self) -> int:
            return 1

        def XMLDesc(self, _flags: int) -> str:
            return (
                "<pool type='dir'><name>authority-systems</name><target><path>"
                f"{config.remote_libvirt_pool_dir}</path></target></pool>"
            )

    class Connection:
        def storagePoolLookupByName(self, _name: str) -> Pool:
            return Pool()

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(libvirt, "open", lambda _uri: Connection())

    with pytest.raises(HostReadinessError, match="remote-module-appliance: invalid"):
        host._open_remote_module_connection(config)  # noqa: SLF001
    assert closed == [True]


def test_remote_module_readiness_rejects_wrong_pool_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import libvirt

    config = replace(
        _access_boundary_config(tmp_path),
        remote_module_enabled=True,
        remote_module_architectures=("x86_64",),
        remote_libvirt_storage_pool="authority-systems",
        remote_module_appliance_root=tmp_path / "appliance",
    )
    _install_remote_module_appliance(config.remote_module_appliance_root, "x86_64")

    class Pool:
        def isActive(self) -> int:
            return 1

        def XMLDesc(self, _flags: int) -> str:
            return (
                "<pool type='dir'><name>authority-systems</name>"
                "<target><path>/wrong</path></target></pool>"
            )

    class Connection:
        def storagePoolLookupByName(self, _name: str) -> Pool:
            return Pool()

        def close(self) -> None:
            return None

    monkeypatch.setattr(libvirt, "open", lambda _uri: Connection())

    with pytest.raises(HostReadinessError, match="remote-module-pool: invalid"):
        host._open_remote_module_connection(config)  # noqa: SLF001


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
    assert notices == ["READY=1", "WATCHDOG=1", "STOPPING=1"]


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
    assert notices == ["READY=1", "WATCHDOG=1", "STOPPING=1"]


def test_probe_recovers_owned_stale_socket(tmp_path: Path) -> None:
    config = _config(tmp_path)
    probe = config.request_socket.with_name("authority-probe.sock")
    probe.parent.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(probe))
    stale.close()
    transport.check_stale_socket(probe, config.authority_uid)
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
        with pytest.raises(OSError, match="live-socket"):
            transport.check_stale_socket(probe, config.authority_uid)
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
    with pytest.raises(OSError, match="unsafe-socket"):
        transport.check_stale_socket(probe, config.authority_uid)
    monkeypatch.setattr(host.os, "stat", real_stat)
    probe.unlink()

    target = tmp_path / "target"
    target.touch()
    probe.symlink_to(target)
    with pytest.raises(OSError, match="unsafe-socket"):
        transport.check_stale_socket(probe, config.authority_uid)

    probe.unlink()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(probe))
    stale.close()
    stat_calls = 0
    replacement: socket.socket | None = None

    def replace_before_recheck(path: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal replacement, stat_calls
        if Path(path) == probe:
            stat_calls += 1
            if stat_calls == 2:
                probe.unlink()
                replacement = socket.socket(socket.AF_UNIX)
                replacement.bind(str(probe))
                status = real_stat(path, *args, **kwargs)
                return SimpleNamespace(st_dev=status.st_dev, st_ino=status.st_ino + 1)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(host.os, "stat", replace_before_recheck)
    with pytest.raises(OSError, match="replaced-socket"):
        transport.check_stale_socket(probe, config.authority_uid)
    assert replacement is not None
    replacement.close()
    monkeypatch.setattr(host.os, "stat", real_stat)
    probe.unlink()

    lock = config.request_socket.with_name("authority-probe.lock")
    lock.touch(mode=0o600)
    descriptor = transport.acquire_socket_lock(lock, config.authority_uid)
    try:
        with pytest.raises(OSError, match="busy"):
            transport.acquire_socket_lock(lock, config.authority_uid)
    finally:
        os.close(descriptor)


def test_one_shot_probe_reports_lock_contention(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    config.request_socket.parent.mkdir(mode=0o2750)
    config.request_socket.parent.chmod(0o2750)

    async def static_check(*_args: object) -> None:
        return None

    async def busy(*_args: object, **_kwargs: object) -> object:
        raise transport.SocketLockBusyError("busy")

    monkeypatch.setattr(host, "_check_static_authority_host", static_check)
    monkeypatch.setattr(host, "serve_authority_transport", busy)
    with pytest.raises(HostReadinessError, match="probe: probe-busy"):
        asyncio.run(host.check_authority_host_once(config))


def test_one_shot_probe_validates_remote_prerequisites_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = replace(
        _config(tmp_path),
        remote_module_enabled=True,
        remote_module_architectures=("x86_64",),
    )
    events: list[str] = []

    class Connection:
        def close(self) -> None:
            events.append("remote-close")

    class Listener:
        def validate(self) -> None:
            events.append("listener-validate")

        async def start_serving(self) -> None:
            events.append("listener-start")

        async def close(self) -> None:
            events.append("listener-close")

    async def static_check(*_args: object) -> None:
        events.append("static")

    async def serve(*_args: object, **_kwargs: object) -> Listener:
        return Listener()

    async def health(*_args: object) -> None:
        events.append("health")

    monkeypatch.setattr(host, "_check_static_authority_host", static_check)
    monkeypatch.setattr(host, "_open_remote_module_connection", lambda _config: Connection())
    monkeypatch.setattr(host, "validate_socket_parent", lambda *_args: None)
    monkeypatch.setattr(host, "serve_authority_transport", serve)
    monkeypatch.setattr(host, "check_tls_health", health)

    asyncio.run(host.check_authority_host_once(config))

    assert events == [
        "static",
        "remote-close",
        "listener-validate",
        "listener-start",
        "health",
        "listener-close",
    ]


def test_provider_socket_rejects_mode_and_group_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = _config(tmp_path)
    provider_dir = tmp_path / "provider"
    provider_dir.mkdir(mode=0o700)
    provider = provider_dir / "libvirt-sock"
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(provider))
    provider.chmod(0o700)

    class Writer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def connect(_path: str) -> tuple[object, Writer]:
        return object(), Writer()

    config = replace(config, provider_socket=provider)
    monkeypatch.setattr(host.asyncio, "open_unix_connection", connect)
    try:
        asyncio.run(host._check_provider_socket(config))  # noqa: SLF001
        provider.chmod(0o770)
        with pytest.raises(HostReadinessError, match="provider-socket: unsafe-acl"):
            asyncio.run(host._check_provider_socket(config))  # noqa: SLF001
        provider.chmod(0o700)
        real_stat = host.os.stat

        def foreign_group(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
            status = real_stat(path, *args, **kwargs)
            if Path(path) in {provider_dir, provider}:
                fields = list(status)
                fields[5] = status.st_gid + 1
                return os.stat_result(fields)
            return status

        monkeypatch.setattr(host.os, "stat", foreign_group)
        with pytest.raises(HostReadinessError, match="provider-socket: unsafe-acl"):
            asyncio.run(host._check_provider_socket(config))  # noqa: SLF001
    finally:
        server.close()


def test_authority_host_config_reads_fixed_registry_and_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from kdive import config as kdive_config

    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", "authority-a")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", str(os.geteuid()))
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_GID", str(os.getegid()))
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID", str(os.getegid()))
    monkeypatch.setenv("KDIVE_REMOTE_LIBVIRT_STORAGE_POOL", "authority-systems")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ENABLED", "true")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ARCHITECTURES", "x86_64")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    kdive_config.load()
    config = AuthorityHostConfig.from_environment()
    assert config.authority_instance == "authority-a"
    assert config.authority_uid == os.geteuid()
    assert config.authority_gid == os.getegid()
    assert config.authority_client_gid == os.getegid()
    assert config.remote_libvirt_storage_pool == "authority-systems"
    assert config.remote_module_enabled is True
    assert config.remote_module_architectures == ("x86_64",)
    assert config.journal_dir == Path("/var/lib/kdive/provider-authority/journal")
    assert config.request_socket == Path("/run/kdive/provider-authority/request/authority.sock")
    assert config.provider_socket == Path("/run/kdive/provider-authority/libvirt/libvirt-sock")
    assert config.proof_socket is None
    assert config.install_dir == Path("/opt/kdive-provider-authority")
    assert config.credentials_source_dir == Path("/etc/kdive/credentials/provider-authority")
    assert config.state_dir == Path("/var/lib/kdive/provider-authority")
    assert config.denied_identities == tuple(
        [f"kdive-worker-{slot}" for slot in range(1, 9)] + ["kdive"]
    )
    assert config.database_dsn == tmp_path / "database-dsn"
    assert config.server_private_key == tmp_path / "service-credential"
    assert {setting.name for setting in authority_settings.SETTINGS} == {
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_UID",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_GID",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_JOURNAL_DIR",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_PROVIDER_SOCKET",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_PROOF_SOCKET",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_ADDRESS",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_PORT",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_DENIED_IDENTITIES",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_STORE_IDENTITY",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_RESERVE_BYTES",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_RECOVERY_MAX_BYTES",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ENABLED",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_REMOTE_MODULE_ARCHITECTURES",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_REQUEST_SOCKET",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_SERVER_CA_REF",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_CERT_REF",
        "KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_CLIENT_KEY_REF",
    }
    assert all(
        setting.processes == frozenset({"worker"})
        for setting in authority_settings.SETTINGS
        if setting.name.startswith("KDIVE_WORKER_")
    )


@pytest.mark.parametrize(
    "raw",
    [
        "",
        ",",
        "operator,",
        ",operator",
        "operator,operator",
        " operator",
        "op erator",
        "Operator",
        "0operator",
        "operátor",
        "operator\n",
        "a" * 33,
        ",".join(f"account{index}" for index in range(33)),
    ],
)
def test_denied_identity_setting_rejects_malformed_values_without_disclosure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, raw: str
) -> None:
    from kdive import config as registry

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    registry.load(
        {
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_UID": "1001",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_GID": "1001",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID": "1002",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_DENIED_IDENTITIES": raw,
        }
    )
    with pytest.raises(HostReadinessError) as caught:
        AuthorityHostConfig.from_environment()
    assert str(caught.value) == "configuration: invalid"
    assert caught.value.details == {"component": "configuration", "reason": "invalid"}


@pytest.mark.parametrize(
    "names",
    [("operator",), tuple(f"account{n}" for n in range(32)), ("_operator", "a" * 32, "account-2")],
)
def test_denied_identity_setting_selects_existing_host_policy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, names: tuple[str, ...]
) -> None:
    from kdive import config as registry

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    registry.load(
        {
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_UID": "1001",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_GID": "1001",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID": "1002",
            "KDIVE_EXTERNAL_BOOT_AUTHORITY_DENIED_IDENTITIES": ",".join(names),
        }
    )
    assert AuthorityHostConfig.from_environment().denied_identities == names


def test_denied_host_account_must_exist_and_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = replace(_access_boundary_config(tmp_path), denied_identities=("missing-account",))

    def missing(name: str) -> None:
        raise KeyError(name)

    monkeypatch.setattr(host.pwd, "getpwnam", missing)
    with pytest.raises(HostReadinessError) as caught:
        host._validate_access_boundary(config)  # noqa: SLF001
    assert str(caught.value) == "access-boundary: identity-missing"
    assert "missing-account" not in repr(caught.value.details)


@pytest.mark.parametrize("unsafe_uid", [0, None])
def test_host_specific_denied_identity_cannot_be_root_or_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe_uid: int | None
) -> None:
    config = replace(_access_boundary_config(tmp_path), denied_identities=("operator",))
    monkeypatch.setattr(
        host.pwd,
        "getpwnam",
        lambda _name: SimpleNamespace(
            pw_uid=config.authority_uid if unsafe_uid is None else unsafe_uid,
            pw_gid=config.authority_gid + 10000,
        ),
    )
    monkeypatch.setattr(host.os, "getgrouplist", lambda _name, gid: [gid])
    with pytest.raises(HostReadinessError, match="access-boundary: denied-identity"):
        host._validate_access_boundary(config)  # noqa: SLF001


@pytest.mark.parametrize(
    ("address", "port", "valid"),
    [
        (None, None, True),
        ("127.0.0.1", "443", True),
        ("0.0.0.0", "65535", True),
        ("127.0.0.1", None, False),
        (None, "443", False),
        ("", "443", False),
        ("::1", "443", False),
        ("localhost", "443", False),
        ("224.0.0.1", "443", False),
        ("127.0.0.1", "0", False),
        ("127.0.0.1", "65536", False),
    ],
)
def test_network_host_settings_require_complete_ipv4_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    address: str | None,
    port: str | None,
    valid: bool,
) -> None:
    from kdive import config as registry

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    env = {
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "authority-a",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_UID": "1001",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_GID": "1001",
        "KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID": "1002",
    }
    if address is not None:
        env["KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_ADDRESS"] = address
    if port is not None:
        env["KDIVE_EXTERNAL_BOOT_AUTHORITY_NETWORK_PORT"] = port
    registry.load(env)
    if valid:
        config = AuthorityHostConfig.from_environment()
        assert config.network_address == address
        assert config.network_port == (int(port) if port is not None else None)
    else:
        with pytest.raises(HostReadinessError, match="configuration: invalid"):
            AuthorityHostConfig.from_environment()


@pytest.mark.parametrize("fault", ["bind", "validate", "health", "drift", "cancel", "close"])
def test_host_closes_both_listeners_on_every_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fault: str,
) -> None:
    config = replace(_config(tmp_path), network_address="127.0.0.1", network_port=443)
    closed: list[str] = []
    health_checked: list[str] = []
    notices: list[str] = []

    class Listener:
        def __init__(self, name: str) -> None:
            self.name = name
            self.checks = 0

        def validate(self) -> None:
            self.checks += 1
            if self.name == "network" and (
                fault == "validate" or (fault == "drift" and self.checks > 1)
            ):
                raise OSError("changed")

        async def start_serving(self) -> None:
            return None

        async def close(self) -> None:
            closed.append(self.name)
            if fault == "close" and self.name == "network":
                raise OSError("close failed")

    async def unix(*_args: object, **_kwargs: object) -> Listener:
        return Listener("unix")

    async def network(*_args: object, **_kwargs: object) -> Listener:
        if fault == "bind":
            raise OSError("bind failed")
        return Listener("network")

    async def static(*_args: object) -> None:
        return None

    async def health(listener: Listener, *_args: object) -> None:
        health_checked.append(listener.name)
        if listener.name == "network" and fault == "health":
            raise HostReadinessError("tls-health", "handshake-failed")

    async def pause(_delay: float) -> None:
        if fault != "drift":
            raise asyncio.CancelledError

    monkeypatch.setattr(host, "serve_authority_transport", unix)
    monkeypatch.setattr(host, "serve_authority_network_transport", network)
    monkeypatch.setattr(host, "_check_static_authority_host", static)
    monkeypatch.setattr(host, "check_tls_health", health)
    monkeypatch.setattr(host, "_notify_systemd", notices.append)
    monkeypatch.setattr(host.asyncio, "sleep", pause)
    with pytest.raises((HostReadinessError, asyncio.CancelledError)):
        asyncio.run(run_authority_host(config))
    assert set(closed) == ({"unix"} if fault == "bind" else {"unix", "network"})
    assert notices[-1] == "STOPPING=1"
    if fault in {"drift", "cancel", "close"}:
        assert health_checked[:2] == ["unix", "network"]
        assert notices[0] == "READY=1"
    else:
        assert "READY=1" not in notices


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", " "),
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", "0"),
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_GID", "0"),
        ("KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID", "0"),
    ],
)
def test_authority_host_config_rejects_invalid_required_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    from kdive import config as kdive_config

    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE", "authority-a")
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_UID", str(os.geteuid()))
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_GID", str(os.getegid()))
    monkeypatch.setenv("KDIVE_EXTERNAL_BOOT_AUTHORITY_CLIENT_GID", str(os.getegid()))
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

"""Tests for the authorize_ssh_key worker handler (ADR-0271, #782; ADR-0289, #963)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs import worker
from kdive.jobs.handlers.connectivity import ssh_authorize
from kdive.jobs.handlers.connectivity.ssh_authorize import (
    _attach_console_tail,
    _raise_on_authorize_failure,
    authorize_ssh_key_handler,
    build_authorize_argv,
)
from kdive.jobs.handlers.connectivity.ssh_reachable import ReachResult
from kdive.prereqs.system_bootstrap_key import ensure_system_bootstrap_key
from kdive.security.secrets.secret_registry import SecretRegistry

_NOW = datetime(2025, 1, 1)
_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 agent@host"


async def _reachable(_host: str, _port: int) -> ReachResult:
    """A pre-flight probe that reports the guest sshd is reachable (the healthy path)."""
    return ReachResult(True, "reachable")


def _probe_returning(result: ReachResult) -> Callable[[str, int], Awaitable[ReachResult]]:
    async def _probe(_host: str, _port: int) -> ReachResult:
        return result

    return _probe


def _job(public_key: str = _KEY) -> Job:
    return _job_for(uuid4(), public_key=public_key)


def _job_for(system_id: UUID, *, public_key: str = _KEY) -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.AUTHORIZE_SSH_KEY,
        payload={"system_id": str(system_id), "public_key": public_key},
        state=JobState.RUNNING,
        max_attempts=3,
        authorizing={"principal": "user", "agent_session": None, "project": "proj"},
        dedup_key="test",
    )


def _resolver(endpoint: tuple[str, int] | None) -> MagicMock:
    connector = MagicMock()
    connector.recorded_ssh_endpoint = MagicMock(return_value=endpoint)
    binding = SimpleNamespace(runtime=SimpleNamespace(connector=connector))
    resolver = MagicMock()
    resolver.binding_for_system = AsyncMock(return_value=binding)
    return resolver


def test_argv_is_fixed_and_excludes_the_key() -> None:
    argv = build_authorize_argv("127.0.0.1", 22022, "/tmp/kdive-bootkey-use-x/id")
    assert argv[0] == "ssh"
    assert "root@127.0.0.1" in argv
    assert "22022" in argv
    # The key is NEVER in the argv/command string — ssh would space-join post-host args into one
    # remotely-reparsed string. It travels on stdin instead. The post-host script is a single arg.
    assert _KEY not in argv
    assert argv.count(argv[-1]) == 1
    script = argv[-1]
    assert "flock" in script and "grep -qxF" in script
    assert "key=$(cat)" in script
    assert argv[argv.index("-i") + 1] == "/tmp/kdive-bootkey-use-x/id"


def test_argv_targets_the_given_host_for_remote_endpoint() -> None:
    # ADR-0291: a remote System's recorded endpoint host is its ACL'd ssh_addr, not loopback.
    argv = build_authorize_argv("10.0.0.9", 47101, "/tmp/k")
    assert "root@10.0.0.9" in argv
    assert "root@127.0.0.1" not in argv
    assert "47101" in argv


def test_handler_unprovisioned_is_configuration_error() -> None:
    resolver = _resolver(None)
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(),
                _job(),
                resolver=resolver,
                secret_registry=SecretRegistry(),
                ssh_exec=lambda _argv, _key: None,
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "ssh_not_provisioned"
    # Provider-capability wording, not a stale reprovision remedy (ADR-0281): the local forward is
    # always rendered now, so a None endpoint means the provider exposes no loopback SSH forward.
    assert "local-libvirt" in str(excinfo.value)
    assert "reprovision" not in str(excinfo.value).lower()


# --- Fast-fail pre-flight (#1012): an unreachable guest fails in seconds, terminally, not a
# multi-minute `running`. The pre-flight runs before the bootstrap-key load and the append retry,
# so these need no DB — a MagicMock conn is never read. ---


@pytest.mark.parametrize(
    ("detail", "reason"),
    [("unreachable", "unreachable"), ("no SSH banner", "banner_timeout")],
)
def test_handler_unreachable_preflight_fails_fast_terminal(detail: str, reason: str) -> None:
    resolver = _resolver(("127.0.0.1", 22022))
    ssh_calls: list[int] = []

    def _ssh(_argv: list[str], _key: str) -> None:
        ssh_calls.append(1)

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(),
                _job(),
                resolver=resolver,
                secret_registry=SecretRegistry(),
                ssh_exec=_ssh,
                probe=_probe_returning(ReachResult(False, detail)),
            )
        )
    exc = excinfo.value
    # Named reason from the shared #1008 vocabulary, not a bare 255…
    assert exc.category is ErrorCategory.TRANSPORT_FAILURE
    assert exc.details["reason"] == reason
    assert exc.details["detail"] == detail
    # …terminal so the worker dead-letters instead of requeuing the doomed window (the ~230 s
    # overrun was max_attempts requeues, each burning the ~90 s append window)…
    assert exc.terminal is True
    # …and the append SSH is never attempted on an unreachable guest.
    assert ssh_calls == []


def test_handler_preflight_reason_survives_failure_context() -> None:
    # The acceptance shape: an unreachable pre-flight surfaces `failure_detail_reason` through the
    # same _failure_context path jobs.get/jobs.wait read — a named reason in seconds, not `running`.
    resolver = _resolver(("127.0.0.1", 22022))
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(),
                _job(),
                resolver=resolver,
                secret_registry=SecretRegistry(),
                ssh_exec=lambda _argv, _key: None,
                probe=_probe_returning(ReachResult(False, "no SSH banner")),
            )
        )
    context = worker._failure_context(excinfo.value, SecretRegistry())
    assert context["failure_detail_reason"] == "banner_timeout"


# Async-DB tests follow the PROVEN in-repo pattern in
# tests/jobs/handlers/test_boot_evidence_run_id.py: a SYNC `def test_(migrated_url)` with an
# inner `async def _run()` driven by `asyncio.run(_run())`. The handler now loads the per-System
# bootstrap key from Postgres (kdive.prereqs.system_bootstrap_key), so these tests need a REAL
# conn — a MagicMock conn cannot answer a real `SELECT`. The mock-based tests above stay mocked
# because they never reach the load-key call (they fail before it, or don't assert on it).


async def _seed_system(conn: AsyncConnection) -> UUID:
    """Seed the resources -> allocations -> systems FK chain; return the system_id."""
    resource_id, allocation_id, system_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
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
    return system_id


def test_handler_authorizes_via_per_system_key_and_cleans_up_temp_key(
    migrated_url: str,
) -> None:
    async def _run() -> tuple[list[tuple[list[str], str]], Path | None, bool | None]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                await ensure_system_bootstrap_key(conn, system_id, secret_registry=SecretRegistry())
                job = _job_for(system_id)
                resolver = _resolver(("127.0.0.1", 22022))

                recorded: list[tuple[list[str], str]] = []
                seen_key_path: Path | None = None
                seen_key_existed: bool | None = None

                def _capture(argv: list[str], key: str) -> None:
                    nonlocal seen_key_path, seen_key_existed
                    recorded.append((argv, key))
                    seen_key_path = Path(argv[argv.index("-i") + 1])
                    seen_key_existed = seen_key_path.exists()

                result = await authorize_ssh_key_handler(
                    conn,
                    job,
                    resolver=resolver,
                    secret_registry=SecretRegistry(),
                    ssh_exec=_capture,
                    probe=_reachable,
                )
                assert result is None
                return recorded, seen_key_path, seen_key_existed

    recorded, key_path, key_existed = asyncio.run(_run())
    assert len(recorded) == 1
    argv, key = recorded[0]
    assert "root@127.0.0.1" in argv and "22022" in argv
    assert _KEY not in argv  # not in the command
    assert key == _KEY  # delivered on stdin
    assert key_existed is True  # temp key was materialized during the call
    assert key_path is not None and not key_path.exists()  # and cleaned up after


def test_handler_resolves_endpoint_by_domain_name(migrated_url: str) -> None:
    # The connector resolves the live libvirt domain by name, so the handler must pass the
    # System's `kdive-<id>` domain name, not the bare id (regression for the live-proof bug where
    # the bare id raised VIR_ERR_NO_DOMAIN -> spurious ssh_not_provisioned).
    async def _run() -> tuple[str, UUID]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                await ensure_system_bootstrap_key(conn, system_id, secret_registry=SecretRegistry())
                job = _job_for(system_id)
                resolver = _resolver(("127.0.0.1", 22022))
                connector = resolver.binding_for_system.return_value.runtime.connector

                await authorize_ssh_key_handler(
                    conn,
                    job,
                    resolver=resolver,
                    secret_registry=SecretRegistry(),
                    ssh_exec=lambda _argv, _key: None,
                    probe=_reachable,
                )
                handle = connector.recorded_ssh_endpoint.call_args.args[0]
                return str(handle), system_id

    domain_name, system_id = asyncio.run(_run())
    assert domain_name == f"kdive-{system_id}"


def test_handler_ssh_failure_propagates_transport_failure(migrated_url: str) -> None:
    def _boom(_argv: list[str], _key: str) -> None:
        raise CategorizedError("ssh down", category=ErrorCategory.TRANSPORT_FAILURE)

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                await ensure_system_bootstrap_key(conn, system_id, secret_registry=SecretRegistry())
                job = _job_for(system_id)
                resolver = _resolver(("127.0.0.1", 22022))
                with pytest.raises(CategorizedError) as excinfo:
                    await authorize_ssh_key_handler(
                        conn,
                        job,
                        resolver=resolver,
                        secret_registry=SecretRegistry(),
                        ssh_exec=_boom,
                        probe=_reachable,
                    )
                assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE

    asyncio.run(_run())


# --- Failure diagnosability (#1008): the authorize failure names *why*, not just exit 255. ---


def _failed_proc(returncode: int, stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout="", stderr=stderr
    )


@pytest.mark.parametrize(
    ("returncode", "stderr", "reason"),
    [
        (255, "kex_exchange_identification: Connection reset by peer", "banner_timeout"),
        (255, "ssh: connect to host 127.0.0.1 port 22: Connection refused", "connection_refused"),
        (255, "root@127.0.0.1: Permission denied (publickey).", "auth_rejected"),
        (255, "Host key verification failed.", "host_key_mismatch"),
        (1, "grep: authorized_keys: No such file or directory", "remote_command_failed"),
    ],
)
def test_authorize_failure_classifies_reason(returncode: int, stderr: str, reason: str) -> None:
    with pytest.raises(CategorizedError) as excinfo:
        _raise_on_authorize_failure(_failed_proc(returncode, stderr))
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE
    assert excinfo.value.details["reason"] == reason
    assert excinfo.value.details["exit_status"] == returncode


def test_authorize_success_does_not_raise() -> None:
    _raise_on_authorize_failure(_failed_proc(0, ""))  # returncode 0 → no error


def test_authorize_banner_timeout_reason_survives_failure_context() -> None:
    # The acceptance case: a forced banner-timeout authorize failure surfaces
    # `failure_detail_reason: banner_timeout` (not just 255) through the same _failure_context
    # path that feeds jobs.get/jobs.wait.
    with pytest.raises(CategorizedError) as excinfo:
        _raise_on_authorize_failure(
            _failed_proc(255, "kex_exchange_identification: Connection reset by peer")
        )
    context = worker._failure_context(excinfo.value, SecretRegistry())
    assert context["failure_detail_reason"] == "banner_timeout"
    assert context["failure_detail_exit_status"] == "255"
    assert "Connection reset by peer" in context["failure_detail_stderr_tail"]


# --- Guest console evidence (#1009, ADR-0306): a guest TRANSPORT_FAILURE carries a bounded,
# redacted console tail so "did sshd start?" is answerable from the failed job alone. ---


def _transport_error() -> CategorizedError:
    return CategorizedError("ssh down", category=ErrorCategory.TRANSPORT_FAILURE)


def test_attach_console_tail_enriches_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _tail(_sid: UUID, _reg: SecretRegistry) -> str:
        return "systemd[1]: Started OpenSSH server daemon.\n"

    monkeypatch.setattr(ssh_authorize, "redacted_console_tail", _tail)
    exc = _transport_error()

    asyncio.run(_attach_console_tail(exc, uuid4(), SecretRegistry()))

    assert exc.details["console_tail"] == "systemd[1]: Started OpenSSH server daemon.\n"


def test_attach_console_tail_skips_non_transport_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _must_not_run(_sid: UUID, _reg: SecretRegistry) -> str:
        raise AssertionError("console tail must not be read for a non-transport failure")

    monkeypatch.setattr(ssh_authorize, "redacted_console_tail", _must_not_run)
    exc = CategorizedError("no key", category=ErrorCategory.CONFIGURATION_ERROR)

    asyncio.run(_attach_console_tail(exc, uuid4(), SecretRegistry()))

    assert "console_tail" not in exc.details


def test_attach_console_tail_best_effort_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_sid: UUID, _reg: SecretRegistry) -> None:
        return None

    monkeypatch.setattr(ssh_authorize, "redacted_console_tail", _none)
    exc = _transport_error()

    asyncio.run(_attach_console_tail(exc, uuid4(), SecretRegistry()))

    assert "console_tail" not in exc.details


def test_unreachable_preflight_failure_context_carries_console_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The acceptance shape: an unreachable authorize surfaces the guest console tail via the same
    # _failure_context path jobs.get/jobs.wait read — beside the #1008 reason — so an agent can
    # answer "did sshd start?" from the failed job alone, without a second session.
    async def _tail(_sid: UUID, _reg: SecretRegistry) -> str:
        return "kdive-guest login:  (sshd never Started)\n"

    monkeypatch.setattr(ssh_authorize, "redacted_console_tail", _tail)
    resolver = _resolver(("127.0.0.1", 22022))
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(),
                _job(),
                resolver=resolver,
                secret_registry=SecretRegistry(),
                ssh_exec=lambda _argv, _key: None,
                probe=_probe_returning(ReachResult(False, "no SSH banner")),
            )
        )
    context = worker._failure_context(excinfo.value, SecretRegistry())
    assert context["failure_detail_reason"] == "banner_timeout"
    assert context["failure_detail_console_tail"] == "kdive-guest login:  (sshd never Started)\n"


def test_handler_no_bootstrap_key_is_configuration_error(migrated_url: str) -> None:
    """No key row (System predates ADR-0289 or was never provisioned) fails closed."""

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)  # no ensure_system_bootstrap_key call
                job = _job_for(system_id)
                resolver = _resolver(("127.0.0.1", 22022))
                with pytest.raises(CategorizedError) as excinfo:
                    await authorize_ssh_key_handler(
                        conn,
                        job,
                        resolver=resolver,
                        secret_registry=SecretRegistry(),
                        ssh_exec=lambda _argv, _key: None,
                        probe=_reachable,
                    )
                assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR

    asyncio.run(_run())

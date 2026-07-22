"""Tests for the check_ssh_reachable probe primitives and worker handler (ADR-0298, #972)."""

from __future__ import annotations

import asyncio
import socket
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.connectivity import ssh_reachable
from kdive.jobs.handlers.connectivity.ssh_reachable import (
    ReachResult,
    _real_probe,
    check_ssh_reachable_handler,
    serialize_reach_verdict,
)
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.clock import FrozenClock

_FROZEN = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)


async def _serve(banner: bytes) -> tuple[str, int, asyncio.AbstractServer]:
    """Start a loopback server that writes ``banner`` once then closes, returning its address."""

    async def handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if banner:
            writer.write(banner)
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return host, port, server


def _free_port() -> int:
    """Reserve then release a port so nothing is listening on it."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_probe_reachable_on_ssh_banner() -> None:
    async def _run() -> ReachResult:
        host, port, server = await _serve(b"SSH-2.0-OpenSSH_9.6\r\n")
        async with server:
            return await _real_probe(host, port, deadline_s=3.0)

    assert asyncio.run(_run()) == ReachResult.ok()


def test_probe_no_ssh_banner_when_wrong_prefix() -> None:
    async def _run() -> ReachResult:
        host, port, server = await _serve(b"HELLO not ssh\r\n")
        async with server:
            return await _real_probe(host, port, deadline_s=3.0)

    assert asyncio.run(_run()) == ReachResult.missing_banner()


def test_probe_no_ssh_banner_when_server_sends_nothing() -> None:
    async def _run() -> ReachResult:
        host, port, server = await _serve(b"")
        async with server:
            return await _real_probe(host, port, deadline_s=1.0)

    assert asyncio.run(_run()) == ReachResult.missing_banner()


def test_probe_unreachable_on_closed_port() -> None:
    async def _run() -> ReachResult:
        return await _real_probe("127.0.0.1", _free_port(), deadline_s=1.0)

    assert asyncio.run(_run()) == ReachResult.tcp_unreachable()


def test_probe_retries_until_sshd_binds() -> None:
    # The port is closed for ~0.4s, then a banner-answering server binds — proving the bounded
    # retry tolerates the readiness (sshd-bind) race instead of a false "unreachable" (ADR-0298).
    port = _free_port()

    async def _run() -> ReachResult:
        async def bind_late() -> asyncio.AbstractServer:
            await asyncio.sleep(0.4)

            async def handle(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
                w.write(b"SSH-2.0-late\r\n")
                await w.drain()
                w.close()

            return await asyncio.start_server(handle, "127.0.0.1", port)

        server_task = asyncio.create_task(bind_late())
        result = await _real_probe("127.0.0.1", port, deadline_s=3.0)
        server = await server_task
        server.close()
        await server.wait_closed()
        return result

    assert asyncio.run(_run()) == ReachResult.ok()


def test_serialize_verdict_is_compact_and_redacted() -> None:
    raw = serialize_reach_verdict(
        ReachResult.missing_banner(), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00"
    )
    assert raw == (
        '{"reachable":false,"checked_at":"2026-07-02T00:00:00+00:00",'
        '"endpoint":{"host":"127.0.0.1","port":22001},"detail":"no SSH banner",'
        '"layer":"ssh_banner",'
        '"checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":false}]}'
    )


def test_serialize_verdict_reachable_names_no_failing_layer() -> None:
    raw = serialize_reach_verdict(ReachResult.ok(), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00")
    assert '"layer":null' in raw
    assert '"checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":true}]' in raw


def test_serialize_verdict_unreachable_names_tcp_connect_layer() -> None:
    # No connection was ever accepted, so the lowest failing layer is tcp_connect and the
    # higher, un-evaluated ssh_banner layer is not claimed as tested.
    raw = serialize_reach_verdict(
        ReachResult.tcp_unreachable(), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00"
    )
    assert '"layer":"tcp_connect"' in raw
    assert '"checks":[{"layer":"tcp_connect","ok":false}]' in raw


def test_serialize_verdict_embeds_console_tail_when_given() -> None:
    # ADR-0306: an unreachable verdict carries a bounded, redacted guest console tail.
    raw = serialize_reach_verdict(
        ReachResult.missing_banner(),
        "127.0.0.1",
        22001,
        "2026-07-02T00:00:00+00:00",
        console_tail="login:  (sshd absent)\n",
    )
    assert '"console_tail":"login:  (sshd absent)\\n"' in raw


def test_serialize_verdict_omits_console_tail_when_none() -> None:
    # A reachable verdict needs no guest diagnostics, so the field stays absent (back-compatible).
    raw = serialize_reach_verdict(ReachResult.ok(), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00")
    assert "console_tail" not in raw


def test_reach_result_detail_is_projected_from_layer_checks() -> None:
    cases = [
        (ReachResult.ok(), "reachable", None),
        (ReachResult.tcp_unreachable(), "unreachable", "tcp_connect"),
        (ReachResult.missing_banner(), "no SSH banner", "ssh_banner"),
    ]
    for result, detail, layer in cases:
        assert result.detail == detail
        assert result.failed_layer == layer


# --- worker handler ---------------------------------------------------------------------------
# Async-DB tests follow the in-repo pattern (test_ssh_authorize.py): a SYNC def test_(migrated_url)
# with an inner async def _run() driven by asyncio.run. The handler reads the System row from
# Postgres via SYSTEMS.get, so these need a REAL conn, not a MagicMock.


def _job_for(system_id: UUID) -> Job:
    return Job(
        id=uuid4(),
        created_at=_FROZEN,
        updated_at=_FROZEN,
        kind=JobKind.CHECK_SSH_REACHABLE,
        payload={"system_id": str(system_id)},
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


async def _seed_system(conn: AsyncConnection, *, state: str = "ready") -> UUID:
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
        "VALUES (%s, %s, %s, '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id, state),
    )
    return system_id


def test_handler_serializes_reachable_verdict(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ssh_reachable, "datetime", FrozenClock(_FROZEN))

    probe_calls: list[tuple[str, int]] = []

    async def probe(host: str, port: int) -> ReachResult:
        probe_calls.append((host, port))
        return ReachResult.ok()

    async def _run() -> str | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                job = _job_for(system_id)
                resolver = _resolver(("127.0.0.1", 22001))
                verdict = await check_ssh_reachable_handler(
                    conn,
                    job,
                    resolver=resolver,
                    secret_registry=SecretRegistry(),
                    probe=probe,
                )
                # The binding is resolved with the handler's own (conn, system_id), and the
                # endpoint is looked up for the System's derived domain (no stored domain_name).
                resolver.binding_for_system.assert_awaited_once_with(conn, system_id)
                connector = resolver.binding_for_system.return_value.runtime.connector
                connector.recorded_ssh_endpoint.assert_called_once_with(
                    SystemHandle(domain_name_for(system_id))
                )
                return verdict

    verdict = asyncio.run(_run())
    # The probe is driven with the exact recorded endpoint (host, port), in that order.
    assert probe_calls == [("127.0.0.1", 22001)]
    assert verdict == (
        '{"reachable":true,"checked_at":"2026-07-02T00:00:00+00:00",'
        '"endpoint":{"host":"127.0.0.1","port":22001},"detail":"reachable","layer":null,'
        '"checks":[{"layer":"tcp_connect","ok":true},{"layer":"ssh_banner","ok":true}]}'
    )


def test_handler_serializes_unreachable_verdict_as_success(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A probe that RAN and found the guest unreachable is a job success, not a failure.
    monkeypatch.setattr(ssh_reachable, "datetime", FrozenClock(_FROZEN))

    async def probe(_host: str, _port: int) -> ReachResult:
        return ReachResult.tcp_unreachable()

    async def _run() -> str | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                job = _job_for(system_id)
                return await check_ssh_reachable_handler(
                    conn,
                    job,
                    resolver=_resolver(("127.0.0.1", 22001)),
                    secret_registry=SecretRegistry(),
                    probe=probe,
                )

    raw = asyncio.run(_run())
    assert raw is not None and '"reachable":false' in raw and '"detail":"unreachable"' in raw


def test_unreachable_verdict_carries_console_tail(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ADR-0306: an unreachable verdict embeds the guest console tail so "did sshd start?" is
    # answerable from the verdict alone.
    monkeypatch.setattr(ssh_reachable, "datetime", FrozenClock(_FROZEN))

    tail_calls: list[tuple[UUID, SecretRegistry]] = []

    async def _tail(sid: UUID, reg: SecretRegistry) -> str:
        tail_calls.append((sid, reg))
        return "kdive-guest login:  (sshd never Started)\n"

    monkeypatch.setattr(ssh_reachable, "redacted_console_tail", _tail)

    async def probe(_host: str, _port: int) -> ReachResult:
        return ReachResult.missing_banner()

    registry = SecretRegistry()

    async def _run() -> tuple[str | None, UUID]:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                job = _job_for(system_id)
                verdict = await check_ssh_reachable_handler(
                    conn,
                    job,
                    resolver=_resolver(("127.0.0.1", 22001)),
                    secret_registry=registry,
                    probe=probe,
                )
                return verdict, system_id

    raw, system_id = asyncio.run(_run())
    assert raw is not None
    assert '"console_tail":"kdive-guest login:  (sshd never Started)\\n"' in raw
    # The tail is fetched for THIS system with the handler's own secret registry (not None).
    assert tail_calls == [(system_id, registry)]


def test_reachable_verdict_omits_console_tail(
    migrated_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A reachable guest needs no diagnostics: the handler does not even read the console.
    monkeypatch.setattr(ssh_reachable, "datetime", FrozenClock(_FROZEN))

    async def _must_not_run(_sid: UUID, _reg: SecretRegistry) -> str:
        raise AssertionError("console must not be read for a reachable verdict")

    monkeypatch.setattr(ssh_reachable, "redacted_console_tail", _must_not_run)

    async def probe(_host: str, _port: int) -> ReachResult:
        return ReachResult.ok()

    async def _run() -> str | None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                job = _job_for(system_id)
                return await check_ssh_reachable_handler(
                    conn,
                    job,
                    resolver=_resolver(("127.0.0.1", 22001)),
                    secret_registry=SecretRegistry(),
                    probe=probe,
                )

    raw = asyncio.run(_run())
    assert raw is not None and "console_tail" not in raw


def test_handler_dead_letters_when_system_not_ready(migrated_url: str) -> None:
    async def probe(_host: str, _port: int) -> ReachResult:
        raise AssertionError("probe must not run for a non-ready System")

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn, state="torn_down")
                job = _job_for(system_id)
                with pytest.raises(CategorizedError) as excinfo:
                    await check_ssh_reachable_handler(
                        conn,
                        job,
                        resolver=_resolver(("127.0.0.1", 22001)),
                        secret_registry=SecretRegistry(),
                        probe=probe,
                    )
                assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
                assert excinfo.value.details["reason"] == "system_not_ready"
                assert (
                    str(excinfo.value) == "system is no longer ready; cannot probe SSH reachability"
                )

    asyncio.run(_run())


def test_handler_dead_letters_when_no_forward(migrated_url: str) -> None:
    async def probe(_host: str, _port: int) -> ReachResult:
        raise AssertionError("probe must not run when there is no recorded forward")

    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2, open=False) as pool:
            await pool.open()
            async with pool.connection() as conn:
                system_id = await _seed_system(conn)
                job = _job_for(system_id)
                resolver = _resolver(None)
                with pytest.raises(CategorizedError) as excinfo:
                    await check_ssh_reachable_handler(
                        conn,
                        job,
                        resolver=resolver,
                        secret_registry=SecretRegistry(),
                        probe=probe,
                    )
                assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
                assert excinfo.value.details["reason"] == "ssh_not_provisioned"
                assert str(excinfo.value) == (
                    "This System's provider exposes no loopback SSH forward; direct SSH to a "
                    "System is a local-libvirt capability"
                )
                # The forward is looked up for the System's derived domain via its own binding.
                resolver.binding_for_system.assert_awaited_once_with(conn, system_id)
                connector = resolver.binding_for_system.return_value.runtime.connector
                connector.recorded_ssh_endpoint.assert_called_once_with(
                    SystemHandle(domain_name_for(system_id))
                )

    asyncio.run(_run())

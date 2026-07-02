"""Tests for the check_ssh_reachable probe primitives and worker handler (ADR-0298, #972)."""

from __future__ import annotations

import asyncio
import socket

from kdive.jobs.handlers.ssh_reachable import (
    ReachResult,
    _real_probe,
    serialize_reach_verdict,
)


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

    assert asyncio.run(_run()) == ReachResult(True, "reachable")


def test_probe_no_ssh_banner_when_wrong_prefix() -> None:
    async def _run() -> ReachResult:
        host, port, server = await _serve(b"HELLO not ssh\r\n")
        async with server:
            return await _real_probe(host, port, deadline_s=3.0)

    assert asyncio.run(_run()) == ReachResult(False, "no SSH banner")


def test_probe_no_ssh_banner_when_server_sends_nothing() -> None:
    async def _run() -> ReachResult:
        host, port, server = await _serve(b"")
        async with server:
            return await _real_probe(host, port, deadline_s=1.0)

    assert asyncio.run(_run()) == ReachResult(False, "no SSH banner")


def test_probe_unreachable_on_closed_port() -> None:
    async def _run() -> ReachResult:
        return await _real_probe("127.0.0.1", _free_port(), deadline_s=1.0)

    assert asyncio.run(_run()) == ReachResult(False, "unreachable")


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

    assert asyncio.run(_run()) == ReachResult(True, "reachable")


def test_serialize_verdict_is_compact_and_redacted() -> None:
    raw = serialize_reach_verdict(
        ReachResult(False, "no SSH banner"), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00"
    )
    assert raw == (
        '{"reachable":false,"checked_at":"2026-07-02T00:00:00+00:00",'
        '"endpoint":{"host":"127.0.0.1","port":22001},"detail":"no SSH banner"}'
    )

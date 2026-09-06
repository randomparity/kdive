"""Closed native-only fault barrier tests (ADR-0622)."""

from __future__ import annotations

import asyncio
import gc
import json
import os
import socket
from pathlib import Path
from uuid import uuid4

import pytest

from kdive.providers.external_boot_authority.proof_barrier import (
    AuthorityProofBarrier,
    ProofBarrierShutdown,
)
from kdive.providers.external_boot_authority.protocol import AuthorityOperation


async def _request(path: Path, document: dict[str, object]) -> dict[str, object]:
    reader, writer = await asyncio.open_unix_connection(path)
    encoded = json.dumps(document, separators=(",", ":")).encode()
    writer.write(len(encoded).to_bytes(4, "big") + encoded)
    await writer.drain()
    size = int.from_bytes(await reader.readexactly(4), "big")
    response = json.loads(await reader.readexactly(size))
    writer.close()
    await writer.wait_closed()
    return response


@pytest.mark.anyio
async def test_barrier_is_closed_until_one_exact_arm_reaches_and_releases(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    barrier = AuthorityProofBarrier(path, operator_uid=os.geteuid())
    await barrier.start()
    system_id = uuid4()
    run_id = uuid4()
    try:
        assert await _request(path, {"action": "status"}) == {"state": "idle"}
        assert await _request(
            path,
            {
                "action": "arm",
                "system_id": str(system_id),
                "run_id": str(run_id),
                "operation": "activate",
                "checkpoint": "before-provider",
            },
        ) == {"state": "armed"}
        assert await _request(
            path,
            {
                "action": "arm",
                "system_id": str(uuid4()),
                "run_id": str(uuid4()),
                "operation": "recover",
                "checkpoint": "after-provider",
            },
        ) == {"state": "busy"}

        unmatched = asyncio.create_task(
            barrier.checkpoint(uuid4(), run_id, AuthorityOperation.ACTIVATE, "before-provider")
        )
        await unmatched
        wrong_run = asyncio.create_task(
            barrier.checkpoint(system_id, uuid4(), AuthorityOperation.ACTIVATE, "before-provider")
        )
        await asyncio.sleep(0)
        assert wrong_run.done()
        await wrong_run
        assert await _request(path, {"action": "status"}) == {"state": "armed"}
        checkpoint = asyncio.create_task(
            barrier.checkpoint(system_id, run_id, AuthorityOperation.ACTIVATE, "before-provider")
        )
        for _ in range(20):
            if await _request(path, {"action": "status"}) == {"state": "reached"}:
                break
            await asyncio.sleep(0)
        else:
            pytest.fail("matching checkpoint did not reach the armed barrier")
        assert await _request(path, {"action": "release"}) == {"state": "released"}
        await checkpoint
        assert await _request(path, {"action": "status"}) == {"state": "idle"}
    finally:
        await barrier.close()


@pytest.mark.anyio
async def test_barrier_rejects_malformed_oversize_and_unauthorized_peers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "control.sock"
    barrier = AuthorityProofBarrier(path, operator_uid=os.geteuid())
    await barrier.start()
    try:
        assert await _request(path, {"action": "unknown"}) == {"error": "invalid-request"}
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((1025).to_bytes(4, "big"))
        await writer.drain()
        size = int.from_bytes(await reader.readexactly(4), "big")
        assert json.loads(await reader.readexactly(size)) == {"error": "invalid-request"}
        writer.close()
        await writer.wait_closed()

        monkeypatch.setattr(barrier, "_peer_uid", lambda _writer: os.geteuid() + 1)
        reader, writer = await asyncio.open_unix_connection(path)
        writer.write((0).to_bytes(4, "big"))
        await writer.drain()
        with pytest.raises(ConnectionError):
            await reader.read()
        writer.close()
        with pytest.raises(ConnectionError):
            await writer.wait_closed()
    finally:
        await barrier.close()


@pytest.mark.anyio
async def test_shutdown_aborts_a_reached_checkpoint_without_releasing_it(tmp_path: Path) -> None:
    barrier = AuthorityProofBarrier(tmp_path / "control.sock", operator_uid=os.geteuid())
    await barrier.start()
    system_id = uuid4()
    run_id = uuid4()
    try:
        await barrier.arm(system_id, run_id, AuthorityOperation.ACTIVATE, "after-provider")
        checkpoint = asyncio.create_task(
            barrier.checkpoint(system_id, run_id, AuthorityOperation.ACTIVATE, "after-provider")
        )
        await barrier.reached.wait()
        await barrier.close()
        with pytest.raises(ProofBarrierShutdown):
            await checkpoint
    finally:
        await barrier.close()


@pytest.mark.anyio
async def test_shutdown_consumes_an_unwaited_arm_failure(tmp_path: Path) -> None:
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        barrier = AuthorityProofBarrier(tmp_path / "control.sock", operator_uid=os.geteuid())
        await barrier.arm(uuid4(), uuid4(), AuthorityOperation.ACTIVATE, "after-provider")
        await barrier.close()
        del barrier
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous)

    assert not [
        context
        for context in contexts
        if context.get("message") == "Future exception was never retrieved"
    ]


@pytest.mark.anyio
async def test_barrier_refuses_to_replace_a_live_owner_socket(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    listener.listen()
    try:
        with pytest.raises(OSError, match="live-socket"):
            await AuthorityProofBarrier(path, operator_uid=os.geteuid()).start()
    finally:
        listener.close()

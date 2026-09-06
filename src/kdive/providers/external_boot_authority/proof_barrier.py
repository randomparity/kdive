"""Disabled-by-default native authority fault barrier (ADR-0622)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from kdive.providers.external_boot_authority.protocol import AuthorityOperation
from kdive.providers.external_boot_authority.transport import check_stale_socket

type ProofCheckpoint = Literal["before-provider", "after-provider"]

_MAX_FRAME_BYTES = 1024
_PEER_SIZE = struct.calcsize("3i")


class ProofBarrierShutdown(RuntimeError):
    """A service shutdown aborted a paused proof checkpoint."""


@dataclass(slots=True)
class _Arm:
    system_id: UUID
    run_id: UUID
    operation: AuthorityOperation
    checkpoint: ProofCheckpoint
    release: asyncio.Future[None]
    reached: bool = False


class AuthorityProofBarrier:
    """One root-operated pause point around an existing authority provider commit."""

    def __init__(self, socket_path: Path, *, operator_uid: int = 0) -> None:
        self._socket_path = socket_path
        self._operator_uid = operator_uid
        self._server: asyncio.AbstractServer | None = None
        self._identity: tuple[int, int] | None = None
        self._armed: _Arm | None = None
        self._closed = False
        self._lock = asyncio.Lock()
        self.reached = asyncio.Event()

    async def start(self) -> None:
        """Bind the fixed authority-owned control socket only when explicitly configured."""
        check_stale_socket(self._socket_path, os.geteuid())
        self._server = await asyncio.start_unix_server(self._handle, path=str(self._socket_path))
        os.chmod(self._socket_path, 0o600, follow_symlinks=False)
        status = os.stat(self._socket_path, follow_symlinks=False)
        if (
            not stat.S_ISSOCK(status.st_mode)
            or status.st_uid != os.geteuid()
            or stat.S_IMODE(status.st_mode) != 0o600
        ):
            await self.close()
            raise OSError("proof socket has invalid metadata")
        self._identity = status.st_dev, status.st_ino

    def _peer_uid(self, writer: asyncio.StreamWriter) -> int:
        connection = writer.get_extra_info("socket")
        if connection is None:
            raise PermissionError("proof peer is unavailable")
        raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEER_SIZE)
        if not isinstance(raw, bytes) or len(raw) != _PEER_SIZE:
            raise PermissionError("proof peer credentials are incomplete")
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if self._peer_uid(writer) != self._operator_uid:
                return
            try:
                size = int.from_bytes(await reader.readexactly(4), "big")
                if size > _MAX_FRAME_BYTES:
                    raise ValueError
                document = json.loads(await reader.readexactly(size))
                response = await self._dispatch(document)
            except asyncio.IncompleteReadError, TypeError, ValueError, json.JSONDecodeError:
                response = {"error": "invalid-request"}
            encoded = json.dumps(response, separators=(",", ":"), sort_keys=True).encode()
            writer.write(len(encoded).to_bytes(4, "big") + encoded)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, document: object) -> dict[str, str]:
        if not isinstance(document, dict) or not all(isinstance(key, str) for key in document):
            return {"error": "invalid-request"}
        data = cast(dict[str, object], document)
        action = data.get("action")
        if action == "status" and set(data) == {"action"}:
            return {"state": await self.status()}
        if action == "release" and set(data) == {"action"}:
            return {"state": await self.release()}
        if action != "arm" or set(data) != {
            "action",
            "system_id",
            "run_id",
            "operation",
            "checkpoint",
        }:
            return {"error": "invalid-request"}
        try:
            system_raw = data["system_id"]
            run_raw = data["run_id"]
            operation_raw = data["operation"]
            checkpoint = data["checkpoint"]
            if (
                not isinstance(system_raw, str)
                or not isinstance(run_raw, str)
                or not isinstance(operation_raw, str)
            ):
                raise ValueError
            system_id = UUID(system_raw)
            run_id = UUID(run_raw)
            operation = AuthorityOperation(operation_raw)
            if checkpoint not in {"before-provider", "after-provider"}:
                raise ValueError
        except AttributeError, TypeError, ValueError:
            return {"error": "invalid-request"}
        return {
            "state": await self.arm(system_id, run_id, operation, cast(ProofCheckpoint, checkpoint))
        }

    async def arm(
        self,
        system_id: UUID,
        run_id: UUID,
        operation: AuthorityOperation,
        checkpoint: ProofCheckpoint,
    ) -> Literal["armed", "busy"]:
        """Reserve the only pause point for one exact existing mutation."""
        async with self._lock:
            if self._closed:
                return "busy"
            if self._armed is not None:
                return "busy"
            self.reached = asyncio.Event()
            self._armed = _Arm(
                system_id=system_id,
                run_id=run_id,
                operation=operation,
                checkpoint=checkpoint,
                release=asyncio.get_running_loop().create_future(),
            )
            return "armed"

    async def status(self) -> Literal["idle", "armed", "reached", "released"]:
        """Report only the single arm's state; it exposes no provider details."""
        async with self._lock:
            armed = self._armed
            if armed is None:
                return "idle"
            if armed.release.done():
                return "released"
            return "reached" if armed.reached else "armed"

    async def release(self) -> Literal["released", "idle"]:
        """Release the one matching arm without selecting a provider action."""
        async with self._lock:
            armed = self._armed
            if armed is None:
                return "idle"
            if not armed.release.done():
                armed.release.set_result(None)
            return "released"

    async def checkpoint(
        self,
        system_id: UUID,
        run_id: UUID,
        operation: AuthorityOperation,
        checkpoint: ProofCheckpoint,
    ) -> None:
        """Pause only the armed exact mutation at the requested fixed checkpoint."""
        async with self._lock:
            armed = self._armed
            if armed is None or (
                armed.system_id,
                armed.run_id,
                armed.operation,
                armed.checkpoint,
            ) != (system_id, run_id, operation, checkpoint):
                return
            armed.reached = True
            self.reached.set()
            release = armed.release
        await release
        async with self._lock:
            if self._armed is armed:
                self._armed = None

    async def close(self) -> None:
        """Abort, never release, a checkpoint while the authority shuts down."""
        async with self._lock:
            self._closed = True
            armed = self._armed
            if armed is not None and not armed.release.done():
                armed.release.set_exception(
                    ProofBarrierShutdown("proof checkpoint aborted by shutdown")
                )
                # Retrieval suppresses an unwaited-Future loop warning without changing the
                # exception a checkpoint already awaiting this Future receives.
                armed.release.exception()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        try:
            status = os.stat(self._socket_path, follow_symlinks=False)
        except FileNotFoundError:
            return
        if (
            self._identity is not None
            and stat.S_ISSOCK(status.st_mode)
            and (status.st_dev, status.st_ino) == self._identity
        ):
            self._socket_path.unlink()

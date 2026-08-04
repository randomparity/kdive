"""Init-only worker credential handoff behavior."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from kdive.processes.lifecycle.kubernetes_credential_broker import BrokerReply, BrokerRequest
from kdive.processes.lifecycle.kubernetes_credential_init import (
    KubernetesCredentialInit,
    write_credential,
)


def test_init_writes_mode_0400_with_fsync_and_atomic_rename(tmp_path: Path, monkeypatch) -> None:
    credential_path = tmp_path / "worker-incarnation-credential"
    calls: list[tuple[str, Path]] = []
    monkeypatch.setattr(
        "kdive.processes.lifecycle.kubernetes_credential_init.os.fsync",
        lambda fd: calls.append(("fsync", credential_path)),
    )
    monkeypatch.setattr(
        "kdive.processes.lifecycle.kubernetes_credential_init.os.replace",
        lambda source, target: (calls.append(("replace", Path(target))), os.rename(source, target)),
    )

    write_credential(credential_path, "credential-value")

    assert credential_path.read_text(encoding="utf-8") == "credential-value\n"
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o400
    assert calls == [("fsync", credential_path), ("replace", credential_path)]
    assert not list(tmp_path.glob("*.tmp"))


def test_init_retries_dropped_delivery_and_ack_without_requesting_secret_after_ack(
    tmp_path: Path,
) -> None:
    credential_path = tmp_path / "worker-incarnation-credential"
    requests: list[str] = []
    attempts = {"deliver": 0, "ack": 0}

    async def exchange(request: BrokerRequest) -> BrokerReply:
        requests.append(request.operation)
        attempts[request.operation] += 1
        if request.operation == "deliver":
            if attempts["deliver"] == 1:
                raise asyncio.IncompleteReadError(b"", 4)
            return BrokerReply(credential="credential-value")
        if attempts["ack"] == 1:
            raise OSError("dropped acknowledgment response")
        return BrokerReply(acknowledged=True)

    init = KubernetesCredentialInit(
        namespace="kdive",
        name="kdive-worker-0",
        uid="uid-1",
        credential_path=credential_path,
        token=lambda: "bound-token",
        exchange=exchange,
        retry_delay=0,
    )
    asyncio.run(init.run())

    assert credential_path.read_text(encoding="utf-8") == "credential-value\n"
    assert requests == ["deliver", "deliver", "ack", "ack"]

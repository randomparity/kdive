"""The Compose Docker authority exposes exact gated worker lifecycle."""

import asyncio
import hashlib
import threading
from typing import cast

import pytest

from kdive.processes.lifecycle.compose.docker_death_api import (
    WorkerLifecycleGate,
    permitted_inspect_path,
)

_CREDENTIAL = "c" * 64  # pragma: allowlist secret


def test_only_exact_container_inspect_get_is_permitted() -> None:
    container_id = "a" * 64
    assert permitted_inspect_path("GET", f"/containers/{container_id}/json")
    assert not permitted_inspect_path("GET", f"/containers/{container_id[:12]}/json")
    assert not permitted_inspect_path("POST", f"/containers/{container_id}/json")
    assert not permitted_inspect_path("GET", "/containers/json")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/logs")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/archive?path=/")
    assert not permitted_inspect_path("GET", f"/containers/{container_id}/json?size=1")


async def _done(*args: object) -> None:
    return None


def _worker(container_id: str, nonce: str, *, status: str) -> dict[str, object]:
    return {
        "Id": container_id,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "kdive",
                "com.docker.compose.service": "worker",
                "io.kdive.managed-worker": "true",
            },
            "Env": [f"KDIVE_WORKER_INCARNATION_ID=docker:{nonce}"],
        },
        "State": {"Status": status, "ExitCode": 0},
    }


def test_gate_commits_binding_before_start_and_termination_before_remove() -> None:
    container_id = "a" * 64
    nonce = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    state = _worker(container_id, nonce, status="created")
    events: list[tuple[str, object]] = []

    def inspect(value: str) -> dict[str, object]:
        status = cast(dict[str, object], state["State"])["Status"]
        events.append(("inspect", status))
        return state

    async def register(holder: str, binding: str, credential_hash: bytes) -> None:
        events.append(("register", (holder, binding, credential_hash)))

    async def inject(value: str, credential: str) -> None:
        events.append(("inject", (value, credential)))

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        events.append(("terminate", f"{holder}:{outcome}"))

    async def start(value: str) -> None:
        assert events[-1][0] == "inject"
        events.append(("start", value))
        state["State"] = {"Status": "running", "ExitCode": 0}

    async def stop(value: str) -> None:
        events.append(("stop", value))
        state["State"] = {"Status": "exited", "ExitCode": 0}

    async def remove(value: str) -> None:
        assert events[-1][0] == "terminate"
        events.append(("remove", value))

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=inspect,
        register=register,
        terminate=terminate,
        credential=lambda: _CREDENTIAL,
        inject=inject,
        start=start,
        stop=stop,
        remove=remove,
    )
    asyncio.run(gate.register_and_start(container_id))
    create_events = [event[0] for event in events]
    assert create_events == ["inspect", "register", "inject", "start"]
    assert events[1][1] == (
        f"docker:{nonce}",
        container_id,
        hashlib.sha256(_CREDENTIAL.encode()).digest(),
    )

    events.clear()
    asyncio.run(gate.terminate_and_remove(container_id))
    assert [event[0] for event in events][1:] == [
        "stop",
        "inspect",
        "terminate",
        "remove",
    ]
    assert str(events[-2][1]).endswith(":succeeded")


def test_gate_offloads_inspect_and_credential_io_from_the_event_loop() -> None:
    event_loop_thread = threading.get_ident()
    callback_threads: dict[str, int] = {}
    container_id = "a" * 64
    state = _worker(container_id, "0" * 32, status="created")

    def inspect(value: str) -> dict[str, object]:
        callback_threads["inspect"] = threading.get_ident()
        return state

    def credential() -> str:
        callback_threads["credential"] = threading.get_ident()
        return _CREDENTIAL

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=inspect,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=lambda holder, binding, outcome: _done(),
        credential=credential,
        inject=lambda value, supplied: _done(),
        start=_done,
        stop=_done,
        remove=_done,
    )

    asyncio.run(gate.register_and_start(container_id))

    assert set(callback_threads) == {"inspect", "credential"}
    assert all(thread != event_loop_thread for thread in callback_threads.values())


def test_registration_outage_prevents_credential_injection_and_start() -> None:
    container_id = "a" * 64
    state = _worker(container_id, "0" * 32, status="created")
    events: list[str] = []

    async def register(holder: str, binding: str, credential_hash: bytes) -> None:
        events.append("register")
        raise RuntimeError("database unavailable")

    async def inject(value: str, credential: str) -> None:
        events.append("inject")

    async def start(value: str) -> None:
        events.append("start")

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=register,
        terminate=lambda holder, binding, outcome: _done(),
        credential=lambda: _CREDENTIAL,
        inject=inject,
        start=start,
        stop=_done,
        remove=_done,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(gate.register_and_start(container_id))

    assert events == ["register"]


def test_termination_outage_retains_terminal_container() -> None:
    container_id = "a" * 64
    state = _worker(container_id, "0" * 32, status="running")
    events: list[str] = []

    async def stop(value: str) -> None:
        events.append("stop")
        state["State"] = {"Status": "exited", "ExitCode": 137}

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        events.append("terminate")
        raise RuntimeError("database unavailable")

    async def remove(value: str) -> None:
        events.append("remove")

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=terminate,
        credential=lambda: _CREDENTIAL,
        inject=lambda value, credential: _done(),
        start=_done,
        stop=stop,
        remove=remove,
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(gate.terminate_and_remove(container_id))

    assert events == ["stop", "terminate"]


def test_gate_records_sigkill_as_killed_before_removal() -> None:
    container_id = "a" * 64
    nonce = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    state = _worker(container_id, nonce, status="exited")
    state["State"] = {"Status": "exited", "ExitCode": 137, "OOMKilled": False}
    outcomes: list[str] = []

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        outcomes.append(outcome)

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=terminate,
        credential=lambda: _CREDENTIAL,
        inject=lambda value, credential: _done(),
        start=_done,
        stop=_done,
        remove=_done,
    )

    asyncio.run(gate.terminate_and_remove(container_id))

    assert outcomes == ["killed"]


def test_gate_reconciles_retained_terminal_worker_before_requesting_replacement() -> None:
    container_id = "a" * 64
    nonce = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    state = _worker(container_id, nonce, status="exited")
    events: list[str] = []

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        events.append("terminate")

    async def remove(value: str) -> None:
        events.append("remove")

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=terminate,
        credential=lambda: _CREDENTIAL,
        inject=lambda value, credential: _done(),
        start=_done,
        stop=_done,
        remove=remove,
    )

    assert asyncio.run(gate.reconcile(container_id)) is True
    assert events == ["terminate", "remove"]


def test_gate_refuses_short_ids_and_wrong_exact_binding() -> None:
    container_id = "a" * 64
    nonce = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    state = _worker(container_id, nonce, status="running")
    removed = False

    async def remove(value: str) -> None:
        nonlocal removed
        removed = True

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=lambda holder, binding, outcome: _done(),
        credential=lambda: _CREDENTIAL,
        inject=lambda value, credential: _done(),
        start=_done,
        stop=_done,
        remove=remove,
    )
    for invalid in ("a" * 12, "b" * 64):
        try:
            asyncio.run(gate.terminate_and_remove(invalid))
        except RuntimeError:
            pass
        else:
            raise AssertionError("invalid exact identity must fail closed")
    assert removed is False


def test_runtime_absence_never_publishes_termination() -> None:
    container_id = "a" * 64
    terminated = False

    async def terminate(holder: str, binding: dict[str, str], outcome: str) -> None:
        nonlocal terminated
        terminated = True

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: None,
        register=lambda holder, binding, credential_hash: _done(),
        terminate=terminate,
        credential=lambda: _CREDENTIAL,
        inject=lambda value, credential: _done(),
        start=_done,
        stop=_done,
        remove=_done,
    )

    with pytest.raises(RuntimeError, match="exact worker container"):
        asyncio.run(gate.terminate_and_remove(container_id))

    assert terminated is False


@pytest.mark.parametrize("credential", ["a" * 63, "g" * 64, "a" * 66])
def test_gate_rejects_non_256_bit_hex_credentials(credential: str) -> None:
    container_id = "a" * 64
    state = _worker(container_id, "0" * 32, status="created")
    registered = False

    async def register(holder: str, binding: str, credential_hash: bytes) -> None:
        nonlocal registered
        registered = True

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=register,
        terminate=lambda holder, binding, outcome: _done(),
        credential=lambda: credential,
        inject=lambda value, supplied: _done(),
        start=_done,
        stop=_done,
        remove=_done,
    )

    with pytest.raises(RuntimeError, match="256-bit"):
        asyncio.run(gate.register_and_start(container_id))

    assert registered is False

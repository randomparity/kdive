"""The Compose Docker authority exposes exact gated worker lifecycle."""

import asyncio

from kdive.processes.docker_death_api import WorkerLifecycleGate, permitted_inspect_path


def test_only_exact_container_inspect_get_is_permitted() -> None:
    container_id = "a" * 64
    assert permitted_inspect_path("GET", f"/containers/{container_id}/json")
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
    events: list[tuple[str, str]] = []

    async def register(holder: str, binding: str) -> None:
        events.append(("register", f"{holder}:{binding}"))

    async def terminate(holder: str, outcome: str) -> None:
        events.append(("terminate", f"{holder}:{outcome}"))

    async def start(value: str) -> None:
        assert events[-1][0] == "register"
        events.append(("start", value))
        state["State"] = {"Status": "exited", "ExitCode": 0}

    async def remove(value: str) -> None:
        assert events[-1][0] == "terminate"
        events.append(("remove", value))

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=register,
        terminate=terminate,
        start=start,
        stop=_done,
        remove=remove,
    )
    asyncio.run(gate.register_and_start(container_id))
    asyncio.run(gate.terminate_and_remove(container_id))
    assert [event[0] for event in events] == ["register", "start", "terminate", "remove"]
    assert events[-2][1].endswith(":succeeded")


def test_gate_records_sigkill_as_killed_before_removal() -> None:
    container_id = "a" * 64
    nonce = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
    state = _worker(container_id, nonce, status="exited")
    state["State"] = {"Status": "exited", "ExitCode": 137, "OOMKilled": False}
    outcomes: list[str] = []

    async def terminate(holder: str, outcome: str) -> None:
        outcomes.append(outcome)

    gate = WorkerLifecycleGate(
        project="kdive",
        inspect=lambda value: state,
        register=lambda holder, binding: _done(),
        terminate=terminate,
        start=_done,
        stop=_done,
        remove=_done,
    )

    asyncio.run(gate.terminate_and_remove(container_id))

    assert outcomes == ["killed"]


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
        register=lambda holder, binding: _done(),
        terminate=lambda holder, outcome: _done(),
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

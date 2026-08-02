"""The reference Compose wrapper preserves worker-incarnation evidence."""

import asyncio
from typing import cast

import pytest

from kdive.processes.compose_worker_lifecycle import ComposeWorkerLifecycle

type _CommandEvent = tuple[tuple[str, ...], dict[str, str] | None]
_NONCE = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret


def _commands(events: list[tuple[str, object]]) -> list[_CommandEvent]:
    return [cast(_CommandEvent, value) for name, value in events if name == "command"]


class _Gate:
    def __init__(self, events: list[tuple[str, object]], *, fail_register: bool = False) -> None:
        self.events = events
        self.fail_register = fail_register

    async def register_and_start(self, container_id: str) -> None:
        self.events.append(("register-and-start", container_id))
        if self.fail_register:
            raise RuntimeError("database unavailable")

    async def terminate_and_remove(self, container_id: str) -> None:
        self.events.append(("terminate-and-remove", container_id))


def _lifecycle(
    *, fail_register: bool = False, initially_created: bool = False
) -> tuple[ComposeWorkerLifecycle, list[tuple[str, object]]]:
    events: list[tuple[str, object]] = []
    container_id = "a" * 64
    created = initially_created

    def command(argv: tuple[str, ...], env: dict[str, str] | None = None) -> str:
        nonlocal created
        events.append(("command", (argv, env)))
        if argv[-3:] == ("create", "--no-recreate", "worker"):
            created = True
        return container_id if created and argv[-4:] == ("ps", "--all", "-q", "worker") else ""

    return (
        ComposeWorkerLifecycle(
            command=command,
            gate=_Gate(events, fail_register=fail_register),
            nonce=lambda: _NONCE,
        ),
        events,
    )


def test_up_starts_stack_then_creates_binds_and_starts_exact_worker() -> None:
    lifecycle, events = _lifecycle()

    asyncio.run(lifecycle.up())

    commands = _commands(events)
    assert commands[0][0] == ("docker", "compose", "up", "-d")
    assert commands[2][0][-3:] == ("create", "--no-recreate", "worker")
    assert commands[2][1] == {"KDIVE_WORKER_INCARNATION_NONCE": _NONCE}
    assert events[-1] == ("register-and-start", "a" * 64)


def test_database_failure_leaves_never_started_worker_for_retry() -> None:
    lifecycle, events = _lifecycle(fail_register=True)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(lifecycle.up())

    assert not any(event[0][-2:] == ("start", "worker") for event in _commands(events))


def test_recreate_terminates_old_generation_before_creating_new_one() -> None:
    lifecycle, events = _lifecycle(initially_created=True)

    asyncio.run(lifecycle.recreate())

    assert events[1] == ("terminate-and-remove", "a" * 64)
    create_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "command"
        and cast(_CommandEvent, event[1])[0][-3:] == ("create", "--no-recreate", "worker")
    )
    assert create_index > 1


def test_down_terminates_worker_before_database_and_gate_services() -> None:
    lifecycle, events = _lifecycle(initially_created=True)

    asyncio.run(lifecycle.down(volumes=True))

    assert events[1] == ("terminate-and-remove", "a" * 64)
    assert events[-1] == (
        "command",
        (("docker", "compose", "down", "--volumes", "--remove-orphans"), None),
    )

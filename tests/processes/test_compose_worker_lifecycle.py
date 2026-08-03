"""The reference Compose wrapper preserves worker-incarnation evidence."""

import asyncio
import io
import tarfile
from pathlib import Path
from typing import cast

import pytest

from kdive.processes import compose_worker_lifecycle
from kdive.processes.compose_worker_lifecycle import ComposeWorkerLifecycle

type _CommandEvent = tuple[tuple[str, ...], dict[str, str] | None]
_NONCE = "0123456789abcdef0123456789abcdef"  # pragma: allowlist secret
_CREDENTIAL = "c" * 64  # pragma: allowlist secret


def _commands(events: list[tuple[str, object]]) -> list[_CommandEvent]:
    return [cast(_CommandEvent, value) for name, value in events if name == "command"]


class _Gate:
    def __init__(
        self,
        events: list[tuple[str, object]],
        *,
        fail_register: bool = False,
        needs_replacement: bool = False,
    ) -> None:
        self.events = events
        self.fail_register = fail_register
        self.needs_replacement = needs_replacement

    async def register_and_start(self, container_id: str) -> None:
        self.events.append(("register-and-start", container_id))
        if self.fail_register:
            raise RuntimeError("database unavailable")

    async def terminate_and_remove(self, container_id: str) -> None:
        self.events.append(("terminate-and-remove", container_id))

    async def reconcile(self, container_id: str) -> bool:
        self.events.append(("reconcile", container_id))
        return self.needs_replacement


def _lifecycle(
    *,
    fail_register: bool = False,
    initially_created: bool = False,
    needs_replacement: bool = False,
    retained_credential: bool = False,
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
            gate=_Gate(
                events,
                fail_register=fail_register,
                needs_replacement=needs_replacement,
            ),
            nonce=lambda: _NONCE,
            credential_retained=lambda: retained_credential,
            cleanup_credential=lambda: events.append(("cleanup-credential", None)),
        ),
        events,
    )


def test_up_starts_stack_then_creates_binds_and_starts_exact_worker() -> None:
    lifecycle, events = _lifecycle()

    asyncio.run(lifecycle.up())

    commands = _commands(events)
    assert commands[0][0] == (
        "docker",
        "compose",
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "120",
    )
    assert commands[2][0][-3:] == ("create", "--no-recreate", "worker")
    assert commands[2][1] == {"KDIVE_WORKER_INCARNATION_NONCE": _NONCE}
    assert events[-1] == ("register-and-start", "a" * 64)


def test_database_failure_leaves_never_started_worker_for_retry() -> None:
    lifecycle, events = _lifecycle(fail_register=True)

    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(lifecycle.up())

    assert not any(event[0][-2:] == ("start", "worker") for event in _commands(events))


def test_retry_resumes_an_existing_never_started_worker() -> None:
    lifecycle, events = _lifecycle(initially_created=True)

    asyncio.run(lifecycle.up())

    assert ("reconcile", "a" * 64) in events
    assert not any(
        event[0][-3:] == ("create", "--no-recreate", "worker") for event in _commands(events)
    )


def test_up_reconciles_terminal_worker_before_creating_replacement() -> None:
    lifecycle, events = _lifecycle(initially_created=True, needs_replacement=True)

    asyncio.run(lifecycle.up())

    assert ("reconcile", "a" * 64) in events
    assert any(
        event[0][-3:] == ("create", "--no-recreate", "worker") for event in _commands(events)
    )


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
    assert events[2] == ("cleanup-credential", None)


def test_down_terminates_worker_before_database_and_gate_services() -> None:
    lifecycle, events = _lifecycle(initially_created=True)

    asyncio.run(lifecycle.down(volumes=True))

    assert events[1] == ("terminate-and-remove", "a" * 64)
    assert events[2] == ("cleanup-credential", None)
    assert events[-1] == (
        "command",
        (("docker", "compose", "down", "--volumes", "--remove-orphans"), None),
    )


def test_absent_worker_with_retained_credential_refuses_destructive_bypass() -> None:
    lifecycle, events = _lifecycle(retained_credential=True)

    with pytest.raises(RuntimeError, match="retained worker credential"):
        asyncio.run(lifecycle.down(volumes=True))

    assert not any(command[0][2:3] == ("down",) for command in _commands(events))


def test_credential_archive_is_worker_only_and_mode_0400() -> None:
    archive_factory = getattr(compose_worker_lifecycle, "_credential_archive", None)
    assert archive_factory is not None
    with tarfile.open(fileobj=io.BytesIO(archive_factory(_CREDENTIAL)), mode="r:") as archive:
        directory = archive.getmember("kdive")
        handoff = archive.getmember("kdive/worker-incarnation-credential")
        extracted = archive.extractfile(handoff)

    assert directory.isdir()
    assert (directory.uid, directory.gid, directory.mode) == (10001, 10001, 0o700)
    assert (handoff.uid, handoff.gid, handoff.mode) == (10001, 10001, 0o400)
    assert extracted is not None
    assert extracted.read().decode() == _CREDENTIAL


def test_credential_handoff_refuses_a_symlink_without_touching_its_target(tmp_path: Path) -> None:
    target = tmp_path / "operator-data"
    target.write_text("do-not-overwrite", encoding="utf-8")
    handoff = tmp_path / "credential"
    handoff.symlink_to(target)

    with pytest.raises(RuntimeError, match="safe supervisor-owned file"):
        compose_worker_lifecycle._prepare_credential_file(handoff)

    assert target.read_text(encoding="utf-8") == "do-not-overwrite"


def test_generated_credential_is_durable_before_database_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handoff = tmp_path / "credential"
    compose_worker_lifecycle._prepare_credential_file(handoff)
    monkeypatch.setattr(compose_worker_lifecycle.secrets, "token_hex", lambda size: _CREDENTIAL)

    assert compose_worker_lifecycle._credential(handoff) == _CREDENTIAL
    assert handoff.read_text(encoding="utf-8") == _CREDENTIAL
    assert handoff.stat().st_mode & 0o777 == 0o600

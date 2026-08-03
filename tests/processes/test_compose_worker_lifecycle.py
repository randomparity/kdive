"""The reference Compose wrapper preserves worker-incarnation evidence."""

import asyncio
import io
import subprocess
import sys
import tarfile
import time
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
    clean_managed_volumes: bool = False,
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

    cleanup_arguments = (
        {"cleanup_managed_volumes": lambda: events.append(("cleanup-volumes", None))}
        if clean_managed_volumes
        else {}
    )
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
            **cleanup_arguments,
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


def test_down_volumes_removes_profile_only_managed_volumes_after_compose_down() -> None:
    lifecycle, events = _lifecycle(initially_created=True, clean_managed_volumes=True)

    asyncio.run(lifecycle.down(volumes=True))

    down_index = events.index(
        (
            "command",
            (("docker", "compose", "down", "--volumes", "--remove-orphans"), None),
        )
    )
    assert events[down_index + 1] == ("cleanup-volumes", None)


def test_managed_volume_cleanup_targets_only_exact_project_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def command(
        argv: tuple[str, ...],
        _env: dict[str, str] | None,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(compose_worker_lifecycle, "_bounded_command", command)

    compose_worker_lifecycle._remove_managed_worker_volumes("exact-project")

    assert calls == [
        (
            "docker",
            "volume",
            "rm",
            "--force",
            "exact-project_kdive-build",
            "exact-project_kdive-install",
        )
    ]


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


def test_inspect_times_out_with_a_dedicated_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, object] = {}

    def command(
        argv: tuple[str, ...],
        extra_env: dict[str, str] | None,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> str:
        observed.update(
            argv=argv,
            extra_env=extra_env,
            timeout=timeout,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(compose_worker_lifecycle, "_bounded_command", command, raising=False)

    with pytest.raises(RuntimeError, match="inspect.*second bound"):
        compose_worker_lifecycle._inspect("a" * 64)

    assert observed == {
        "argv": ("docker", "inspect", "a" * 64),
        "extra_env": None,
        "timeout": 5,
        "max_stdout_bytes": 1_048_576,
        "max_stderr_bytes": 65_536,
    }


def test_inspect_refuses_oversized_output_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed = False

    def command(*args: object, **kwargs: object) -> str:
        raise compose_worker_lifecycle.CommandOutputTooLarge("stdout", 1_048_576)

    def loads(value: str) -> object:
        nonlocal parsed
        parsed = True
        return []

    monkeypatch.setattr(compose_worker_lifecycle, "_bounded_command", command, raising=False)
    monkeypatch.setattr(compose_worker_lifecycle.json, "loads", loads)

    with pytest.raises(RuntimeError, match="inspect output exceeded"):
        compose_worker_lifecycle._inspect("a" * 64)

    assert parsed is False


@pytest.mark.parametrize(("descriptor", "stream"), [(1, "stdout"), (2, "stderr")])
def test_bounded_command_kills_oversized_output(descriptor: int, stream: str) -> None:
    with pytest.raises(compose_worker_lifecycle.CommandOutputTooLarge) as raised:
        compose_worker_lifecycle._bounded_command(
            (sys.executable, "-c", f"import os; os.write({descriptor}, b'x' * 65)"),
            None,
            timeout=5,
            max_stdout_bytes=64,
            max_stderr_bytes=64,
        )

    assert raised.value.stream == stream
    assert raised.value.limit == 64


def test_bounded_command_kills_a_stalled_child_at_its_deadline() -> None:
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        compose_worker_lifecycle._bounded_command(
            (sys.executable, "-c", "import time; time.sleep(10)"),
            None,
            timeout=0.1,
            max_stdout_bytes=64,
            max_stderr_bytes=64,
        )

    assert time.monotonic() - started < 2


def test_inspect_returns_only_the_bounded_identity_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = (
        '[{"Id":"' + "a" * 64 + '","Config":{"Labels":{'
        '"com.docker.compose.project":"kdive",'
        '"com.docker.compose.service":"worker",'
        '"io.kdive.managed-worker":"true"},'
        '"Env":["KDIVE_WORKER_INCARNATION_ID=docker:' + "0" * 32 + '"]},'
        '"State":{"Status":"exited","ExitCode":137,"OOMKilled":false},'
        '"UnboundedIgnored":{"nested":"value"}}]'
    )
    monkeypatch.setattr(
        compose_worker_lifecycle,
        "_bounded_command",
        lambda *args, **kwargs: document,
        raising=False,
    )

    assert compose_worker_lifecycle._inspect("a" * 64) == {
        "Id": "a" * 64,
        "Config": {
            "Labels": {
                "com.docker.compose.project": "kdive",
                "com.docker.compose.service": "worker",
                "io.kdive.managed-worker": "true",
            },
            "Env": [f"KDIVE_WORKER_INCARNATION_ID=docker:{'0' * 32}"],
        },
        "State": {"Status": "exited", "ExitCode": 137},
    }


def test_inspect_refuses_an_oversized_environment_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = (
        '[{"Id":"'
        + "a" * 64
        + '","Config":{"Labels":{},"Env":['
        + ",".join('"x"' for _ in range(513))
        + ']},"State":{"Status":"created","ExitCode":0}}]'
    )
    monkeypatch.setattr(
        compose_worker_lifecycle,
        "_bounded_command",
        lambda *args, **kwargs: document,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="bounded schema"):
        compose_worker_lifecycle._inspect("a" * 64)


def test_lifecycle_commands_use_operation_specific_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadlines: dict[tuple[str, ...], float] = {}

    def command(
        argv: tuple[str, ...],
        extra_env: dict[str, str] | None,
        *,
        timeout: float,
        max_stdout_bytes: int,
        max_stderr_bytes: int,
    ) -> str:
        deadlines[argv] = timeout
        return ""

    monkeypatch.setattr(compose_worker_lifecycle, "_bounded_command", command, raising=False)
    commands = (
        ("docker", "compose", "up", "-d"),
        ("docker", "compose", "--profile", "managed-worker", "create", "worker"),
        ("docker", "compose", "--profile", "managed-worker", "ps", "-q", "worker"),
        ("docker", "compose", "down"),
        ("docker", "start", "a" * 64),
        ("docker", "stop", "a" * 64),
        ("docker", "rm", "a" * 64),
    )
    for argv in commands:
        compose_worker_lifecycle._command(argv, None)

    assert deadlines == {
        commands[0]: 600,
        commands[1]: 120,
        commands[2]: 30,
        commands[3]: 120,
        commands[4]: 30,
        commands[5]: 45,
        commands[6]: 30,
    }


def test_project_command_pins_every_compose_operation_to_the_requested_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
    monkeypatch.setattr(
        compose_worker_lifecycle,
        "_command",
        lambda argv, env: observed.append((argv, env)) or "",
    )

    compose_worker_lifecycle._project_command(
        "kdive-proof-a1b2", ("docker", "compose", "ps"), {"EXTRA": "value"}
    )

    assert observed == [
        (
            ("docker", "compose", "ps"),
            {"COMPOSE_PROJECT_NAME": "kdive-proof-a1b2", "EXTRA": "value"},
        )
    ]

"""Tests for the authorize_ssh_key worker handler (ADR-0271, #782)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind, JobState
from kdive.jobs.handlers.ssh_authorize import (
    authorize_ssh_key_handler,
    build_authorize_argv,
)

_NOW = datetime(2025, 1, 1)
_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 agent@host"


def _job(public_key: str = _KEY) -> Job:
    return Job(
        id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        kind=JobKind.AUTHORIZE_SSH_KEY,
        payload={"system_id": str(uuid4()), "public_key": public_key},
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


def test_argv_is_fixed_and_excludes_the_key() -> None:
    argv = build_authorize_argv(22022)
    assert argv[0] == "ssh"
    assert "root@127.0.0.1" in argv
    assert "22022" in argv
    # The key is NEVER in the argv/command string — ssh would space-join post-host args into one
    # remotely-reparsed string. It travels on stdin instead. The post-host script is a single arg.
    assert _KEY not in argv
    assert argv.count(argv[-1]) == 1
    script = argv[-1]
    assert "flock" in script and "grep -qxF" in script
    assert "key=$(cat)" in script


def test_handler_authorizes_via_managed_key_ssh_and_pipes_key_on_stdin() -> None:
    recorded: list[tuple[list[str], str]] = []
    resolver = _resolver(("127.0.0.1", 22022))

    result = asyncio.run(
        authorize_ssh_key_handler(
            MagicMock(),
            _job(),
            resolver=resolver,
            ssh_exec=lambda argv, key: recorded.append((argv, key)),
        )
    )

    assert result is None
    assert len(recorded) == 1
    argv, key = recorded[0]
    assert "root@127.0.0.1" in argv and "22022" in argv
    assert _KEY not in argv  # not in the command
    assert key == _KEY  # delivered on stdin


def test_handler_unprovisioned_is_configuration_error() -> None:
    resolver = _resolver(None)
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(), _job(), resolver=resolver, ssh_exec=lambda _argv, _key: None
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "ssh_not_provisioned"


def test_handler_ssh_failure_propagates_transport_failure() -> None:
    resolver = _resolver(("127.0.0.1", 22022))

    def _boom(_argv: list[str], _key: str) -> None:
        raise CategorizedError("ssh down", category=ErrorCategory.TRANSPORT_FAILURE)

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(MagicMock(), _job(), resolver=resolver, ssh_exec=_boom)
        )
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE

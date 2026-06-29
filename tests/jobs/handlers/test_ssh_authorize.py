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


def test_argv_is_fixed_and_carries_key_as_a_single_element() -> None:
    argv = build_authorize_argv(22022, _KEY)
    assert argv[0] == "ssh"
    assert "root@127.0.0.1" in argv
    assert "22022" in argv
    # the key travels as exactly one argv element, never interpolated into a shell string
    assert _KEY in argv
    joined = " ".join(argv)
    assert "flock" in joined and "grep -qxF" in joined


def test_handler_authorizes_via_managed_key_ssh() -> None:
    recorded: list[list[str]] = []
    resolver = _resolver(("127.0.0.1", 22022))

    result = asyncio.run(
        authorize_ssh_key_handler(MagicMock(), _job(), resolver=resolver, ssh_exec=recorded.append)
    )

    assert result is None
    assert len(recorded) == 1
    argv = recorded[0]
    assert "root@127.0.0.1" in argv and "22022" in argv and _KEY in argv


def test_handler_unprovisioned_is_configuration_error() -> None:
    resolver = _resolver(None)
    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(
                MagicMock(), _job(), resolver=resolver, ssh_exec=lambda _argv: None
            )
        )
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert excinfo.value.details["reason"] == "ssh_not_provisioned"


def test_handler_ssh_failure_propagates_transport_failure() -> None:
    resolver = _resolver(("127.0.0.1", 22022))

    def _boom(_argv: list[str]) -> None:
        raise CategorizedError("ssh down", category=ErrorCategory.TRANSPORT_FAILURE)

    with pytest.raises(CategorizedError) as excinfo:
        asyncio.run(
            authorize_ssh_key_handler(MagicMock(), _job(), resolver=resolver, ssh_exec=_boom)
        )
    assert excinfo.value.category is ErrorCategory.TRANSPORT_FAILURE

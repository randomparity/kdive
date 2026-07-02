"""Unit tests for the guest-agent bootstrap-key injector (ADR-0291, #966).

The injector writes the per-System bootstrap public key into the remote guest's root
authorized_keys via one fixed ``/bin/sh -c`` guest-exec hop with the key on stdin. Driven with a
scripted agent fake — no libvirt host.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.bootstrap_key import (
    INJECT_SCRIPT,
    RemoteBootstrapKeyInjector,
)

_PUBKEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 kdive-bootstrap"


class _Domain:
    def name(self) -> str:
        return "kdive-x"


class _FakeAgent:
    """Scripts guest-exec→pid then guest-exec-status→exit for one in-guest run."""

    def __init__(self, *, exitcode: int = 0) -> None:
        self._exitcode = exitcode
        self.commands: list[dict[str, Any]] = []

    def __call__(self, domain: object, command: str, timeout: int, flags: int) -> str:
        parsed = json.loads(command)
        self.commands.append(parsed)
        if parsed["execute"] == "guest-exec":
            return json.dumps({"return": {"pid": 4242}})
        return json.dumps({"return": {"exited": True, "exitcode": self._exitcode}})


def test_inject_runs_allowlisted_shell_with_key_on_stdin() -> None:
    agent = _FakeAgent(exitcode=0)
    RemoteBootstrapKeyInjector(agent_command=agent).inject(_Domain(), _PUBKEY)
    spawn = agent.commands[0]["arguments"]
    assert spawn["path"] == "/bin/sh"
    assert spawn["arg"] == ["-c", INJECT_SCRIPT]
    # The key rides stdin (input-data), never argv or the command string.
    assert base64.b64decode(spawn["input-data"]).decode("utf-8") == _PUBKEY
    assert _PUBKEY not in json.dumps(spawn["arg"])


def test_inject_nonzero_exit_raises_provisioning_failure() -> None:
    agent = _FakeAgent(exitcode=1)
    with pytest.raises(CategorizedError) as excinfo:
        RemoteBootstrapKeyInjector(agent_command=agent).inject(_Domain(), _PUBKEY)
    assert excinfo.value.category is ErrorCategory.PROVISIONING_FAILURE
    assert excinfo.value.details == {"domain": "kdive-x", "exit_status": 1}


def test_inject_script_is_idempotent_append_with_tight_umask() -> None:
    # The script guards against duplicate lines (grep -qxF) and 0600/0700 perms (umask 077).
    assert "umask 077" in INJECT_SCRIPT
    assert "grep -qxF" in INJECT_SCRIPT
    assert "key=$(cat)" in INJECT_SCRIPT
    assert _PUBKEY not in INJECT_SCRIPT

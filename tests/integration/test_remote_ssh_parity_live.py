"""Operator-run e2e for remote-libvirt SSH-parity bootstrap-key injection (#966, ADR-0291).

``live_vm``-gated and preflighted to a clean skip: it needs an operator-provided remote libvirt
domain that is running with a connected qemu-guest-agent. CI deselects ``live_vm`` (``just test``
runs ``-m "not live_vm and not live_stack"``), so this is safe on any host; an operator runs it on
the two-host remote-libvirt HW with ``just test-live``.

It proves the novel, non-unit-provable half of ADR-0291 against the **real** guest-agent channel:
the worker writes the per-System bootstrap public key into the guest's
``/root/.ssh/authorized_keys`` over a single ``/bin/sh -c`` guest-exec hop (the pre-SSH channel),
and the key is then present in the guest — read back over the same channel.

The remaining acceptance-criteria legs — ``systems.ssh_info`` returning a reachable endpoint and an
agent SSHing in with its own key after ``systems.authorize_ssh_key`` — are exercised end-to-end by
running the remote live-stack spine (``docs/operating/runbooks/remote-live-stack.md``) against a
``[[remote_libvirt]]`` instance that declares ``ssh_addr`` + ``ssh_range`` (§2.1). They are not
reproduced here because they need the full server/worker spine + a routable SSH path, not just an
agent-ready domain.

Required env: ``KDIVE_SSH_PARITY_DOMAIN`` (a running, agent-ready remote domain name) and
``KDIVE_SSH_PARITY_URI`` (its ``qemu+tls://`` connect URI).
"""

from __future__ import annotations

import os
from uuid import uuid4

import libvirt
import pytest

from kdive.providers.remote_libvirt.guest.agent import GuestAgentExec, qemu_agent_command
from kdive.providers.remote_libvirt.guest.bootstrap_key import RemoteBootstrapKeyInjector

pytestmark = pytest.mark.live_vm

_DOMAIN_ENV = "KDIVE_SSH_PARITY_DOMAIN"
_URI_ENV = "KDIVE_SSH_PARITY_URI"
_SHELL = "/bin/sh"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is not set; the remote SSH-parity e2e needs an operator guest")
    return value


def test_bootstrap_key_reaches_the_guest_over_the_guest_agent() -> None:
    domain_name = _require_env(_DOMAIN_ENV)
    uri = _require_env(_URI_ENV)
    pubkey = f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5{uuid4().hex} kdive-e2e"

    conn = libvirt.open(uri)
    try:
        domain = conn.lookupByName(domain_name)
        RemoteBootstrapKeyInjector(agent_command=qemu_agent_command).inject(domain, pubkey)
        # Read authorized_keys back over the same guest-agent channel to prove the key landed.
        agent = GuestAgentExec(
            agent_command=qemu_agent_command, allowed_programs=frozenset({_SHELL})
        )
        result = agent.run(domain, [_SHELL, "-c", "cat /root/.ssh/authorized_keys"])
    finally:
        conn.close()

    assert result.exit_status == 0
    assert pubkey in result.stdout.decode("utf-8", "replace")

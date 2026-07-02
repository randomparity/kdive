"""Inject the per-System bootstrap key into a remote guest over the guest agent (ADR-0291).

The worker cannot ``virt-customize`` a remote disk (the ADR-0289 obstacle), so the only
pre-SSH channel to a remote guest is the qemu-guest-agent. This writes the bootstrap public key
into ``/root/.ssh/authorized_keys`` via one fixed, worker-composed ``/bin/sh -c`` hop with the
key delivered on **stdin** (never in argv or the command string — no injection surface),
allowlist ``{'/bin/sh'}``. The script is the ADR-0271 ``authorize_ssh_key`` shape
(``umask 077`` + idempotent ``grep -qxF`` append), so a provision retry that reuses the overlay
re-runs it harmlessly. This is a bounded exception to ADR-0078's debug-target no-shell rule
(precedent ADR-0100, the build VM), documented in ADR-0291.
"""

from __future__ import annotations

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.guest.agent import (
    AgentCommand,
    GuestAgentExec,
    GuestDomain,
    qemu_agent_command,
)

_SHELL = "/bin/sh"
_INJECT_TIMEOUT_S = 60.0

# The key arrives on stdin (`key=$(cat)`), so it never occupies an argv/command-string position it
# could break out of. `umask 077` makes ~/.ssh 0700 and a fresh authorized_keys 0600; `grep -qxF`
# keeps the append idempotent across provision retries.
INJECT_SCRIPT = (
    "set -e\n"
    "umask 077\n"
    "mkdir -p /root/.ssh\n"
    "key=$(cat)\n"
    "touch /root/.ssh/authorized_keys\n"
    'grep -qxF "$key" /root/.ssh/authorized_keys '
    "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
)


class RemoteBootstrapKeyInjector:
    """Write the bootstrap public key into a remote guest's root authorized_keys via guest-agent.

    The agent round-trip is injected (production opener ``qemu_agent_command``) so unit tests
    drive the two-phase protocol with no libvirt host.
    """

    def __init__(
        self,
        *,
        agent_command: AgentCommand = qemu_agent_command,
        timeout_s: float = _INJECT_TIMEOUT_S,
    ) -> None:
        self._agent_command = agent_command
        self._timeout_s = timeout_s

    def inject(self, domain: GuestDomain, pubkey: str) -> None:
        """Append ``pubkey`` to the guest root authorized_keys, idempotently.

        Raises:
            CategorizedError: ``PROVISIONING_FAILURE`` for a non-zero in-guest exit; the
                guest-agent error contract (``CONFIGURATION_ERROR`` / ``TRANSPORT_FAILURE`` /
                ``INFRASTRUCTURE_FAILURE``) propagated from :class:`GuestAgentExec`.
        """
        agent = GuestAgentExec(
            agent_command=self._agent_command,
            allowed_programs=frozenset({_SHELL}),
            timeout_s=self._timeout_s,
        )
        result = agent.run(domain, [_SHELL, "-c", INJECT_SCRIPT], input_data=pubkey)
        if result.exit_status != 0:
            raise CategorizedError(
                "guest-agent bootstrap key injection exited non-zero",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"domain": domain.name(), "exit_status": result.exit_status},
            )


__all__ = ["INJECT_SCRIPT", "RemoteBootstrapKeyInjector"]

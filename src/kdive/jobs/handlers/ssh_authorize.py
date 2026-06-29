"""Worker handler: append an agent public key to a guest's root authorized_keys (ADR-0271).

The worker already holds the kdive-managed SSH private key (ADR-0052) and can root-SSH any
SSH-provisioned guest over the loopback forward (ADR-0218). This handler reuses that identity to
append the agent's validated public key to ``/root/.ssh/authorized_keys`` — a flock-serialized,
idempotent append — so the agent can then SSH in with its own private key. KDIVE never holds the
agent's private key.
"""

from __future__ import annotations

import subprocess  # noqa: S404 - fixed argv, no shell
from collections.abc import Callable
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job
from kdive.jobs.payloads import AuthorizeSshKeyPayload, load_payload
from kdive.prereqs.managed_ssh_key import managed_private_key_path
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.handles import SystemHandle

type SshExec = Callable[[list[str]], None]

_LOOPBACK_HOST = "127.0.0.1"
_SSH_USER = "root"
_SSH_CONNECT_TIMEOUT_S = 10
_SSH_RUN_TIMEOUT_S = 30
_LOCK = "/root/.ssh/.kdive-authz.lock"

# Idempotent, flock-serialized append (ADR-0271). The outer sh creates ~/.ssh 0700, then under the
# lock the inner sh ensures authorized_keys 0600 and appends the key only if an exact line match is
# absent. The key is "$1" — an argv element, never interpolated into the shell string, so it cannot
# break out of the data position even with shell metacharacters.
_REMOTE_CMD = (
    "mkdir -p /root/.ssh && chmod 700 /root/.ssh && "
    f"flock {_LOCK} sh -c '"
    "touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && "
    'grep -qxF "$1" /root/.ssh/authorized_keys || printf "%s\\n" "$1" '
    '>> /root/.ssh/authorized_keys\' _ "$1"'
)


def build_authorize_argv(port: int, public_key: str) -> list[str]:
    """Build the fixed loopback SSH argv that authorizes ``public_key`` in guest root."""
    return [
        "ssh",
        "-i",
        str(managed_private_key_path()),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "UserKnownHostsFile=/dev/null",
        "-o",
        f"ConnectTimeout={_SSH_CONNECT_TIMEOUT_S}",
        "-p",
        str(port),
        f"{_SSH_USER}@{_LOOPBACK_HOST}",
        "--",
        "/bin/sh",
        "-c",
        _REMOTE_CMD,
        "kdive-authz",
        public_key,
    ]


def _real_ssh_exec(argv: list[str]) -> None:  # pragma: no cover - live_vm
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell; key is a data element
            argv, timeout=_SSH_RUN_TIMEOUT_S, check=False, capture_output=True
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise CategorizedError(
            "ssh to the guest to authorize the key timed out or could not launch",
            category=ErrorCategory.TRANSPORT_FAILURE,
        ) from exc
    if proc.returncode != 0:
        raise CategorizedError(
            "ssh authorize-key command failed in the guest",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details={"exit_status": proc.returncode},
        )


async def authorize_ssh_key_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    ssh_exec: SshExec = _real_ssh_exec,
) -> str | None:
    """Append the agent public key to the guest root authorized_keys over the managed-key SSH.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the System has no recorded SSH forward;
            ``TRANSPORT_FAILURE`` when the guest sshd is unreachable or the append fails.
    """
    payload = load_payload(job, AuthorizeSshKeyPayload)
    system_id = UUID(payload.system_id)
    binding = await resolver.binding_for_system(conn, system_id)
    endpoint = binding.runtime.connector.recorded_ssh_endpoint(SystemHandle(str(system_id)))
    if endpoint is None:
        raise CategorizedError(
            "System was not provisioned for SSH; reprovision with ssh_credential_ref set",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ssh_not_provisioned"},
        )
    _host, port = endpoint
    ssh_exec(build_authorize_argv(port, payload.public_key))
    return None

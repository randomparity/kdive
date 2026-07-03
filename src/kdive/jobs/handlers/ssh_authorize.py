"""Worker handler: append an agent public key to a guest's root authorized_keys (ADR-0271).

The worker loads the System's per-System SSH bootstrap private key (ADR-0289, #963) and can
root-SSH that guest over the loopback forward (ADR-0218). This handler uses that identity to
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
from kdive.prereqs.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.ports.handles import SystemHandle
from kdive.providers.shared.runtime_paths import domain_name_for
from kdive.providers.shared.ssh_connect_retry import (
    SshRetryPolicy,
    run_ssh_with_retry,
    ssh_failure_details,
)

type SshExec = Callable[[list[str], str], None]

_SSH_USER = "root"
_SSH_CONNECT_TIMEOUT_S = 10
_SSH_RUN_TIMEOUT_S = 30
_LOCK = "/root/.ssh/.kdive-authz.lock"
# A freshly-`ready` System's guest sshd may not be accepting yet (readiness is the boot marker,
# ~46 ms before sshd binds — ADR-0289 live proof), so the first authorize SSH is refused. Retry
# connection-level failures over this window; auth/host-key errors still fail fast.
_AUTHORIZE_SSH_RETRY = SshRetryPolicy()

# The remote append script (ADR-0271). The key is **never** in this string: `ssh host CMD` joins
# any post-host argv into one string the remote login shell re-parses, so an argv-positioned key
# would not be isolated. Instead the worker pipes the validated key on the SSH session's stdin and
# the script reads it with `key=$(cat)` — there is no command-string position for the key to break
# out of. `umask 077` makes ~/.ssh 0700 and a freshly created authorized_keys 0600; `flock` on a
# dedicated FD serializes concurrent authorize jobs so the read-modify-write cannot interleave;
# `grep -qxF` keeps the append idempotent (re-authorizing the same key is a no-op).
_REMOTE_SCRIPT = (
    "set -e\n"
    "umask 077\n"
    "mkdir -p /root/.ssh\n"
    "key=$(cat)\n"
    f"exec 9>{_LOCK}\n"
    "flock -w 5 9\n"
    "touch /root/.ssh/authorized_keys\n"
    'grep -qxF "$key" /root/.ssh/authorized_keys '
    "|| printf '%s\\n' \"$key\" >> /root/.ssh/authorized_keys\n"
)


def build_authorize_argv(host: str, port: int, key_path: str) -> list[str]:
    """Build the fixed SSH argv that runs the append script (key arrives on stdin).

    ``host`` is the recorded SSH endpoint host: ``127.0.0.1`` for local-libvirt's loopback
    forward, or the operator-ACL'd ``ssh_addr`` for a remote-libvirt System (ADR-0291). The
    remote path is unspoofable-loopback no longer, so ``StrictHostKeyChecking=no`` is an accepted,
    ACL-mitigated risk (ADR-0291); host-key pinning is a named future hardening.
    """
    return [
        "ssh",
        "-i",
        key_path,
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
        f"{_SSH_USER}@{host}",
        # Exactly one post-host argument: ssh sends it verbatim; the remote login shell runs it as a
        # single `-c` script. Adding more argv here would be space-joined and re-parsed remotely.
        _REMOTE_SCRIPT,
    ]


def _raise_on_authorize_failure(proc: subprocess.CompletedProcess[str]) -> None:
    """Raise a diagnosable ``TRANSPORT_FAILURE`` when the authorize ssh exited non-zero (#1008).

    Classifies ssh's stderr into a closed reason vocabulary and attaches a length-capped,
    downstream-redacted stderr tail so ``jobs.get``/``jobs.wait`` report *why* it failed, not just
    exit ``255``. Split out from :func:`_real_ssh_exec` (whose ssh subprocess is ``live_vm``-only)
    so the classify-and-raise path is unit-tested.
    """
    if proc.returncode != 0:
        raise CategorizedError(
            "ssh authorize-key command failed in the guest",
            category=ErrorCategory.TRANSPORT_FAILURE,
            details=ssh_failure_details(proc.returncode, proc.stderr),
        )


def _real_ssh_exec(argv: list[str], key: str) -> None:  # pragma: no cover - live_vm
    def run_once() -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(  # noqa: S603 - fixed argv, no shell; key is piped on stdin
                argv,
                input=key,
                text=True,
                timeout=_SSH_RUN_TIMEOUT_S,
                check=False,
                capture_output=True,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise CategorizedError(
                "ssh to the guest to authorize the key timed out or could not launch",
                category=ErrorCategory.TRANSPORT_FAILURE,
            ) from exc

    _raise_on_authorize_failure(run_ssh_with_retry(run_once, policy=_AUTHORIZE_SSH_RETRY))


async def authorize_ssh_key_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    ssh_exec: SshExec = _real_ssh_exec,
) -> str | None:
    """Append the agent public key to the guest root authorized_keys over the bootstrap-key SSH.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when the System has no recorded SSH forward or
            no bootstrap key; ``TRANSPORT_FAILURE`` when the guest sshd is unreachable or the
            append fails.
    """
    payload = load_payload(job, AuthorizeSshKeyPayload)
    system_id = UUID(payload.system_id)
    binding = await resolver.binding_for_system(conn, system_id)
    endpoint = binding.runtime.connector.recorded_ssh_endpoint(
        SystemHandle(domain_name_for(system_id))
    )
    if endpoint is None:
        # The local-libvirt forward is always rendered now (ADR-0281, #937); a None endpoint means
        # the System's provider exposes no loopback SSH forward (a defensive guard for
        # remote/fault-inject — the server tool already rejects before enqueue).
        raise CategorizedError(
            "This System's provider exposes no loopback SSH forward; direct SSH to a System is a "
            "local-libvirt capability",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ssh_not_provisioned"},
        )
    host, port = endpoint
    private_key = await load_system_bootstrap_private_key(conn, system_id)
    with materialized_private_key(private_key) as key_path:
        ssh_exec(build_authorize_argv(host, port, str(key_path)), payload.public_key)
    return None

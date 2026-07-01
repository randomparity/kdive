"""Per-System SSH bootstrap keypair: generate, store, load, reclaim (ADR-0289, #963).

Each System gets a unique throwaway ed25519 keypair generated at provision. The public half is
injected into the System's overlay; the private half lives in ``system_bootstrap_keys`` and is
loaded by the worker to root-SSH the guest for ``authorize_ssh_key`` and live drgn. This replaces
the standing managed key (ADR-0052): no credential is baked into catalog images, and the blast
radius of any one key is one System for its lifetime.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

from psycopg import AsyncConnection

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.secrets.secret_registry import PROCESS_SECRET_REGISTRY, SecretRegistry

_SSH_KEYGEN_TIMEOUT_S = 30


def generate_keypair() -> tuple[str, str]:
    """Generate an ed25519 keypair; return ``(private_openssh, public_openssh)``.

    Generates into a ``0700`` scratch dir removed in a ``finally`` (no key material survives the
    call). Raises:
        CategorizedError: ``MISSING_DEPENDENCY``/``INFRASTRUCTURE_FAILURE`` if ``ssh-keygen`` is
            absent or fails.
    """
    scratch = Path(tempfile.mkdtemp(prefix="kdive-bootkey-"))
    try:
        os.chmod(scratch, 0o700)
        key = scratch / "id"
        _run_keygen(["-t", "ed25519", "-N", "", "-f", str(key), "-q", "-C", "kdive-system"])
        private_openssh = key.read_text(encoding="utf-8")
        public_openssh = key.with_suffix(".pub").read_text(encoding="utf-8").strip()
        return private_openssh, public_openssh
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _run_keygen(args: list[str]) -> None:
    executable = shutil.which("ssh-keygen")
    if executable is None:
        raise CategorizedError(
            "ssh-keygen not found on PATH; install the OpenSSH client",
            category=ErrorCategory.MISSING_DEPENDENCY,
        )
    try:
        completed = subprocess.run(  # noqa: S603 - fixed ssh-keygen argv, kdive-owned paths
            [executable, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SSH_KEYGEN_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CategorizedError(
            "ssh-keygen failed to generate the per-System bootstrap key",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
        ) from exc
    if completed.returncode != 0:
        raise CategorizedError(
            "ssh-keygen returned non-zero generating the per-System bootstrap key",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"stderr": completed.stderr[-500:]},
        )


async def ensure_system_bootstrap_key(conn: AsyncConnection, system_id: UUID) -> str:
    """Return the System's bootstrap **public** key, generating+storing it once (idempotent).

    Concurrency-safe: an ``INSERT ... ON CONFLICT DO NOTHING`` with a freshly generated pair, then
    a ``SELECT`` of the winning/pre-existing ``public_key``. A losing INSERT's keypair is
    discarded. Callers rely on this committing before the overlay is created (commit-ordering
    invariant, ADR-0289) — run it in its own transaction.
    """
    private_key, public_key = generate_keypair()
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO system_bootstrap_keys (system_id, private_key, public_key) "
            "VALUES (%s, %s, %s) ON CONFLICT (system_id) DO NOTHING",
            (system_id, private_key, public_key),
        )
        await cur.execute(
            "SELECT public_key FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        row = await cur.fetchone()
    if row is None:  # pragma: no cover - the INSERT+SELECT under one txn always yields a row
        raise CategorizedError(
            "failed to persist the per-System bootstrap key",
            category=ErrorCategory.INFRASTRUCTURE_FAILURE,
            details={"system_id": str(system_id)},
        )
    return row[0]


async def load_system_bootstrap_private_key(
    conn: AsyncConnection,
    system_id: UUID,
    *,
    secret_registry: SecretRegistry = PROCESS_SECRET_REGISTRY,
) -> str:
    """Return the System's bootstrap private key (registered for redaction) or raise.

    Registers the key value with ``secret_registry`` so it is scrubbed from logs/errors — callers
    that hold their own registry (e.g. the drgn tool) pass it; a caller without one uses the
    process singleton. Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` when no key row exists (System predates ADR-0289
            or was never provisioned) — fail closed.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT private_key FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        row = await cur.fetchone()
    if row is None:
        raise CategorizedError(
            "this System has no bootstrap key; it cannot be reached over managed SSH",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "no_bootstrap_key", "system_id": str(system_id)},
        )
    secret_registry.register(row[0], scope=None)
    return row[0]


async def delete_system_bootstrap_key(conn: AsyncConnection, system_id: UUID) -> None:
    """Delete the System's bootstrap key row; idempotent (absent row is a no-op)."""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM system_bootstrap_keys WHERE system_id = %s", (system_id,))


@contextlib.contextmanager
def materialized_private_key(private_key: str) -> Iterator[Path]:
    """Yield a ``0600`` file holding ``private_key`` in a ``0700`` dir; removed on every exit path.

    ``ssh -i`` refuses a group/world-readable key, hence ``0600``. The dir is ``rmtree``'d in a
    ``finally`` so a raised body never leaks the key on disk.
    """
    scratch = Path(tempfile.mkdtemp(prefix="kdive-bootkey-use-"))
    try:
        os.chmod(scratch, 0o700)
        key_path = scratch / "id"
        key_path.write_text(private_key, encoding="utf-8")
        os.chmod(key_path, 0o600)
        yield key_path
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

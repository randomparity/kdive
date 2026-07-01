# Per-System SSH bootstrap key — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the standing baked root SSH key (ADR-0052) with a per-System unique bootstrap
keypair generated at provision and injected into the per-System overlay, so catalog images bake no
credential.

**Architecture:** The `systems` job handlers own the secret (generate once, store the private half
in a new `system_bootstrap_keys` table, load for authorize/drgn, delete at teardown); the
connectionless provider owns the overlay mutation via an extensible ordered overlay-customizer seam
run only when it creates the overlay (`virt-customize --ssh-inject`, off the cloud-init path).
`authorize_ssh_key` and drgn-live re-source from the per-System key; `managed_ssh_key.py` and the
build-time `--ssh-inject` are deleted.

**Tech Stack:** Python 3.14, `uv`/`.venv`, `pytest -q`, `ruff`, `ty`, psycopg (async), libguestfs
(`virt-customize`), Postgres migrations.

## Global Constraints

- Absolute imports only; ≤100 lines/function, complexity ≤8; ≤100-char lines; Google-style
  docstrings on public APIs.
- Guardrails run individually in CI: `just lint`, `just type`, `just test`. Run all three before
  every commit (or focused `pytest` + `ruff check` + `ty check` on touched files, then the full
  suite before push).
- Conventional-commit subjects ≤72 chars, imperative; end every commit body with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Reference: spec `docs/superpowers/specs/2026-07-01-per-system-ssh-bootstrap-key-design.md`, ADR
  `docs/adr/0289-per-system-ssh-bootstrap-key.md`.
- **Pre-assigned:** migration number **0056**, ADR **0289**. Do not pick "next free".
- **Commit-ordering invariant (load-bearing):** `ensure_system_bootstrap_key` must commit its row
  in its **own** transaction **before** the provision transaction, so a post-provision rollback can
  never leave a running overlay trusting a key the DB dropped.
- **Task order is a hard dependency chain:** Tasks 4 and 5 (re-source authorize + drgn) MUST land
  before Task 6 (delete `managed_ssh_key.py`) — deleting it while a consumer still imports it
  breaks the build. Keep every commit green.
- Secrets: register any loaded private key with `SecretRegistry`; never log it; `# pragma:
  allowlist secret` only on non-secret literals that trip detect-secrets.

## File structure

- `src/kdive/db/schema/0056_system_bootstrap_keys.sql` — new table (Task 1).
- `src/kdive/prereqs/system_bootstrap_key.py` — keygen + DB accessors + temp-key context manager
  (Task 1).
- `src/kdive/providers/local_libvirt/lifecycle/overlay_customize.py` — overlay-customizer seam +
  `--ssh-inject` injector (Task 2).
- `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` — `provision()` runs customizers
  (Task 2).
- `src/kdive/jobs/handlers/systems.py` — ensure/pass/delete wiring (Task 3).
- `src/kdive/jobs/handlers/ssh_authorize.py` — re-source (Task 4).
- `src/kdive/providers/local_libvirt/debug/introspect.py` + `src/kdive/mcp/tools/debug/
  introspect.py` — re-source (Task 5).
- `src/kdive/images/families/{base,rhel,debian}.py`, `src/kdive/providers/local_libvirt/
  rootfs_build.py`, delete `src/kdive/prereqs/managed_ssh_key.py` (Task 6).

---

### Task 1: Migration 0056 + per-System key service

**Files:**
- Create: `src/kdive/db/schema/0056_system_bootstrap_keys.sql`
- Create: `src/kdive/prereqs/system_bootstrap_key.py`
- Test: `tests/prereqs/test_system_bootstrap_key.py`

**Interfaces:**
- Produces: `generate_keypair() -> tuple[str, str]` (private_pem, public_openssh);
  `async ensure_system_bootstrap_key(conn, system_id: UUID) -> str` (public key);
  `async load_system_bootstrap_private_key(conn, system_id: UUID) -> str`;
  `async delete_system_bootstrap_key(conn, system_id: UUID) -> None`;
  `materialized_private_key(private_key: str) -> ContextManager[Path]` (0700 dir + 0600 file,
  cleaned up on every path).

- [ ] **Step 1: Write the migration.**

`src/kdive/db/schema/0056_system_bootstrap_keys.sql`:

```sql
-- 0056_system_bootstrap_keys.sql — per-System SSH bootstrap keypair (ADR-0289, #963).
-- Additive (forward-only, ADR-0015). Each System gets a unique throwaway ed25519 keypair
-- generated at provision and injected into its overlay; the private half lives here and is
-- reclaimed by the teardown handler (explicit DELETE), with ON DELETE CASCADE as the backstop
-- for a hard systems-row delete. No standing credential is baked into catalog images (ADR-0289
-- supersedes ADR-0052).
CREATE TABLE system_bootstrap_keys (
    system_id   uuid PRIMARY KEY REFERENCES systems (id) ON DELETE CASCADE,
    private_key text NOT NULL,
    public_key  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Write failing tests for the key service.**

`tests/prereqs/test_system_bootstrap_key.py` (async tests use the `migrated_url` fixture from
`tests/db/conftest.py`; open an `psycopg.AsyncConnection`):

```python
from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.prereqs.system_bootstrap_key import (
    delete_system_bootstrap_key,
    ensure_system_bootstrap_key,
    generate_keypair,
    load_system_bootstrap_private_key,
    materialized_private_key,
)


def test_generate_keypair_returns_ed25519_pair_and_leaves_no_scratch() -> None:
    private_pem, public_openssh = generate_keypair()
    assert "OPENSSH PRIVATE KEY" in private_pem
    assert public_openssh.startswith("ssh-ed25519 ")


def test_materialized_private_key_is_0600_and_cleaned_up() -> None:
    seen: Path | None = None
    with materialized_private_key("KEY-MATERIAL\n") as key_path:
        seen = key_path
        assert key_path.read_text() == "KEY-MATERIAL\n"
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert seen is not None and not seen.exists()


def test_materialized_private_key_cleans_up_on_exception() -> None:
    captured: Path | None = None
    with pytest.raises(RuntimeError):
        with materialized_private_key("K\n") as key_path:
            captured = key_path
            raise RuntimeError("boom")
    assert captured is not None and not captured.exists()


async def _seed_system(conn: psycopg.AsyncConnection) -> UUID:
    # Minimal FK parent rows so system_bootstrap_keys.system_id resolves.
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO allocations (id, project, principal, state) "
            "VALUES (gen_random_uuid(), 'demo', 'demo', 'active') RETURNING id"
        )
        row = await cur.fetchone()
        allocation_id = row[0]
        await cur.execute(
            "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, "
            "project) VALUES (gen_random_uuid(), %s, 'defined', '{}'::jsonb, 'demo', 'demo') "
            "RETURNING id",
            (allocation_id,),
        )
        row = await cur.fetchone()
        return row[0]


@pytest.fixture
async def conn(migrated_url: str):
    async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as c:
        yield c


async def test_ensure_is_idempotent_one_row_one_pubkey(conn: psycopg.AsyncConnection) -> None:
    system_id = await _seed_system(conn)
    first = await ensure_system_bootstrap_key(conn, system_id)
    second = await ensure_system_bootstrap_key(conn, system_id)
    assert first == second and first.startswith("ssh-ed25519 ")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        assert (await cur.fetchone())[0] == 1


async def test_load_returns_private_key_and_raises_when_absent(
    conn: psycopg.AsyncConnection,
) -> None:
    system_id = await _seed_system(conn)
    with pytest.raises(CategorizedError) as excinfo:
        await load_system_bootstrap_private_key(conn, system_id)
    assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
    await ensure_system_bootstrap_key(conn, system_id)
    private_key = await load_system_bootstrap_private_key(conn, system_id)
    assert "OPENSSH PRIVATE KEY" in private_key


async def test_delete_is_idempotent(conn: psycopg.AsyncConnection) -> None:
    system_id = await _seed_system(conn)
    await ensure_system_bootstrap_key(conn, system_id)
    await delete_system_bootstrap_key(conn, system_id)
    await delete_system_bootstrap_key(conn, system_id)  # no-op, no raise
    with pytest.raises(CategorizedError):
        await load_system_bootstrap_private_key(conn, system_id)
```

- [ ] **Step 3: Run tests, verify they fail** (module missing).
  Run: `.venv/bin/python -m pytest tests/prereqs/test_system_bootstrap_key.py -q`
  Expected: FAIL (ImportError / table missing).

- [ ] **Step 4: Implement the service.**

`src/kdive/prereqs/system_bootstrap_key.py`:

```python
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
from kdive.security.secrets.secret_registry import register_secret

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
        return key.read_text(encoding="utf-8"), (key.with_suffix(".pub")).read_text(
            encoding="utf-8"
        ).strip()
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


async def load_system_bootstrap_private_key(conn: AsyncConnection, system_id: UUID) -> str:
    """Return the System's bootstrap private key (registered as a secret) or raise.

    Raises:
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
    register_secret(row[0])
    return row[0]


async def delete_system_bootstrap_key(conn: AsyncConnection, system_id: UUID) -> None:
    """Delete the System's bootstrap key row; idempotent (absent row is a no-op)."""
    async with conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )


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
```

> **Implementer note:** verify `register_secret` is the real symbol exported by
> `kdive.security.secrets.secret_registry` (Task-1 self-check: `rg -n "def register_secret|^def
> register" src/kdive/security/secrets/secret_registry.py`). If the redaction API is instead an
> instance method (`SecretRegistry.register`), adapt `load_*` to accept/register through the
> caller's registry and update the callers in Tasks 4/5; note the choice in the report.

- [ ] **Step 5: Run tests, verify they pass.**
  Run: `.venv/bin/python -m pytest tests/prereqs/test_system_bootstrap_key.py -q`
  Expected: PASS. Then `just lint && just type`.

- [ ] **Step 6: Commit.**
  `git add src/kdive/db/schema/0056_system_bootstrap_keys.sql src/kdive/prereqs/system_bootstrap_key.py tests/prereqs/test_system_bootstrap_key.py`
  `git commit -m "feat(963): per-System bootstrap key table + service"`

---

### Task 2: Provider overlay-customizer seam + `--ssh-inject` injector

**Files:**
- Create: `src/kdive/providers/local_libvirt/lifecycle/overlay_customize.py`
- Modify: `src/kdive/providers/local_libvirt/lifecycle/provisioning.py` (`provision` signature +
  run customizers)
- Test: `tests/providers/local_libvirt/test_overlay_customize.py`,
  `tests/providers/local_libvirt/test_provisioning.py` (extend if present)

**Interfaces:**
- Produces: `type OverlayCustomizer = Callable[[str], None]`;
  `inject_authorized_key_argv(overlay_path: str, pubkey_file: str) -> list[str]`;
  `authorized_key_customizer(pubkey: str) -> OverlayCustomizer`.
- Consumes (Task 3): `LocalLibvirtProvisioning.provision(system_id, profile, *,
  overlay_customizers: tuple[OverlayCustomizer, ...] = ())`.

- [ ] **Step 1: Write failing tests.**

`tests/providers/local_libvirt/test_overlay_customize.py`:

```python
from __future__ import annotations

from kdive.providers.local_libvirt.lifecycle.overlay_customize import (
    inject_authorized_key_argv,
)


def test_inject_authorized_key_argv_uses_ssh_inject_root() -> None:
    argv = inject_authorized_key_argv("/var/lib/kdive/rootfs/s-overlay.qcow2", "/tmp/k.pub")
    j = " ".join(argv)
    assert argv[0] == "virt-customize"
    assert "-a" in argv and "/var/lib/kdive/rootfs/s-overlay.qcow2" in argv
    assert "--ssh-inject" in argv and "root:file:/tmp/k.pub" in j
```

Extend the provisioning test (create `tests/providers/local_libvirt/test_provisioning.py` if it
does not already cover `provision`) with a recording customizer to pin the run-iff-created rule.
Use the existing `ProvisioningFiles` injection seam so no libguestfs/libvirt runs — inject fake
`make_overlay`/`overlay_exists`/`connect` as the sibling tests do:

```python
def test_provision_runs_customizers_only_when_overlay_created(...) -> None:
    calls: list[str] = []
    customizer = lambda overlay: calls.append(overlay)  # noqa: E731
    # overlay ABSENT -> created -> customizer runs once with the overlay path
    provisioner.provision(system_id, profile, overlay_customizers=(customizer,))
    assert calls == [overlay_path(system_id)]
    calls.clear()
    # overlay PRESENT -> created=False -> customizer skipped
    provisioner_with_existing_overlay.provision(system_id, profile, overlay_customizers=(customizer,))
    assert calls == []
```

> Implementer: mirror the fakes in the existing local-libvirt provisioning tests (search
> `tests/providers/local_libvirt/` for how `LocalLibvirtProvisioning` is constructed with fake
> `connect`/`ProvisioningFiles`). Do not run real libvirt/libguestfs.

- [ ] **Step 2: Run tests, verify they fail.**
  Run: `.venv/bin/python -m pytest tests/providers/local_libvirt/test_overlay_customize.py tests/providers/local_libvirt/test_provisioning.py -q`

- [ ] **Step 3: Implement the seam.**

`src/kdive/providers/local_libvirt/lifecycle/overlay_customize.py`:

```python
"""Provision-time per-System overlay customization (ADR-0289, #963).

An ordered list of customizers `provision()` runs against the per-System overlay **only when it
creates the overlay** (so a retry against a running QEMU never re-mutates a live disk). The first
consumer is the per-System SSH bootstrap key injection; future provision-time mutations append a
customizer here rather than adding parallel one-offs.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from kdive.domain.errors import CategorizedError, ErrorCategory

type OverlayCustomizer = Callable[[str], None]

_VIRT_CUSTOMIZE_TIMEOUT_S = 5 * 60


def inject_authorized_key_argv(overlay_path: str, pubkey_file: str) -> list[str]:
    """Build the ``virt-customize --ssh-inject`` argv writing ``root``'s authorized_keys."""
    return [
        "virt-customize",
        "-a",
        overlay_path,
        "--ssh-inject",
        f"root:file:{pubkey_file}",
    ]


def _real_inject_authorized_key(overlay_path: str, pubkey: str) -> None:  # pragma: no cover - live_vm
    """Inject ``pubkey`` into the overlay's ``/root/.ssh/authorized_keys`` via libguestfs."""
    scratch = Path(tempfile.mkdtemp(prefix="kdive-inject-"))
    try:
        pub = scratch / "key.pub"
        pub.write_text(pubkey + "\n", encoding="utf-8")
        executable = shutil.which("virt-customize")
        if executable is None:
            raise CategorizedError(
                "virt-customize is not installed; cannot inject the per-System bootstrap key",
                category=ErrorCategory.MISSING_DEPENDENCY,
            )
        result = subprocess.run(  # noqa: S603 - fixed argv, kdive-owned paths
            [executable, *inject_authorized_key_argv(overlay_path, str(pub))[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=_VIRT_CUSTOMIZE_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise CategorizedError(
                "virt-customize failed to inject the per-System bootstrap key",
                category=ErrorCategory.PROVISIONING_FAILURE,
                details={"stderr": result.stderr[-2000:]},
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def authorized_key_customizer(pubkey: str) -> OverlayCustomizer:
    """Return an overlay customizer that injects ``pubkey`` into ``root``'s authorized_keys."""
    return lambda overlay_path: _real_inject_authorized_key(overlay_path, pubkey)
```

In `provisioning.py`, add the parameter and run customizers after `prepare_overlay`, before
`_define_and_start`, only when the overlay was created:

```python
from kdive.providers.local_libvirt.lifecycle.overlay_customize import OverlayCustomizer

def provision(
    self,
    system_id: UUID,
    profile: ProvisioningProfile,
    *,
    overlay_customizers: tuple[OverlayCustomizer, ...] = (),
) -> str:
    ...
    overlay = self._files.prepare_overlay(system_id, base=base)
    if overlay.created:
        for customize in overlay_customizers:
            customize(overlay.path)
    ...
```

Keep the existing `except CategorizedError: self._files.cleanup_overlay_if_created(overlay); raise`
so a customizer failure reclaims the just-created overlay (wrap the customizer loop inside the
existing `try`, or ensure it is covered by it — verify the try boundary).

- [ ] **Step 4: Run tests, verify they pass;** `just lint && just type`.

- [ ] **Step 5: Commit.**
  `git commit -m "feat(963): provision-time overlay-customizer seam + key inject"`

---

### Task 3: Handler wiring (provision / reprovision / teardown)

**Files:**
- Modify: `src/kdive/jobs/handlers/systems.py`
- Test: `tests/jobs/handlers/test_systems_bootstrap_key.py` (new)

**Interfaces:**
- Consumes: Task 1 (`ensure_/delete_system_bootstrap_key`), Task 2 (`authorized_key_customizer`,
  `provision(..., overlay_customizers=...)`).

- [ ] **Step 1: Write failing tests.** Assert: (a) provision_handler calls
  `ensure_system_bootstrap_key` and passes `overlay_customizers` containing one customizer to
  `provision`; (b) the ensure commit happens before the provision transaction (test that a forced
  failure inside the provision transaction leaves the key row present — the rollback-invariant
  test); (c) teardown_handler calls `delete_system_bootstrap_key`. Drive the handler with the
  existing fake provisioner used by sibling handler tests (search
  `tests/jobs/handlers/test_*` for the `provision_handler` harness — e.g. a stub `runtime` whose
  `provisioner.provision` records its kwargs). Use the `migrated_url` async conn so the key row is
  real.

```python
async def test_provision_handler_ensures_key_and_passes_customizer(...) -> None:
    recorded: dict = {}
    def fake_provision(system_id, profile, *, overlay_customizers=()):
        recorded["customizers"] = overlay_customizers
        return domain_name_for(system_id)
    # ... wire fake_provision into the runtime, run provision_handler ...
    assert len(recorded["customizers"]) == 1
    # key row exists and its public key round-trips
    assert (await ensure_system_bootstrap_key(conn, system_id)).startswith("ssh-ed25519 ")

async def test_key_row_survives_provision_transaction_rollback(...) -> None:
    # fake_provision raises after the key was ensured+committed; the row must remain.
    ...
    with pytest.raises(CategorizedError):
        await provision_handler(conn, job, ...)  # provisioner raises
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM system_bootstrap_keys WHERE system_id=%s",
                          (system_id,))
        assert (await cur.fetchone())[0] == 1

async def test_teardown_handler_deletes_key(...) -> None:
    await ensure_system_bootstrap_key(conn, system_id)
    await teardown_handler(conn, job, ...)
    async with conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM system_bootstrap_keys WHERE system_id=%s",
                          (system_id,))
        assert (await cur.fetchone())[0] == 0
```

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement the wiring.** In `provision_handler` and `reprovision_handler`, before
  the main `async with conn.transaction(), advisory_xact_lock(...)` block:

```python
async with conn.transaction():
    pubkey = await ensure_system_bootstrap_key(conn, system_id)  # committed before the overlay
```

Then pass the customizer into the `to_thread` provision call:

```python
customizers = (authorized_key_customizer(pubkey),)
domain_name = await asyncio.to_thread(
    functools.partial(provisioner.provision, system_id, profile, overlay_customizers=customizers)
)
```

(`reprovision` recreates the overlay, so it re-injects the same stored key — pass the customizer
into `provisioner.reprovision` the same way; add the kwarg to `reprovision` if not already
threaded, forwarding to its internal `provision` call.)

In `teardown_handler`, inside its existing transaction after `provisioner.teardown`, add:

```python
await delete_system_bootstrap_key(conn, system_id)
```

- [ ] **Step 4: Run tests + `just lint && just type`.** Confirm the ensure runs in its own
  committed transaction *before* the provision transaction (grep the handler to verify ordering).

- [ ] **Step 5: Commit.** `git commit -m "feat(963): wire per-System key into provision/teardown"`

---

### Task 4: Re-source `authorize_ssh_key` from the per-System key

**Files:**
- Modify: `src/kdive/jobs/handlers/ssh_authorize.py`
- Test: `tests/jobs/handlers/test_ssh_authorize.py` (extend)

**Interfaces:** `build_authorize_argv(port: int, key_path: str) -> list[str]` (was `(port)`);
handler loads the per-System private key and materializes it.

- [ ] **Step 1: Update the failing test.** Change existing assertions on `build_authorize_argv`
  to pass a `key_path` and assert it appears after `-i`. Add a handler test: with a key row
  present, the handler materializes a 0600 temp key and calls `ssh_exec` (inject the `ssh_exec`
  seam and capture the argv — assert `-i <temp>` and that the temp file is gone after the handler
  returns). With no key row, the handler raises `CONFIGURATION_ERROR`.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** Replace the `managed_private_key_path()` import/use:

```python
from kdive.prereqs.system_bootstrap_key import (
    load_system_bootstrap_private_key,
    materialized_private_key,
)

def build_authorize_argv(port: int, key_path: str) -> list[str]:
    return ["ssh", "-i", key_path, "-o", "BatchMode=yes", ...]  # rest unchanged

async def authorize_ssh_key_handler(conn, job, *, resolver, ssh_exec=_real_ssh_exec):
    ...
    _host, port = endpoint
    private_key = await load_system_bootstrap_private_key(conn, system_id)
    with materialized_private_key(private_key) as key_path:
        ssh_exec(build_authorize_argv(port, str(key_path)), payload.public_key)
    return None
```

- [ ] **Step 4: Run tests + `just lint && just type`.**

- [ ] **Step 5: Commit.** `git commit -m "feat(963): authorize_ssh_key uses the per-System key"`

---

### Task 5: Re-source drgn-live from the per-System key

**Files:**
- Modify: `src/kdive/providers/local_libvirt/debug/introspect.py` (engine: add `key_path` param to
  `_live_ssh_argv` and the `introspect_live`/`run_script` entry points)
- Modify: `src/kdive/mcp/tools/debug/introspect.py` (tool loads the key, materializes, passes path)
- Test: `tests/providers/local_libvirt/test_introspect*.py` + `tests/mcp/.../test_introspect*.py`
  (extend the existing suites — search for `_live_ssh_argv` / `managed_private_key_path` usage)

**Interfaces:** engine `_live_ssh_argv(transport_handle, secret_registry, drgn_args, key_path)`;
engine `introspect_live(*, transport_handle, helper, key_path)` /
`run_script(..., key_path=...)`; the MCP tool loads + materializes the key and passes `key_path`.

- [ ] **Step 1: Update failing tests.** Assert `_live_ssh_argv` builds `-i <key_path>` from the
  passed path (drop the `managed_private_key_path` expectation). Add/adjust a tool test asserting
  the debug/introspect tool loads `load_system_bootstrap_private_key` and passes a temp key path
  into the engine, cleaning it up afterward.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.** In `introspect.py`, remove the `managed_private_key_path` import and
  the in-engine `key_path = managed_private_key_path()`; take `key_path` as a parameter and register
  its content with the `secret_registry` at the tool boundary (the tool already holds the
  `SecretRegistry`). Thread `key_path` through `introspect_live`/`run_script` to `_live_ssh_argv`.
  In `mcp/tools/debug/introspect.py`, load the per-System key and materialize it:

```python
private_key = await load_system_bootstrap_private_key(conn, system_id)
with materialized_private_key(private_key) as key_path:
    result = engine.introspect_live(transport_handle=..., helper=..., key_path=str(key_path))
```

> Implementer: confirm the tool has (or can get) `conn` and `system_id`. If the tool reaches the
> engine without a connection, load the key one layer up where `conn` exists and pass the path
> down; record the exact call path in the report.

- [ ] **Step 4: Run tests + `just lint && just type`.**

- [ ] **Step 5: Commit.** `git commit -m "feat(963): drgn-live uses the per-System key"`

---

### Task 6: Remove the baked key and delete `managed_ssh_key.py`

**Files:**
- Modify: `src/kdive/images/families/rhel.py` (drop lines ~117-118 `--ssh-inject root:file:...`)
- Modify: `src/kdive/images/families/debian.py` (drop lines ~113-114)
- Modify: `src/kdive/images/families/base.py` (remove `CustomizeContext.authorized_key`)
- Modify: `src/kdive/providers/local_libvirt/rootfs_build.py` (remove the
  `ensure_managed_keypair()`/`managed_public_key_path()` seam that populates `authorized_key`)
- Delete: `src/kdive/prereqs/managed_ssh_key.py`
- Test: `tests/images/families/test_rhel.py`, `test_debian.py` (assert no `--ssh-inject`);
  `tests/prereqs/test_managed_ssh_key.py` (delete); any `rootfs_build` test referencing the key.

**Interfaces:** removes `CustomizeContext.authorized_key`; every constructor of `CustomizeContext`
drops that argument.

- [ ] **Step 1: Update tests first.** In `test_rhel.py`/`test_debian.py`, change the
  `test_*_injects_key*` assertions to assert `--ssh-inject` and `root:file:` are **absent** from
  `customize_argv`. Delete `tests/prereqs/test_managed_ssh_key.py`. Update every
  `CustomizeContext(...)` construction in tests to drop `authorized_key=...`.

- [ ] **Step 2: Run, verify fail** (argv still contains `--ssh-inject`).

- [ ] **Step 3: Implement the removal.**
  - Remove the `--ssh-inject`/`root:file:{ctx.authorized_key}` fragment from `rhel.py` and
    `debian.py`.
  - Remove `authorized_key: Path` from `CustomizeContext` (base.py) and its docstring line; update
    every constructor (families, `rootfs_build.py`, tests).
  - In `rootfs_build.py`, delete the `ensure_managed_keypair`/`managed_public_key_path` import and
    the seam (~lines 68-86) that computed `authorized_key`; drop the now-unused parameter threading.
  - `git rm src/kdive/prereqs/managed_ssh_key.py`.
  - Guard: `rg -n "managed_ssh_key|managed_public_key_path|ensure_managed_keypair|authorized_key" src/`
    returns nothing (all consumers already re-sourced in Tasks 4/5). If anything remains, it is a
    missed call site — fix it in this task.

- [ ] **Step 4: Run the full suite** (`.venv/bin/python -m pytest -q`) + `just lint && just type`.
  This task removes a module, so a broad run is required, not just the touched files.

- [ ] **Step 5: Commit.** `git commit -m "feat(963): drop the baked managed key; delete managed_ssh_key"`

---

## Post-task: guardrails, branch review, live proof, PR

- Full `just lint && just type && just test` green.
- Adversarial branch review (`/challenge --base main`) + `security-review`; fix findings.
- **Live proof (operator-run, behind live-VM markers):** rebuild the two dev images keyless,
  provision a System, `systems.authorize_ssh_key` succeeds, an agent SSHes in as root and runs an
  in-guest command — the full agent path #962's proof left blocked. Also exercise a drgn-live
  introspection to confirm the re-sourced key path.
- Open the PR against `main`, `Closes #963`.

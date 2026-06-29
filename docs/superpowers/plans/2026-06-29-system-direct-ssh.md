# Direct SSH to a System (agent-supplied key) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an agent supply its own SSH public key, have it authorized in a ready System's guest, and read the connection coordinates — so it SSHes in with its own private key, which KDIVE never holds.

**Architecture:** Two MCP tools on the `systems` plane over the existing local-libvirt loopback SSH forward (ADR-0218) and managed-key root SSH (ADR-0052): `systems.ssh_info` (VIEWER, synchronous read of the recorded SSH endpoint via the connect port) and `systems.authorize_ssh_key` (OPERATOR, a new `authorize_ssh_key` worker job that appends the validated public key to the guest `root` `authorized_keys`). See `docs/specs/2026-06-29-system-direct-ssh.md` and `docs/adr/0271-system-direct-ssh-access.md`.

**Tech Stack:** Python 3.14, `uv`, FastMCP, psycopg, libvirt-python, pytest. Guardrails: `just lint`, `just type`, `just test` (CI gates each individually).

## Global Constraints

- Python 3.14; absolute imports only (no relative `..`); ≤100 lines/function; ≤100-char lines; Google-style docstrings on public APIs.
- Return `ToolResponse` from every tool; pick the most specific existing `ErrorCategory` (never invent one). Native JSON int/bool in `data` (ADR-0263).
- Service-layer validation, not FastMCP `Field` bounds, for the public key (a `Field` bound leaks a raw `ValidationError` through `BindingErrorMiddleware`; ADR-0247/0259/0264).
- The private key is never in scope; the public key is not a secret (no redaction). The managed private key (`managed_private_key_path()`) is the worker's identity.
- Run `just lint && just type && just test` green before every commit. Each commit ends with the trailer `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Single-line key types accepted: `ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp256`, `ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `sk-ssh-ed25519@openssh.com`, `sk-ecdsa-sha2-nistp256@openssh.com`.

---

### Task 1: Public-key validator

**Files:**
- Create: `src/kdive/security/ssh_authorized_key.py`
- Test: `tests/security/test_ssh_authorized_key.py`

**Interfaces:**
- Produces: `validate_authorized_public_key(raw: str) -> str` — returns the normalized single-line key (stripped), or raises `CategorizedError(category=ErrorCategory.CONFIGURATION_ERROR, details={"reason": "invalid_public_key"})`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/security/test_ssh_authorized_key.py
import base64

import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.security.ssh_authorized_key import validate_authorized_public_key

_BLOB = base64.b64encode(b"\x00\x00\x00\x0bssh-ed25519abcdefgh").decode()


def _ed25519(comment: str = "agent@host") -> str:
    return f"ssh-ed25519 {_BLOB} {comment}"


def test_accepts_ed25519_with_comment() -> None:
    assert validate_authorized_public_key(f"  {_ed25519()}\n") == _ed25519()


def test_accepts_rsa_and_ecdsa_typeless_comment() -> None:
    assert validate_authorized_public_key(f"ssh-rsa {_BLOB}") == f"ssh-rsa {_BLOB}"
    assert (
        validate_authorized_public_key(f"ecdsa-sha2-nistp256 {_BLOB}")
        == f"ecdsa-sha2-nistp256 {_BLOB}"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        f"{_BLOB}",  # no key-type token
        f"ssh-ed25519 not_base64!!",  # blob not base64
        f'command="rm -rf /" ssh-ed25519 {_BLOB}',  # options field smuggled
        f"ssh-ed25519 {_BLOB}\nssh-ed25519 {_BLOB}",  # multi-line
        f"ssh-ed25519 {_BLOB}\x07 comment",  # control char
        f"ssh-ed25519 {_BLOB} " + "x" * 9000,  # over length
        f"ssh-dss {_BLOB}",  # disallowed key type
    ],
)
def test_rejects_malformed(bad: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        validate_authorized_public_key(bad)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "invalid_public_key"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/security/test_ssh_authorized_key.py -q`
Expected: FAIL — `ModuleNotFoundError: kdive.security.ssh_authorized_key`.

- [ ] **Step 3: Write the minimal implementation**

```python
# src/kdive/security/ssh_authorized_key.py
"""Validate an agent-supplied SSH public key before it is authorized in a guest (ADR-0271)."""

from __future__ import annotations

import base64
import binascii

from kdive.domain.errors import CategorizedError, ErrorCategory

_ALLOWED_KEY_TYPES = frozenset(
    {
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    }
)
_MAX_LEN = 8 * 1024


def _reject(detail: str) -> CategorizedError:
    return CategorizedError(
        detail,
        category=ErrorCategory.CONFIGURATION_ERROR,
        details={"reason": "invalid_public_key"},
    )


def validate_authorized_public_key(raw: str) -> str:
    """Return the normalized one-line public key, or raise ``CONFIGURATION_ERROR``.

    The trust boundary for a root-granting ``authorized_keys`` append: rejects multi-line
    input, control characters, an ``authorized_keys`` options/command prefix, a
    non-allow-listed key type, a non-base64 blob, and over-length input.
    """
    if len(raw) > _MAX_LEN:
        raise _reject("public key exceeds the maximum length")
    if any(ord(ch) < 0x20 and ch not in "\r\n" or ord(ch) == 0x7F for ch in raw):
        raise _reject("public key contains a control character")
    line = raw.strip()
    if not line or "\n" in line or "\r" in line:
        raise _reject("public key must be exactly one non-empty line")
    fields = line.split()
    if len(fields) < 2:
        raise _reject("public key must have a type token and a key blob")
    key_type, blob = fields[0], fields[1]
    if key_type not in _ALLOWED_KEY_TYPES:
        raise _reject(f"unsupported or non-key-type leading token: {key_type!r}")
    try:
        base64.b64decode(blob, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _reject("public key blob is not valid base64") from exc
    return line
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/security/test_ssh_authorized_key.py -q`
Expected: PASS (all cases).

- [ ] **Step 5: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/security/ssh_authorized_key.py tests/security/test_ssh_authorized_key.py
git commit -m "feat(security): validate agent-supplied SSH public keys

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `authorize_ssh_key` JobKind, migration 0052, and payload model

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py` (add the enum value)
- Create: `src/kdive/db/schema/0052_authorize_ssh_key_job_kind.sql`
- Modify: `src/kdive/jobs/payloads.py` (add `AuthorizeSshKeyPayload` + `_PAYLOAD_MODELS` entry)
- Test: `tests/jobs/test_payloads.py` (extend), schema replay covered by the existing migration test suite

**Interfaces:**
- Produces: `JobKind.AUTHORIZE_SSH_KEY = "authorize_ssh_key"`; `AuthorizeSshKeyPayload(system_id: str, public_key: str)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/jobs/test_payloads.py  (add)
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.payloads import AuthorizeSshKeyPayload, _PAYLOAD_MODELS


def test_authorize_ssh_key_payload_roundtrips() -> None:
    payload = AuthorizeSshKeyPayload(system_id="2b2e...", public_key="ssh-ed25519 AAAA x")
    assert payload.system_id and payload.public_key
    assert _PAYLOAD_MODELS[JobKind.AUTHORIZE_SSH_KEY] is AuthorizeSshKeyPayload
```
(Use a valid UUID string for `system_id` — copy one from a sibling payload test in the file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/jobs/test_payloads.py -q`
Expected: FAIL — `ImportError: cannot import name 'AuthorizeSshKeyPayload'`.

- [ ] **Step 3: Add the enum value**

In `src/kdive/domain/operations/jobs.py`, in `class JobKind(StrEnum)`, after `BUILD_INSTALL_BOOT = "build_install_boot"`:
```python
    AUTHORIZE_SSH_KEY = "authorize_ssh_key"
```

- [ ] **Step 4: Add the payload model + registry entry**

In `src/kdive/jobs/payloads.py`, define the payload near `SystemPayload` (validate the UUID like `SystemPayload` does):
```python
class AuthorizeSshKeyPayload(_PayloadBase):
    system_id: str
    public_key: str

    @field_validator("system_id")
    @classmethod
    def _valid_system_id(cls, value: str) -> str:
        UUID(value)
        return value
```
Add to `_PAYLOAD_MODELS`:
```python
    JobKind.AUTHORIZE_SSH_KEY: AuthorizeSshKeyPayload,
```

- [ ] **Step 5: Write migration 0052**

```sql
-- src/kdive/db/schema/0052_authorize_ssh_key_job_kind.sql
-- 0052_authorize_ssh_key_job_kind.sql — direct-SSH key authorization job (ADR-0271, #782).
-- Additive to 0003/0024/0040/0051 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit
-- the `authorize_ssh_key` op (systems.authorize_ssh_key enqueues one job whose handler appends
-- the agent public key to the guest root authorized_keys). Drop-and-recreate keeps the constraint
-- name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key'));
```

- [ ] **Step 6: Run payload + migration tests**

Run: `uv run python -m pytest tests/jobs/test_payloads.py -q` → PASS.
Run the schema/migration replay tests (find with `rg -l "schema|migration" tests/db`) — e.g. `uv run python -m pytest tests/db -q -k "schema or migration"`. If Docker is absent they skip; that is acceptable locally (CI sets `KDIVE_REQUIRE_DOCKER=1`). Expected: PASS or SKIP, never FAIL.

- [ ] **Step 7: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/domain/operations/jobs.py src/kdive/jobs/payloads.py \
        src/kdive/db/schema/0052_authorize_ssh_key_job_kind.sql tests/jobs/test_payloads.py
git commit -m "feat(jobs): add authorize_ssh_key JobKind + migration 0052

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `recorded_ssh_endpoint` on the Connect port

**Files:**
- Modify: `src/kdive/providers/ports/lifecycle.py` (add the method to `Connector` Protocol)
- Modify: `src/kdive/providers/local_libvirt/lifecycle/connect.py` (real implementation)
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/connect.py` (return `None`)
- Modify: `src/kdive/providers/fault_inject/lifecycle/connect.py` (return `None`)
- Test: `tests/providers/local_libvirt/test_connect.py` (extend)

**Interfaces:**
- Produces: `Connector.recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None` — the recorded loopback SSH `(host, port)` for a System, or `None` when the System has no SSH forward / no domain. Raises `CategorizedError(INFRASTRUCTURE_FAILURE)` only on an unexpected libvirt/parse error.
- Consumes (Task 5/6): `binding.runtime.connector.recorded_ssh_endpoint(SystemHandle(str(system_id)))`.

- [ ] **Step 1: Write the failing test**

```python
# tests/providers/local_libvirt/test_connect.py  (add)
from kdive.providers.local_libvirt.lifecycle.connect import LocalLibvirtConnect
from kdive.providers.ports.lifecycle import SystemHandle


def _connect_with(ssh_endpoint):
    # mirror the existing fake-seam construction used by the other connect tests
    return LocalLibvirtConnect(
        resolve_endpoint=lambda _s: ("127.0.0.1", 1),
        probe=lambda _h, _p: True,
        resolve_ssh_endpoint=ssh_endpoint,
        ssh_connect=lambda _h, _p: True,
    )


def test_recorded_ssh_endpoint_returns_host_port() -> None:
    connect = _connect_with(lambda _s: ("127.0.0.1", 22022))
    assert connect.recorded_ssh_endpoint(SystemHandle("sys-1")) == ("127.0.0.1", 22022)


def test_recorded_ssh_endpoint_none_when_not_provisioned() -> None:
    def _raise(_s):
        from kdive.domain.errors import CategorizedError, ErrorCategory

        raise CategorizedError("no ssh", category=ErrorCategory.CONFIGURATION_ERROR)

    connect = _connect_with(_raise)
    assert connect.recorded_ssh_endpoint(SystemHandle("sys-1")) is None
```
(Match the exact constructor kwargs the existing `test_connect.py` fakes use; adjust if the helper there differs.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_connect.py -q -k recorded_ssh_endpoint`
Expected: FAIL — `AttributeError: 'LocalLibvirtConnect' object has no attribute 'recorded_ssh_endpoint'`.

- [ ] **Step 3: Add the Protocol method**

In `src/kdive/providers/ports/lifecycle.py`, inside `class Connector(Protocol)` after `close_transport`:
```python
    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        """Return the recorded loopback SSH ``(host, port)`` for ``system``, or ``None``.

        ``None`` means the System was not provisioned with an SSH forward (no agent SSH is
        available). Providers without local SSH disclosure return ``None``.

        Raises:
            CategorizedError: ``INFRASTRUCTURE_FAILURE`` on an unexpected provider read error.
        """
        ...
```

- [ ] **Step 4: Implement on the three connectors**

`LocalLibvirtConnect` (`local_libvirt/lifecycle/connect.py`) — reuse the existing resolver, treat a `CONFIGURATION_ERROR` (no domain / no recorded port) as `None`:
```python
    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        try:
            return self._resolve_ssh_endpoint(system)
        except CategorizedError as exc:
            if exc.category is ErrorCategory.CONFIGURATION_ERROR:
                return None
            raise
```
`RemoteLibvirtConnect` and `FaultInjectConnect` — they have no local SSH forward:
```python
    def recorded_ssh_endpoint(self, system: SystemHandle) -> tuple[str, int] | None:
        return None
```
(Ensure `CategorizedError`/`ErrorCategory` are imported in `connect.py`; they already are for the existing resolver.)

- [ ] **Step 5: Run tests + type-check (the Protocol addition is enforced structurally)**

Run: `uv run python -m pytest tests/providers/local_libvirt/test_connect.py -q -k recorded_ssh_endpoint` → PASS.
Run: `just type` — confirms all three connectors satisfy the widened `Connector` Protocol at the composition site (a missing impl is a type error here).

- [ ] **Step 6: Lint, full type, commit**

```bash
just lint && just type
git add src/kdive/providers/ports/lifecycle.py \
        src/kdive/providers/local_libvirt/lifecycle/connect.py \
        src/kdive/providers/remote_libvirt/lifecycle/connect.py \
        src/kdive/providers/fault_inject/lifecycle/connect.py \
        tests/providers/local_libvirt/test_connect.py
git commit -m "feat(connect): expose recorded_ssh_endpoint on the Connect port

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `authorize_ssh_key` worker handler

**Files:**
- Create: `src/kdive/jobs/handlers/ssh_authorize.py`
- Modify: `src/kdive/jobs/handlers/systems.py` (register the handler in `register_handlers`)
- Test: `tests/jobs/handlers/test_ssh_authorize.py`

**Interfaces:**
- Consumes: `AuthorizeSshKeyPayload` (Task 2), `binding.runtime.connector.recorded_ssh_endpoint` (Task 3), `managed_private_key_path()`.
- Produces: `authorize_ssh_key_handler(conn, job, *, resolver, ssh_exec=<default>) -> str | None`; injected `ssh_exec: Callable[[list[str]], None]` (default runs the real managed-key SSH append; raises `CategorizedError` on failure). `build_authorize_argv(port: int, public_key: str) -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/jobs/handlers/test_ssh_authorize.py
import pytest

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers.ssh_authorize import build_authorize_argv


def test_argv_is_fixed_and_carries_key_as_a_single_element() -> None:
    key = "ssh-ed25519 AAAAC3Nz comment"
    argv = build_authorize_argv(22022, key)
    assert argv[0] == "ssh"
    assert "127.0.0.1" in " ".join(a for a in argv if a.startswith("root@"))
    assert "22022" in argv
    # the key travels as exactly one argv element, never interpolated into a shell string
    assert key in argv
    # the remote command is flock-guarded and idempotent
    joined = " ".join(argv)
    assert "flock" in joined and "grep -qxF" in joined
```
Add handler-level tests with a fake `ssh_exec` and a fake resolver/connector once the handler shape is in (assert: builds `root@127.0.0.1:<port>`; `ssh_exec` raising `TRANSPORT_FAILURE` propagates as `TRANSPORT_FAILURE`; `recorded_ssh_endpoint` returning `None` → `CONFIGURATION_ERROR`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/jobs/handlers/test_ssh_authorize.py -q`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement the handler + argv builder**

```python
# src/kdive/jobs/handlers/ssh_authorize.py
"""Worker handler: append an agent public key to a guest's root authorized_keys (ADR-0271)."""

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
from kdive.providers.ports.lifecycle import SystemHandle

type SshExec = Callable[[list[str]], None]

_LOOPBACK_HOST = "127.0.0.1"
_SSH_USER = "root"
_SSH_CONNECT_TIMEOUT_S = 10
_SSH_RUN_TIMEOUT_S = 30
_LOCK = "/root/.ssh/.kdive-authz.lock"

# Idempotent, flock-serialized append: create ~/.ssh 0700, then under the lock append the key to
# authorized_keys 0600 only if an exact line match is absent. "$1" is the key argv element — never
# interpolated into the shell string.
_REMOTE_CMD = (
    'mkdir -p /root/.ssh && chmod 700 /root/.ssh && '
    f'flock {_LOCK} sh -c \''
    'touch /root/.ssh/authorized_keys && chmod 600 /root/.ssh/authorized_keys && '
    'grep -qxF "$1" /root/.ssh/authorized_keys || printf "%s\\n" "$1" '
    ">> /root/.ssh/authorized_keys' _ \"$1\""
)


def build_authorize_argv(port: int, public_key: str) -> list[str]:
    """Build the fixed loopback SSH argv that authorizes ``public_key`` in the guest."""
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
    """Append the agent public key to the guest root authorized_keys over the managed-key SSH."""
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
```

- [ ] **Step 4: Register the handler**

In `src/kdive/jobs/handlers/systems.py` `register_handlers(...)` (which already receives `resolver`), add:
```python
    from kdive.jobs.handlers.ssh_authorize import authorize_ssh_key_handler

    registry.register(
        JobKind.AUTHORIZE_SSH_KEY,
        lambda conn, job: authorize_ssh_key_handler(conn, job, resolver=resolver),
    )
```
(Move the import to the module top if the file's convention is top-level imports.)

- [ ] **Step 5: Run handler tests**

Run: `uv run python -m pytest tests/jobs/handlers/test_ssh_authorize.py -q`
Expected: PASS (argv shape + the fake-`ssh_exec` handler cases).

- [ ] **Step 6: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/jobs/handlers/ssh_authorize.py src/kdive/jobs/handlers/systems.py \
        tests/jobs/handlers/test_ssh_authorize.py
git commit -m "feat(jobs): authorize_ssh_key worker handler

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `systems.ssh_info` MCP tool

**Files:**
- Create: `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py` (the two new handlers' shared service logic + envelope helpers)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (register `systems.ssh_info`)
- Test: `tests/mcp/tools/lifecycle/systems/test_ssh_info.py`

**Interfaces:**
- Consumes: `resolver` (already injected into the systems registrar), `binding.runtime.connector.recorded_ssh_endpoint`, `visible_next_actions` (ADR-0261), `require_role`, `current_context`.
- Produces: `async def ssh_info(pool, ctx, system_id, *, resolver) -> ToolResponse`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/tools/lifecycle/systems/test_ssh_info.py
# Mirror the harness of tests/mcp/tools/lifecycle/systems/test_systems_get.py (fake pool/ctx,
# a ready System fixture, a fake resolver whose connector.recorded_ssh_endpoint is configurable).
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools.lifecycle.systems.ssh_access import ssh_info


@pytest.mark.anyio
async def test_ssh_info_ready_returns_descriptor(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=("127.0.0.1", 22022), role="operator")
    resp = await ssh_info(env.pool, env.ctx, str(env.system_id), resolver=env.resolver)
    assert resp.status == "ok"
    assert resp.data["ssh"] == {
        "user": "root",
        "host": "127.0.0.1",
        "port": 22022,
        "jump_host": None,
        "host_scope": "worker_loopback",
    }
    assert isinstance(resp.data["ssh"]["port"], int)
    assert "systems.authorize_ssh_key" in resp.suggested_next_actions  # operator sees it


@pytest.mark.anyio
async def test_ssh_info_viewer_omits_operator_action(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=("127.0.0.1", 22022), role="viewer")
    resp = await ssh_info(env.pool, env.ctx, str(env.system_id), resolver=env.resolver)
    assert "systems.authorize_ssh_key" not in resp.suggested_next_actions


@pytest.mark.anyio
async def test_ssh_info_not_ready_is_readiness_failure(ready_system_env) -> None:
    env = ready_system_env(state="provisioning", role="operator")
    resp = await ssh_info(env.pool, env.ctx, str(env.system_id), resolver=env.resolver)
    assert resp.error_category == ErrorCategory.READINESS_FAILURE.value


@pytest.mark.anyio
async def test_ssh_info_unprovisioned_is_config_error(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=None, role="operator")
    resp = await ssh_info(env.pool, env.ctx, str(env.system_id), resolver=env.resolver)
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value
    assert resp.data["reason"] == "ssh_not_provisioned"
```
(Build `ready_system_env` from the existing `systems.get` test fixtures — reuse the same pool/context/System construction; add a fake resolver with `binding_for_system(...).runtime.connector.recorded_ssh_endpoint` returning the configured value.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/systems/test_ssh_info.py -q`
Expected: FAIL — module/function not found.

- [ ] **Step 3: Implement the handler**

```python
# src/kdive/mcp/tools/lifecycle/systems/ssh_access.py
"""systems.ssh_info / systems.authorize_ssh_key service logic (ADR-0271)."""

from __future__ import annotations

from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import SYSTEMS  # systems/view.py:13
from kdive.domain.capacity.state import SystemState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.mcp.exposure import visible_next_actions
from kdive.mcp.responses import ToolResponse
from kdive.providers.core.resolver import ProviderResolver  # resolver.py:64
from kdive.providers.ports.lifecycle import SystemHandle
from kdive.security.authz.context import RequestContext  # systems/view.py:32
from kdive.security.authz.rbac import Role, require_role


async def ssh_info(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Return the SSH connection descriptor for a ready System (read-only, VIEWER)."""
    uid = UUID(system_id)
    async with pool.connection() as conn:
        system = await SYSTEMS.get(conn, uid)  # raises NOT_FOUND via the plane's helper
        require_role(ctx, system.project, Role.VIEWER)
        if system.state is not SystemState.READY:
            return ToolResponse.failure(
                system_id,
                ErrorCategory.READINESS_FAILURE,
                detail="System is not ready; SSH is available only on a ready System.",
            )
        binding = await resolver.binding_for_system(conn, uid)
        try:
            endpoint = binding.runtime.connector.recorded_ssh_endpoint(SystemHandle(system_id))
        except CategorizedError as exc:
            # recorded_ssh_endpoint raises INFRASTRUCTURE_FAILURE on an unexpected provider
            # read error; tools must return an envelope, not raise (mirrors view.py:228).
            return ToolResponse.failure_from_error(system_id, exc)
    if endpoint is None:
        return ToolResponse.failure(
            system_id,
            ErrorCategory.CONFIGURATION_ERROR,
            detail="System was not provisioned for SSH; reprovision with ssh_credential_ref set.",
            data={"reason": "ssh_not_provisioned"},
        )
    host, port = endpoint
    actions = visible_next_actions(
        ["systems.authorize_ssh_key", "systems.get"], ctx, system.project
    )
    return ToolResponse.success(
        object_id=system_id,
        status="ok",
        data={
            "ssh": {
                "user": "root",
                "host": host,
                "port": port,
                "jump_host": None,
                "host_scope": "worker_loopback",
            }
        },
        suggested_next_actions=actions,
    )
```
(Resolve the exact import symbols by copying them from `systems/view.py` — `SYSTEMS`, `RequestContext`, the success `status` literal the plane uses, and the not-found handling. Match them; do not guess.)

- [ ] **Step 4: Register the tool**

In `registrar.py`, add `_register_systems_ssh_info(app, pool, resolver)` mirroring `_register_systems_get` but `resolver`-aware, and call it from `register(...)`:
```python
def _register_systems_ssh_info(app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver) -> None:
    @app.tool(
        name="systems.ssh_info",
        annotations=_docmeta.read_only(),
        meta={"maturity": "partial"},
    )
    async def systems_ssh_info(
        system_id: Annotated[str, Field(description="The ready System to return SSH coordinates for.")],
    ) -> ToolResponse:
        """Return SSH connection coordinates (user, host, port, jump_host) for a ready System."""
        return await ssh_info(pool, current_context(), system_id, resolver=resolver)
```
Add `_register_systems_ssh_info(app, pool, resolver)` to the `register(...)` body. (Use `maturity: "partial"` — the live SSH path is not CI-proven, mirroring `debug.*`.)

- [ ] **Step 5: Classify the tool in the same commit (keep the completeness guard green)**

Registering a tool without classifying it makes `tests/mcp/core/test_app.py` fail
(`CLASSIFIED_TOOLS | PUBLIC_TOOLS` must equal the live registry), so the classification must
land in this commit, not a later one. In `src/kdive/mcp/exposure.py` `_TOOL_SCOPES`, in the
`# systems` block, add:
```python
    "systems.ssh_info": _VIEWER,
```

- [ ] **Step 6: Run tests (tool + completeness guard)**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/systems/test_ssh_info.py tests/mcp/core/test_app.py tests/mcp/core/test_no_adr_leak.py -q` → PASS (completeness guard green because the tool is now classified; no ADR refs in the new descriptions).

- [ ] **Step 7: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/systems/ssh_access.py \
        src/kdive/mcp/tools/lifecycle/systems/registrar.py \
        src/kdive/mcp/exposure.py \
        tests/mcp/tools/lifecycle/systems/test_ssh_info.py
git commit -m "feat(systems): systems.ssh_info connection descriptor tool (viewer)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `systems.authorize_ssh_key` MCP tool

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py` (add `authorize_ssh_key` handler)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (register the tool)
- Test: `tests/mcp/tools/lifecycle/systems/test_authorize_ssh_key.py`

**Interfaces:**
- Consumes: `validate_authorized_public_key` (Task 1), the enqueue path (`queue.enqueue` / the systems-plane enqueue helper used by `systems.provision`), `AuthorizeSshKeyPayload`, `require_role(Role.OPERATOR)`.
- Produces: `async def authorize_ssh_key(pool, ctx, system_id, public_key, *, resolver) -> ToolResponse` returning `ToolResponse.from_job(...)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/tools/lifecycle/systems/test_authorize_ssh_key.py
import pytest

from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools.lifecycle.systems.ssh_access import authorize_ssh_key

_GOOD = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc agent@host"


@pytest.mark.anyio
async def test_viewer_denied(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=("127.0.0.1", 22022), role="viewer")
    with pytest.raises(Exception):  # RoleDenied from require_role
        await authorize_ssh_key(env.pool, env.ctx, str(env.system_id), _GOOD, resolver=env.resolver)


@pytest.mark.anyio
async def test_malformed_key_synchronous_config_error(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=("127.0.0.1", 22022), role="operator")
    resp = await authorize_ssh_key(env.pool, env.ctx, str(env.system_id), "not-a-key", resolver=env.resolver)
    assert resp.error_category == ErrorCategory.CONFIGURATION_ERROR.value


@pytest.mark.anyio
async def test_not_ready_readiness_failure(ready_system_env) -> None:
    env = ready_system_env(state="provisioning", role="operator")
    resp = await authorize_ssh_key(env.pool, env.ctx, str(env.system_id), _GOOD, resolver=env.resolver)
    assert resp.error_category == ErrorCategory.READINESS_FAILURE.value


@pytest.mark.anyio
async def test_happy_path_enqueues_job_with_normalized_key(ready_system_env) -> None:
    env = ready_system_env(recorded_endpoint=("127.0.0.1", 22022), role="operator")
    resp = await authorize_ssh_key(env.pool, env.ctx, str(env.system_id), f"  {_GOOD}\n", resolver=env.resolver)
    assert resp.status == "running"
    assert env.last_enqueued.kind.value == "authorize_ssh_key"
    assert env.last_enqueued.payload["public_key"] == _GOOD  # stripped/normalized
```
(Extend `ready_system_env` with an `enqueue` capture — record the payload the tool enqueues, mirroring the existing `systems.provision` tool test's enqueue fake.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/systems/test_authorize_ssh_key.py -q`
Expected: FAIL — function not found.

- [ ] **Step 3: Implement the handler**

Add to `ssh_access.py`. Note `authorize` does **not** go through the admission service that
`systems.provision` uses (no capacity to admit) — it enqueues directly via `queue.enqueue`.
Add these imports: `from kdive.domain.operations.jobs import JobKind`,
`from kdive.jobs import queue` (match the module path `admission.py:queue.enqueue` resolves
to), `from kdive.jobs.context import authorizing as job_authorizing` (admission.py:37),
`from kdive.jobs.payloads import AuthorizeSshKeyPayload`,
`from kdive.security.ssh_authorized_key import validate_authorized_public_key`.

```python
async def authorize_ssh_key(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    public_key: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Authorize an agent public key in a ready System's guest (mutating, OPERATOR job)."""
    uid = UUID(system_id)
    async with pool.connection() as conn:
        system = await SYSTEMS.get(conn, uid)
        require_role(ctx, system.project, Role.OPERATOR)
        if system.state is not SystemState.READY:
            return ToolResponse.failure(
                system_id,
                ErrorCategory.READINESS_FAILURE,
                detail="System is not ready; SSH is available only on a ready System.",
            )
        binding = await resolver.binding_for_system(conn, uid)
        try:
            endpoint = binding.runtime.connector.recorded_ssh_endpoint(SystemHandle(system_id))
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(system_id, exc)
        if endpoint is None:
            return ToolResponse.failure(
                system_id,
                ErrorCategory.CONFIGURATION_ERROR,
                detail="System was not provisioned for SSH; reprovision with ssh_credential_ref set.",
                data={"reason": "ssh_not_provisioned"},
            )
        try:
            normalized = validate_authorized_public_key(public_key)
        except CategorizedError as exc:
            return ToolResponse.failure_from_error(system_id, exc)
        job = await queue.enqueue(
            conn,
            JobKind.AUTHORIZE_SSH_KEY,
            AuthorizeSshKeyPayload(system_id=system_id, public_key=normalized),
            job_authorizing(ctx, system.project),
            f"{system_id}:authorize_ssh_key",
        )
    return ToolResponse.from_job(job)
```
(Confirm `queue.enqueue`'s exact import path by copying the line `admission.py` uses; the
call signature is `enqueue(conn, kind, payload, authorizing, dedup_key)`.)

- [ ] **Step 4: Register the tool**

In `registrar.py`, add `_register_systems_authorize_ssh_key(app, pool, resolver)` mirroring the
mutating-tool registration (use the same mutating annotation `systems.provision` uses — copy
`_docmeta.mutating()` / its exact annotations; `meta={"maturity": "partial"}`):
```python
def _register_systems_authorize_ssh_key(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.authorize_ssh_key",
        annotations=_docmeta.mutating(),
        meta={"maturity": "partial"},
    )
    async def systems_authorize_ssh_key(
        system_id: Annotated[str, Field(description="The ready System to authorize the key on.")],
        public_key: Annotated[
            str, Field(description="The agent SSH public key to authorize in the guest root account.")
        ],
    ) -> ToolResponse:
        """Authorize an agent SSH public key in a ready System's guest root account."""
        return await authorize_ssh_key(
            pool, current_context(), system_id, public_key, resolver=resolver
        )
```
Call `_register_systems_authorize_ssh_key(app, pool, resolver)` from `register(...)`.

- [ ] **Step 5: Classify the tool in the same commit (keep the completeness guard green)**

In `src/kdive/mcp/exposure.py` `_TOOL_SCOPES`, in the `# systems` block, add:
```python
    "systems.authorize_ssh_key": _OPERATOR,
```

- [ ] **Step 6: Run tests (tool + completeness guard)**

Run: `uv run python -m pytest tests/mcp/tools/lifecycle/systems/test_authorize_ssh_key.py tests/mcp/core/test_app.py tests/mcp/core/test_no_adr_leak.py -q` → PASS.

- [ ] **Step 7: Lint, type, commit**

```bash
just lint && just type
git add src/kdive/mcp/tools/lifecycle/systems/ssh_access.py \
        src/kdive/mcp/tools/lifecycle/systems/registrar.py \
        src/kdive/mcp/exposure.py \
        tests/mcp/tools/lifecycle/systems/test_authorize_ssh_key.py
git commit -m "feat(systems): systems.authorize_ssh_key tool enqueues the authorize job (operator)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Regenerate the committed tool reference + full-suite gate

Exposure classification already landed with each tool (Tasks 5/6), so this task only
regenerates the generated docs and runs the whole suite as the pre-push gate.

**Files:**
- Modify: committed tool reference (regenerated by `just docs`)

- [ ] **Step 1: Regenerate the committed tool reference**

Run: `just docs` then `just docs-check` → confirms the generated reference matches the two new tools.

- [ ] **Step 2: Full local gate**

Run: `just lint && just type && just test` — the whole suite (architecture, registry, and doc-generation guards live outside the dirs touched). Expected: all green; `tests/mcp/core/test_app.py` and `tests/mcp/core/test_no_adr_leak.py` green.

- [ ] **Step 3: Commit**

```bash
git add docs/   # the regenerated tool reference
git commit -m "docs: regenerate tool reference for the direct-SSH tools

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Live end-to-end proof (`live_vm`-gated)

**Files:**
- Create: `tests/live/test_system_direct_ssh.py` (marked `live_vm`)

**Interfaces:**
- Consumes: the full stack on this KVM host. Gated behind the existing `live_vm` marker; runs under `just test-live`, skipped by `just test`.

- [ ] **Step 1: Write the gated end-to-end test**

Mirror an existing `live_vm` test's bring-up (find one with `rg -l "live_vm" tests/live`). The test: provision a System with `ssh_credential_ref` set (so the SSH forward renders); generate a throwaway ed25519 keypair in `tmp_path`; call `systems.authorize_ssh_key` with the pubkey and drive the job to done via `jobs.wait`; call `systems.ssh_info` and read `data.ssh.port`; run `ssh -i <tmp privkey> -o BatchMode=yes -o StrictHostKeyChecking=no -p <port> root@127.0.0.1 true` and assert exit 0. Assert it fails closed before authorize (the same `ssh ... true` returns non-zero with only the generated key, before the append).

- [ ] **Step 2: Run it on this host**

Run: `just test-live -k direct_ssh` (or the repo's live invocation). Expected: PASS on this KVM host. If the guest SSH-NIC does not DHCP (the #697 open risk), this is the live-proof blocker to surface — not a code fault in this change.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_system_direct_ssh.py
git commit -m "test(live): end-to-end agent-supplied-key direct SSH proof

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** §1 validator → Task 1. §2 ssh_info (descriptor, host_scope, role-filter, READINESS/CONFIG errors, connect-seam read) → Tasks 3+5. §3 authorize_ssh_key (validate-before-enqueue, worker append, flock-atomic, ConnectTimeout, retryable TRANSPORT_FAILURE) → Tasks 1+4+6. §4 registration/exposure/migration 0052 → Tasks 2+7. §5 acceptance → Tasks 1-7 (CI) + Task 8 (live). All sections mapped.
- **Placeholder scan:** code steps carry real code; wiring steps point at exact existing call sites to copy (`systems.provision` enqueue, `systems.get` registration, `systems/view.py` imports) rather than vague "add wiring" — the implementer resolves the few repo-specific symbols (`SYSTEMS`, `RequestContext`, `job_authorizing`, success-`status` literal) by copying from the named sibling, which is the correct anti-guess instruction in this codebase.
- **Type consistency:** `recorded_ssh_endpoint(system: SystemHandle) -> tuple[str, int] | None` is defined in Task 3 and consumed with that exact signature in Tasks 4-5; `AuthorizeSshKeyPayload(system_id, public_key)` defined in Task 2 and used in Tasks 4+6; `validate_authorized_public_key(raw) -> str` defined in Task 1 and used in Task 6; `build_authorize_argv(port, public_key)` defined and tested in Task 4.

# SSH-reachability runtime probe (#972) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a caller learn, before attempting SSH, whether a ready System's guest sshd is answering — via a new VIEWER tool `systems.check_ssh_reachable` that enqueues a worker job whose verdict surfaces through `jobs.wait`.

**Architecture:** A worker-job runtime probe (the worker is the only vantage that can reach the `worker_loopback` forward). The server tool validates + enqueues; the worker handler re-checks readiness, resolves the recorded endpoint, opens a bounded/retried TCP connect, reads the SSH banner, classifies it into a fixed vocabulary, and returns a compact-JSON verdict inline in `result_ref` (surfaced as `jobs.wait` `refs.result`).

**Tech Stack:** Python 3.14, `asyncio` (probe), psycopg (queue), FastMCP (tool wrapper), pytest. Spec: `docs/superpowers/specs/2026-07-02-ssh-reachable-runtime-probe-972.md`; ADR: `docs/adr/0298-ssh-reachable-runtime-probe.md`.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict whole-tree.
- Guardrails before every commit: `just lint && just type && uv run python -m pytest <focused> -q`. Full `just ci` before push.
- Google-style docstrings on non-trivial public APIs. Absolute imports only.
- **The `@app.tool` wrapper docstring + `Field` text is the agent-facing contract** and must carry **no** `ADR-NNNN` reference (#880 guard `tests/mcp/core/test_no_adr_leak.py`). ADR citations live only on module/handler docstrings.
- **Clock injection convention:** stamp wall-clock reads from the module-level `datetime` (`from datetime import UTC, datetime`; call `datetime.now(UTC)`) and, in tests, `monkeypatch.setattr(<module>, "datetime", FrozenClock(instant))` — the repo's standardized idiom (post-#931; e.g. `tests/mcp/lifecycle/test_allocations_renew.py:262`). Do **not** thread a clock parameter; `FrozenClock` is an instance, not a `type[datetime]`, so a param would fail `just type`.
- New tool in the `systems` namespace must be named in `docs/guide/toolsets/systems.md` (#940 guard).
- Reuse the most specific existing `ErrorCategory`; do not invent strings. No new `ErrorCategory`.
- Never echo raw guest banner bytes into a persisted verdict.
- `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer on every commit.

## File structure

- Create `src/kdive/jobs/handlers/ssh_reachable.py` — probe fn (`ReachResult`, `_real_probe`), verdict codec (`serialize_reach_verdict`), `check_ssh_reachable_handler`.
- Modify `src/kdive/domain/operations/jobs.py` — add `JobKind.CHECK_SSH_REACHABLE`.
- Create `src/kdive/db/schema/0057_check_ssh_reachable_job_kind.sql` — widen `jobs_kind_check`.
- Modify `src/kdive/jobs/payloads.py` — `CheckSshReachablePayload` + PAYLOAD map entry.
- Modify `src/kdive/jobs/handlers/systems.py` — register the handler in `register_handlers`.
- Modify `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py` — `check_ssh_reachable` server handler; add `systems.check_ssh_reachable` to `ssh_info`'s next actions.
- Modify `src/kdive/mcp/tools/lifecycle/systems/registrar.py` — register the `systems.check_ssh_reachable` wrapper.
- Modify `src/kdive/images/capability_signals.py` — drop the `ssh_reachable` `PlannedSignal`.
- Modify `docs/guide/toolsets/systems.md` — name the new tool.
- Tests: `tests/db/test_migration_0057_check_ssh_reachable.py`, `tests/jobs/handlers/test_ssh_reachable.py`, `tests/mcp/lifecycle/test_ssh_access_tools.py` (extend), `tests/images/test_capability_signals.py` (update).

---

### Task 1: `JobKind.CHECK_SSH_REACHABLE` + migration 0057

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py:33`
- Create: `src/kdive/db/schema/0057_check_ssh_reachable_job_kind.sql`
- Create: `tests/db/test_migration_0057_check_ssh_reachable.py`

**Interfaces:**
- Produces: `JobKind.CHECK_SSH_REACHABLE = "check_ssh_reachable"`.

- [ ] **Step 1: Add the enum member.** In `jobs.py`, after `DIAGNOSTIC_SYSRQ = "diagnostic_sysrq"`:

```python
    DIAGNOSTIC_SYSRQ = "diagnostic_sysrq"
    CHECK_SSH_REACHABLE = "check_ssh_reachable"
```

- [ ] **Step 2: Write the migration** (`0057_check_ssh_reachable_job_kind.sql`), mirroring 0055:

```sql
-- 0057_check_ssh_reachable_job_kind.sql — SSH-reachability probe job kind (#972).
-- Additive to 0052/0055 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `check_ssh_reachable` job kind (systems.check_ssh_reachable enqueues one job whose handler opens
-- a bounded TCP connect to the recorded loopback endpoint and reads the SSH banner, ADR-0298).
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable'));
```

- [ ] **Step 3: Write the per-migration test** (`tests/db/test_migration_0057_check_ssh_reachable.py`), copying the 0055 test's `_apply_through`/`_insert_job` helpers verbatim, then:

```python
def test_migration_0057_admits_check_ssh_reachable(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0055")
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_job(pg_conn, JobKind.CHECK_SSH_REACHABLE.value, "before-0057")
    pg_conn.rollback()
    _apply_through(pg_conn, "0057")
    _insert_job(pg_conn, JobKind.CHECK_SSH_REACHABLE.value, "after-0057")  # no raise
```

The fixture is `pg_conn: psycopg.Connection` (confirmed in `test_migration_0055_diagnostic_sysrq.py`); reuse its exact `_apply_through`/`_insert_job` bodies. Mirror all three 0055 shapes: a `test_pre_migration_0057_rejects_check_ssh_reachable` (raises `CheckViolation` at 0055), the admits-at-0057 test above, and a `test_migration_0057_keeps_all_prior_kinds` that inserts one job of each earlier kind after 0057.

- [ ] **Step 4: Update the exact-JobKind-set assertion (required).** `tests/domain/test_models.py:302` asserts `{kind.value for kind in JobKind} == {<explicit set>}`. Add `"check_ssh_reachable"` to that set literal, or Task 1's commit is red. (Grep `check_ssh_reachable` across `tests/` after Task 1 to catch any other exact-set parity assertion.)

- [ ] **Step 5: Run.** `uv run python -m pytest tests/db/test_migration_0057_check_ssh_reachable.py tests/db/test_migrate.py tests/domain/test_models.py -q` (the last two hold the SQL↔enum tie). Expected: PASS.

- [ ] **Step 6: Commit.** `git add -A && git commit` — `feat(972): add check_ssh_reachable job kind + migration 0057`.

---

### Task 2: `CheckSshReachablePayload`

**Files:**
- Modify: `src/kdive/jobs/payloads.py` (near `AuthorizeSshKeyPayload`, and the `_PAYLOAD_MODELS` map)
- Test: covered via Task 4's handler test (payload load path)

**Interfaces:**
- Produces: `CheckSshReachablePayload(SystemPayload)` — field `system_id: str` only.

- [ ] **Step 1: Add the payload** after `AuthorizeSshKeyPayload`:

```python
class CheckSshReachablePayload(SystemPayload):
    """A request to probe a ready System's guest sshd reachability (ADR-0298, #972)."""
```

- [ ] **Step 2: Register it** in the non-run payload map (the dict containing `JobKind.AUTHORIZE_SSH_KEY: AuthorizeSshKeyPayload`):

```python
    JobKind.CHECK_SSH_REACHABLE: CheckSshReachablePayload,
```

- [ ] **Step 3: Run** `just type` and `uv run python -m pytest tests/jobs -q -k payload`. Expected: PASS.

- [ ] **Step 4: Commit** — `feat(972): add CheckSshReachablePayload`.

---

### Task 3: Probe function + verdict codec

**Files:**
- Create: `src/kdive/jobs/handlers/ssh_reachable.py`
- Test: `tests/jobs/handlers/test_ssh_reachable.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True, slots=True) class ReachResult: reachable: bool; detail: str`
  - `async def _real_probe(host: str, port: int, *, deadline_s: float = _PROBE_DEADLINE_S) -> ReachResult`
  - `def serialize_reach_verdict(result: ReachResult, host: str, port: int, checked_at: str) -> str`
  - constants `_PROBE_DEADLINE_S = 15.0`, `_CONNECT_TIMEOUT_S = 5.0`, `_BANNER_MAX_BYTES = 255`, `_BACKOFF_S = 0.5`
  - `type ProbeFn = Callable[[str, int], Awaitable[ReachResult]]`

- [ ] **Step 1: Write failing tests** (`tests/jobs/handlers/test_ssh_reachable.py`). Drive `_real_probe` against a real asyncio loopback server so the paths are behavioral, not mocked-logic:

```python
import asyncio
import pytest
from kdive.jobs.handlers.ssh_reachable import _real_probe, serialize_reach_verdict, ReachResult


async def _serve_once(banner: bytes) -> tuple[str, int, asyncio.AbstractServer]:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if banner:
            writer.write(banner)
            await writer.drain()
        writer.close()
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    return host, port, server


@pytest.mark.asyncio
async def test_probe_reachable_on_ssh_banner() -> None:
    host, port, server = await _serve_once(b"SSH-2.0-OpenSSH_9.6\r\n")
    async with server:
        result = await _real_probe(host, port, deadline_s=3.0)
    assert result == ReachResult(True, "reachable")


@pytest.mark.asyncio
async def test_probe_no_ssh_banner_when_wrong_prefix() -> None:
    host, port, server = await _serve_once(b"HELLO not ssh\r\n")
    async with server:
        result = await _real_probe(host, port, deadline_s=3.0)
    assert result == ReachResult(False, "no SSH banner")


@pytest.mark.asyncio
async def test_probe_unreachable_on_closed_port() -> None:
    # bind then close to get a definitely-closed port
    host, port, server = await _serve_once(b"")
    server.close()
    await server.wait_closed()
    result = await _real_probe(host, port, deadline_s=1.0)
    assert result == ReachResult(False, "unreachable")


@pytest.mark.asyncio
async def test_probe_retries_until_sshd_binds() -> None:
    # Port is closed for the first ~0.4s, then a banner-answering server binds — proves the
    # bounded retry tolerates the readiness (sshd-bind) race instead of a false "unreachable".
    host = "127.0.0.1"
    import socket
    s = socket.socket()
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()  # port now free but nothing listening

    async def bind_late() -> asyncio.AbstractServer:
        await asyncio.sleep(0.4)
        async def handle(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
            w.write(b"SSH-2.0-x\r\n"); await w.drain(); w.close()
        return await asyncio.start_server(handle, host, port)

    server_task = asyncio.create_task(bind_late())
    result = await _real_probe(host, port, deadline_s=3.0)
    server = await server_task
    server.close(); await server.wait_closed()
    assert result == ReachResult(True, "reachable")


def test_serialize_verdict_is_compact_and_redacted() -> None:
    raw = serialize_reach_verdict(ReachResult(False, "no SSH banner"), "127.0.0.1", 22001, "2026-07-02T00:00:00+00:00")
    assert raw == (
        '{"reachable":false,"checked_at":"2026-07-02T00:00:00+00:00",'
        '"endpoint":{"host":"127.0.0.1","port":22001},"detail":"no SSH banner"}'
    )
```

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/jobs/handlers/test_ssh_reachable.py -q`. Expected: FAIL (module missing).

- [ ] **Step 3: Implement** `src/kdive/jobs/handlers/ssh_reachable.py`:

```python
"""SSH-reachability probe primitives for the check_ssh_reachable worker job (ADR-0298, #972).

The probe opens a bounded, connection-retried TCP connection to a System's recorded loopback SSH
forward and reads the server banner. It sends nothing (sshd banners first; no handshake, no auth)
and never echoes the raw banner — the guest banner is external output, so it is classified into a
fixed vocabulary. The bounded retry tolerates the ~46 ms readiness (sshd-bind) race that
authorize_ssh_key also retries for (ADR-0289), so the probe is not more pessimistic than the op it
gates, while a far shorter deadline keeps it a quick check.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass

from kdive.serialization import JsonValue

_PROBE_DEADLINE_S = 15.0
_CONNECT_TIMEOUT_S = 5.0
_BANNER_MAX_BYTES = 255
_BACKOFF_S = 0.5


@dataclass(frozen=True, slots=True)
class ReachResult:
    """The classified outcome of one probe: reachable, plus a fixed-vocabulary detail."""

    reachable: bool
    detail: str  # "reachable" | "unreachable" | "no SSH banner"


type ProbeFn = Callable[[str, int], Awaitable[ReachResult]]


async def _real_probe(host: str, port: int, *, deadline_s: float = _PROBE_DEADLINE_S) -> ReachResult:
    """Probe ``host:port`` for an SSH banner, retrying connection-level failures until ``deadline_s``.

    Returns ``reachable`` iff a banner beginning ``SSH-`` arrives; ``no SSH banner`` when a
    connection is accepted but no ``SSH-`` line arrives; ``unreachable`` when nothing accepts a
    connection before the deadline. Sends no bytes and never returns the raw banner.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + deadline_s
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            return ReachResult(False, "unreachable")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=min(_CONNECT_TIMEOUT_S, remaining),
            )
        except OSError:  # refused / reset / connect-timeout: sshd may still be binding — retry
            await asyncio.sleep(min(_BACKOFF_S, max(0.0, end - loop.time())))
            continue
        try:
            banner = await asyncio.wait_for(
                reader.read(_BANNER_MAX_BYTES), timeout=max(0.1, end - loop.time())
            )
        except OSError:
            banner = b""
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        return ReachResult(True, "reachable") if banner.startswith(b"SSH-") else ReachResult(
            False, "no SSH banner"
        )


def serialize_reach_verdict(result: ReachResult, host: str, port: int, checked_at: str) -> str:
    """Compact-JSON reachability verdict carried inline in the job ``result_ref`` (ADR-0164 pattern)."""
    verdict: dict[str, JsonValue] = {
        "reachable": result.reachable,
        "checked_at": checked_at,
        "endpoint": {"host": host, "port": port},
        "detail": result.detail,
    }
    return json.dumps(verdict, separators=(",", ":"))
```

- [ ] **Step 4: Run.** `uv run python -m pytest tests/jobs/handlers/test_ssh_reachable.py -q && just lint && just type`. Expected: PASS. (`asyncio.TimeoutError` is a subclass of `OSError` in 3.14, so the single `except OSError` covers connect timeouts.)

- [ ] **Step 5: Commit** — `feat(972): add SSH-reachability probe + verdict codec`.

---

### Task 4: Worker handler + registration

**Files:**
- Modify: `src/kdive/jobs/handlers/ssh_reachable.py` (add `check_ssh_reachable_handler`)
- Modify: `src/kdive/jobs/handlers/systems.py` (register in `register_handlers`)
- Test: `tests/jobs/handlers/test_ssh_reachable.py` (extend)

**Interfaces:**
- Consumes: `ReachResult`, `ProbeFn`, `serialize_reach_verdict`; `CheckSshReachablePayload` (Task 2); `JobKind.CHECK_SSH_REACHABLE` (Task 1).
- Produces: `async def check_ssh_reachable_handler(conn, job, *, resolver, probe=_real_probe) -> str | None` (reads module-level `datetime.now(UTC)`; tests monkeypatch it).

- [ ] **Step 1: Write failing handler tests.** Use a fake resolver/binding returning a fixed endpoint (mirror the `authorize_ssh_key_handler` tests in the repo — find them with `rg -l authorize_ssh_key_handler tests`), a stub `probe`, and `monkeypatch` the handler module's `datetime` with `FrozenClock` for a deterministic `checked_at` (the repo convention — do **not** pass a clock param):

```python
from datetime import UTC, datetime
from tests.clock import FrozenClock
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.jobs.handlers import ssh_reachable
from kdive.jobs.handlers.ssh_reachable import check_ssh_reachable_handler, ReachResult

_FROZEN = datetime(2026, 7, 2, 0, 0, tzinfo=UTC)

@pytest.mark.asyncio
async def test_handler_serializes_reachable_verdict(..., monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ssh_reachable, "datetime", FrozenClock(_FROZEN))
    async def probe(host: str, port: int) -> ReachResult:
        return ReachResult(True, "reachable")
    raw = await check_ssh_reachable_handler(conn, job, resolver=resolver, probe=probe)
    assert raw == (
        '{"reachable":true,"checked_at":"2026-07-02T00:00:00+00:00",'
        '"endpoint":{"host":"127.0.0.1","port":22001},"detail":"reachable"}'
    )

@pytest.mark.asyncio
async def test_handler_dead_letters_when_system_not_ready(...) -> None:
    # System row state != READY
    with pytest.raises(CategorizedError) as exc:
        await check_ssh_reachable_handler(conn, job, resolver=resolver)
    assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
    assert exc.value.details["reason"] == "system_not_ready"

@pytest.mark.asyncio
async def test_handler_dead_letters_when_no_forward(...) -> None:
    # binding.runtime.connector.recorded_ssh_endpoint returns None
    with pytest.raises(CategorizedError) as exc:
        await check_ssh_reachable_handler(conn, job, resolver=resolver)
    assert exc.value.details["reason"] == "ssh_not_provisioned"
```

(Seed the System row through the same fixtures the `authorize_ssh_key_handler` tests use — reuse that test module's DB/system setup helpers so this stays a behavioral DB test, not a mock of `SYSTEMS.get`.)

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/jobs/handlers/test_ssh_reachable.py -q`. Expected: FAIL (`check_ssh_reachable_handler` missing).

- [ ] **Step 3: Implement the handler** (append to `ssh_reachable.py`; add module-level imports `from datetime import UTC, datetime`, `from uuid import UUID`, plus `load_payload`/`CheckSshReachablePayload`, `SYSTEMS`, `SystemState`, `CategorizedError`/`ErrorCategory`, `Job`, `AsyncConnection`, `ProviderResolver`, `SystemHandle`, `domain_name_for` — mirroring `ssh_authorize.py` + `systems.py`). `checked_at` reads the module-level `datetime.now(UTC)` (monkeypatched in tests), **not** a param:

```python
async def check_ssh_reachable_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver,
    probe: ProbeFn = _real_probe,
) -> str | None:
    """Probe a ready System's guest sshd and return the compact-JSON reachability verdict.

    Re-checks the System is still ``ready`` before probing (a torn-down System's loopback port
    can be reused, so a stale endpoint could misattribute another guest's liveness). A probe that
    *ran* — reachable or not — is a success; only an inability to run raises.

    Raises:
        CategorizedError: ``CONFIGURATION_ERROR`` ``reason="system_not_ready"`` when the System is
            no longer ready, or ``reason="ssh_not_provisioned"`` when it exposes no loopback forward.
    """
    payload = load_payload(job, CheckSshReachablePayload)
    system_id = UUID(payload.system_id)
    system = await SYSTEMS.get(conn, system_id)
    if system is None or system.state is not SystemState.READY:
        raise CategorizedError(
            "system is no longer ready; cannot probe SSH reachability",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "system_not_ready"},
        )
    binding = await resolver.binding_for_system(conn, system_id)
    endpoint = binding.runtime.connector.recorded_ssh_endpoint(
        SystemHandle(system.domain_name or domain_name_for(system_id))
    )
    if endpoint is None:
        raise CategorizedError(
            "This System's provider exposes no loopback SSH forward; direct SSH to a System is a "
            "local-libvirt capability",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={"reason": "ssh_not_provisioned"},
        )
    host, port = endpoint
    result = await probe(host, port)
    return serialize_reach_verdict(result, host, port, datetime.now(UTC).isoformat())
```

- [ ] **Step 4: Register the handler** in `systems.py` `register_handlers` (import `check_ssh_reachable_handler` at top), after the `AUTHORIZE_SSH_KEY` registration:

```python
    registry.register(
        JobKind.CHECK_SSH_REACHABLE,
        lambda conn, job: check_ssh_reachable_handler(conn, job, resolver=resolver),
    )
```

- [ ] **Step 5: Run.** `uv run python -m pytest tests/jobs/handlers/test_ssh_reachable.py -q && just type`. Also run any "every JobKind has a registered handler" test (`rg -l "register_handlers\|HandlerRegistry\|all.*handler" tests/jobs tests/mcp`). Expected: PASS.

- [ ] **Step 6: Commit** — `feat(972): add check_ssh_reachable worker handler + registration`.

---

### Task 5: `systems.check_ssh_reachable` tool + registrar

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/ssh_access.py`
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py`
- Test: `tests/mcp/lifecycle/test_ssh_access_tools.py` (or the existing module testing `ssh_info`/`authorize_ssh_key` — find via `rg -l "def test.*ssh_info\|check_ssh_reachable\|authorize_ssh_key" tests/mcp`)

**Interfaces:**
- Consumes: `JobKind.CHECK_SSH_REACHABLE`, `CheckSshReachablePayload`, `queue.enqueue`, `job_authorizing`.
- Produces: `async def check_ssh_reachable(pool, ctx, system_id, *, resolver) -> ToolResponse`; MCP tool `systems.check_ssh_reachable`.

- [ ] **Step 1: Write failing handler tests** mirroring the `ssh_info` tests (reuse that module's fixtures for a ready System with a recorded endpoint, a non-ready System, a non-viewer ctx, and a None-endpoint provider):

```python
async def test_check_ssh_reachable_enqueues_for_ready_system(...) -> None:
    resp = await check_ssh_reachable(pool, viewer_ctx, str(system.id), resolver=resolver)
    assert resp.status == "running"
    assert resp.object_id  # a job id

async def test_check_ssh_reachable_fresh_job_each_call(...) -> None:
    a = await check_ssh_reachable(pool, viewer_ctx, str(system.id), resolver=resolver)
    b = await check_ssh_reachable(pool, viewer_ctx, str(system.id), resolver=resolver)
    assert a.object_id != b.object_id

async def test_check_ssh_reachable_not_ready_is_readiness_failure(...) -> None:
    resp = await check_ssh_reachable(pool, viewer_ctx, str(not_ready.id), resolver=resolver)
    assert resp.error_category == ErrorCategory.READINESS_FAILURE.value

async def test_check_ssh_reachable_no_forward_is_ssh_not_provisioned(...) -> None:
    resp = await check_ssh_reachable(pool, viewer_ctx, str(system.id), resolver=none_endpoint_resolver)
    assert resp.data["reason"] == "ssh_not_provisioned"
```

Also assert a non-member ctx gets `_not_found`-shaped output. For the authz path, mirror **exactly** how the `ssh_info` tests handle a member-without-viewer: `require_role` **raises** `RoleDenied`/`AuthorizationError` rather than returning an envelope, so that case is `pytest.raises(...)`, not an `error_category` assertion — unlike the not-ready/no-forward cases above, which *do* return failure envelopes. Check the `ssh_info` test module for the precise exception type before writing this assertion.

- [ ] **Step 2: Run to verify failure.** Expected: FAIL (`check_ssh_reachable` missing).

- [ ] **Step 3: Implement `check_ssh_reachable`** in `ssh_access.py` (add imports `from uuid import uuid4`, `from kdive.jobs import queue`, `from kdive.jobs.context import authorizing as job_authorizing`, `from kdive.jobs.payloads import CheckSshReachablePayload`, `from kdive.domain.operations.jobs import JobKind`, `from kdive.mcp.responses import ToolResponse`):

```python
async def check_ssh_reachable(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
    *,
    resolver: ProviderResolver,
) -> ToolResponse:
    """Enqueue a runtime SSH-reachability probe for a ready System (read-only, VIEWER)."""
    uid = _as_uuid(system_id)
    if uid is None:
        return _invalid_uuid_error("system_id", system_id)
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _not_found(system_id)
            require_role(ctx, system.project, Role.VIEWER)
            if system.state is not SystemState.READY:
                return ToolResponse.failure(
                    system_id, ErrorCategory.READINESS_FAILURE, detail=_NOT_READY_DETAIL
                )
            try:
                binding = await resolver.binding_for_system(conn, uid)
                recorded = binding.runtime.connector.recorded_ssh_endpoint(
                    SystemHandle(system.domain_name or str(system.id))
                )
            except CategorizedError as exc:
                return ToolResponse.failure_from_error(system_id, exc)
            if recorded is None:
                return ToolResponse.failure(
                    system_id,
                    ErrorCategory.CONFIGURATION_ERROR,
                    detail=_UNPROVISIONED_DETAIL,
                    data={"reason": "ssh_not_provisioned"},
                )
            # A liveness probe is a fresh measurement each call: a nonce dedup_key mints a distinct
            # job so a re-issue never returns a prior (succeeded, permanent-UNIQUE) job's stale
            # verdict (ADR-0298). authorize_ssh_key keys on the key fingerprint for the opposite,
            # idempotent, reason.
            job = await queue.enqueue(
                conn,
                JobKind.CHECK_SSH_REACHABLE,
                CheckSshReachablePayload(system_id=system_id),
                job_authorizing(ctx, system.project),
                f"{system_id}:check_ssh_reachable:{uuid4().hex}",
            )
    return ToolResponse.from_job(job)
```

- [ ] **Step 4: Add `systems.check_ssh_reachable` to `ssh_info`'s next actions** in `ssh_access.py`:

```python
    actions = visible_next_actions(
        ["systems.check_ssh_reachable", "systems.authorize_ssh_key", "systems.get"],
        ctx,
        system.project,
    )
```

- [ ] **Step 5: Register the wrapper** in `registrar.py`. Import `check_ssh_reachable as _check_ssh_reachable` from `ssh_access`, add `_register_systems_check_ssh_reachable(app, pool, resolver)` to the registration list beside `_register_systems_ssh_info`, and define it (read-only annotation, **no ADR ref in the docstring/Field**):

```python
def _register_systems_check_ssh_reachable(
    app: FastMCP, pool: AsyncConnectionPool, resolver: ProviderResolver
) -> None:
    @app.tool(
        name="systems.check_ssh_reachable",
        annotations=_docmeta.read_only(),
        meta=_docmeta.maturity_meta("implemented"),
    )
    async def systems_check_ssh_reachable(
        system_id: Annotated[
            str, Field(description="The ready System whose guest sshd reachability to probe.")
        ],
    ) -> ToolResponse:
        """Probe whether a ready System's guest sshd is answering right now.

        Enqueues a worker job and returns a job handle; poll `jobs.wait` until it is `succeeded`,
        then read the verdict from `refs.result` — a compact JSON object
        `{"reachable": bool, "checked_at", "endpoint": {host, port}, "detail"}`. `reachable=false`
        is a normal answer (a successful measurement), not an error. Each call is a fresh
        point-in-time measurement (a new job), so re-poll rather than reuse an old result. The
        probe tolerates the brief window after `ready` before sshd binds, so a single `false`
        immediately after provisioning may become `true` on a repeat call. Available on any ready
        System whose provider exposes an SSH forward; reports `ssh_not_provisioned` otherwise.
        """
        return await _check_ssh_reachable(pool, current_context(), system_id, resolver=resolver)
```

- [ ] **Step 6: Name the tool in the toolset doc (same commit).** Registering the tool trips the #940 completeness guard until `docs/guide/toolsets/systems.md` names it, so this lands in Task 5's commit, not a later one. After the `systems.authorize_ssh_key` bullet (around line 27) add:

```markdown
- `systems.check_ssh_reachable` — probe whether a ready system's guest sshd is answering
  now (a worker job; poll `jobs.wait` and read `refs.result`).
```

- [ ] **Step 7: Run** `uv run python -m pytest tests/mcp/lifecycle/ -q -k "ssh or reachable" && just lint && just type && uv run python -m pytest tests/mcp/core/test_no_adr_leak.py tests/mcp/test_tool_index.py tests/mcp/resources/test_toolset_doc_completeness.py -q`. Expected: PASS. If `test_tool_index` snapshots the tool set, regenerate/extend it to include `systems.check_ssh_reachable`.

- [ ] **Step 8: Commit** — `feat(972): add systems.check_ssh_reachable tool` (tool + registrar + ssh_info next-action + toolset doc together, so no commit is red on the completeness guard).

---

### Task 6: Drop the `ssh_reachable` PlannedSignal

**Files:**
- Modify: `src/kdive/images/capability_signals.py:118-136`
- Test: `tests/images/test_capability_signals.py:47-56`

**Interfaces:** none exported change beyond removing one `PLANNED_SIGNALS` member.

- [ ] **Step 1: Update the test** `test_ssh_reachable_signal_repointed_off_resolved_956` → assert the signal is now absent from both sets (the #972 fork resolved to a runtime probe, not an image signal):

```python
def test_ssh_reachable_is_not_an_image_signal_after_972() -> None:
    """#972 resolved the ssh_reachable fork to a runtime probe (systems.check_ssh_reachable,

    ADR-0298), not a static image-capability signal — so it is neither planned nor registered here.
    """
    names = {s.name for s in REGISTERED_SIGNALS} | {p.name for p in PLANNED_SIGNALS}
    assert "ssh_reachable" not in names
```

- [ ] **Step 2: Run to verify failure.** `uv run python -m pytest tests/images/test_capability_signals.py -q`. Expected: FAIL (ssh_reachable still in `PLANNED_SIGNALS`).

- [ ] **Step 3: Remove** the `PlannedSignal("ssh_reachable", ...)` entry (lines ~124-130) from `PLANNED_SIGNALS` in `capability_signals.py`.

- [ ] **Step 4: Run.** `uv run python -m pytest tests/images/test_capability_signals.py -q && just type`. Expected: PASS.

- [ ] **Step 5: Commit** — `refs(972): drop ssh_reachable PlannedSignal (fork resolved to runtime probe)`.

---

> **Note:** The `docs/guide/toolsets/systems.md` update was folded into **Task 5, Step 6** so
> the tool registration and its documenting line land in one commit — `test_toolset_doc_completeness`
> (the #940 guard) is red for any commit where the tool is registered but undocumented.

---

## Final verification

- [ ] `just ci` (full gate) green.
- [ ] Re-read the diff for dead code, the wrapper docstring for any leaked `ADR-NNNN`, and that no `ErrorCategory` string was invented.

## Self-review notes

- **Spec coverage:** tool + VIEWER + pre-checks (T5) · worker job/handler + state re-check + endpoint None (T4) · bounded retry + deadline + banner classify + redaction + injectable clock (T3/T4) · fresh-nonce dedup (T5) · inline verdict → `refs.result` (T3/T4, surfaced by existing `from_job`) · migration 0057 + enum tie (T1) · payload (T2) · drop PlannedSignal (T6) · toolset doc + no-ADR-leak (T5/T7). All covered.
- **Type consistency:** `ReachResult(reachable, detail)`, `_real_probe(host, port, *, deadline_s)`, `serialize_reach_verdict(result, host, port, checked_at)`, `check_ssh_reachable_handler(conn, job, *, resolver, probe, clock)` are used identically across tasks.
- **Coupling note:** Tasks 1–5 share the JobKind→payload→handler→tool chain and touch overlapping files (`systems.py`, `ssh_access.py`); implement sequentially in one session (not parallel subagents on a shared tree).

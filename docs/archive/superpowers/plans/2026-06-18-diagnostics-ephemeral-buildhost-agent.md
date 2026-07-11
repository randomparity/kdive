# Ephemeral build-host guest-agent diagnostic — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an opt-in `ephemeral_libvirt_buildhost_agent` server-vantage diagnostic that provisions a throwaway builder per `ephemeral_libvirt` build host, probes guest-agent reachability (FAIL when the agent never connects, ERROR when the host is unreachable), and DB-`enabled`-gate the existing `local_kernel_src` check.

**Architecture:** A new aggregating `Check` (in `diagnostics/checks.py`) holds the three-state policy over an injected async probe; the production probe adapter (`diagnostics/buildhost_agent.py`) enumerates hosts via the pool and wraps the synchronous `EphemeralBuildVm.session(wait_network=False)` in `asyncio.to_thread` with a module-level per-host `SingleFlight`, a reaper-visible heartbeat marker (`buildhost_agent_probe_guests`), and coroutine-level cleanup. The reconciler's existing build-VM reaper learns one live-holder clause. The check assembles only under a new `with_buildhost_agent` opt-in (CLI `--with-buildhost-agent`, MCP param, distinct audit event), with generous service timeouts.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`; psycopg async pool; libvirt (gated `live_vm`); FastMCP.

## Global Constraints

- ADR: **0167**. Migration: **0041**. Use these exact numbers; do not pick "next free".
- **Test harness facts (verified against the tree — use these, the prose below sometimes wrote earlier guesses):**
  - DB fixtures are `migrated_url` (a URL string) and `pg_conn`, re-exported from `tests/db/conftest.py`. There is **no** `migrated_pool`/`a_pool` fixture. DB-backed tests build their own pool: `async with AsyncConnectionPool(migrated_url, open=False) as pool: await pool.open()`, wrapped in `asyncio.run(...)` (see `tests/diagnostics/test_worker_dispatch_db.py`).
  - Real test module paths: build-host check policy → **new** `tests/diagnostics/test_buildhost_agent_check.py`; `local_kernel_src` enabled-gate → `tests/diagnostics/test_local_kernel_src.py`; probe adapter → **new** `tests/diagnostics/test_buildhost_agent.py`; service assembly → `tests/diagnostics/test_service.py` **and** the exact-assembled-set assertion in `tests/diagnostics/test_default_factory.py`; `wait_network` → `tests/providers/remote_libvirt/lifecycle/test_build_vm.py`; reaper → `tests/reconciler/test_build_hosts.py`; tool → `tests/mcp/ops/test_diagnostics.py`; CLI → `tests/cli/test_doctor_verb.py`; marker repo → **new** `tests/db/test_buildhost_agent_probes.py`.
  - `CategorizedError.category` is the attribute (`domain/errors.py`). `db.build_hosts.get_by_id` + `WORKER_LOCAL_ID` exist.
- Cite the ADR in new-module docstrings; pick the most specific existing `ErrorCategory`/`failure_category` string — never invent.
- Three-state `CheckResult` rules (enforced in `__post_init__`): a `fail` **must** carry a `fix`; only a `fail` may carry a `fix`; a `pass` must **not** carry a `failure_category`.
- `diagnostics → providers` and `diagnostics → db` are the only legal import directions out of diagnostics; `checks.py` must stay free of `libvirt`/provider/DB imports (policy only, via injected probes).
- ≤100 lines/function, cyclomatic ≤8, ≤5 positional params, 100-char lines, absolute imports only, Google-style docstrings on public APIs.
- Guardrails before every commit: `just lint` (ruff check + format), `just type` (ty, whole tree), and the focused tests. Full suite (`just test`) before the first push. Doc-style: no "critical/crucial/essential/comprehensive/robust/elegant", use "Milestone" not "Sprint".
- Redact untrusted/guest/external output before persisting or returning it (`redacted_tail` / `redact_url_credentials`).

---

### Task 1: Migration — `buildhost_agent_probe_guests`

**Files:**
- Create: `src/kdive/db/schema/0041_buildhost_agent_probe_guests.sql`
- Test: `tests/db/test_migrate.py` (existing — verify discovery), `tests/db/test_schema.py` if present

**Interfaces:**
- Produces: table `buildhost_agent_probe_guests(id, build_host_id, run_id, heartbeat_at, ttl_deadline, released_at, created_at)`; partial-unique index `buildhost_agent_probe_guests_one_live_per_host` on `(build_host_id) WHERE released_at IS NULL`.

- [ ] **Step 1: Write the SQL migration**

```sql
-- 0041_buildhost_agent_probe_guests.sql — reaper-visible markers for doctor ephemeral
-- build-host guest-agent probe builders (ADR-0167, #544/#531). Additive (forward-only, ADR-0015).
--
-- The `ephemeral_libvirt_buildhost_agent` doctor check provisions a throwaway `kdive-build-<run_id>`
-- builder per ephemeral_libvirt host (ADR-0100) and execs a trivial command over its guest agent.
-- The builder is a real build-VM domain the reconciler's `reap_orphan_build_vms` sweep already owns
-- (it reaps a build VM whose owning BUILD job is gone) — but a doctor probe has no BUILD job, so
-- without a marker the sweep would reap the probe mid-check. Each probe registers a row here under
-- the builder's `run_id`, carrying an active-run heartbeat (`heartbeat_at`) and a hard TTL
-- (`ttl_deadline`). `reap_orphan_build_vms` treats a `kdive-build-<run_id>` domain whose run_id has a
-- fresh, unreleased probe heartbeat as live; a stale one is reaped, with `ttl_deadline` as backstop.
CREATE TABLE buildhost_agent_probe_guests (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    build_host_id uuid        NOT NULL REFERENCES build_hosts (id) ON DELETE CASCADE,
    run_id        uuid        NOT NULL UNIQUE,
    heartbeat_at  timestamptz NOT NULL DEFAULT now(),
    ttl_deadline  timestamptz NOT NULL,
    released_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- At most one live (not-yet-released) probe per build host: the DB-level single-flight fence.
CREATE UNIQUE INDEX buildhost_agent_probe_guests_one_live_per_host
    ON buildhost_agent_probe_guests (build_host_id)
    WHERE released_at IS NULL;
```

- [ ] **Step 2: Run migration-discovery + schema tests**

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS (the new file is auto-discovered; `discover_migrations` validates the `NNNN_*.sql` shape and unique version). If a test enumerates the highest version, update its expected count.

- [ ] **Step 3: Commit**

```bash
git add src/kdive/db/schema/0041_buildhost_agent_probe_guests.sql
git commit -m "feat(db): add buildhost_agent_probe_guests marker table (ADR-0167)"
```

---

### Task 2: Marker repository — `db/buildhost_agent_probes.py`

**Files:**
- Create: `src/kdive/db/buildhost_agent_probes.py`
- Test: `tests/db/test_buildhost_agent_probes.py`

**Interfaces:**
- Consumes: an `AsyncConnectionPool` (register/heartbeat/release) and an `AsyncConnection` (`is_probe_live`, so the reconciler reuses its sweep connection).
- Produces:
  - `DEFAULT_PROBE_TTL: timedelta` (reuse `egress_probe.DEFAULT_PROBE_TTL` value semantics; import it).
  - `class ProbeInFlightError(Exception)` — re-export `egress_probe.ProbeInFlightError` (do **not** define a second type; import and re-export so the check catches one type).
  - `async def register(pool, *, build_host_id: UUID, run_id: UUID, ttl: timedelta = DEFAULT_PROBE_TTL) -> UUID`
  - `async def heartbeat(pool, probe_id: UUID) -> None`
  - `async def release(pool, probe_id: UUID) -> None`
  - `async def is_probe_live(conn, run_id: UUID, *, stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/db/test_buildhost_agent_probes.py
from datetime import timedelta
from uuid import uuid4

import pytest

from kdive.db import buildhost_agent_probes as probes
from kdive.db.build_hosts import BuildHostKind

pytestmark = pytest.mark.asyncio


async def _seed_host(pool):
    host_id = uuid4()
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "INSERT INTO build_hosts (id, name, kind, workspace_root, max_concurrent, "
            "enabled, state, base_image_volume) VALUES (%s, %s, %s, %s, %s, true, 'ready', %s)",
            (host_id, f"eph-{host_id}", BuildHostKind.EPHEMERAL_LIBVIRT.value, "/build", 1, "base.qcow2"),
        )
    return host_id


async def test_register_then_is_probe_live_true(migrated_pool):
    host_id = await _seed_host(migrated_pool)
    run_id = uuid4()
    await probes.register(migrated_pool, build_host_id=host_id, run_id=run_id)
    async with migrated_pool.connection() as conn:
        assert await probes.is_probe_live(conn, run_id) is True


async def test_second_live_probe_same_host_raises_inflight(migrated_pool):
    host_id = await _seed_host(migrated_pool)
    await probes.register(migrated_pool, build_host_id=host_id, run_id=uuid4())
    with pytest.raises(probes.ProbeInFlightError):
        await probes.register(migrated_pool, build_host_id=host_id, run_id=uuid4())


async def test_release_frees_the_slot_and_is_probe_live_false(migrated_pool):
    host_id = await _seed_host(migrated_pool)
    run_id = uuid4()
    probe_id = await probes.register(migrated_pool, build_host_id=host_id, run_id=run_id)
    await probes.release(migrated_pool, probe_id)
    async with migrated_pool.connection() as conn:
        assert await probes.is_probe_live(conn, run_id) is False
    # slot freed: a new probe registers without ProbeInFlightError
    await probes.register(migrated_pool, build_host_id=host_id, run_id=uuid4())


async def test_stale_heartbeat_is_not_live(migrated_pool):
    host_id = await _seed_host(migrated_pool)
    run_id = uuid4()
    await probes.register(migrated_pool, build_host_id=host_id, run_id=run_id)
    async with migrated_pool.connection() as conn:
        assert await probes.is_probe_live(conn, run_id, stale_after=timedelta(seconds=0)) is False
```

(Reuse the existing migrated-pool fixture; check `tests/conftest.py` for its exact name — likely `migrated_pool` or `db_pool`. Match the repo's convention.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/db/test_buildhost_agent_probes.py -q`
Expected: FAIL (module `kdive.db.buildhost_agent_probes` not found).

- [ ] **Step 3: Implement the repository**

```python
"""Async repository for buildhost_agent_probe_guests reaper markers (ADR-0167).

The `ephemeral_libvirt_buildhost_agent` doctor check (ADR-0167) provisions a throwaway
`kdive-build-<run_id>` builder per ephemeral_libvirt host. Because that builder is a real build-VM
domain the reconciler's `reap_orphan_build_vms` sweep already owns — and a doctor probe has no BUILD
job to prove liveness — the probe registers a marker here under the builder's `run_id`, carrying an
active-run `heartbeat_at` and a hard `ttl_deadline`. `is_probe_live` is the predicate that sweep
consults: a build VM whose run_id has a fresh, unreleased probe heartbeat is live and is not reaped.
The partial unique index on `build_host_id` (live rows only) is the cross-process single-flight fence.

All time predicates evaluate `now()` in Postgres, never a Python clock (the `provider_reaping`
convention), so the reconciler and the probe agree on staleness regardless of clock skew.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from kdive.diagnostics.egress_probe import (
    DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
    DEFAULT_PROBE_TTL,
    ProbeInFlightError,
)

__all__ = [
    "DEFAULT_PROBE_HEARTBEAT_STALE_AFTER",
    "DEFAULT_PROBE_TTL",
    "ProbeInFlightError",
    "heartbeat",
    "is_probe_live",
    "register",
    "release",
]


async def register(
    pool: AsyncConnectionPool,
    *,
    build_host_id: UUID,
    run_id: UUID,
    ttl: timedelta = DEFAULT_PROBE_TTL,
) -> UUID:
    """Insert a live marker row for the probe builder; return its id.

    Raises:
        ProbeInFlightError: a live probe row already exists for ``build_host_id`` (the partial
            unique index fired — the cross-process single-flight fence).
    """
    try:
        async with (
            pool.connection() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            await cur.execute(
                "INSERT INTO buildhost_agent_probe_guests (build_host_id, run_id, ttl_deadline) "
                "VALUES (%s, %s, now() + %s) RETURNING id",
                (build_host_id, run_id, ttl),
            )
            row = await cur.fetchone()
    except UniqueViolation as exc:
        raise ProbeInFlightError(str(build_host_id)) from exc
    if row is None:  # invariant: INSERT ... RETURNING always yields one row
        raise RuntimeError("INSERT into buildhost_agent_probe_guests returned no row")
    return row["id"]


async def heartbeat(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    """Advance the active-run heartbeat so the reaper never mistakes a live probe for a leak."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE buildhost_agent_probe_guests SET heartbeat_at = now() WHERE id = %s", (probe_id,)
        )


async def release(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    """Stamp ``released_at`` so the host's single-flight slot frees for the next run."""
    async with pool.connection() as conn, conn.transaction():
        await conn.execute(
            "UPDATE buildhost_agent_probe_guests SET released_at = now() "
            "WHERE id = %s AND released_at IS NULL",
            (probe_id,),
        )


async def is_probe_live(
    conn: AsyncConnection,
    run_id: UUID,
    *,
    stale_after: timedelta = DEFAULT_PROBE_HEARTBEAT_STALE_AFTER,
) -> bool:
    """Whether a probe builder for ``run_id`` is live: fresh heartbeat, unreleased, before TTL.

    The staleness window and TTL are evaluated in Postgres (``now()``), matching the reaper's
    clock-in-DB convention so a live probe is never reaped on clock skew.
    """
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM buildhost_agent_probe_guests "
            "WHERE run_id = %s AND released_at IS NULL "
            "  AND heartbeat_at > now() - %s AND now() < ttl_deadline",
            (run_id, stale_after),
        )
        return await cur.fetchone() is not None
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/db/test_buildhost_agent_probes.py -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/buildhost_agent_probes.py tests/db/test_buildhost_agent_probes.py
git commit -m "feat(db): buildhost agent probe marker repo (ADR-0167)"
```

---

### Task 3: `wait_network` kwarg on `EphemeralBuildVm.session`

**Files:**
- Modify: `src/kdive/providers/remote_libvirt/lifecycle/build_vm.py:209-260` (the `session` method) and `:390-408` (`ephemeral_build_session`)
- Test: `tests/providers/remote_libvirt/lifecycle/test_build_vm.py` (existing test module for build_vm)

**Interfaces:**
- Produces: `EphemeralBuildVm.session(base_image_volume, *, run_id, source=None, wait_network=True)` — when `wait_network=False`, skip `_wait_for_network` (and the egress preflight is already skipped because `source=None`). `ephemeral_build_session(..., wait_network=True)` forwards it.

- [ ] **Step 1: Write the failing test** (orchestration-level, no libvirt — use the existing injected-fake-connection pattern in the test module)

```python
def test_session_skips_network_wait_when_disabled(fake_build_vm_env):
    # fake_build_vm_env: the existing harness that injects a fake _BuildConn + agent_command and
    # records calls. Mirror the existing happy-path test's setup.
    vm, recorder = fake_build_vm_env
    with vm.session("base.qcow2", run_id=SOME_UUID, wait_network=False) as transport:
        assert transport is not None
    assert recorder.network_probe_calls == 0  # _wait_for_network never ran
```

If the existing test module drives `session()` with a fake that records the network probe, assert it is not invoked. If no such recorder exists, assert via a sentinel: make the fake transport's network probe raise, and assert `wait_network=False` does not raise while `wait_network=True` does.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_build_vm.py -k network -q`
Expected: FAIL (`session() got an unexpected keyword argument 'wait_network'`).

- [ ] **Step 3: Implement — add the kwarg, gate the network wait**

In `session`:

```python
    @contextmanager
    def session(
        self,
        base_image_volume: str,
        *,
        run_id: UUID,
        source: GitSourceRef | None = None,
        wait_network: bool = True,
    ) -> Iterator[GuestExecBuildTransport]:
```

Update the docstring's Args with:

```
            wait_network: When ``True`` (the BUILD default), block until the guest has a default
                route before yielding (the clone needs network). The ``ephemeral_libvirt_buildhost_agent``
                diagnostic passes ``False`` (ADR-0167): it asserts only guest-agent reachability, so it
                must not wait for — or fail on — the network, and runs a trivial command that needs none.
```

Replace the body's readiness sequence:

```python
                transport = GuestExecBuildTransport(
                    domain=conn.lookupByName(domain_name),
                    agent_command=self._agent_command,
                    secret_registry=self._secret_registry,
                )
                if wait_network:
                    self._wait_for_network(transport, domain_name)
                if source is not None:
                    self._preflight_egress(transport, source)
                yield transport
```

In `ephemeral_build_session`, add `wait_network: bool = True` and forward it to `vm.session(...)`.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/providers/remote_libvirt/lifecycle/test_build_vm.py -q && just type`
Expected: PASS (existing tests still green — default `True` preserves BUILD behavior), ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/remote_libvirt/lifecycle/build_vm.py tests/providers/remote_libvirt/lifecycle/test_build_vm.py
git commit -m "feat(build-vm): add wait_network kwarg to EphemeralBuildVm.session (ADR-0167)"
```

---

### Task 4: Outcome enum, fix constant, check class, and `local_kernel_src` enabled-gate — `diagnostics/checks.py`

**Files:**
- Modify: `src/kdive/diagnostics/checks.py` (add after the existing build-host check region)
- Test: `tests/diagnostics/test_buildhost_agent_check.py` (existing)

**Interfaces:**
- Consumes: `Check`, `CheckResult`, `CheckStatus`, `Vantage`, `_CONFIGURATION_ERROR`, `_TRANSPORT_FAILURE` (existing in this module).
- Produces:
  - `BUILDHOST_AGENT_ID = "ephemeral_libvirt_buildhost_agent"`
  - `BUILDHOST_AGENT_FIX: str`
  - `class BuildHostAgentOutcome(StrEnum)`: `AGENT_READY="agent_ready"`, `AGENT_UNREACHABLE="agent_unreachable"`, `HOST_UNREACHABLE="host_unreachable"`.
  - `@dataclass(frozen=True, slots=True) class BuildHostProbeResult`: `host_name: str`, `outcome: BuildHostAgentOutcome`, `transport_error: bool = False` (True only when a `HOST_UNREACHABLE` was a transport drop, for the aggregate category rule).
  - `type BuildHostAgentProbe = Callable[[], Awaitable[list[BuildHostProbeResult]]]` (probe-time host enumeration + per-host probing live in the adapter; the check only applies policy).
  - `class EphemeralLibvirtBuildHostAgentCheck(Check)`: `__init__(self, *, probe: BuildHostAgentProbe)`; `id` → `BUILDHOST_AGENT_ID`; `vantage` → `Vantage.SERVER`.
  - `LocalKernelSrcCheck.__init__` gains `enabled_probe: Callable[[], Awaitable[bool]] = <always True>`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/diagnostics/test_buildhost_agent_check.py (append)
import pytest
from kdive.diagnostics.checks import (
    BUILDHOST_AGENT_FIX, BuildHostAgentOutcome, BuildHostProbeResult,
    CheckStatus, EphemeralLibvirtBuildHostAgentCheck, LocalKernelSrcCheck, WarmTreeSourceOutcome,
)

pytestmark = pytest.mark.asyncio


def _probe(results):
    async def probe():
        return list(results)
    return probe


async def test_buildhost_agent_all_ready_is_pass():
    check = EphemeralLibvirtBuildHostAgentCheck(
        probe=_probe([BuildHostProbeResult("a", BuildHostAgentOutcome.AGENT_READY)])
    )
    r = await check.run()
    assert r.status is CheckStatus.PASS and r.fix is None and r.failure_category is None


async def test_buildhost_agent_unreachable_is_fail_with_fix_and_names_host():
    check = EphemeralLibvirtBuildHostAgentCheck(
        probe=_probe([
            BuildHostProbeResult("good", BuildHostAgentOutcome.AGENT_READY),
            BuildHostProbeResult("broken", BuildHostAgentOutcome.AGENT_UNREACHABLE),
        ])
    )
    r = await check.run()
    assert r.status is CheckStatus.FAIL
    assert r.fix == BUILDHOST_AGENT_FIX
    assert "broken" in r.detail and r.failure_category == "configuration_error"


async def test_buildhost_agent_only_host_unreachable_is_error_no_fix():
    check = EphemeralLibvirtBuildHostAgentCheck(
        probe=_probe([BuildHostProbeResult("x", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=True)])
    )
    r = await check.run()
    assert r.status is CheckStatus.ERROR and r.fix is None
    assert r.failure_category == "transport_failure"


async def test_buildhost_agent_no_hosts_is_error_configuration():
    check = EphemeralLibvirtBuildHostAgentCheck(probe=_probe([]))
    r = await check.run()
    assert r.status is CheckStatus.ERROR and r.failure_category == "configuration_error"


async def test_buildhost_agent_mixed_unreachable_causes_is_configuration_error():
    check = EphemeralLibvirtBuildHostAgentCheck(
        probe=_probe([
            BuildHostProbeResult("t", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=True),
            BuildHostProbeResult("c", BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error=False),
        ])
    )
    r = await check.run()
    assert r.status is CheckStatus.ERROR and r.failure_category == "configuration_error"


async def _outcome(value):
    async def probe():
        return value
    return probe


async def _enabled(value):
    async def probe():
        return value
    return probe


async def test_local_kernel_src_disabled_host_is_na_pass():
    check = LocalKernelSrcCheck(
        probe=await _outcome(WarmTreeSourceOutcome.UNSET),
        enabled_probe=await _enabled(False),
    )
    r = await check.run()
    assert r.status is CheckStatus.PASS and r.failure_category is None
    assert "disabled" in r.detail.lower()


async def test_local_kernel_src_enabled_host_unset_still_fails():
    check = LocalKernelSrcCheck(
        probe=await _outcome(WarmTreeSourceOutcome.UNSET),
        enabled_probe=await _enabled(True),
    )
    r = await check.run()
    assert r.status is CheckStatus.FAIL
```

Note the `_outcome`/`_enabled` helpers above return the probe coroutine-factory; adapt to match the existing module's probe-construction helper if one exists.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_buildhost_agent_check.py -k "buildhost_agent or local_kernel_src_disabled" -q`
Expected: FAIL (names not defined; `LocalKernelSrcCheck` has no `enabled_probe`).

- [ ] **Step 3: Implement**

Add near the top constants:

```python
BUILDHOST_AGENT_ID = "ephemeral_libvirt_buildhost_agent"

# The remediation the ephemeral build-host agent check surfaces as its ``fix`` (ADR-0167). Owned in
# diagnostics (diagnostic-output policy), like LOCAL_KERNEL_SRC_FIX / BASE_VOLUME_NOT_STAGED_FIX.
BUILDHOST_AGENT_FIX = (
    "an ephemeral_libvirt build host's throwaway builder boots but its qemu-guest-agent never "
    "becomes usable; rebuild or repair the operator-staged base build image so its guest agent "
    "starts (docs/operating/build-source-staging.md), then re-run doctor --with-buildhost-agent"
)
```

Add the always-enabled default and extend `LocalKernelSrcCheck.__init__`:

```python
async def _always_enabled() -> bool:
    return True
```

In `LocalKernelSrcCheck`:

```python
    def __init__(
        self,
        *,
        probe: WarmTreeSourceProbe,
        enabled_probe: Callable[[], Awaitable[bool]] = _always_enabled,
    ) -> None:
        self._probe = probe
        self._enabled_probe = enabled_probe
```

And at the top of its `run`:

```python
    async def run(self) -> CheckResult:
        if not await self._enabled_probe():
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.PASS,
                detail="local build host is disabled; KDIVE_KERNEL_SRC is not required (n/a)",
            )
        outcome = await self._probe()
        ...  # existing body unchanged
```

Add the new enum, result, probe type, and check at the end of the module:

```python
class BuildHostAgentOutcome(StrEnum):
    """The per-host observable outcomes of the ephemeral build-host agent probe (ADR-0167).

    ``AGENT_READY`` — the builder booted, its guest agent connected, and a trivial command ran.
    ``AGENT_UNREACHABLE`` — the builder started but never reached a usable agent (the agent never
    connected, the trivial command returned non-zero, or the agent dropped mid-exec): a contract
    ``fail``. ``HOST_UNREACHABLE`` — the host/config could not be reached before the agent connected
    (TLS down, missing pool/base image, a probe already in flight): an ``error``, never a confident
    "agent broken".
    """

    AGENT_READY = "agent_ready"
    AGENT_UNREACHABLE = "agent_unreachable"
    HOST_UNREACHABLE = "host_unreachable"


@dataclass(frozen=True, slots=True)
class BuildHostProbeResult:
    """One probed host's outcome. ``transport_error`` marks a HOST_UNREACHABLE that was a transport
    drop (vs a config cause), for the deterministic aggregate failure_category rule."""

    host_name: str
    outcome: BuildHostAgentOutcome
    transport_error: bool = False


BuildHostAgentProbe = Callable[[], Awaitable[list[BuildHostProbeResult]]]


class EphemeralLibvirtBuildHostAgentCheck(Check):
    """Server-vantage: each ephemeral_libvirt build host's throwaway builder reaches its guest agent.

    Aggregates the per-host outcomes from an injected probe into one three-state verdict (the
    ``secret_ref`` precedent): any AGENT_UNREACHABLE → ``fail`` (a build routed there fails
    deterministically); else any HOST_UNREACHABLE or no hosts → ``error`` (indeterminate/absent
    target is never a confident fail and never a silent pass); else ``pass``. The aggregate ``error``
    failure_category is ``transport_failure`` only when every error cause was a transport drop, else
    ``configuration_error`` — a fixed rule, so the category is stable for triage.
    """

    def __init__(self, *, probe: BuildHostAgentProbe) -> None:
        self._probe = probe

    @property
    def id(self) -> str:
        return BUILDHOST_AGENT_ID

    @property
    def vantage(self) -> Vantage:
        return Vantage.SERVER

    async def run(self) -> CheckResult:
        results = await self._probe()
        failed = [r.host_name for r in results if r.outcome is BuildHostAgentOutcome.AGENT_UNREACHABLE]
        if failed:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.FAIL,
                detail="ephemeral_libvirt build host(s) reachable but their guest agent never "
                f"became usable: {', '.join(sorted(failed))}",
                fix=BUILDHOST_AGENT_FIX,
                failure_category=_CONFIGURATION_ERROR,
            )
        unreachable = [r for r in results if r.outcome is BuildHostAgentOutcome.HOST_UNREACHABLE]
        if unreachable or not results:
            return CheckResult(
                check_id=self.id,
                status=CheckStatus.ERROR,
                detail=self._error_detail(unreachable, results),
                failure_category=self._error_category(unreachable),
            )
        return CheckResult(
            check_id=self.id,
            status=CheckStatus.PASS,
            detail=f"all {len(results)} ephemeral_libvirt build host(s) reached their guest agent",
        )

    @staticmethod
    def _error_detail(unreachable: list[BuildHostProbeResult], results: list[BuildHostProbeResult]) -> str:
        if not results:
            return "no ephemeral_libvirt build host is registered; nothing to probe"
        names = ", ".join(sorted(r.host_name for r in unreachable))
        return f"ephemeral_libvirt build host(s) could not be reached: {names}"

    @staticmethod
    def _error_category(unreachable: list[BuildHostProbeResult]) -> str:
        if unreachable and all(r.transport_error for r in unreachable):
            return _TRANSPORT_FAILURE
        return _CONFIGURATION_ERROR
```

(Ensure `Awaitable` and `Callable` are imported in `checks.py` — they already are. `dataclass`/`StrEnum` are imported.)

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/diagnostics/test_buildhost_agent_check.py -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/diagnostics/checks.py tests/diagnostics/test_buildhost_agent_check.py
git commit -m "feat(diagnostics): build-host agent check + local_kernel_src enabled-gate (ADR-0167)"
```

---

### Task 5: Production probe adapter — `diagnostics/buildhost_agent.py`, and the `local_kernel_src` enabled probe

**Files:**
- Create: `src/kdive/diagnostics/buildhost_agent.py`
- Modify: `src/kdive/diagnostics/kernel_src.py` (add `local_host_enabled_probe(pool)`)
- Test: `tests/diagnostics/test_buildhost_agent.py`, extend `tests/diagnostics/test_local_kernel_src.py`

**Interfaces:**
- Consumes: `AsyncConnectionPool`; `db.build_hosts.list_all_hosts` + `BuildHostKind`; `db.buildhost_agent_probes`; `egress_probe.SingleFlight`; `providers.remote_libvirt.lifecycle.build_vm.EphemeralBuildVm`; `security.secrets.secret_registry.SecretRegistry`; `checks.BuildHostAgentOutcome`/`BuildHostProbeResult`/`BuildHostAgentProbe`.
- Produces:
  - `_SINGLE_FLIGHT: SingleFlight` — **module-level** singleton (process-scope, per the spec).
  - `def buildhost_agent_probe(pool, *, secret_registry=None, session_factory=ephemeral_build_session) -> BuildHostAgentProbe` — returns the async no-arg probe the check calls.
  - In `kernel_src.py`: `def local_host_enabled_probe(pool) -> Callable[[], Awaitable[bool]]` — reads the seeded `worker-local` host's `enabled` flag at probe time; fail-open to `True` on DB error or missing row.

- [ ] **Step 1: Write the failing tests** (adapter orchestration with injected fakes — no libvirt, no DB for the classification tests; one DB-backed test for enumeration)

```python
# tests/diagnostics/test_buildhost_agent.py
import pytest
from kdive.diagnostics.checks import BuildHostAgentOutcome
from kdive.diagnostics import buildhost_agent as adapter
from kdive.domain.errors import CategorizedError, ErrorCategory

pytestmark = pytest.mark.asyncio


class _FakeSession:
    """Stands in for ephemeral_build_session: a context manager factory recording calls."""
    def __init__(self, *, raise_category=None, raise_after_agent=False, rc=0):
        self.raise_category = raise_category
        self.raise_after_agent = raise_after_agent
        self.rc = rc

    def __call__(self, base_image_volume, secret_registry, *, run_id, source=None, wait_network=True):
        outer = self
        class _CM:
            def __enter__(self_inner):
                if outer.raise_category is not None:
                    raise CategorizedError("boom", category=outer.raise_category)
                return _FakeTransport(outer)
            def __exit__(self_inner, *a):
                return False
        return _CM()


class _FakeTransport:
    def __init__(self, cfg):
        self._cfg = cfg
    def run(self, argv, *, cwd, timeout_s):
        from kdive.providers.ports.build_transport import CommandResult
        if self._cfg.raise_after_agent:
            raise CategorizedError("agent dropped", category=ErrorCategory.TRANSPORT_FAILURE)
        return CommandResult(returncode=self._cfg.rc, stdout="", stderr="")
```

Then per-outcome tests calling `adapter._probe_one_host(host, pool, secret_registry, session_factory, single_flight)` (factor a testable per-host helper) and asserting:
- session yields, rc 0 → `AGENT_READY`
- `raise_category=PROVISIONING_FAILURE` → `AGENT_UNREACHABLE`
- `raise_after_agent=True` (TRANSPORT_FAILURE after agent) → `AGENT_UNREACHABLE`
- `rc=1` → `AGENT_UNREACHABLE`
- `raise_category=CONFIGURATION_ERROR` → `HOST_UNREACHABLE`, transport_error False
- `raise_category=TRANSPORT_FAILURE` (raised from the CM `__enter__`, i.e. before agent) → `HOST_UNREACHABLE`, transport_error True
- host with `base_image_volume=None` → `HOST_UNREACHABLE` without calling the session
- `ProbeInFlightError` from register → `HOST_UNREACHABLE`

Important: the adapter must distinguish "before vs after agent". Because `session()` couples provision + wait_for_agent, the adapter cannot see "agent connected" separately from the yielded transport. **Resolution:** a session that *yields a transport* means the agent connected (wait_for_agent returned inside session). So: an exception escaping the `with` block **before** the body runs (the CM `__enter__` raised) is "before agent" → `HOST_UNREACHABLE` unless its category is `PROVISIONING_FAILURE` (agent-never-connected), which is `AGENT_UNREACHABLE`. A failure **inside** the body (transport.run raised, or rc != 0) is "after agent" → `AGENT_UNREACHABLE`. Encode exactly this mapping.

Mapping table the adapter implements (per host, blocking part):

| Where / what | Outcome | transport_error |
|---|---|---|
| `base_image_volume is None` (no session) | HOST_UNREACHABLE | False |
| `ProbeInFlightError` at register | HOST_UNREACHABLE | False |
| `CategorizedError(PROVISIONING_FAILURE)` from the `with` (agent never connected) | AGENT_UNREACHABLE | — |
| `CategorizedError(CONFIGURATION_ERROR)` from the `with` | HOST_UNREACHABLE | False |
| `CategorizedError(TRANSPORT_FAILURE/INFRASTRUCTURE_FAILURE)` from the `with` | HOST_UNREACHABLE | True |
| body: `transport.run` raises CategorizedError | AGENT_UNREACHABLE | — |
| body: `rc != 0` | AGENT_UNREACHABLE | — |
| body: `rc == 0` | AGENT_READY | — |

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_buildhost_agent.py -q`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the adapter**

```python
"""Production probe adapter for the ephemeral build-host guest-agent check (ADR-0167).

The build-host boundary for :class:`~kdive.diagnostics.checks.EphemeralLibvirtBuildHostAgentCheck`:
the only place that imports :class:`EphemeralBuildVm` (``diagnostics → providers``, the legal
direction). It enumerates the ``ephemeral_libvirt`` + ``enabled`` build hosts at probe time, and for
each provisions a throwaway builder via ``EphemeralBuildVm.session(wait_network=False)``, waits for
its guest agent, execs one trivial command, and tears it down — all under a reaper-visible heartbeat
marker (``db.buildhost_agent_probes``) and a **module-level** per-host :class:`SingleFlight` so
concurrent doctor runs in one process spin exactly one builder per host.

Because ``session()`` is a synchronous (blocking libvirt + ``time.sleep``) contextmanager, the
blocking provision/exec/teardown runs in :func:`asyncio.to_thread` while an async heartbeat task
beats; the heartbeat-cancel and marker-release live in the probe coroutine's ``finally`` so a
``run_check`` timeout that cancels the coroutine still stops the heartbeat and frees the marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterator
from typing import Protocol
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from kdive.db import buildhost_agent_probes as probes
from kdive.db.build_hosts import BuildHost, BuildHostKind, list_all_hosts
from kdive.diagnostics.checks import (
    BuildHostAgentOutcome,
    BuildHostAgentProbe,
    BuildHostProbeResult,
)
from kdive.diagnostics.egress_probe import DEFAULT_PROBE_HEARTBEAT_INTERVAL, SingleFlight
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.providers.remote_libvirt.lifecycle.build_vm import ephemeral_build_session
from kdive.security.secrets.secret_registry import SecretRegistry

_log = logging.getLogger(__name__)
_TRIVIAL_ARGV = ["true"]
_TRIVIAL_CWD = "/"
_TRIVIAL_TIMEOUT_S = 30

# Module-level (process-scope) single-flight: default_service_factory runs per ops.diagnostics call,
# so a per-call coalescer would coalesce nothing (egress_probe.SingleFlight docstring).
_SINGLE_FLIGHT = SingleFlight()


class _SessionFactory(Protocol):
    def __call__(
        self,
        base_image_volume: str,
        secret_registry: SecretRegistry,
        *,
        run_id: UUID,
        source: object | None = ...,
        wait_network: bool = ...,
    ) -> Iterator: ...


def buildhost_agent_probe(
    pool: AsyncConnectionPool,
    *,
    secret_registry: SecretRegistry | None = None,
    session_factory: _SessionFactory = ephemeral_build_session,
) -> BuildHostAgentProbe:
    """Build the async probe the check calls: enumerate ephemeral_libvirt hosts, probe each."""
    registry = secret_registry or SecretRegistry()

    async def probe() -> list[BuildHostProbeResult]:
        async with pool.connection() as conn:
            hosts = [
                h
                for h in await list_all_hosts(conn)
                if h.kind is BuildHostKind.EPHEMERAL_LIBVIRT and h.enabled
            ]
        results: list[BuildHostProbeResult] = []
        for host in hosts:
            results.append(
                await _SINGLE_FLIGHT.run(
                    str(host.id),
                    lambda host=host: _probe_one_host(host, pool, registry, session_factory),
                )
            )
        return results

    return probe
```

Note: `SingleFlight.run` is typed `Callable[[], Coroutine[Any, Any, CheckResult]]` in `egress_probe`. It returns whatever the factory returns; `ty` may complain about the `CheckResult` annotation. **If ty rejects the reuse**, generalize `SingleFlight` to be generic (`SingleFlight[T]`) in `egress_probe.py` and update the egress call site — a small, contained change. Prefer that over a second copy. (Add this as a sub-step if ty flags it.)

```python
async def _probe_one_host(
    host: BuildHost,
    pool: AsyncConnectionPool,
    secret_registry: SecretRegistry,
    session_factory: _SessionFactory,
) -> BuildHostProbeResult:
    """Probe one host: register marker, beat heartbeat, run the blocking session, classify."""
    if not host.base_image_volume:
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    run_id = uuid4()
    try:
        probe_id = await probes.register(pool, build_host_id=host.id, run_id=run_id)
    except probes.ProbeInFlightError:
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    except Exception:  # noqa: BLE001 - marker backend down → indeterminate, never a fail
        _log.error("buildhost agent probe marker register failed for host=%s", host.name, exc_info=True)
        return BuildHostProbeResult(host.name, BuildHostAgentOutcome.HOST_UNREACHABLE)
    beat = asyncio.create_task(_beat_until_cancelled(pool, probe_id))
    try:
        outcome, transport_error = await asyncio.to_thread(
            _blocking_probe, host, run_id, secret_registry, session_factory
        )
        return BuildHostProbeResult(host.name, outcome, transport_error)
    finally:
        await _cancel(beat)
        await _release(pool, probe_id, host.name)


def _blocking_probe(
    host: BuildHost,
    run_id: UUID,
    secret_registry: SecretRegistry,
    session_factory: _SessionFactory,
) -> tuple[BuildHostAgentOutcome, bool]:
    """The synchronous provision → wait_for_agent → trivial exec → teardown, classified.

    A CategorizedError escaping the ``with`` is "before the agent connected": PROVISIONING_FAILURE
    is agent-never-connected (AGENT_UNREACHABLE); anything else is HOST_UNREACHABLE (transport_error
    True for transport/infra). A failure inside the body (the transport yielded → the agent
    connected) — exec raised or rc != 0 — is AGENT_UNREACHABLE.
    """
    try:
        with session_factory(
            host.base_image_volume, secret_registry, run_id=run_id, source=None, wait_network=False
        ) as transport:
            try:
                result = transport.run(_TRIVIAL_ARGV, cwd=_TRIVIAL_CWD, timeout_s=_TRIVIAL_TIMEOUT_S)
            except CategorizedError:
                _log.warning("buildhost agent probe exec dropped on host=%s", host.name, exc_info=True)
                return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
            if result.returncode != 0:
                return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
            return BuildHostAgentOutcome.AGENT_READY, False
    except CategorizedError as exc:
        if exc.category is ErrorCategory.PROVISIONING_FAILURE:
            return BuildHostAgentOutcome.AGENT_UNREACHABLE, False
        transport_error = exc.category in (
            ErrorCategory.TRANSPORT_FAILURE,
            ErrorCategory.INFRASTRUCTURE_FAILURE,
        )
        _log.warning("buildhost agent probe host=%s unreachable: %s", host.name, exc.category, exc_info=True)
        return BuildHostAgentOutcome.HOST_UNREACHABLE, transport_error


async def _beat_until_cancelled(pool: AsyncConnectionPool, probe_id: UUID) -> None:
    while True:
        with contextlib.suppress(Exception):  # a heartbeat blip must not fail the verdict
            await probes.heartbeat(pool, probe_id)
        await asyncio.sleep(DEFAULT_PROBE_HEARTBEAT_INTERVAL.total_seconds())


async def _release(pool: AsyncConnectionPool, probe_id: UUID, host_name: str) -> None:
    try:
        await probes.release(pool, probe_id)
    except Exception:  # noqa: BLE001 - release is best-effort; TTL is the backstop
        _log.warning("buildhost agent probe marker release failed for host=%s", host_name, exc_info=True)


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
```

(Verify `ErrorCategory` has `PROVISIONING_FAILURE`, `TRANSPORT_FAILURE`, `INFRASTRUCTURE_FAILURE`, `CONFIGURATION_ERROR` — it does, per `build_vm.py`/`readiness.py` usage. Verify `CategorizedError.category` attribute name — confirm against `domain/errors.py`.)

In `kernel_src.py` add:

```python
from kdive.db.build_hosts import WORKER_LOCAL_ID, get_by_id

def local_host_enabled_probe(
    pool: AsyncConnectionPool,
) -> Callable[[], Awaitable[bool]]:
    """Build the deferred probe for whether the seeded worker-local build host is enabled (ADR-0167).

    Read at check time via the pool; fail **open to enabled** (return True) on a DB error or a
    missing row, so a transient blip never hides the latent local-lane failure.
    """

    async def probe() -> bool:
        try:
            async with pool.connection() as conn:
                host = await get_by_id(conn, WORKER_LOCAL_ID)
        except Exception:  # noqa: BLE001 - fail open to enabled; never hide the latent failure
            _log.warning("local_kernel_src enabled probe DB read failed; assuming enabled", exc_info=True)
            return True
        return host is None or host.enabled

    return probe
```

(Add the needed imports: `AsyncConnectionPool`, `Awaitable`, `Callable`, `logging` to `kernel_src.py`.)

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/diagnostics/test_buildhost_agent.py tests/diagnostics/test_local_kernel_src.py -q && just type`
Expected: PASS, ty clean. If ty rejects `SingleFlight.run` reuse, generalize `SingleFlight` to `SingleFlight[T]` (sub-step) and re-run.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/diagnostics/buildhost_agent.py src/kdive/diagnostics/kernel_src.py tests/diagnostics/test_buildhost_agent.py tests/diagnostics/test_local_kernel_src.py
git commit -m "feat(diagnostics): production build-host agent probe + enabled probe (ADR-0167)"
```

---

### Task 6: Service assembly — `with_buildhost_agent` + generous timeouts + wire enabled probe

**Files:**
- Modify: `src/kdive/diagnostics/service.py` (`_build_host_checks`, `default_service_factory`, timeout constants)
- Test: `tests/diagnostics/test_service.py` (existing) **and** `tests/diagnostics/test_default_factory.py` (existing — holds the "assembled set is exactly {secret_ref, local_kernel_src}" assertion ADR-0163 referenced; update it in THIS task so the cross-file regression is caught and committed where it is caused, not deferred to Task 10).

**Interfaces:**
- Consumes: Task 4/5 outputs.
- Produces: `default_service_factory(provider, *, with_egress=False, with_buildhost_agent=False, pool=None, provider_contributions=())`. When `with_buildhost_agent and pool is not None`: assemble `EphemeralLibvirtBuildHostAgentCheck(probe=buildhost_agent.buildhost_agent_probe(pool))` and build the service with generous timeouts. When `with_buildhost_agent and pool is None`: raise `CategorizedError(CONFIGURATION_ERROR)` (the probe needs the pool — mirror the egress fail-fast spirit). `_build_host_checks(pool)` wires the `local_kernel_src` enabled probe when `pool is not None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/diagnostics/test_service.py (append / adjust)
async def test_factory_assembles_buildhost_agent_check_when_opted_in(a_pool):
    svc = default_service_factory(None, with_buildhost_agent=True, pool=a_pool)
    ids = {c.id for c in svc._checks}  # or a public accessor if one exists
    assert "ephemeral_libvirt_buildhost_agent" in ids

def test_factory_buildhost_agent_without_pool_raises():
    with pytest.raises(CategorizedError):
        default_service_factory(None, with_buildhost_agent=True, pool=None)

async def test_buildhost_agent_service_uses_generous_per_check_timeout(a_pool):
    svc = default_service_factory(None, with_buildhost_agent=True, pool=a_pool)
    assert svc._timeout >= 180.0  # >= the wait_for_agent bound

def test_default_factory_no_flag_keeps_tight_timeouts():
    svc = default_service_factory(None, pool=None)
    assert svc._timeout == 10.0 and svc._overall_timeout == 30.0
```

Adjust the existing "assembled set is exactly {secret_ref, local_kernel_src}" test to confirm the build-host check is **absent** without the flag.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/diagnostics/test_service.py -q`
Expected: FAIL (`with_buildhost_agent` unknown; timeout assertions fail).

- [ ] **Step 3: Implement**

Add constants near the existing timeout constants:

```python
# The builder's wait_for_agent bound is 180s; the probe self-bounds per host, and the service
# per-check timeout is the backstop above it. Generous on purpose — see ADR-0167 (the per-check
# timeout is service-global, so the opt-in mutating probe loosens the bound for co-assembled checks
# in that run; accepted for an explicit, rarely-run operator action).
_BUILDHOST_AGENT_PER_CHECK_TIMEOUT = 600.0
```

Extend `_build_host_checks`:

```python
def _build_host_checks(pool: AsyncConnectionPool | None) -> list[Check]:
    """Assemble the always-on server-vantage build-host preflight checks (ADR-0163, ADR-0167)."""
    enabled_probe = (
        kernel_src.local_host_enabled_probe(pool) if pool is not None else None
    )
    if enabled_probe is None:
        return [LocalKernelSrcCheck(probe=kernel_src.warm_tree_source_probe())]
    return [
        LocalKernelSrcCheck(
            probe=kernel_src.warm_tree_source_probe(), enabled_probe=enabled_probe
        )
    ]
```

Modify `default_service_factory` signature + body:

```python
def default_service_factory(
    provider: str | None,
    *,
    with_egress: bool = False,
    with_buildhost_agent: bool = False,
    pool: AsyncConnectionPool | None = None,
    provider_contributions: Sequence[DiagnosticProviderContribution] = (),
) -> DiagnosticsService:
    if with_egress:
        raise CategorizedError(... unchanged ...)
    checks: list[Check] = [_secret_ref_check(), *_build_host_checks(pool)]
    per_check_timeout = _DEFAULT_PER_CHECK_TIMEOUT
    overall_timeout: float | None = _DEFAULT_OVERALL_TIMEOUT
    if with_buildhost_agent:
        if pool is None:
            raise CategorizedError(
                "ephemeral_libvirt_buildhost_agent (--with-buildhost-agent) needs a database pool "
                "to enumerate build hosts; none is wired in this deployment (ADR-0167)",
                category=ErrorCategory.CONFIGURATION_ERROR,
            )
        # Function-local import: avoid a module import cycle (diagnostics.buildhost_agent imports
        # providers + db); only needed when the opt-in is set.
        from kdive.diagnostics.buildhost_agent import buildhost_agent_probe

        checks.append(
            EphemeralLibvirtBuildHostAgentCheck(probe=buildhost_agent_probe(pool))
        )
        per_check_timeout = _BUILDHOST_AGENT_PER_CHECK_TIMEOUT
        overall_timeout = None
    ... existing provider_contributions loop unchanged ...
    return DiagnosticsService(
        checks=checks,
        per_check_timeout=per_check_timeout,
        overall_timeout=overall_timeout,
        worker_mode=worker_mode,
    )
```

Add imports: `EphemeralLibvirtBuildHostAgentCheck` from `kdive.diagnostics.checks`.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/diagnostics/test_service.py tests/diagnostics/test_default_factory.py -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/diagnostics/service.py tests/diagnostics/test_service.py tests/diagnostics/test_default_factory.py
git commit -m "feat(diagnostics): assemble build-host agent check under with_buildhost_agent (ADR-0167)"
```

---

### Task 7: Reaper guard — honor a fresh probe heartbeat

**Files:**
- Modify: `src/kdive/reconciler/repairs/build_hosts.py` (`reap_orphan_build_vms`, line ~93-110)
- Test: `tests/reconciler/test_build_hosts.py` (existing reaper tests)

**Interfaces:**
- Consumes: `db.buildhost_agent_probes.is_probe_live`.
- Produces: `reap_orphan_build_vms` skips a build VM whose `run_id` has a live probe heartbeat.

- [ ] **Step 1: Write the failing test**

```python
async def test_reaper_skips_build_vm_with_live_probe_heartbeat(migrated_pool, fake_reaper):
    host_id = await _seed_eph_host(migrated_pool)
    run_id = uuid4()
    await probes.register(migrated_pool, build_host_id=host_id, run_id=run_id)
    fake_reaper.set_vms([BuildVm(domain_name=f"kdive-build-{run_id}", run_id=run_id)])
    async with migrated_pool.connection() as conn:
        reaped = await reap_orphan_build_vms(conn, fake_reaper)
    assert reaped == 0 and fake_reaper.deleted == []

async def test_reaper_reaps_build_vm_with_stale_probe_and_no_job(migrated_pool, fake_reaper):
    host_id = await _seed_eph_host(migrated_pool)
    run_id = uuid4()
    pid = await probes.register(migrated_pool, build_host_id=host_id, run_id=run_id)
    # force staleness by releasing (released → not live)
    await probes.release(migrated_pool, pid)
    fake_reaper.set_vms([BuildVm(domain_name=f"kdive-build-{run_id}", run_id=run_id)])
    async with migrated_pool.connection() as conn:
        reaped = await reap_orphan_build_vms(conn, fake_reaper)
    assert reaped == 1
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/reconciler/test_build_hosts.py -k probe -q`
Expected: FAIL (live-probe VM is reaped — the guard does not yet exist).

- [ ] **Step 3: Implement — extend the liveness guard**

```python
from kdive.db.buildhost_agent_probes import is_probe_live

async def reap_orphan_build_vms(conn: AsyncConnection, reaper: BuildVmReaper) -> int:
    reaped = 0
    for vm in await reaper.list_build_vms():
        if vm.run_id is None:
            continue
        if await _build_job_is_live(conn, vm.run_id) or await is_probe_live(conn, vm.run_id):
            continue
        await reaper.delete_build_vm(vm.domain_name)
        reaped += 1
    if reaped:
        _log.info("reconciler: reaped %d leaked build VM(s)", reaped)
    return reaped
```

Update the docstring to note the new live-holder clause (a fresh doctor-probe heartbeat, ADR-0167) and that staleness is Postgres-evaluated.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/reconciler/test_build_hosts.py -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/reconciler/repairs/build_hosts.py tests/reconciler/test_build_hosts.py
git commit -m "feat(reconciler): keep build VMs with a live doctor-probe heartbeat (ADR-0167)"
```

---

### Task 8: MCP tool — `with_buildhost_agent` param + distinct audit

**Files:**
- Modify: `src/kdive/mcp/tools/ops/diagnostics.py` (ServiceFactory protocol, `run_diagnostics`, `_audit_run`, `_audit_args`, the `@app.tool` wrapper, the `_BUILDHOST_TOOL` audit constant)
- Test: `tests/mcp/ops/test_diagnostics.py` (or the existing diagnostics-tool test module)

**Interfaces:**
- Produces: `ops.diagnostics(provider=None, with_egress=False, with_buildhost_agent=False)`; audits `ops.diagnostics.buildhost_agent` distinctly when `with_buildhost_agent` is set; `ServiceFactory.__call__(provider, *, with_egress=False, with_buildhost_agent=False)`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_buildhost_agent_audits_distinct_event(...):
    # call run_diagnostics with with_buildhost_agent=True and a fake operator ctx + fake factory
    # assert an audit row with tool == "ops.diagnostics.buildhost_agent" was recorded
    ...

async def test_buildhost_agent_denied_for_non_operator(...):
    # a platform_admin-only ctx is denied; the over-reach denial is audited; factory not called
    ...

async def test_buildhost_agent_threads_flag_to_factory(...):
    # the fake factory records with_buildhost_agent=True
    ...
```

Mirror the existing `with_egress` tests in this module exactly (they already cover the distinct-audit + denial pattern); add the `with_buildhost_agent` analogues.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/ops/test_diagnostics.py -k buildhost -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

Add constant: `_BUILDHOST_TOOL = "ops.diagnostics.buildhost_agent"`.

Extend `ServiceFactory.__call__` with `with_buildhost_agent: bool = False`.

Thread the param through `run_diagnostics(..., with_buildhost_agent: bool = False)` → `_diagnostics_report_from_service(service_factory, provider, with_egress, with_buildhost_agent)` → `service_factory(provider, with_egress=..., with_buildhost_agent=...)`.

Extend `_audit_args` to include `"with_buildhost_agent": with_buildhost_agent`. In `_audit_run`, after the `with_egress` distinct-audit block, add the analogous block recording `_BUILDHOST_TOOL` when `with_buildhost_agent`.

Add the tool parameter:

```python
        with_buildhost_agent: Annotated[
            bool,
            Field(
                description="Opt into the heavy ephemeral_libvirt_buildhost_agent probe: provisions "
                "a throwaway builder on each ephemeral_libvirt build host and checks its guest-agent "
                "reachability. Audited distinctly; off by default."
            ),
        ] = False,
```

and pass it to `run_diagnostics(...)`.

Also thread `with_buildhost_agent` through the audit-denial path's `args=_audit_args(provider, with_egress, with_buildhost_agent)`.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/mcp/ops/test_diagnostics.py -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/ops/diagnostics.py tests/mcp/ops/test_diagnostics.py
git commit -m "feat(mcp): ops.diagnostics with_buildhost_agent param + distinct audit (ADR-0167)"
```

---

### Task 9: CLI flag + app wiring

**Files:**
- Modify: `src/kdive/cli/commands/registry.py` (`_doctor_parser`), `src/kdive/cli/commands/doctor.py` (payload), `src/kdive/mcp/app.py` (`_service_factory` closure)
- Test: `tests/cli/test_doctor_verb.py` (existing), `tests/mcp/test_app.py` if it asserts factory wiring

**Interfaces:**
- Produces: `--with-buildhost-agent` flag → `payload["with_buildhost_agent"] = True` when set; `_service_factory(provider, *, with_egress=False, with_buildhost_agent=False)` forwards both to `default_service_factory(..., pool=pool, ...)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_doctor_payload_includes_buildhost_agent_when_flag_set():
    args = argparse.Namespace(provider=None, with_egress=False, with_buildhost_agent=True)
    assert _build_payload(args) == {"with_buildhost_agent": True}  # match the real builder name

def test_doctor_payload_omits_buildhost_agent_by_default():
    args = argparse.Namespace(provider=None, with_egress=False, with_buildhost_agent=False)
    assert "with_buildhost_agent" not in _build_payload(args)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/cli/test_doctor_verb.py -k buildhost -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `registry.py` `_doctor_parser`:

```python
    parser.add_argument(
        "--with-buildhost-agent", dest="with_buildhost_agent", action="store_true"
    )
```

In `doctor.py` payload builder, after the `with_egress` block:

```python
    if getattr(args, "with_buildhost_agent", False):
        payload["with_buildhost_agent"] = True
```

In `mcp/app.py` `_register_diagnostics_tools._service_factory`:

```python
    def _service_factory(
        provider: str | None, *, with_egress: bool = False, with_buildhost_agent: bool = False
    ) -> DiagnosticsService:
        return default_service_factory(
            provider,
            with_egress=with_egress,
            with_buildhost_agent=with_buildhost_agent,
            pool=pool,
            provider_contributions=diagnostic_provider_contributions(),
        )
```

- [ ] **Step 4: Run tests + full diagnostics/cli slice**

Run: `uv run python -m pytest tests/cli/test_doctor_verb.py tests/diagnostics tests/mcp -q && just type`
Expected: PASS, ty clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/cli/commands/registry.py src/kdive/cli/commands/doctor.py src/kdive/mcp/app.py tests/cli/test_doctor_verb.py
git commit -m "feat(cli): --with-buildhost-agent flag wired to ops.diagnostics (ADR-0167)"
```

---

### Task 10: Tool-doc snapshot + full-suite reconciliation

**Files:**
- Modify (regenerate): any generated tool-reference / doc snapshot that lists `ops.diagnostics` parameters or the migration count (e.g. `docs/guide/*tool*`, `tests/mcp/test_tool_docs.py` expectations).
- Test: whole suite.

**Interfaces:** none new — this task reconciles cross-cutting snapshots the prior tasks invalidate.

- [ ] **Step 1: Run the full suite to find what broke**

Run: `just test 2>&1 | tail -40`
Expected: identify any failures in `test_tool_docs`, migration-count, boundary/arch tests, or generated-doc checks outside the directories touched.

- [ ] **Step 2: Regenerate/refresh the affected snapshots**

If a tool-doc generator exists (search `just` recipes / `scripts/` for a docs-gen command), run it; otherwise update the expected snapshot to include the new `with_buildhost_agent` parameter. Review the diff (it should add only the new parameter / the new check id, nothing else).

- [ ] **Step 3: Run the full gate**

Run: `just lint && just type && just test`
Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test: refresh tool-doc snapshots for with_buildhost_agent (ADR-0167)"
```

---

## Self-Review

**Spec coverage:**
- New check + outcome enum + aggregation precedence + fix → Tasks 4 (policy) + 5 (probe).
- `wait_network=False` → Task 3.
- Opt-in flag (CLI + MCP) + distinct audit → Tasks 8, 9.
- Reaper markers + heartbeat + single-flight + TTL → Tasks 1, 2, 5, 7.
- Postgres-clock staleness → Task 2 (`is_probe_live`).
- Generous-timeout contract + cross-check coupling → Task 6.
- Cancellation/coroutine-finally cleanup → Task 5 (`_probe_one_host` finally).
- `enabled`-gate `local_kernel_src` (incl. fail-open) → Tasks 4 (policy) + 5 (probe) + 6 (wiring).
- Capacity stance (no lease) → inherent (probe never acquires a lease); documented in ADR/spec, no code needed.
- Acceptance criteria 1-10 → covered by the unit tests in Tasks 4-9; criteria 1/2 (live VM provision) are validated via the injected fake session (no live host in CI) — the live path runs only under the `live_vm`/operator runbook, stated as a PR limitation.

**Placeholder scan:** none — every code step has concrete code.

**Type consistency:** `BuildHostProbeResult`, `BuildHostAgentOutcome`, `BuildHostAgentProbe`, `buildhost_agent_probe`, `is_probe_live`, `local_host_enabled_probe`, `with_buildhost_agent` used consistently across Tasks 2/4/5/6/8/9.

**Open implementation risk flagged for the implementer:** if `ty` rejects reusing `egress_probe.SingleFlight` (annotated to return `CheckResult`) for a `BuildHostProbeResult`-returning factory, generalize `SingleFlight` to `SingleFlight[T]` in `egress_probe.py` and update its one egress call site — do this in Task 5 and re-run the egress tests.

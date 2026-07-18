# Discovery capability refresh (#1172) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** On discovery registration, refresh an existing resource row's `capabilities` jsonb in
place (preserving operator-owned keys) so a capability key added after row creation rolls out to
existing local-libvirt rows automatically.

**Architecture:** The insert-only `ensure_discovered_resource_registered` becomes
`register_or_refresh_discovered_resource`. Its existence probe becomes a
`SELECT capabilities … FOR UPDATE`; when the row exists it best-effort discovers the current
record and change-guarded-`UPDATE`s `capabilities`, discovery-authoritative except for
operator-owned keys (`concurrent_allocation_cap`), which are overlaid back from the stored row.
The absent branch (discover + insert) is unchanged. The `FOR UPDATE` serializes the
read-modify-write against `ops.set_host_capacity`'s row lock.

**Tech Stack:** Python 3.14, `uv`, `psycopg` (async), Postgres, `pytest` (disposable Postgres via
testcontainers).

**Spec:** `docs/design/2026-07-18-discovery-capability-refresh-1172.md`
**ADR:** `docs/adr/0384-refresh-discovered-resource-capabilities.md`

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. Absolute imports only (no `..`).
- `ty check` runs whole-tree (src + tests) with strict defaults.
- Guardrail suite: `just lint`, `just type`, `just test` (full PR gate: `just ci`).
- Doc-style: use plain factual prose; avoid "critical"/"robust"/"comprehensive"/"elegant".
- Isolate per-runtime/registration faults with `# noqa: BLE001` on the broad `except`, matching
  the existing pattern in `providers/core/resolver.py` and `providers/assembly/composition.py`.
- The change-guard (`merged == stored`) is a **write-avoidance optimization**, not the
  correctness gate — correctness comes from the `FOR UPDATE` row lock. A conservative
  false-positive (an extra harmless `UPDATE`, e.g. from JSON list-order differences) is
  acceptable; a false-negative that skips a needed write is not expected for the scalar/list/bool
  capability values in play.
- No schema change, no migration, no new tool, no new `ErrorCategory`.

---

### Task 1: Rename `ensure_discovered_resource_registered` → `register_or_refresh_discovered_resource`

Pure mechanical rename with **no behavior change**, so the existing tests stay green. Splitting
the rename from the behavior change keeps each independently reviewable.

**Files:**
- Modify: `src/kdive/providers/core/resource_registration.py` (the `async def` at line 60 and its
  docstring)
- Modify: `src/kdive/providers/assembly/composition.py` (import at line 24; **comment at line
  68**; call at line 72)
- Modify: `tests/services/test_resource_discovery.py` (import at line 16-19; call in `_ensure`
  helper at line 118)

**Interfaces:**
- Produces: `async def register_or_refresh_discovered_resource(pool, discovery, *, kind,
  resource_id, pool_name, cost_class) -> None` — same signature as the old name.

- [ ] **Step 1: Rename the function and update its docstring**

In `src/kdive/providers/core/resource_registration.py`, rename the function and reword the
docstring (behavior still insert-only at this step):

```python
async def register_or_refresh_discovered_resource(
    pool: AsyncConnectionPool,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    pool_name: str,
    cost_class: str,
) -> None:
    """Insert the target discovered Resource when absent (refresh added in a later step)."""
```

- [ ] **Step 2: Update the call site and stale comment in composition.py**

In `src/kdive/providers/assembly/composition.py`, change the import (line 24) and the call
(line 72):

```python
from kdive.providers.core.resource_registration import register_or_refresh_discovered_resource
```

```python
        await register_or_refresh_discovered_resource(
            pool,
            target.discovery,
            kind=registration.kind,
            resource_id=target.resource_id,
            pool_name=registration.pool_name,
            cost_class=registration.cost_class,
        )
```

Also update the comment at lines 68-70, which names the old symbol and predates the refresh —
reword it to the new name and note the refresh now reads discovery on the exists path too, while
the remote-connect safety stays structural (this registrar only reaches it for `creates=True`
local kinds):

```python
        # Known remote limitation: register_or_refresh_discovered_resource calls
        # discovery.list_resources() synchronously inside its async transaction (on both the
        # insert and the exists/refresh path, ADR-0384), and a remote TLS connect has no
        # pre-connect timeout. This registrar only reaches it for creates=True (local) kinds, so
        # no remote connect occurs here. Async offload is deferred.
```

- [ ] **Step 3: Update the test import and helper**

In `tests/services/test_resource_discovery.py`, change the import (lines 16-19) and the `_ensure`
helper call (line 118):

```python
from kdive.providers.core.resource_registration import (
    register_discovered_resource,
    register_or_refresh_discovered_resource,
)
```

```python
async def _ensure(pool: AsyncConnectionPool, discovery: _Discovery) -> None:
    await register_or_refresh_discovered_resource(
        pool,
        discovery,
        kind=ResourceKind.LOCAL_LIBVIRT,
        resource_id="qemu:///system",
        pool_name="local-libvirt",
        cost_class="local",
    )
```

- [ ] **Step 4: Run lint + the discovery tests to confirm the rename is clean and green**

Run: `just lint && uv run python -m pytest tests/services/test_resource_discovery.py -q`
Expected: PASS (behavior unchanged; all three existing tests still pass).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/core/resource_registration.py \
        src/kdive/providers/assembly/composition.py \
        tests/services/test_resource_discovery.py
git commit -m "refactor(discovery): rename to register_or_refresh_discovered_resource"
```

---

### Task 2: Add the `OPERATOR_OWNED_CAP_KEYS` constant

**Files:**
- Modify: `src/kdive/domain/catalog/resource_capabilities.py` (beside
  `CONCURRENT_ALLOCATION_CAP_KEY` at line 18)

**Interfaces:**
- Produces: `OPERATOR_OWNED_CAP_KEYS: frozenset[str]` — the capability keys a `platform_operator`
  can write directly onto a resource row; the refresh preserves the stored value of each.

- [ ] **Step 1: Add the constant**

In `src/kdive/domain/catalog/resource_capabilities.py`, immediately after the
`CONCURRENT_ALLOCATION_CAP_KEY` definition (line 18), add:

```python
# Capability keys a platform_operator writes directly onto a resource row (ops.set_host_capacity,
# ADR-0384). The discovery capability refresh preserves the stored value of each of these instead
# of overwriting it with the discovery record, so an audited operator change is not reverted on a
# redeploy/process-start. Grow this set when a new operator tool writes another capability key
# onto a discovery/runtime row.
OPERATOR_OWNED_CAP_KEYS = frozenset({CONCURRENT_ALLOCATION_CAP_KEY})
```

- [ ] **Step 2: Confirm it type-checks and lints**

Run: `just lint && uv run ty check src/kdive/domain/catalog/resource_capabilities.py`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add src/kdive/domain/catalog/resource_capabilities.py
git commit -m "feat(discovery): add OPERATOR_OWNED_CAP_KEYS for capability refresh"
```

---

### Task 3: Implement the capability refresh on the exists branch

The core change: replace the bare-`SELECT 1` existence probe with a `SELECT capabilities …
FOR UPDATE`, and refresh `capabilities` in place when the row exists. TDD: write the behavior
tests first (they fail against the still-insert-only code from Task 1), then implement.

**Files:**
- Modify: `src/kdive/providers/core/resource_registration.py` (add logger + `OPERATOR_OWNED_CAP_KEYS`
  import; rewrite the function body; add `_locked_capabilities`, `_refresh_capabilities`,
  `_merge_capabilities`; remove the now-unused `_resource_exists`)
- Test: `tests/services/test_resource_discovery.py`

**Interfaces:**
- Consumes: `OPERATOR_OWNED_CAP_KEYS` (Task 2); `register_or_refresh_discovered_resource` (Task 1).
- Produces (module-private helpers):
  - `async def _locked_capabilities(conn, kind, resource_id) -> dict[str, Any] | None`
  - `async def _refresh_capabilities(conn, discovery, *, kind, resource_id, stored) -> None`
  - `def _merge_capabilities(fresh: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]`

- [ ] **Step 1: Rework the existing behavior tests + add the new ones (all test edits happen here)**

Do every test-file edit in this one step so the module is fully in its final test state before
Step 2 runs. In `tests/services/test_resource_discovery.py`: (a) extend `_Discovery` to support
extra capability keys and a failing mode; (b) **delete**
`test_ensure_discovered_resource_registered_does_not_overwrite_existing_row` entirely — the
refresh inverts its insert-only intent; (c) replace
`test_ensure_discovered_resource_registered_bootstraps_one_row`'s body with the `discovery.calls
== 2` version shown below (the exists pass now re-reads); (d) add the `test_refresh_*` tests plus
the absent-branch AC7 test below. Keep `test_register_discovered_resource_is_idempotent` and the
`_pg`/`_ensure` helpers. Full code:

```python
class _Discovery:
    def __init__(self, cap: int = 2, extra: dict[str, object] | None = None) -> None:
        self.cap = cap
        self.extra = extra or {}
        self.calls = 0
        self.fail = False

    def list_resources(self) -> list[ResourceRecord]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("libvirt unreachable")
        return [
            ResourceRecord(
                resource_id="qemu:///system",
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={
                    "arch": "x86_64",
                    "vcpus": 8,
                    "memory_mb": 16384,
                    "transports": ["gdbstub"],
                    CONCURRENT_ALLOCATION_CAP_KEY: self.cap,
                    **self.extra,
                },
                status=ResourceStatus.AVAILABLE,
            )
        ]


def test_refresh_gains_missing_capability_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())  # inserts without pseries_fadump
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT id, managed_by FROM resources")
                before = await cur.fetchone()
            await _ensure(pool, _Discovery(extra={"pseries_fadump": True}))
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, managed_by, capabilities->>'pseries_fadump' FROM resources"
                )
                after = await cur.fetchone()
        assert before is not None and after is not None
        assert after[0] == before[0]  # id unchanged
        assert after[1] == before[1]  # managed_by unchanged
        assert after[2] == "true"  # gained the key

    asyncio.run(_run())


def test_refresh_updates_changed_discovery_value(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(extra={"guest_arches": ["x86_64"]}))
            await _ensure(pool, _Discovery(extra={"guest_arches": ["x86_64", "ppc64le"]}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT capabilities->'guest_arches' FROM resources")
            row = await cur.fetchone()
        assert row is not None and row[0] == ["x86_64", "ppc64le"]

    asyncio.run(_run())


def test_refresh_preserves_operator_cap_and_gains_new_key(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(cap=1))
            # Operator sets the cap directly on the discovery row (ops.set_host_capacity shape).
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET capabilities = "
                    "capabilities || jsonb_build_object('concurrent_allocation_cap', 5)"
                )
                await conn.commit()
            # A later deploy carries a net-new key AND the env-default cap (1).
            await _ensure(pool, _Discovery(cap=1, extra={"pseries_fadump": True}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT capabilities->>'concurrent_allocation_cap', "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "5"  # operator cap preserved, NOT reverted to env default 1
        assert row[1] == "true"  # net-new discovery key still gained

    asyncio.run(_run())


def test_refresh_preserves_status_pool_cost_and_cordoned(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())
            async with pool.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET status = 'degraded', pool = 'custom', "
                    "cost_class = 'premium', cordoned = true"
                )
                await conn.commit()
            await _ensure(pool, _Discovery(extra={"pseries_fadump": True}))
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT status, pool, cost_class, cordoned, "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[:4] == ("degraded", "custom", "premium", True)  # all preserved
        assert row[4] == "true"  # capabilities still refreshed

    asyncio.run(_run())


def test_refresh_read_failure_keeps_existing_capabilities(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery(cap=2, extra={"pseries_fadump": True}))
            failing = _Discovery()
            failing.fail = True
            await _ensure(pool, failing)  # must NOT raise
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT capabilities->>'pseries_fadump' FROM resources")
            row = await cur.fetchone()
        assert row is not None and row[0] == "true"  # existing capabilities intact

    asyncio.run(_run())


def test_refresh_change_guard_skips_write_when_unchanged(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, _Discovery())
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT xmin FROM resources")
                before = await cur.fetchone()
            await _ensure(pool, _Discovery())  # identical discovery record
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT xmin FROM resources")
                after = await cur.fetchone()
        assert before is not None and after is not None
        assert after[0] == before[0]  # no row write: xmin unchanged

    asyncio.run(_run())


def test_absent_branch_discovery_failure_raises(migrated_url: str) -> None:
    async def _run() -> None:
        failing = _Discovery()
        failing.fail = True
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            try:
                await _ensure(pool, failing)  # empty DB → absent branch must raise
            except RuntimeError:
                pass
            else:
                raise AssertionError("absent-branch discovery failure did not raise")
            async with pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("SELECT count(*) FROM resources")
                row = await cur.fetchone()
        assert row is not None and row[0] == 0  # nothing inserted

    asyncio.run(_run())
```

And replace the existing `bootstraps_one_row` test body with the re-read count (this is edit
(c) — the second `_ensure` now refreshes, so discovery is read twice):

```python
def test_ensure_discovered_resource_registered_bootstraps_one_row(migrated_url: str) -> None:
    async def _run() -> None:
        discovery = _Discovery(cap=2)
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=2) as pool:
            await _ensure(pool, discovery)
            await _ensure(pool, discovery)
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute("SELECT kind, host_uri FROM resources")
            rows = await cur.fetchall()
        assert rows == [("local-libvirt", "qemu:///system")]
        assert discovery.calls == 2  # insert reads once; the second pass refreshes (reads again)

    asyncio.run(_run())
```

- [ ] **Step 2: Run the tests to confirm they fail against the insert-only code**

Run: `uv run python -m pytest tests/services/test_resource_discovery.py -q`
Expected: FAIL — the `test_refresh_*` tests fail (insert-only code never updates an existing row)
and the reworked `bootstraps_one_row` `discovery.calls == 2` assertion fails against the
short-circuit. `test_absent_branch_discovery_failure_raises` should already PASS (the absent
branch already raises today).

- [ ] **Step 3: Implement the refresh**

Rewrite `src/kdive/providers/core/resource_registration.py`. Add near the top (after the existing
imports):

```python
import logging

from kdive.domain.catalog.resource_capabilities import OPERATOR_OWNED_CAP_KEYS

_log = logging.getLogger(__name__)
```

Replace the body of `register_or_refresh_discovered_resource` and the old `_resource_exists`
helper with:

```python
async def register_or_refresh_discovered_resource(
    pool: AsyncConnectionPool,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    pool_name: str,
    cost_class: str,
) -> None:
    """Insert the target discovered Resource when absent, else refresh its capabilities.

    On an existing row the refresh is discovery-authoritative except for operator-owned keys
    (``OPERATOR_OWNED_CAP_KEYS``), best-effort (a discovery-read failure keeps the stored
    capabilities), and change-guarded. The existence probe is ``SELECT … FOR UPDATE`` so the
    read-modify-write serializes against ``ops.set_host_capacity`` on the row lock (ADR-0384).
    """
    async with (
        pool.connection() as conn,
        conn.transaction(),
        advisory_xact_lock(conn, LockScope.RESOURCE, _resource_key(kind, resource_id)),
    ):
        stored = await _locked_capabilities(conn, kind, resource_id)
        if stored is not None:
            await _refresh_capabilities(
                conn, discovery, kind=kind, resource_id=resource_id, stored=stored
            )
            return
        records = await asyncio.to_thread(discovery.list_resources)
        record = _select_record(records, kind=kind, resource_id=resource_id)
        resource = _resource_from_record(record, pool=pool_name, cost_class=cost_class)
        async with conn.cursor(row_factory=dict_row) as cur:
            await _insert_resource(cur, resource)


async def _locked_capabilities(
    conn: AsyncConnection, kind: ResourceKind, resource_id: str
) -> dict[str, Any] | None:
    """Lock the row ``FOR UPDATE`` and return its capabilities, or ``None`` when it is absent."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT capabilities FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
            (kind.value, resource_id),
        )
        row = await cur.fetchone()
    return row["capabilities"] if row is not None else None


async def _refresh_capabilities(
    conn: AsyncConnection,
    discovery: DiscoverySource,
    *,
    kind: ResourceKind,
    resource_id: str,
    stored: dict[str, Any],
) -> None:
    """Best-effort refresh of an existing (already ``FOR UPDATE``-locked) row's capabilities.

    The ``try`` wraps only the discovery read + record selection; a failure logs and keeps the
    stored capabilities. The change-guarded ``UPDATE`` runs outside the catch, so a genuine DB
    write error propagates rather than poisoning the outer transaction.
    """
    try:
        records = await asyncio.to_thread(discovery.list_resources)
        record = _select_record(records, kind=kind, resource_id=resource_id)
    except Exception:  # noqa: BLE001 - best-effort refresh keeps the existing row on any read fault
        _log.warning(
            "capability refresh skipped for %s:%s; keeping existing capabilities",
            kind.value,
            resource_id,
            exc_info=True,
        )
        return
    merged = _merge_capabilities(record["capabilities"], stored)
    if merged == stored:
        return
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE resources SET capabilities = %s WHERE kind = %s AND host_uri = %s",
            (Jsonb(merged), kind.value, resource_id),
        )


def _merge_capabilities(fresh: dict[str, Any], stored: dict[str, Any]) -> dict[str, Any]:
    """Discovery-authoritative merge that overlays operator-owned keys from the stored row."""
    merged = dict(fresh)
    for key in OPERATOR_OWNED_CAP_KEYS:
        if key in stored:
            merged[key] = stored[key]
    return merged
```

- [ ] **Step 4: Run the full discovery test module**

Run: `uv run python -m pytest tests/services/test_resource_discovery.py -q`
Expected: PASS (all reworked + new tests green).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/providers/core/resource_registration.py \
        tests/services/test_resource_discovery.py
git commit -m "feat(discovery): refresh existing resource capabilities on registration"
```

---

### Task 4: Concurrency test — operator cap survives a concurrent refresh

Validate AC4: with the exists-branch read as `FOR UPDATE`, a concurrent `ops.set_host_capacity`
cap change is not lost. This is the behavioral proof that the row lock (not the advisory lock)
serializes the two writers.

**Files:**
- Test: `tests/services/test_resource_discovery.py`

**Interfaces:**
- Consumes: `register_or_refresh_discovered_resource` (Task 3), `_Discovery` (Task 3).

- [ ] **Step 1: Write the interleave test**

Add to `tests/services/test_resource_discovery.py`. It holds the row `FOR UPDATE` in one
connection (standing in for an in-flight `ops.set_host_capacity`), launches the refresh as a
task, confirms the refresh is blocked on the row lock, then commits the operator cap and asserts
the refresh preserved it while still gaining the net-new key:

```python
def test_concurrent_operator_cap_is_not_lost_by_refresh(migrated_url: str) -> None:
    async def _run() -> None:
        async with AsyncConnectionPool(migrated_url, min_size=2, max_size=4) as pool:
            await _ensure(pool, _Discovery(cap=1))
            async with _pg(migrated_url) as op_conn:
                await op_conn.execute("BEGIN")
                await op_conn.execute(
                    "SELECT id FROM resources WHERE kind = %s AND host_uri = %s FOR UPDATE",
                    ("local-libvirt", "qemu:///system"),
                )
                # Refresh carries a net-new key; it must block on the row lock op_conn holds.
                refresh = asyncio.create_task(
                    _ensure(pool, _Discovery(cap=1, extra={"pseries_fadump": True}))
                )
                await asyncio.sleep(0.3)
                assert not refresh.done()  # blocked on the FOR UPDATE row lock
                await op_conn.execute(
                    "UPDATE resources SET capabilities = "
                    "capabilities || jsonb_build_object('concurrent_allocation_cap', 5)"
                )
                await op_conn.execute("COMMIT")
                await asyncio.wait_for(refresh, timeout=5)
        async with _pg(migrated_url) as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT capabilities->>'concurrent_allocation_cap', "
                "capabilities->>'pseries_fadump' FROM resources"
            )
            row = await cur.fetchone()
        assert row is not None
        assert row[0] == "5"  # operator cap read under the lock and preserved
        assert row[1] == "true"  # refresh still rolled out the net-new key

    asyncio.run(_run())
```

- [ ] **Step 2: Run the concurrency test**

Run: `uv run python -m pytest tests/services/test_resource_discovery.py::test_concurrent_operator_cap_is_not_lost_by_refresh -q`
Expected: PASS. If it FAILS at `assert not refresh.done()` because the harness serializes
connections rather than blocking, fall back to the narrower coverage the spec permits: delete the
`assert not refresh.done()` line and the interleave, and instead assert the sequential
preservation already covered by `test_refresh_preserves_operator_cap_and_gains_new_key`, adding a
`# NOTE:` comment that the full two-transaction race is not exercised in this tier. Do not leave a
flaky timing assertion in.

- [ ] **Step 3: Commit**

```bash
git add tests/services/test_resource_discovery.py
git commit -m "test(discovery): operator cap survives a concurrent capability refresh"
```

---

### Task 5: Guardrails + creates=False regression check

**Files:**
- Verify only: `tests/providers/test_composition.py` (existing `creates=False` bind-only coverage)

- [ ] **Step 1: Confirm the `creates=False` bind-only no-op is still covered**

Run: `uv run python -m pytest tests/providers/test_composition.py -q`
Expected: PASS. The `creates=False` early return in `_discovery_registrar`
(`composition.py`) is unchanged by this work, so remote-libvirt/fault-inject never reach
`register_or_refresh_discovered_resource`. If no test asserts a `creates=False` registrar makes
no DB write / no `list_resources` call, add one mirroring the existing composition tests. If it is
already covered, record that and move on (no new test needed).

- [ ] **Step 2: Run the full guardrail suite**

Run: `just lint && just type && just test`
Expected: PASS. `just type` is whole-tree (src + tests); fix any typing gap in the new helpers or
tests before proceeding.

- [ ] **Step 3: Commit any guardrail fixes (only if Step 1/2 required changes)**

```bash
git add -- <explicit paths touched>
git commit -m "test(discovery): cover creates=False bind-only registrar no-op"
```

---

## Self-Review

**Spec coverage** (each acceptance criterion → task):
- AC1 (gains missing key, id/managed_by unchanged) → Task 3 `test_refresh_gains_missing_capability_key`.
- AC2 (changed discovery value updated) → Task 3 `test_refresh_updates_changed_discovery_value`.
- AC3 (operator cap survives + net-new key gained) → Task 3 `test_refresh_preserves_operator_cap_and_gains_new_key`.
- AC4 (FOR UPDATE serialization, concurrent cap not lost) → Task 4.
- AC5 (status/pool/cost_class/cordoned unchanged) → Task 3 `test_refresh_preserves_status_pool_cost_and_cordoned`.
- AC6 (creates=False bind-only no-op) → Task 5.
- AC7 (absent-path discovery failure still raises) → Task 3 `test_absent_branch_discovery_failure_raises`.
- AC8 (exists-path read failure swallowed) → Task 3 `test_refresh_read_failure_keeps_existing_capabilities`.
- AC9 (change-guard: no write when unchanged) → Task 3 `test_refresh_change_guard_skips_write_when_unchanged`.
- AC10 (guardrails green) → Task 5.

**Placeholder scan:** No `TODO`/`TBD`/"handle edge cases"/"similar to" — every code and test step
carries full code.

**Type consistency:** `register_or_refresh_discovered_resource` signature matches across Task 1
(rename) and Task 3 (body). Helpers `_locked_capabilities`/`_refresh_capabilities`/
`_merge_capabilities` are defined once (Task 3) and named consistently. `OPERATOR_OWNED_CAP_KEYS`
defined in Task 2, imported in Task 3. `_Discovery` extended in Task 3 and reused in Task 4.

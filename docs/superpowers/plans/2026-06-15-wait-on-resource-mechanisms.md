# Wait-on-resource mechanisms Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three gaps in token-efficient waiting on a resource (#430): a bounded `allocations.wait` long-poll, a per-request queue-position hint, and a `retryable` flag derived from `error_category` — backed by one new column so a failed queued allocation reports its cause.

**Architecture:** Each gap lands at a single insertion point in existing code. `allocations.wait` mirrors the proven `jobs.wait` poll loop. `queue_position` is one count query on the existing pending-queue index. `retryable` is derived once in the `ToolResponse` model validator from a static table. One additive nullable column `allocations.failure_category` lets the two queued-terminate transitions record `queue_timeout` vs `allocation_denied`, so the derived `retryable` is correct on the wait path.

**Tech Stack:** Python 3.13, FastMCP, psycopg (async) + Postgres, Pydantic v2, pytest. Guardrails via `just`. Specs: `docs/design/wait-on-resource-mechanisms.md`, `docs/adr/0118-wait-on-resource-mechanisms.md`.

---

## Conventions every task follows

- **Guardrails before every commit:** `just lint && just type && uv run python -m pytest <the task's tests> -q`. `just type` is whole-tree (src + tests); fix every warning. Never commit on red.
- **Test boundary:** drive handlers directly with an injected pool + `RequestContext` (no MCP transport), the project convention. DB tests use the `migrated_url` fixture (disposable Postgres via testcontainers); they skip when Docker is absent unless `KDIVE_REQUIRE_DOCKER=1`.
- **Envelope rule:** every tool returns a `ToolResponse`; a failure status carries an `error_category`. Never invent error strings — use `kdive.domain.errors.ErrorCategory`.
- **Commit style:** Conventional Commits, imperative subject ≤72 chars, end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Migration-number caveat:** `0033` is the next free number on `main` at authoring time. A cost-class branch (`fix/tool-feedback`) is in flight and may also take a number here; if `0033` collides at rebase, renumber this migration to the next free value and update the `test_migrate.py` version list accordingly.

## File structure (what each touched file is responsible for)

- **Create** `src/kdive/db/schema/0033_allocation_failure_category.sql` — the additive nullable column.
- **Modify** `src/kdive/domain/models.py` — add `Allocation.failure_category`.
- **Modify** `src/kdive/mcp/responses.py` — add the `retryable` field + classification table + derivation in the existing validator.
- **Modify** `src/kdive/services/allocation/promotion.py` — persist the cause at the two queued-terminate transitions.
- **Modify** `src/kdive/mcp/tools/lifecycle/allocations.py` — read the cause in the envelope, add the queue-position helper, surface it in `get`, add the `wait` handler + tool.
- **Modify** `tests/db/test_migrate.py` — add `"0033"` to the applied-version list.
- **Create** `tests/mcp/lifecycle/test_allocations_wait.py` — `allocations.wait` + position behavior tests.
- **Modify** `tests/domain/test_models.py`, `tests/mcp/core/test_responses.py` (or the existing responses test module), `tests/mcp/lifecycle/test_allocations_tools.py`, `tests/mcp/core/test_tool_docs.py` — unit/behavior coverage and the tool-docs mapping.
- **Regenerate** `docs/guide/reference/allocations.md` (+ index) via `just docs`.

---

### Task 1: Migration — add `allocations.failure_category`

**Files:**
- Create: `src/kdive/db/schema/0033_allocation_failure_category.sql`
- Modify: `tests/db/test_migrate.py` (the applied-version list, around line 95-128)

- [ ] **Step 1: Write the migration file**

```sql
-- 0033_allocation_failure_category.sql — record the terminal cause of a failed
-- allocation so a waiting agent (allocations.wait, #430 / ADR-0118) can tell a
-- queue_timeout (retryable) from a budget terminate (allocation_denied, terminal).
-- Additive, forward-only (ADR-0015). NULL for every existing failed row and for any
-- failed path that does not yet set it; the response envelope falls back to
-- infrastructure_failure when NULL.
ALTER TABLE allocations
    ADD COLUMN failure_category text;
```

- [ ] **Step 2: Add `"0033"` to the migration-list assertion**

In `tests/db/test_migrate.py`, the first-run assertion lists every applied version ending `…, "0031", "0032"`. Append the new version:

```python
        "0031",
        "0032",
        "0033",
    ]
```

- [ ] **Step 3: Run the migration test to verify it applies and is idempotent**

Run: `uv run python -m pytest tests/db/test_migrate.py -q`
Expected: PASS (first run applies through `0033`; the second run is `[]` — idempotent). If Docker is absent it skips; set `KDIVE_REQUIRE_DOCKER=1` to force it where Docker exists.

- [ ] **Step 4: Run guardrails**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/db/schema/0033_allocation_failure_category.sql tests/db/test_migrate.py
git commit -m "feat(db): add allocations.failure_category column (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: Model field — `Allocation.failure_category`

**Files:**
- Modify: `src/kdive/domain/models.py` (the `Allocation` model, lines 253-266; imports near the top)
- Test: `tests/domain/test_models.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/domain/test_models.py`:

```python
def test_allocation_failure_category_coerces_and_defaults() -> None:
    from uuid import uuid4
    from datetime import UTC, datetime

    from kdive.domain.errors import ErrorCategory
    from kdive.domain.models import Allocation
    from kdive.domain.state import AllocationState

    base = {
        "id": uuid4(),
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
        "principal": "p",
        "agent_session": "s",
        "project": "proj",
        "state": AllocationState.FAILED.value,
    }
    # Defaults to None when the column is absent/NULL.
    assert Allocation.model_validate(base).failure_category is None
    # A wire string coerces to the enum.
    got = Allocation.model_validate({**base, "failure_category": "queue_timeout"})
    assert got.failure_category is ErrorCategory.QUEUE_TIMEOUT
```

(If `Allocation.model_validate` needs more required attribution/columns than shown, copy the field set from an existing allocation construction in `tests/domain/test_models.py`; the two assertions are what matter.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python -m pytest tests/domain/test_models.py::test_allocation_failure_category_coerces_and_defaults -q`
Expected: FAIL — `Allocation` has no attribute/field `failure_category`.

- [ ] **Step 3: Add the field**

In `src/kdive/domain/models.py`, ensure the error taxonomy is imported (top of file, with the other `kdive.domain` imports):

```python
from kdive.domain.errors import ErrorCategory
```

Then add the field to `Allocation`, after `requested_resource_id`:

```python
    requested_resource_id: UUID | None = None
    failure_category: ErrorCategory | None = None
```

Update the `Allocation` docstring's `requested` paragraph with one sentence:

```
    ``failure_category`` records the terminal cause of a ``failed`` allocation (ADR-0118):
    ``allocation_denied`` for a budget terminate, ``queue_timeout`` for a reap; ``None`` for
    any other failed path (the response envelope then falls back to ``infrastructure_failure``).
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/domain/test_models.py::test_allocation_failure_category_coerces_and_defaults -q`
Expected: PASS.

- [ ] **Step 5: Run guardrails (the model is inserted/read widely)**

Run: `just lint && just type && uv run python -m pytest tests/domain tests/db/test_repositories.py -q`
Expected: clean. (`_insert_columns` derives from model fields, so a fresh INSERT now writes `failure_category` = NULL — the column exists from Task 1, so inserts still succeed.)

- [ ] **Step 6: Commit**

```bash
git add src/kdive/domain/models.py tests/domain/test_models.py
git commit -m "feat(domain): add Allocation.failure_category field (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: Derived `retryable` on the response envelope

**Files:**
- Modify: `src/kdive/mcp/responses.py` (add the field, the table, and the derivation in `_category_iff_failed` at lines 60-93)
- Test: `tests/mcp/core/test_responses.py` (create if absent; otherwise the existing responses test module)

- [ ] **Step 1: Write the failing tests**

Create/extend `tests/mcp/core/test_responses.py`:

```python
from kdive.domain.errors import ErrorCategory
from kdive.mcp.responses import _RETRYABLE_BY_CATEGORY, ToolResponse


def test_retryable_table_is_exhaustive_over_error_category() -> None:
    # Every category is classified; none stale. A new ErrorCategory must be a deliberate edit.
    assert set(_RETRYABLE_BY_CATEGORY) == set(ErrorCategory)


def test_retryable_is_none_on_success() -> None:
    resp = ToolResponse.success("id", "ok", data={"x": 1})
    assert resp.retryable is None


def test_retryable_derived_on_failure() -> None:
    transient = ToolResponse.failure("id", ErrorCategory.QUEUE_TIMEOUT)
    terminal = ToolResponse.failure("id", ErrorCategory.ALLOCATION_DENIED)
    assert transient.retryable is True
    assert terminal.retryable is False


def test_retryable_is_never_caller_set() -> None:
    # A caller-supplied value is overwritten by the derived one.
    forced = ToolResponse(
        object_id="id", status="error",
        error_category=ErrorCategory.CONFIGURATION_ERROR.value, retryable=True,
    )
    assert forced.retryable is False  # configuration_error is terminal


def test_every_category_has_an_explicit_expected_bool() -> None:
    # Pin each category's classification so a reclassification is a visible diff.
    expected = {
        ErrorCategory.INFRASTRUCTURE_FAILURE: True,
        ErrorCategory.PROVISIONING_FAILURE: True,
        ErrorCategory.BOOT_TIMEOUT: True,
        ErrorCategory.READINESS_FAILURE: True,
        ErrorCategory.TRANSPORT_FAILURE: True,
        ErrorCategory.TRANSPORT_CONFLICT: True,
        ErrorCategory.DEBUG_ATTACH_FAILURE: True,
        ErrorCategory.CONTROL_FAILURE: True,
        ErrorCategory.CAPACITY_EXHAUSTED: True,
        ErrorCategory.QUEUE_TIMEOUT: True,
        ErrorCategory.CONFIGURATION_ERROR: False,
        ErrorCategory.MISSING_DEPENDENCY: False,
        ErrorCategory.BUILD_FAILURE: False,
        ErrorCategory.INSTALL_FAILURE: False,
        ErrorCategory.STALE_HANDLE: False,
        ErrorCategory.LEASE_EXPIRED: False,
        ErrorCategory.NOT_IMPLEMENTED: False,
        ErrorCategory.NOT_FOUND: False,
        ErrorCategory.CONFLICT: False,
        ErrorCategory.AUTHORIZATION_DENIED: False,
        ErrorCategory.QUOTA_EXCEEDED: False,
        ErrorCategory.ALLOCATION_DENIED: False,
    }
    assert _RETRYABLE_BY_CATEGORY == expected
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/core/test_responses.py -q`
Expected: FAIL — `_RETRYABLE_BY_CATEGORY` and `ToolResponse.retryable` do not exist.

- [ ] **Step 3: Add the table, the field, and the derivation**

In `src/kdive/mcp/responses.py`, after the `_FAILURE_STATUSES` definition (line 34), add the table:

```python
# Retryability is a pure function of the failure category (ADR-0118): a bare
# re-invocation may succeed once a transient condition clears, with no caller change.
# Exhaustive over ErrorCategory; the bias is terminal when transience is ambiguous,
# since the flag exists to stop an agent hammering a permanent failure (#430).
_RETRYABLE_BY_CATEGORY: dict[ErrorCategory, bool] = {
    ErrorCategory.INFRASTRUCTURE_FAILURE: True,
    ErrorCategory.PROVISIONING_FAILURE: True,
    ErrorCategory.BOOT_TIMEOUT: True,
    ErrorCategory.READINESS_FAILURE: True,
    ErrorCategory.TRANSPORT_FAILURE: True,
    ErrorCategory.TRANSPORT_CONFLICT: True,
    ErrorCategory.DEBUG_ATTACH_FAILURE: True,
    ErrorCategory.CONTROL_FAILURE: True,
    ErrorCategory.CAPACITY_EXHAUSTED: True,
    ErrorCategory.QUEUE_TIMEOUT: True,
    ErrorCategory.CONFIGURATION_ERROR: False,
    ErrorCategory.MISSING_DEPENDENCY: False,
    ErrorCategory.BUILD_FAILURE: False,
    ErrorCategory.INSTALL_FAILURE: False,
    ErrorCategory.STALE_HANDLE: False,
    ErrorCategory.LEASE_EXPIRED: False,
    ErrorCategory.NOT_IMPLEMENTED: False,
    ErrorCategory.NOT_FOUND: False,
    ErrorCategory.CONFLICT: False,
    ErrorCategory.AUTHORIZATION_DENIED: False,
    ErrorCategory.QUOTA_EXCEEDED: False,
    ErrorCategory.ALLOCATION_DENIED: False,
}
```

Add the field to `ToolResponse`, right after `error_category` (line 67):

```python
    error_category: str | None = None
    retryable: bool | None = None
```

Extend the existing `_category_iff_failed` validator to derive `retryable` (keep the existing raises; add the derivation before `return self`):

```python
    @model_validator(mode="after")
    def _category_iff_failed(self) -> ToolResponse:
        """Enforce category-iff-failure and derive ``retryable`` from the category.

        A failure status without a category, or any other status carrying one, is a
        producer bug — fail fast at construction (ADR-0019). ``retryable`` is a pure
        function of the category (ADR-0118), derived here so it can never drift and is
        never caller-set; ``None`` on success, a ``bool`` on a classified failure.
        """
        is_failure = self.status in _FAILURE_STATUSES
        if is_failure and self.error_category is None:
            raise ValueError(f"status {self.status!r} requires an error_category")
        if not is_failure and self.error_category is not None:
            raise ValueError(f"error_category set on non-failure status {self.status!r}")
        self.retryable = (
            _RETRYABLE_BY_CATEGORY[ErrorCategory(self.error_category)]
            if self.error_category is not None
            else None
        )
        return self
```

Update the module docstring's field list (lines 1-8) to name `retryable` alongside `error_category`, e.g. append: "and a derived ``retryable`` flag the agent branches on (ADR-0118)."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/core/test_responses.py -q`
Expected: PASS.

- [ ] **Step 5: Run the wider envelope/consumer guardrails (the field lands on every response)**

Run: `just lint && just type && uv run python -m pytest tests/mcp tests/cli/test_structured_content_envelope.py -q`
Expected: clean. Adding `retryable` serializes `"retryable": null` on every success response. If any test asserts a whole-response dict by equality, update it to include `"retryable": None` (mechanical) — this is the audit the spec's "Wire shape" note flagged. Do not add `exclude_none`; present-but-null is the deliberate wire choice.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/responses.py tests/mcp/core/test_responses.py
# plus any response-assertion fixtures you had to update
git commit -m "feat(mcp): derive retryable from error_category in the envelope (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: Persist the cause at the two queued-terminate transitions

**Files:**
- Modify: `src/kdive/services/allocation/promotion.py` (`_terminate` ~lines 258-276; `_reap_one` ~lines 402-436)
- Test: `tests/mcp/lifecycle/test_allocations_reconcile.py` (promotion/reap behavior lives here) or `tests/adversarial`/`tests/services` if that is where promotion is exercised — place the test beside the existing promotion/reap tests.

- [ ] **Step 1: Write the failing tests**

Find the existing test that drives a budget-terminate at promotion and one that drives `reap_queue_timeouts`. Add assertions (or two new tests beside them) that read the row's `failure_category` after the transition:

```python
async def _failure_category(conn, alloc_id) -> str | None:
    async with conn.cursor() as cur:
        await cur.execute("SELECT failure_category FROM allocations WHERE id = %s", (alloc_id,))
        row = await cur.fetchone()
    return row[0] if row else None


# After a budget recheck terminates a queued request at promotion:
assert await _failure_category(conn, alloc_id) == "allocation_denied"

# After reap_queue_timeouts flips an aged queued row:
assert await _failure_category(conn, alloc_id) == "queue_timeout"
```

(Reuse the surrounding test's setup that drives `promote_pending` / `reap_queue_timeouts`; only the `failure_category` assertion is new.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_reconcile.py -q -k "terminate or reap or queue_timeout"`
Expected: FAIL — `failure_category` is NULL (not yet written).

- [ ] **Step 3: Write the cause in both terminate paths**

`ErrorCategory` is already imported in `promotion.py` (line 41). In `_terminate`, after the state flip `await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.FAILED)`:

```python
    await ALLOCATIONS.update_state(conn, alloc.id, AllocationState.FAILED)
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE allocations SET failure_category = %s WHERE id = %s",
            (ErrorCategory.ALLOCATION_DENIED.value, alloc.id),
        )
```

In `_reap_one`, after `await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.FAILED)`:

```python
        await ALLOCATIONS.update_state(conn, alloc_id, AllocationState.FAILED)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE allocations SET failure_category = %s WHERE id = %s",
                (ErrorCategory.QUEUE_TIMEOUT.value, alloc_id),
            )
```

Both writes run inside the transaction the caller already holds (`_promote_one`'s and `_reap_one`'s `conn.transaction()`), so the cause and the state flip commit atomically.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_reconcile.py -q -k "terminate or reap or queue_timeout"`
Expected: PASS.

- [ ] **Step 5: Run guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle/test_allocations_reconcile.py -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/services/allocation/promotion.py tests/mcp/lifecycle/test_allocations_reconcile.py
git commit -m "feat(allocation): record failure_category at queued-terminate paths (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: Envelope reads the cause for a failed allocation

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/allocations.py` (`_envelope_for_allocation`, lines 58-71)
- Test: `tests/mcp/lifecycle/test_allocations_tools.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/mcp/lifecycle/test_allocations_tools.py` (a unit test on the pure envelope function — no DB needed):

```python
def test_failed_envelope_reports_failure_category_else_infrastructure() -> None:
    from uuid import uuid4
    from datetime import UTC, datetime

    from kdive.domain.errors import ErrorCategory
    from kdive.domain.models import Allocation
    from kdive.domain.state import AllocationState
    from kdive.mcp.tools.lifecycle.allocations import _envelope_for_allocation

    base = dict(
        id=uuid4(), created_at=datetime.now(UTC), updated_at=datetime.now(UTC),
        principal="p", agent_session="s", project="proj", state=AllocationState.FAILED,
    )
    # NULL cause -> the unchanged infrastructure_failure fallback.
    null_cause = _envelope_for_allocation(Allocation(**base))
    assert null_cause.error_category == ErrorCategory.INFRASTRUCTURE_FAILURE.value
    assert null_cause.retryable is True
    # A budget terminate -> allocation_denied, terminal.
    budget = _envelope_for_allocation(
        Allocation(**base, failure_category=ErrorCategory.ALLOCATION_DENIED)
    )
    assert budget.error_category == ErrorCategory.ALLOCATION_DENIED.value
    assert budget.retryable is False
    # A queue timeout -> queue_timeout, retryable.
    timed_out = _envelope_for_allocation(
        Allocation(**base, failure_category=ErrorCategory.QUEUE_TIMEOUT)
    )
    assert timed_out.error_category == ErrorCategory.QUEUE_TIMEOUT.value
    assert timed_out.retryable is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py::test_failed_envelope_reports_failure_category_else_infrastructure -q`
Expected: FAIL — the envelope hardcodes `infrastructure_failure`, so the budget/timeout assertions fail.

- [ ] **Step 3: Read the cause in the failed branch**

In `src/kdive/mcp/tools/lifecycle/allocations.py`, change `_envelope_for_allocation`'s failed branch:

```python
def _envelope_for_allocation(alloc: Allocation) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6).

    A failed allocation reports its persisted ``failure_category`` (ADR-0118) — so a
    waiting agent learns ``queue_timeout`` vs ``allocation_denied`` — falling back to
    ``infrastructure_failure`` when the cause was not recorded.
    """
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value},
        )
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data={"project": alloc.project},
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_tools.py::test_failed_envelope_reports_failure_category_else_infrastructure -q`
Expected: PASS.

- [ ] **Step 5: Run guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/allocations.py tests/mcp/lifecycle/test_allocations_tools.py
git commit -m "feat(mcp): report failure_category on a failed allocation envelope (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 6: Queue-position hint on a `requested` allocation

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/allocations.py` (add `_queue_position`; add a `queue_position` param to `_envelope_for_allocation`; wire `get_allocation`)
- Test: `tests/mcp/lifecycle/test_allocations_wait.py` (create — shared by Task 7)

- [ ] **Step 1: Write the failing test**

Create `tests/mcp/lifecycle/test_allocations_wait.py`. Use the project's allocation-test setup (copy the resource/budget/quota seeding + `request_allocation` helpers from `tests/mcp/lifecycle/test_allocations_tools.py`; configure `max_pending_allocations > 0` and a host cap of 0/full so `on_capacity="queue"` enqueues). Then:

```python
def test_queue_position_counts_same_target_fifo(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            # Enqueue three same-kind requests in order a, b, c (host cap full).
            a = await _enqueue_requested(pool)  # helper: returns allocation_id, kind K
            b = await _enqueue_requested(pool)
            c = await _enqueue_requested(pool)
            ra = await alloc_tools.get_allocation(pool, OPERATOR_CTX, a)
            rb = await alloc_tools.get_allocation(pool, OPERATOR_CTX, b)
            rc = await alloc_tools.get_allocation(pool, OPERATOR_CTX, c)
        assert ra.data["queue_position"] == 1 and ra.data["queue_ahead"] == 0
        assert rb.data["queue_position"] == 2 and rb.data["queue_ahead"] == 1
        assert rc.data["queue_position"] == 3 and rc.data["queue_ahead"] == 2

    asyncio.run(_run())


def test_queue_position_absent_on_non_requested_and_in_list(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            granted = await _enqueue_granted(pool)  # a host with free capacity -> granted
            rg = await alloc_tools.get_allocation(pool, OPERATOR_CTX, granted)
            rl = await alloc_tools.list_allocations(pool, OPERATOR_CTX, project=PROJECT, limit=50)
        assert "queue_position" not in rg.data
        assert all("queue_position" not in item.data for item in rl.items)

    asyncio.run(_run())
```

(If a by-id queued request is easy to set up in this harness, add a third test asserting a by-id request counts only same-`requested_resource_id` rows and a cross-kind queued row does not shift the count. If the harness makes by-id setup heavy, cover the by-id branch in a focused `_queue_position` unit test against seeded rows instead.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_wait.py -q`
Expected: FAIL — `queue_position` is not in `data`.

- [ ] **Step 3: Add the helper, the envelope param, and wire `get`**

In `src/kdive/mcp/tools/lifecycle/allocations.py`, add imports near the top:

```python
from psycopg import AsyncConnection
from kdive.mcp.responses import JsonValue, ToolResponse
```

Add the helper (two full literal queries so `ty` sees `LiteralString`, never an f-string):

```python
async def _queue_position(conn: AsyncConnection, alloc: Allocation) -> int:
    """1-based FIFO rank of a ``requested`` allocation among same-target queued rows.

    Same target is the by-id ``requested_resource_id`` or the by-kind ``requested_kind``,
    ordered ``(created_at, id)`` — the order ``promote_pending`` selects on. An **advisory
    hint, not an ETA**: promotion is work-conserving and per-host (ADR-0118), so a younger
    request on a free host can be promoted ahead of an older one on a busy host.
    """
    if alloc.requested_resource_id is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = 'requested' "
            "AND requested_resource_id = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        target: object = alloc.requested_resource_id
    elif alloc.requested_kind is not None:
        query = (
            "SELECT count(*) FROM allocations WHERE state = 'requested' "
            "AND requested_kind = %(target)s "
            "AND (created_at, id) < (%(created_at)s, %(id)s)"
        )
        target = alloc.requested_kind.value
    else:
        return 1  # A requested row with no target is degenerate; report "next in line".
    async with conn.cursor() as cur:
        await cur.execute(
            query, {"target": target, "created_at": alloc.created_at, "id": alloc.id}
        )
        row = await cur.fetchone()
    ahead = int(row[0]) if row is not None else 0
    return ahead + 1
```

Give `_envelope_for_allocation` an optional `queue_position` keyword and surface it on a `requested` row (full function — note this also keeps the Task-5 failed branch):

```python
def _envelope_for_allocation(
    alloc: Allocation, *, queue_position: int | None = None
) -> ToolResponse:
    """Render an allocation; ``failed`` becomes a failure envelope (ADR-0023 §6).

    A failed allocation reports its persisted ``failure_category`` (ADR-0118), falling back
    to ``infrastructure_failure`` when unset. A ``requested`` row carries the advisory
    ``queue_position``/``queue_ahead`` hint when one was computed (ADR-0118).
    """
    if alloc.state is AllocationState.FAILED:
        category = alloc.failure_category or ErrorCategory.INFRASTRUCTURE_FAILURE
        return ToolResponse.failure(
            str(alloc.id),
            category,
            data={"current_status": alloc.state.value},
        )
    data: dict[str, JsonValue] = {"project": alloc.project}
    if alloc.state is AllocationState.REQUESTED and queue_position is not None:
        data["queue_position"] = queue_position
        data["queue_ahead"] = queue_position - 1
    return ToolResponse.success(
        str(alloc.id),
        alloc.state.value,
        suggested_next_actions=["allocations.get", "allocations.release"],
        data=data,
    )
```

Wire `get_allocation` to compute the position in the same connection (replace its body's connection block):

```python
    with bind_context(principal=ctx.principal):
        async with pool.connection() as conn:
            alloc = await ALLOCATIONS.get(conn, uid)
            if alloc is None or alloc.project not in ctx.projects:
                return _not_found(allocation_id)
            require_role(ctx, alloc.project, Role.VIEWER)
            position = (
                await _queue_position(conn, alloc)
                if alloc.state is AllocationState.REQUESTED
                else None
            )
        return _envelope_for_allocation(alloc, queue_position=position)
```

(`list_allocations` is unchanged — it deliberately omits the hint to avoid an N+1 query.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_wait.py -q`
Expected: PASS.

- [ ] **Step 5: Run guardrails**

Run: `just lint && just type && uv run python -m pytest tests/mcp/lifecycle -q`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/allocations.py tests/mcp/lifecycle/test_allocations_wait.py
git commit -m "feat(mcp): surface queue_position hint on a requested allocation (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 7: `allocations.wait` long-poll tool

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/allocations.py` (add `wait_allocation` + the `allocations.wait` tool in `register`)
- Modify: `tests/mcp/lifecycle/test_allocations_wait.py` (wait behavior)
- Modify: `tests/mcp/core/test_tool_docs.py` (`_BEHAVIOR_TESTS_BY_TOOL` entry)
- Regenerate: `docs/guide/reference/allocations.md` (+ index) via `just docs`

- [ ] **Step 1: Write the failing tests**

Add to `tests/mcp/lifecycle/test_allocations_wait.py` (mirror `test_jobs_tools.py`'s seam — inject `sleep` and flip the row mid-wait):

```python
def test_wait_returns_immediately_when_already_settled(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            granted = await _enqueue_granted(pool)
            resp = await alloc_tools.wait_allocation(pool, OPERATOR_CTX, granted, timeout_s=5.0)
        assert resp.status == "granted"

    asyncio.run(_run())


def test_wait_returns_on_requested_to_granted_transition(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            queued = await _enqueue_requested(pool)
            promoted = {"done": False}

            async def _sleep(delay: float) -> None:
                if not promoted["done"]:
                    await _force_grant(pool, queued)  # flip requested -> granted out of band
                    promoted["done"] = True
                await asyncio.sleep(0)

            resp = await alloc_tools.wait_allocation(
                pool, OPERATOR_CTX, queued, timeout_s=5.0, sleep=_sleep
            )
        assert resp.status == "granted"

    asyncio.run(_run())


def test_wait_returns_current_envelope_at_deadline_while_requested(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            queued = await _enqueue_requested(pool)
            resp = await alloc_tools.wait_allocation(pool, OPERATOR_CTX, queued, timeout_s=0.0)
        assert resp.status == "requested"
        assert resp.data["queue_position"] == 1

    asyncio.run(_run())


def test_wait_not_found_for_absent_and_malformed(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            from uuid import uuid4
            absent = await alloc_tools.wait_allocation(pool, OPERATOR_CTX, str(uuid4()), timeout_s=0.0)
            bad = await alloc_tools.wait_allocation(pool, OPERATOR_CTX, "not-a-uuid", timeout_s=0.0)
        assert absent.error_category == "not_found"
        assert bad.error_category == "configuration_error"

    asyncio.run(_run())
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_wait.py -q -k wait`
Expected: FAIL — `wait_allocation` does not exist.

- [ ] **Step 3: Add the handler and register the tool**

In `src/kdive/mcp/tools/lifecycle/allocations.py`, add stdlib imports at the top:

```python
import asyncio
import math
from collections.abc import Awaitable, Callable
```

Add module constants near `_log`:

```python
POLL_INTERVAL_S = 0.5
MAX_WAIT_S = 300.0
```

Add the handler (mirrors `wait_job`):

```python
async def wait_allocation(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    allocation_id: str,
    timeout_s: float,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> ToolResponse:
    """Poll until the allocation leaves ``requested`` or ``timeout_s`` (clamped) elapses.

    A queued ``requested`` allocation settles into ``granted`` (promoted), ``released``
    (cancelled), or ``failed`` (budget terminate / ``queue_timeout`` reap). Each poll
    acquires and releases a pool connection (holds none while sleeping); a non-positive or
    non-finite timeout means a single read. Auth/no-leak match ``allocations.get`` (ADR-0118).
    """
    uid = _as_uuid(allocation_id)
    if uid is None:
        return _config_error(allocation_id)
    if not math.isfinite(timeout_s):
        return _config_error(allocation_id)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + min(max(timeout_s, 0.0), MAX_WAIT_S)
    with bind_context(principal=ctx.principal):
        while True:
            async with pool.connection() as conn:
                alloc = await ALLOCATIONS.get(conn, uid)
                if alloc is None or alloc.project not in ctx.projects:
                    return _not_found(allocation_id)
                require_role(ctx, alloc.project, Role.VIEWER)
                position = (
                    await _queue_position(conn, alloc)
                    if alloc.state is AllocationState.REQUESTED
                    else None
                )
            now = loop.time()
            if alloc.state is not AllocationState.REQUESTED or now >= deadline:
                return _envelope_for_allocation(alloc, queue_position=position)
            await sleep(min(POLL_INTERVAL_S, deadline - now))
```

Register the tool inside `register` (alongside the other `@app.tool`s):

```python
    @app.tool(
        name="allocations.wait",
        annotations=_docmeta.read_only(),
        meta={"maturity": "implemented"},
    )
    async def allocations_wait(
        allocation_id: Annotated[
            str, Field(description="The Allocation to poll until it leaves the requested (queued) state.")
        ],
        timeout_s: Annotated[
            float, Field(description="Maximum seconds to wait (capped at 300).")
        ] = 30.0,
    ) -> ToolResponse:
        return await wait_allocation(pool, current_context(), allocation_id, timeout_s)
```

- [ ] **Step 4: Map the new tool to its covering test**

In `tests/mcp/core/test_tool_docs.py`, add an entry to `_BEHAVIOR_TESTS_BY_TOOL` (keep the dict's ordering/format consistent with its neighbours):

```python
    "allocations.wait": ["mcp/lifecycle/test_allocations_wait.py"],
```

- [ ] **Step 5: Regenerate the tool reference**

Run: `just docs`
Then verify it matches: `just docs-check`
Expected: `docs/guide/reference/allocations.md` (and the index) now include `allocations.wait`; `docs-check` passes.

- [ ] **Step 6: Run the tests + tool-docs guards**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_allocations_wait.py tests/mcp/core/test_tool_docs.py -q`
Expected: PASS (including `test_active_tools_have_a_covering_test`).

- [ ] **Step 7: Run guardrails**

Run: `just lint && just type`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/allocations.py tests/mcp/lifecycle/test_allocations_wait.py tests/mcp/core/test_tool_docs.py docs/guide/reference/
git commit -m "feat(mcp): add allocations.wait long-poll tool (#430)

$(printf 'Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>')"
```

---

### Task 8: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full PR gate locally**

Run: `just ci`
Expected: lint, type, lint-shell, lint-workflows, check-mermaid, and the test suite all green. `live_vm`/`live_stack` markers stay skipped (no hardware) — that is expected, not a failure.

- [ ] **Step 2: If anything is red, fix it on the owning task and re-run**

Do not proceed with a red gate. Common late breakers: a whole-response equality assertion that now needs `"retryable": None` (Task 3 audit), or the `docs-check` gate if `just docs` was not committed (Task 7).

- [ ] **Step 3: Confirm the working tree is clean**

Run: `git status --short`
Expected: empty.

---

## Self-review notes (author)

- **Spec coverage:** Gap 1 → Tasks 5-7; Gap 2 → Task 6; Gap 3 → Task 3; the failed-settle cause (the spec's load-bearing fix) → Tasks 1, 2, 4, 5; out-of-scope items (ETA, `kdivectl --watch`, MCP progress push) are intentionally untouched.
- **No new public contract beyond the spec:** one tool (`allocations.wait`), one envelope field (`retryable`), two `data` keys (`queue_position`/`queue_ahead`), one column (`failure_category`).
- **Type consistency:** `_queue_position(conn, alloc) -> int`; `_envelope_for_allocation(alloc, *, queue_position: int | None = None)`; `wait_allocation(..., *, sleep=asyncio.sleep)`; `_RETRYABLE_BY_CATEGORY: dict[ErrorCategory, bool]`; `retryable: bool | None`. These names are used identically wherever referenced across tasks.

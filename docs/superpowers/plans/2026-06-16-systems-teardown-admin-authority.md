# systems.teardown Admin-Only Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `systems.teardown` require only project `admin` (it is currently denied for every caller because the three-check destructive gate's `capability_scope` factor is structurally unpopulated), and surface the failed gate-check names on every destructive-op denial envelope.

**Architecture:** Replace the three-check `assert_destructive_allowed` call inside `teardown_system` with a direct `require_role(ADMIN)`, catching `RoleDenied` locally so the dispatch-boundary `DenialAuditMiddleware` never double-audits and the envelope stays keyed on `system_id`. A new shared `authz_denied(object_id, missing_checks)` helper carries the gate's closed-enum check tokens in `data["missing_checks"]` — applied at all four destructive denial sites (teardown's new role denial + the still-gated reprovision/power/force_crash). `force_crash`/`power`/`reprovision` keep the full gate. Authority decision and rationale: `docs/adr/0129-systems-teardown-admin-authority.md`.

**Tech Stack:** Python 3.13, `uv`, `pytest`, FastMCP, psycopg, ruff, ty.

**Guardrail commands (run before every commit):**
- `just lint` — ruff check + format check
- `just type` — ty check (whole tree)
- Focused test: `uv run python -m pytest <path>::<name> -q`
- `just test` — full non-live suite (run before the final push)
- `just docs-check` — only if a tool description/maturity changed (Task 3)

**Conventions (from CLAUDE.md / AGENTS.md):**
- Uniform `ToolResponse` envelope; `error_category` only on failures; pick the most specific `ErrorCategory`, never invent strings.
- Replace, don't deprecate — remove dead code (the `_teardown_opt_in` helper, the `resolver` teardown param), no shims.
- Absolute imports only; ruff line length 100.
- Conventional-commit subjects ≤72 chars, imperative, ending with the `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.

---

## File structure

- `src/kdive/mcp/tools/_common.py` — add `authz_denied` helper (sits beside `config_error`/`not_found`/`stale_handle`).
- `src/kdive/mcp/tools/lifecycle/systems/admin.py` — `teardown_system` authority change; drop the teardown `resolver` param, the profile parse + runtime resolve, and the `_teardown_opt_in` helper; reprovision denial envelope gains `missing_checks`.
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` — `_register_systems_teardown` drops `resolver`; tool description corrected; its call site in `register` updated.
- `src/kdive/mcp/tools/lifecycle/control.py` — `_authorize_destructive` denial envelope gains `missing_checks` (covers `force_crash` + destructive `power`).
- `tests/mcp/lifecycle/test_systems_tools.py` — teardown tests rewritten for admin-only authority + `missing_checks`.
- `tests/mcp/lifecycle/test_control_tools.py` — assert `missing_checks` on the control denial envelope.

---

### Task 1: Shared `authz_denied` envelope helper

The gate's denied-check tokens (`capability_scope`, `admin_role`, `operator_role`, `profile_opt_in`) are a closed policy enum carrying no resource identifiers, so they are safe to surface in `data` under the no-leak seam (ADR-0123/0129, which suppresses `detail`, not `data`). Four call sites need the same envelope, so a shared helper is justified (DRY; the same dict literal would otherwise repeat).

**Files:**
- Modify: `src/kdive/mcp/tools/_common.py`
- Test: `tests/mcp/test_common.py` (create if absent; otherwise append)

- [ ] **Step 1: Write the failing test**

```python
# tests/mcp/test_common.py
from kdive.domain.errors import ErrorCategory
from kdive.mcp.tools._common import authz_denied


def test_authz_denied_surfaces_missing_checks() -> None:
    resp = authz_denied("sys-123", ["admin_role"])
    assert resp.status == "error"
    assert resp.error_category == ErrorCategory.AUTHORIZATION_DENIED.value
    assert resp.object_id == "sys-123"
    assert resp.data["missing_checks"] == ["admin_role"]
    # No-leak seam: detail stays the suppressed constant, never a resource name.
    assert resp.detail == "access denied"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/mcp/test_common.py::test_authz_denied_surfaces_missing_checks -q`
Expected: FAIL with `ImportError: cannot import name 'authz_denied'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/kdive/mcp/tools/_common.py` (after `stale_handle`), and import `JsonValue`:

```python
from kdive.serialization import JsonValue
```

```python
def authz_denied(object_id: str, missing_checks: list[str]) -> ToolResponse:
    """Build an ``authorization_denied`` envelope naming the failed gate checks (ADR-0129).

    ``missing_checks`` is the destructive-op gate's closed enum of policy-check tokens
    (``capability_scope``, ``admin_role``/``operator_role``, ``profile_opt_in``) — never a
    resource identifier — so it is safe to surface in ``data`` under the no-leak seam
    (ADR-0123), which suppresses ``detail`` only, not ``data``.
    """
    checks: list[JsonValue] = list(missing_checks)
    return ToolResponse.failure(
        object_id, ErrorCategory.AUTHORIZATION_DENIED, data={"missing_checks": checks}
    )
```

Add `"authz_denied"` to `__all__` (keep it alphabetically ordered: after `as_uuid`/`authorizing`, before `clamp_list_limit`).

Note: `checks` is typed `list[JsonValue]` deliberately — `list[str]` is *not* assignable to `JsonValue` under ty (list invariance), but `list[JsonValue]` is.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/mcp/test_common.py::test_authz_denied_surfaces_missing_checks -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
Expected: clean.

```bash
git add src/kdive/mcp/tools/_common.py tests/mcp/test_common.py
git commit  # subject: "feat(mcp): add authz_denied envelope helper with missing_checks"
```

---

### Task 2: `teardown_system` requires admin only

This is the core authority fix. `teardown_system` drops the three-check gate, the `resolver` parameter, the profile parse, and the `_teardown_opt_in` helper; it calls `require_role(ADMIN)` and catches `RoleDenied` locally (so `DenialAuditMiddleware` never double-audits and the envelope stays `system_id`-keyed), auditing through the existing `_audit_destructive_denied` and returning `authz_denied(..., ["admin_role"])`.

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/admin.py` (`teardown_system` ~lines 213-264; remove `_teardown_opt_in` ~136-137)
- Test: `tests/mcp/lifecycle/test_systems_tools.py` (~lines 1010-1140)

- [ ] **Step 1: Rewrite the teardown tests to express admin-only authority**

In `tests/mcp/lifecycle/test_systems_tools.py`:

(a) Drop the `resolver` arg from the `_teardown` helper:

```python
async def _teardown(pool: AsyncConnectionPool, ctx: RequestContext, system_id: str):
    return await teardown_system(pool, ctx, system_id)
```

(b) Change `test_teardown_tool_enqueues_job` to use an **unscoped** allocation (`_granted_allocation`, not `_scoped_teardown_allocation`) so it proves admin-without-`capability_scope` now succeeds:

```python
def test_teardown_admin_without_scope_enqueues_job(migrated_url: str) -> None:
    # ADR-0129: admin on the owning project may tear down with no capability_scope grant.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
            assert resp.data["system_id"] == sys_id
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT count(*) AS n FROM jobs WHERE dedup_key = %s", (f"{sys_id}:teardown",)
                )
                row = await cur.fetchone()
        assert row is not None and row["n"] == 1

    asyncio.run(_run())
```

(c) Change `test_teardown_tool_below_admin_denied` to assert the new `missing_checks` envelope and the `admin_role` audit digest:

```python
@pytest.mark.parametrize("role", [Role.VIEWER, Role.OPERATOR])
def test_teardown_below_admin_denied_with_missing_checks(migrated_url: str, role: Role) -> None:
    # teardown is admin-only: viewer AND operator are refused (ADR-0129).
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            resp = await _teardown(pool, _ctx(role), sys_id)
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("SELECT count(*) AS n FROM jobs WHERE kind = 'teardown'")
                row = await cur.fetchone()
                await cur.execute(
                    "SELECT args_digest FROM audit_log WHERE transition = 'teardown:denied'"
                )
                audit_row = await cur.fetchone()
        assert resp.status == "error"
        assert resp.error_category == "authorization_denied"
        assert resp.data["missing_checks"] == ["admin_role"]
        assert row is not None and row["n"] == 0
        assert audit_row is not None
        assert audit_row["args_digest"] == args_digest(
            {"system_id": sys_id, "missing": ["admin_role"]}
        )

    asyncio.run(_run())
```

(d) **Delete** `test_teardown_tool_without_scope_denied_and_audited` and `test_teardown_tool_without_profile_opt_in_denied` — both assert behavior (deny an admin for missing scope / missing profile opt-in) that ADR-0129 deliberately removes. Replace them with one robustness test proving an admin can tear down even a System whose profile does not opt teardown in (and would previously have been denied):

```python
def test_teardown_admin_succeeds_without_profile_opt_in(migrated_url: str) -> None:
    # ADR-0129: teardown no longer reads the profile, so a profile that does not opt
    # teardown in (or is otherwise unhelpful for the gate) does not block an admin.
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_teardown_system(
                pool, alloc_id, SystemState.READY, profile=_profile()
            )
            resp = await _teardown(pool, _ctx(Role.ADMIN), sys_id)
        assert resp.data["system_id"] == sys_id
        assert resp.status != "error"

    asyncio.run(_run())
```

(e) `test_teardown_tool_already_torn_down_no_job` keeps working but switch its allocation to `_granted_allocation` for consistency (admin on a torn-down System still short-circuits to `torn_down`). If `_scoped_teardown_allocation` becomes unused after these edits, delete it.

- [ ] **Step 2: Run the teardown tests to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py -k teardown -q`
Expected: FAIL — `_teardown` still passes `resolver`, `teardown_system` still runs the gate (so unscoped admin is denied, `missing_checks` absent).

- [ ] **Step 3: Rewrite `teardown_system` and remove the dead opt-in helper**

In `src/kdive/mcp/tools/lifecycle/systems/admin.py`, replace `teardown_system` with:

```python
async def teardown_system(
    pool: AsyncConnectionPool,
    ctx: RequestContext,
    system_id: str,
) -> ToolResponse:
    """Enqueue an idempotent teardown for a System the caller's project administers.

    Requires `admin` on the owning project (ADR-0129). Teardown is the normal lifecycle
    terminus of a granted System, so it no longer runs the three-check destructive gate —
    the un-grantable `capability_scope` layer and the no-op-for-teardown profile opt-in add
    no safety here. `RoleDenied` is caught locally (not propagated to `DenialAuditMiddleware`)
    so the denial is audited once, keyed on `system_id`, with `data["missing_checks"]`.
    """
    uid = _as_uuid(system_id)
    if uid is None:
        return _config_error(system_id)
    with bind_context(principal=ctx.principal):
        async with (
            pool.connection() as conn,
            conn.transaction(),
            advisory_xact_lock(conn, LockScope.SYSTEM, uid),
        ):
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            allocation = await ALLOCATIONS.get(conn, system.allocation_id)
            if allocation is None or allocation.project not in ctx.projects:
                return _config_error(system_id)
            try:
                require_role(ctx, allocation.project, Role.ADMIN)
            except RoleDenied:
                await _audit_destructive_denied(conn, ctx, system, _TEARDOWN, ["admin_role"])
                return _authz_denied(system_id, ["admin_role"])
            if system.state is SystemState.TORN_DOWN:
                return ToolResponse.success(
                    system_id,
                    "torn_down",
                    suggested_next_actions=["systems.get"],
                    data={"project": system.project},
                )
            job = await queue.enqueue(
                conn,
                JobKind.TEARDOWN,
                SystemPayload(system_id=str(uid)),
                job_authorizing(ctx, system.project),
                f"{uid}:teardown",
            )
        return job_envelope(job, "system_id", uid)
```

Delete the now-dead `_teardown_opt_in` helper (lines 136-137).

Update imports at the top of `admin.py`:
- Add: `from kdive.mcp.tools._common import authz_denied as _authz_denied`
- Add `require_role` and `RoleDenied` to the existing rbac import:
  `from kdive.security.authz.rbac import Role, RoleDenied, require_role`
- Remove `ProviderResolver` import (`from kdive.providers.core.resolver import ProviderResolver`) — only `teardown_system` used it, and reprovision uses `ProfilePolicy` off the dataclass, not the resolver. Verify with `rg -n "ProviderResolver|resolver" src/kdive/mcp/tools/lifecycle/systems/admin.py` after the edit; if reprovision still references it, keep the import.

Keep `assert_destructive_allowed`, `DestructiveOp`, `DestructiveOpDenied`, `ProvisioningProfile`, `_TEARDOWN` — reprovision (and Task 4) still use them.

- [ ] **Step 4: Run the teardown tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py -k teardown -q`
Expected: PASS.

- [ ] **Step 5: Guardrails + commit**

Run: `just lint && just type`
Expected: clean (no unused-import warning for `ProviderResolver`/`_teardown_opt_in`).

```bash
git add src/kdive/mcp/tools/lifecycle/systems/admin.py tests/mcp/lifecycle/test_systems_tools.py
git commit  # subject: "fix(mcp): require only project admin for systems.teardown"
```

---

### Task 3: Drop the teardown `resolver` wiring and correct the tool description

`teardown_system` no longer takes `resolver`, so the registrar must stop passing it; the tool description must drop the false "destructive-op opt-in" clause.

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (`_register_systems_teardown` ~241-253 and its call in `register`)
- Modify (generated): `docs/guide/reference/systems.md`

- [ ] **Step 1: Update the registrar**

In `src/kdive/mcp/tools/lifecycle/systems/registrar.py`, change `_register_systems_teardown` to drop the `resolver` parameter and fix the docstring:

```python
def _register_systems_teardown(app: FastMCP, pool: AsyncConnectionPool) -> None:
    @app.tool(
        name="systems.teardown",
        annotations=_docmeta.destructive(),
        meta={"maturity": "partial"},
    )
    async def systems_teardown(
        system_id: Annotated[str, Field(description="The System to tear down.")],
    ) -> ToolResponse:
        """Enqueue teardown for a System. Requires admin on the System's project."""
        return await _teardown_system(pool, current_context(), system_id)
```

Update the call site in `register` (search for `_register_systems_teardown(app, pool, resolver)`):

```python
    _register_systems_teardown(app, pool)
```

Leave `meta={"maturity": "partial"}` unchanged: the provider-level teardown is unrelated to this authority fix, so do not re-grade maturity here (out of scope).

- [ ] **Step 2: Run to verify it fails**

Run: `just docs-check`
Expected: FAIL — the generated `docs/guide/reference/systems.md` still shows the old description "Requires admin and destructive-op opt-in".

- [ ] **Step 3: Regenerate the committed tool reference**

Run: `just docs`
This rewrites `docs/guide/reference/systems.md` from the registry. Review the diff: only the `systems.teardown` description line should change.

- [ ] **Step 4: Verify**

Run: `just docs-check && just lint && just type`
Expected: clean.
Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py -k teardown -q`
Expected: PASS (no `resolver` reference remains).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/registrar.py docs/guide/reference/systems.md
git commit  # subject: "fix(mcp): correct systems.teardown description and drop resolver arg"
```

---

### Task 4: Surface `missing_checks` on the still-gated denial envelopes

Apply the new helper at the three remaining gate-denial sites so a denied `reprovision`/`power`/`force_crash` self-explains (diagnostic only — `capability_scope` stays unsatisfiable until the follow-up; ADR-0129).

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/control.py` (`_authorize_destructive` denial return ~148)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/admin.py` (reprovision denial return ~111)
- Test: `tests/mcp/lifecycle/test_control_tools.py`, `tests/mcp/lifecycle/test_systems_tools.py`

This task changes **three** denial sites (reprovision, destructive `power`, `force_crash`), so it needs an assertion on each. The exact tests and their expected `missing_checks` (verified against the current tree):

1. `test_power_destructive_action_denied_without_scope` — `tests/mcp/lifecycle/test_control_tools.py:288` (admin ctx, profile opts `power` in, unscoped allocation → only `capability_scope` missing). Asserts `missing=["capability_scope"]` in audit args at line 309.
2. `test_force_crash_denied_returns_authorization_denied` — `tests/mcp/lifecycle/test_control_tools.py:451` — **parametrized** over which gate checks fail (e.g. line 446 `(False, True, True)  # missing capability_scope`). Assert the envelope's `missing_checks` equals the *same* expected-missing list the test already feeds its audit-args assertion, so it stays correct across every parametrized combination.
3. `test_reprovision_without_scope_denied` — `tests/mcp/lifecycle/test_systems_tools.py:1482` (operator/admin ctx, unscoped allocation → `capability_scope` missing). Expect `missing_checks=["capability_scope"]`.

- [ ] **Step 1: Add a failing `missing_checks` envelope assertion to each of the three tests**

In `test_power_destructive_action_denied_without_scope` (`test_control_tools.py:288`), after the existing `error_category` assertion add:

```python
        assert resp.data["missing_checks"] == ["capability_scope"]
```

In `test_force_crash_denied_returns_authorization_denied` (`test_control_tools.py:451`), this test is parametrized over `(scope_ok, is_admin, opt_in)` and currently asserts only `error_category`, the audit-row count, and the job count — it computes **no** expected-missing list. Each row flips exactly one factor false, so add a fourth parametrize column carrying that row's single expected token and assert it. Change the decorator and signature:

```python
@pytest.mark.parametrize(
    ("scope_ok", "is_admin", "opt_in", "expected_missing"),
    [
        (False, True, True, "capability_scope"),
        (True, False, True, "admin_role"),
        (True, True, False, "profile_opt_in"),
    ],
)
def test_force_crash_denied_returns_authorization_denied(
    migrated_url: str, scope_ok: bool, is_admin: bool, opt_in: bool, expected_missing: str
) -> None:
```

and, after the existing `error_category` assertion (`test_control_tools.py:462`), add:

```python
            assert resp.data["missing_checks"] == [expected_missing]
```

(The gate appends missing checks in fixed order — `capability_scope`, `<role>_role`, `profile_opt_in` — and each row leaves exactly one missing, so the expected list is always single-element.)

In `test_reprovision_without_scope_denied` (`test_systems_tools.py:1482`), after the `error_category` assertion add:

```python
        assert resp.data["missing_checks"] == ["capability_scope"]
```

Before editing, confirm none of the three already assert `resp.data == {}` (they assert audit args + `error_category`, so a new `data` key is additive).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -k "denied or force_crash" tests/mcp/lifecycle/test_systems_tools.py -k reprovision_without_scope -q`
Expected: FAIL — `resp.data` has no `missing_checks` key at any of the three sites.

- [ ] **Step 3: Apply the helper at the denial sites**

In `src/kdive/mcp/tools/lifecycle/control.py`, change the `_authorize_destructive` denial return:

```python
        return _authz_denied(str(system_uid), denied.missing)
```

Add the import: `from kdive.mcp.tools._common import authz_denied as _authz_denied`.

In `src/kdive/mcp/tools/lifecycle/systems/admin.py`, change the reprovision denial return (the `except DestructiveOpDenied` block in `_reprovision_locked`):

```python
        except DestructiveOpDenied as denied:
            await _audit_destructive_denied(conn, ctx, system, _REPROVISION, denied.missing)
            return _authz_denied(str(system_id), denied.missing)
```

(`_authz_denied` is already imported from Task 2.)

- [ ] **Step 4: Verify**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -k "denied or force_crash" -q`
Expected: PASS (power-without-scope and force_crash denial envelopes carry `missing_checks`).
Run: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py -k reprovision -q`
Expected: PASS (the reprovision denial envelope now also carries `missing_checks`; other reprovision tests unaffected).
Run: `just lint && just type`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/control.py src/kdive/mcp/tools/lifecycle/systems/admin.py tests/mcp/lifecycle/test_control_tools.py tests/mcp/lifecycle/test_systems_tools.py
git commit  # subject: "feat(mcp): surface missing_checks on destructive-op denials"
```

---

### Task 5: File the follow-up issue for the gate-wide `capability_scope` gap

ADR-0129 scopes the fix to teardown; `reprovision`/`power`/`force_crash` remain structurally denied because nothing populates `capability_scope.destructive_ops`. Record that so it is not buried.

- [ ] **Step 1: Create the issue**

```bash
gh issue create \
  --title "Destructive gate's capability_scope is never populated (reprovision/power/force_crash dead on normal path)" \
  --label "area:security,area:mcp-api,type:bug,status:needs-design" \
  --body "Spun out of #463 / ADR-0129. The three-check destructive gate (assert_destructive_allowed) checks allocation.capability_scope.destructive_ops, but admission hard-codes capability_scope={} (services/allocation/admission/core.py:461,606) and no production path ever writes destructive_ops — only tests do, via raw SQL. So systems.reprovision, control.power (off/cycle/reset), and control.force_crash are denied for every caller on the normal MCP path (only the seeded live_stack/integration fixtures pass). #463/ADR-0129 fixed teardown by dropping it to admin-only; the remaining three need a deliberate design: either an allocation-request grant path that populates capability_scope, or a per-op gate revision. Denials now surface data[\"missing_checks\"] (ADR-0129) so the gap is at least observable."
```

- [ ] **Step 2: Record nothing in git** — this task creates an issue only; no commit.

---

## Self-review

**Spec coverage (vs ADR-0129):**
- Decision §1 (admin-only teardown, authz-before-state order) → Task 2.
- Decision §2 (`missing_checks` in `data`; local `RoleDenied` catch; single audit row) → Task 1 (helper) + Task 2 (teardown) + Task 4 (other three ops).
- Decision §3 (corrected description) → Task 3.
- Consequences (drop `resolver`/`_teardown_opt_in`; follow-up issue) → Task 2 + Task 3 + Task 5.

**Type consistency:** `authz_denied(object_id: str, missing_checks: list[str])` is called as `_authz_denied(system_id, ["admin_role"])` (Task 2), `_authz_denied(str(system_uid), denied.missing)` (Task 4 control), `_authz_denied(str(system_id), denied.missing)` (Task 4 reprovision) — `denied.missing` is `list[str]` per `DestructiveOpDenied`. Consistent. `_audit_destructive_denied(conn, ctx, system, _TEARDOWN, ["admin_role"])` matches its `(conn, ctx, system, op_kind, missing)` signature.

**Placeholder scan:** none — every code step shows the full edit.

**Edge cases covered by tests:** admin-without-scope success (Task 2b), below-admin denial with `missing_checks` + audit digest (Task 2c), admin success with non-opt-in profile (Task 2d), already-torn-down idempotency (Task 2e), and `missing_checks` on all three still-gated denial envelopes — power-without-scope, parametrized force_crash, and reprovision-without-scope (Task 4). The non-member path is unreachable (ownership checks at the top guarantee `allocation.project in ctx.projects`, so `require_role` raises only `RoleDenied`), so catching `RoleDenied` alone is correct.

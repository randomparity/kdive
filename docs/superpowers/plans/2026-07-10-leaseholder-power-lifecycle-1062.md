# Leaseholder Power Lifecycle (#1062) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclassify `control.power` (`on`/`off`/`cycle`/`reset`) as leaseholder lifecycle requiring only `contributor`, admitted only on `READY` Systems, with `force_crash` unchanged — closing the #1062 (BLACK_BOX_REVIEW P2) no-reboot gap without a break-glass bypass.

**Architecture:** Remove the destructive-gate branch from `power_system`; require `contributor` for every power action; narrow power admission from `{READY, CRASHED}` to `{READY}` and re-check `READY` in `power_handler` before the physical op. Remove `POWER` from `DESTRUCTIVE_JOB_KINDS` and decouple the profile write-validator's accepted-token set to the opt-in-consuming kinds `{FORCE_CRASH, REPROVISION}`, so `destructive_ops` governs only `force_crash`+`reprovision` and both `"power"` and inert `"teardown"` become rejected tokens.

**Tech Stack:** Python 3.14, `uv`, FastMCP, psycopg/Postgres, pytest. Spec: `docs/superpowers/specs/2026-07-10-leaseholder-power-lifecycle-1062-design.md`; ADR-0320.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` strict (whole tree, `src`+`tests`).
- Absolute imports only; Google-style docstrings on non-trivial public APIs.
- Doc-style guard: **Milestone** not "Sprint"; avoid "critical", "robust", "comprehensive", "elegant".
- The `@app.tool` wrapper docstring + `Field(description=...)` **is** the agent-facing contract — update it, not only the handler.
- No DB migration. `destructive_ops` stays a freeform `list[NonEmptyStr]`.
- `force_crash` is UNCHANGED (`admin` + two-check gate + `destructive_ops` opt-in). Do not touch its authz.
- Guardrails: `just lint`, `just type`, `just test` (run individually; CI hard-gates each). Single test: `uv run python -m pytest <path>::<name> -q`.
- Conventional-commit subjects ≤72 chars; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- The residual force_crash physical-crash-window race is OUT OF SCOPE (tracked as #1078). Do not add a `crashing` marker.

## File Structure

- `src/kdive/mcp/tools/lifecycle/control.py` — power authz → contributor, READY-only admission, wrapper docstring/Field (Task 1).
- `src/kdive/mcp/exposure.py` — `control.power` visibility tier `_OPERATOR` → `_CONTRIBUTOR` (Task 1).
- `src/kdive/jobs/handlers/control.py` — `power_handler` READY re-check before physical op (Task 2).
- `src/kdive/domain/operations/jobs.py` — drop `POWER` from `DESTRUCTIVE_JOB_KINDS`; add `OPT_IN_DESTRUCTIVE_JOB_KINDS` (Task 3).
- `src/kdive/services/systems/validation.py` — accepted-token set from `OPT_IN_DESTRUCTIVE_JOB_KINDS` (Task 3).
- `src/kdive/profiles/provisioning.py` — 2 `destructive_ops` class-docstring updates (local ~115, remote ~164) (Task 4).
- `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py` — `destructive_ops` scope note (Task 4).
- Tests: `tests/mcp/lifecycle/test_control_tools.py`, `tests/services/systems/test_system_validation.py`, `tests/mcp/lifecycle/systems/test_profile_examples*.py` (or equivalent).
- Docs: `src/kdive/security/authz/gate.py` docstring, `docs/design/destructive-gate-per-op-revision.md`, `docs/guide/**` control-power role text (Task 5).

---

### Task 1: Reclassify power — contributor role + exposure tier, READY-only admission

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/control.py` (`power_system`, module constants, wrapper docstring/Field, module docstring line 1-14)
- Modify: `src/kdive/mcp/exposure.py` (line 119: `control.power` tier — see Step 1c)
- Modify: `tests/mcp/core/test_tool_docs.py` (gate-caller backstop — see Step 1b), `tests/mcp/core/test_exposure.py` (visibility sets — see Step 1c)
- Regenerate: `docs/guide/reference/control.md` (`just docs`), `docs/guide/safety-and-rbac.md` rbac-matrix region (`just rbac-matrix`)
- Test: `tests/mcp/lifecycle/test_control_tools.py`

**Interfaces:**
- Consumes: `Role.CONTRIBUTOR`, `require_role` (already imported); `SystemState.READY`.
- Produces: `power_system(pool, ctx, *, system_id, action, resolver, idempotency_key=None) -> ToolResponse` (signature unchanged); admits only `READY`; requires `contributor` for all actions.

- [ ] **Step 1: Write/flip the failing tests.** In `test_control_tools.py`:
  - Replace `test_power_destructive_action_refused_for_operator` (asserts operator refused) with a test that a **contributor** may `off`/`cycle`/`reset` with **no** `destructive_ops` opt-in:

```python
@pytest.mark.parametrize("action", ["off", "cycle", "reset"])
def test_power_destructive_action_allowed_for_contributor_no_optin(
    migrated_url: str, action: str
) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action=action)
            assert resp.status == "queued"

    asyncio.run(scenario())
```

  - Add: a `viewer` is denied every power action (below `contributor`):

```python
@pytest.mark.parametrize("action", ["on", "off", "cycle", "reset"])
def test_power_denied_for_viewer(migrated_url: str, action: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.READY)
            with pytest.raises(AuthorizationError):
                await _power(pool, _ctx(Role.VIEWER), system_id=sys_id, action=action)

    asyncio.run(scenario())
```

  - Add: power on a `CRASHED` System is a `configuration_error` (evidence protected):

```python
@pytest.mark.parametrize("action", ["on", "off", "cycle", "reset"])
def test_power_on_crashed_system_is_config_error(migrated_url: str, action: str) -> None:
    async def scenario() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED)
            resp = await _power(pool, _ctx(Role.CONTRIBUTOR), system_id=sys_id, action=action)
            assert resp.status == "error" and resp.error_category == "configuration_error"
            assert resp.data.get("current_status") == "crashed"

    asyncio.run(scenario())
```

  - Update `test_power_on_is_operator_and_enqueues_job` → rename to `test_power_on_is_contributor_and_enqueues_job`, call with `_ctx(Role.CONTRIBUTOR)`.
  - Update `test_power_on_without_operator_raises` → the boundary is now `contributor`: assert `_ctx(Role.VIEWER)` raises (already covered by the viewer test above — delete this now-redundant test or keep as viewer-specific).
  - Update `test_power_off_with_gate_checks_enqueues_job` → it seeds `destructive_ops=["power"]`; the opt-in is now irrelevant. Rename to `test_power_off_enqueues_job`, drop the `destructive_ops` seed, call with `_ctx(Role.CONTRIBUTOR)`.
  - Keep `test_power_non_started_system_is_config_error` (DEFINED → config error) — still valid.

- [ ] **Step 1b: Update the gate-caller backstop.** `tests/mcp/core/test_tool_docs.py` computes, by transitive-call introspection (`_gate_reachers()`, dynamic — there is no static registry to edit), which tools reach `assert_destructive_allowed`. After Task 1, `control.power` no longer does. **One** reference needs updating:
  - `test_backstop_actually_detects_the_known_gate_callers` (line ~714) — change the expected set to `{"control.force_crash", "systems.reprovision"}`.

  Do **not** touch `_BEHAVIOR_TESTS_BY_TOOL` (the coverage map near line 42/67): `control.power` stays a registered, `implemented` tool covered by `test_control_tools.py`, and `test_active_tools_have_a_covering_test` asserts `active == mapped` — removing its entry would red that test. Leave `test_destructive_hint_matches_reviewed_set` unchanged too — `control.power` keeps its `_docmeta.destructive()` annotation (spec-retained), so the destructive-hint set is unaffected.

- [ ] **Step 1c: Reclassify the exposure (discoverability) tier.** Tool *visibility* in `list_tools` and the generated RBAC matrix is driven by `src/kdive/mcp/exposure.py` (line 119: `"control.power": _OPERATOR`), separate from the handler's `require_role`. Without this, a contributor could invoke but not *see* `control.power` (contradicting ADR-0320). Change it to `_CONTRIBUTOR` and update the inline comment:

```python
    "control.power": _CONTRIBUTOR,  # leaseholder lifecycle over a READY transient VM (ADR-0320)
```

  In `tests/mcp/core/test_exposure.py`, move `"control.power"` out of `_ABOVE_CONTRIBUTOR` (line ~206) and add it to `_CONTRIBUTOR_LOOP` (line ~179). `test_operator_still_sees_the_whole_loop_and_above` recomputes from those frozensets, so it needs no manual edit. Leave `control.force_crash` in `_ABOVE_CONTRIBUTOR` (stays admin). `test_viewer_sees_reads_but_not_mutations` (line ~158, control.power still invisible to viewer) stays green.

- [ ] **Step 2: Run the tests, verify they fail.**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -q -k "power" && uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/core/test_exposure.py -q`
Expected: FAIL — the new contributor/CRASHED/viewer tests fail against current admin-gated behavior; the backstop equality assertion fails until Step 3 removes the gate branch; `test_exposure.py` fails until Step 1c's exposure edit lands.

- [ ] **Step 3: Implement the authz change in `control.py`.**
  - Delete constants `_POWER_ON_ACTIONS`, `_DESTRUCTIVE_POWER_ACTIONS`, `_STARTED_SYSTEM`, and the `_power_required_role` function.
  - In `power_system`, replace the `if power_action in _DESTRUCTIVE_POWER_ACTIONS: … else: require_role(...)` block with a single check, and narrow the state gate to `READY`:

```python
        async with pool.connection() as conn:
            system = await SYSTEMS.get(conn, uid)
            if system is None or system.project not in ctx.projects:
                return _config_error(system_id)
            require_role(ctx, system.project, Role.CONTRIBUTOR)
            if system.state is not SystemState.READY:
                return _config_error(system_id, data={"current_status": system.state.value})
```

  Keep everything below (idempotency, enqueue) unchanged. Leave `_authorize_destructive`, `_op_opt_in`, and the `resolver` param intact — `force_crash` still uses them. Remove the now-unused `PowerAction`-set imports only if they become unreferenced (`PowerAction` itself is still used).
  - Update the wrapper `Field` + docstring (agent-facing contract), lines ~309-319:

```python
        action: Annotated[
            str,
            Field(
                description=(
                    "Power action: `on`/`off`/`cycle`/`reset`. All require `contributor` "
                    "(leaseholder control over your transient VM). Use `reset`/`cycle` to "
                    "recover a wedged but READY guest. Admitted only on a READY System."
                )
            ),
        ],
```

```python
        """Power action on a READY System: on/off/cycle/reset, all contributor-level
        leaseholder control. reset/cycle recover a wedged READY guest. Refused on a
        non-READY System (a CRASHED System holds crash evidence — use the crash workflow).
        Enqueues a power job."""
```

  - Update the module docstring (lines 1-14) to drop "admin, ADR-0037 §1/§2" for power and state power is contributor lifecycle admitted on READY (cite ADR-0320). Leave the `force_crash` sentence (`admin` + two-check gate).

- [ ] **Step 4: Run the tests, verify they pass.**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -q -k "power" && uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/core/test_exposure.py -q`
Expected: PASS (control tests, gate-caller backstop, and exposure/visibility sets).

- [ ] **Step 5: Regenerate the generated docs, lint + type, then commit.**

`docs/guide/reference/control.md` is GENERATED from the wrapper docstring/`Field` (`scripts/gen_tool_reference.py`, header "do not edit. Regenerate: just docs"); the `docs/guide/safety-and-rbac.md` rbac-matrix region is generated from `exposure.py` (`just rbac-matrix`). Step 3's docstring change and Step 1c's exposure change make both stale, so regenerate both or `just docs-check`/`rbac-matrix-check` (in `just ci`) fail:

```bash
just docs
just rbac-matrix
just lint && just type
just docs-check rbac-matrix-check   # confirm no residual drift
git add src/kdive/mcp/tools/lifecycle/control.py src/kdive/mcp/exposure.py \
  tests/mcp/lifecycle/test_control_tools.py tests/mcp/core/test_tool_docs.py \
  tests/mcp/core/test_exposure.py docs/guide/reference/control.md docs/guide/safety-and-rbac.md
git commit -m "feat(control): power is contributor-level, READY-only (#1062)"
```

If `just rbac-matrix` also touches a snapshot test (`tests/scripts/test_gen_rbac_tool_matrix.py`), run it and update the expected fixture if it asserts the `control.power` row.

---

### Task 2: Worker-side READY re-check in `power_handler`

**Files:**
- Modify: `src/kdive/jobs/handlers/control.py` (`_control_target` / `power_handler`)
- Test: `tests/mcp/lifecycle/test_control_tools.py`

**Interfaces:**
- Consumes: `SystemState.READY`, `advisory_xact_lock(LockScope.SYSTEM, …)` (already used).
- Produces: `power_handler` fails the job terminally (raises `CategorizedError`, `configuration_error`) and does **not** call the provider's `power` when the System is not `READY` at execution.

- [ ] **Step 1: Write the failing test.** In `test_control_tools.py`, mirror `test_power_handler_calls_provider_and_audits` (lines ~387-418) **verbatim** for the wiring — the job is built via `queue.enqueue`, the controller fake is the local `_FakeControl` (records `.powered`), and the resolver is `provider_resolver(controller=ctrl)` (keyword-only). The only differences: seed `CRASHED`, expect a raise, and assert the provider was never called:

```python
def test_power_handler_refuses_non_ready_system(migrated_url: str) -> None:
    async def _run() -> None:
        async with _pool(migrated_url) as pool:
            alloc_id = await _granted_allocation(pool)
            sys_id = await _seed_system(pool, alloc_id, SystemState.CRASHED, domain_name="kdive-x")
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.POWER,
                    PowerPayload(system_id=sys_id, action=PowerAction.RESET),
                    {"principal": "user-1", "agent_session": "s", "project": "proj"},
                    f"{sys_id}:power:reset:{uuid4()}",
                )
            ctrl = _FakeControl()
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError) as exc:
                    await control_plane.power_handler(
                        conn, job, resolver=provider_resolver(controller=ctrl)
                    )
            assert exc.value.category is ErrorCategory.CONFIGURATION_ERROR
            assert exc.value.terminal is True  # dead-letters, does not retry (Req 1a)
            assert ctrl.powered == []  # physical power op never invoked

    asyncio.run(_run())
```

  Use `_granted_allocation` (the helper the existing power-handler test uses), not `_seed_alloc`; confirm the helper name against the neighbouring test before writing. The `terminal is True` assertion directly verifies the terminal contract without wiring the whole worker/`queue.fail` path.

- [ ] **Step 2: Run the test, verify it fails.**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py::test_power_handler_refuses_non_ready_system -q`
Expected: FAIL — current handler drives the domain regardless of state.

- [ ] **Step 3: Implement the re-check.** In `jobs/handlers/control.py`, make `_control_target` also enforce `READY` (it is used only by `power_handler`), reading state under the lock it already holds:

```python
async def _control_target(conn: AsyncConnection, system_id: UUID, *, op: str) -> _ControlTarget:
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise CategorizedError(
                f"{op} target system is gone",
                category=ErrorCategory.INFRASTRUCTURE_FAILURE,
                details={"system_id": str(system_id)},
            )
        if system.state is not SystemState.READY:
            raise CategorizedError(
                f"{op} requires a READY system; crash evidence on a non-READY system is "
                "protected from the power path",
                category=ErrorCategory.CONFIGURATION_ERROR,
                details={"system_id": str(system_id), "current_status": system.state.value},
                terminal=True,
            )
        return _ControlTarget(_resolved_domain_name(system), system.project)
```

  `terminal=True` is required (Req 1a: "fail the job terminally"): a power job whose System is no longer `READY` must dead-letter, not retry — the state will not improve on re-attempt, and re-reading `CRASHED` each retry is churn. This matches the `exc.terminal = True` idiom in `jobs/handlers/systems.py:277,334`. The physical `control.power` call at `power_handler` stays **after** this locked read (do not hold the lock across it). No other handler uses `_control_target`.

- [ ] **Step 4: Run the tests, verify they pass.**

Run: `uv run python -m pytest tests/mcp/lifecycle/test_control_tools.py -q -k "power_handler"`
Expected: PASS (both the new refusal test and the existing READY audit test).

- [ ] **Step 5: Lint + type, then commit.**

```bash
just lint && just type
git add src/kdive/jobs/handlers/control.py tests/mcp/lifecycle/test_control_tools.py
git commit -m "feat(control): power_handler refuses non-READY at execution (#1062)"
```

---

### Task 3: Drop POWER from the destructive set; decouple the write-validator

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py`, `src/kdive/services/systems/validation.py`
- Test: `tests/services/systems/test_system_validation.py`; gate test if any (`tests/security/**` — search for `DestructiveOp`)

**Interfaces:**
- Produces: `DESTRUCTIVE_JOB_KINDS = {REPROVISION, TEARDOWN, FORCE_CRASH}`; new `OPT_IN_DESTRUCTIVE_JOB_KINDS = frozenset({FORCE_CRASH, REPROVISION})` (exported). `_VALID_DESTRUCTIVE_OP_VALUES` derives from the new constant.

- [ ] **Step 1: Flip the failing validation tests.** In `test_system_validation.py` (~lines 185-200): change the `valid_destructive_ops` assertion to `["force_crash", "reprovision"]`; change `test_reject_unknown_destructive_ops_accepts_known_directly` to accept only `["force_crash", "reprovision"]`; add a case asserting `["power"]` and `["teardown"]` are each rejected with `unknown_destructive_ops` naming the token:

```python
@pytest.mark.parametrize("token", ["power", "teardown"])
def test_reject_unknown_destructive_ops_rejects_non_opt_in_tokens(token: str) -> None:
    with pytest.raises(CategorizedError) as exc:
        _reject_unknown_destructive_ops(_profile_with_ops([token]))
    assert exc.value.details["unknown_destructive_ops"] == [token]
    assert exc.value.details["valid_destructive_ops"] == ["force_crash", "reprovision"]
```

  Also add a gate test (find where `DestructiveOp` is unit-tested, e.g. `tests/security/authz/test_gate.py`): `DestructiveOp(kind=JobKind.POWER)` raises `ValueError`; `DestructiveOp(kind=JobKind.REPROVISION)` constructs.

- [ ] **Step 2: Run tests, verify they fail.**

Run: `uv run python -m pytest tests/services/systems/test_system_validation.py -q`
Expected: FAIL (current valid set includes power/teardown).

- [ ] **Step 3: Implement.** In `domain/operations/jobs.py`:

```python
DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.REPROVISION, JobKind.TEARDOWN, JobKind.FORCE_CRASH}
)
"""Job kinds gated by the destructive-operation admission gate (ADR-0130, ADR-0320)."""

OPT_IN_DESTRUCTIVE_JOB_KINDS: frozenset[JobKind] = frozenset(
    {JobKind.FORCE_CRASH, JobKind.REPROVISION}
)
"""Destructive ops whose opt-in factor is resolved from a profile's ``destructive_ops``
(ADR-0320). ``teardown`` is gated by role only (ADR-0129); ``power`` is not destructive."""
```

  Add `"OPT_IN_DESTRUCTIVE_JOB_KINDS"` to `__all__`. In `services/systems/validation.py`, change the import and derived set:

```python
from kdive.domain.operations.jobs import OPT_IN_DESTRUCTIVE_JOB_KINDS

_VALID_DESTRUCTIVE_OP_VALUES = frozenset(kind.value for kind in OPT_IN_DESTRUCTIVE_JOB_KINDS)
```

  Update the `_reject_unknown_destructive_ops` docstring to cite ADR-0320 and say the accepted set is the opt-in-consuming kinds.

- [ ] **Step 4: Run tests, verify they pass.**

Run: `uv run python -m pytest tests/services/systems/test_system_validation.py tests/security tests/mcp/lifecycle/test_systems_tools.py::test_reprovision_without_profile_opt_in_denied -q`
Expected: PASS (the reprovision-opt-in regression guard confirms narrowing `destructive_ops`'s scope did not drop reprovision).

- [ ] **Step 5: Lint + type, then commit.**

```bash
just lint && just type
git add src/kdive/domain/operations/jobs.py src/kdive/services/systems/validation.py \
  tests/services/systems/test_system_validation.py tests/security
git commit -m "feat(control): power leaves destructive set; validator accepts opt-in kinds (#1062)"
```

---

### Task 4: Profile docstrings + `profile_examples` destructive_ops note

**Files:**
- Modify: `src/kdive/profiles/provisioning.py` (2 `destructive_ops` class docstrings — local-libvirt ~115, remote ~164; the fault-inject section omits it, leave unchanged), `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py`
- Test: the existing profile-examples test (find via `rg -l profile_examples tests/`)

**Interfaces:**
- Produces: `profile_examples` items carry a note naming `destructive_ops` scope (force_crash + reprovision).

- [ ] **Step 1: Write the failing test.** Locate the profile-examples test and add an assertion that the emitted note mentions `destructive_ops` and `force_crash`:

```python
def test_local_example_note_documents_destructive_ops_scope(...) -> None:
    resp = build_profile_examples(doc, frozenset({ResourceKind.LOCAL_LIBVIRT}))
    note = resp.items[0].data["note"]  # match the actual accessor used by neighbours
    assert "destructive_ops" in note
    assert "force_crash" in note and "reprovision" in note
```

  Match the exact response accessor pattern used by existing tests in that file.

- [ ] **Step 2: Run it, verify it fails.**

Run: `uv run python -m pytest <profile_examples_test_path> -q -k destructive`
Expected: FAIL — the note does not mention `destructive_ops` yet.

- [ ] **Step 3: Implement.** In `profile_examples.py`, append one sentence to `_REPLACE_NOTE`:

```python
    "The provider destructive_ops list opts into force_crash (deliberate kernel crash / "
    "fault injection) and reprovision only — leave it empty unless you need those; "
    "power/reboot no longer require it (ADR-0320)."
```

  In `provisioning.py`, update the **two** class docstrings that describe `destructive_ops` — the local-libvirt section (~line 115-119) and `RemoteLibvirtProfile` (~line 164-165) — to: "opts into `force_crash` and `reprovision` (deny-by-default); power is contributor lifecycle and is not gated by it (ADR-0320)." The `FaultInjectProfile` class docstring does not mention `destructive_ops` and its field is a bare `Field(default_factory=list)`; leave it unchanged (no third docstring exists).

- [ ] **Step 4: Run it, verify it passes.**

Run: `uv run python -m pytest <profile_examples_test_path> -q`
Expected: PASS.

- [ ] **Step 5: Lint + type, then commit.**

```bash
just lint && just type
git add src/kdive/profiles/provisioning.py src/kdive/mcp/tools/lifecycle/systems/profile_examples.py <profile_examples_test_path>
git commit -m "docs(profiles): surface destructive_ops scope in profile_examples (#1062)"
```

---

### Task 5: Prose docs — gate docstring, design doc, guides, recovery note

**Files:**
- Modify: `src/kdive/security/authz/gate.py` (module docstring), `docs/design/destructive-gate-per-op-revision.md`, hand-authored `docs/guide/**` control-power role prose, recovery note in `docs/guide/toolsets/control.md`
- Regenerate (do NOT hand-edit): the `toolsets-control.md` doc-resource snapshot (`just resources-docs`). `docs/guide/reference/control.md` and the `docs/guide/safety-and-rbac.md` rbac-matrix region are already regenerated in Task 1 — do not touch them here.

**Interfaces:** none (docs only).

**Generated-file caution:** several `docs/guide/**` files (or regions) are generator-owned and checked by `just ci` (`docs-check`, `resources-docs-check`, `rbac-matrix-check`). Never hand-edit them; regenerate and commit the output. Specifically: `docs/guide/reference/control.md` (header "do not edit"), the `<!-- BEGIN GENERATED: rbac-tool-matrix --> … <!-- END GENERATED -->` region of `docs/guide/safety-and-rbac.md` (lines ~37-189), and any `docs/guide/toolsets/*.md` that backs a `DOC_RESOURCES` snapshot.

- [ ] **Step 1: Update the gate docstring.** In `gate.py`, the header lists `force_crash/power` as admin destructive ops. Change to name `force_crash` (admin) and `reprovision` (operator) as the gated ops; state power is no longer gated (ADR-0320). Keep it accurate to the two-check mechanism.

- [ ] **Step 2: Update `docs/design/destructive-gate-per-op-revision.md`.** Its "affected behavior" table row for `control.power off/cycle/reset` (admin + power-in-destructive_ops) is now stale. Replace that row with a note that power is reclassified to `contributor` lifecycle (READY-only) by ADR-0320, and remove `power` from the opt-in column; add a one-line "Superseded in part by ADR-0320" pointer near the table.

- [ ] **Step 3: Audit and update hand-authored guide prose.** Run `rg -n "control.power|off/cycle/reset|power.*admin" docs/guide/`. Update only **hand-authored** prose stating the power role — change "admin"/"operator" for power to "contributor" and note READY-only admission; do not change `force_crash` text. **Skip** `docs/guide/reference/control.md` and the `<!-- BEGIN/END GENERATED: rbac-tool-matrix -->` region of `docs/guide/safety-and-rbac.md` — both were regenerated in Task 1. For `safety-and-rbac.md`, edit only hand-authored prose OUTSIDE those markers (there may be none — the power role there is matrix-driven).

- [ ] **Step 4: Add the wedged-guest recovery note.** In `docs/guide/toolsets/control.md` (the hand-authored source that backs the `toolsets-control.md` doc-resource snapshot), add a short "Recovering a wedged guest" note: `control.power reset` (contributor) recovers a wedged **READY** guest; if it will not respond, or the System is not READY (wedged before boot, or CRASHED), fall back to `runs.install` (changed cmdline) + `runs.boot`, and for a CRASHED System use `capture_vmcore` → `teardown`/`reprovision`. Also apply the Step-3 power-role prose update here.

- [ ] **Step 5: Regenerate the generated docs, run doc guardrails, then commit.** The `toolsets/control.md` edit invalidates its packaged snapshot, and the exposure-derived rbac matrix must be re-run in case anything shifted:

```bash
just resources-docs          # regenerate the toolsets-control.md doc-resource snapshot
just check-mermaid
# Verify the generated-doc checks are green (these run inside `just ci`):
just docs-check resources-docs-check rbac-matrix-check
rg -n "Sprint|critical|robust|comprehensive|elegant" src/kdive/security/authz/gate.py docs/design/destructive-gate-per-op-revision.md docs/guide/toolsets/control.md
git add src/kdive/security/authz/gate.py docs/design/destructive-gate-per-op-revision.md docs/guide
git commit -m "docs(control): power is contributor lifecycle; recovery note (#1062)"
```

If any `*-check` reports drift, run the matching `just docs`/`just resources-docs`/`just rbac-matrix`, `git add` the regenerated file, and re-check before committing.

---

### Task 6: Full guardrail suite

**Files:** none (verification).

- [ ] **Step 1: Run the full gate.**

Run: `just ci`
Expected: PASS (lint, type, lint-shell, lint-workflows, check-mermaid, test).

- [ ] **Step 2: Grep for stragglers.** Confirm no remaining source/doc claims that power is admin-gated or that `destructive_ops` includes power:

Run: `rg -n "power.*(admin|destructive_ops)|_STARTED_SYSTEM|off/cycle/reset.*admin" src/ docs/guide docs/design`
Expected: only `force_crash`/historical-ADR references remain; no live claim that power is admin-gated.

- [ ] **Step 3: If all green, the branch is ready for review.** No commit (verification only).

## Self-Review notes

- **Spec coverage:** Req 1/1a → Tasks 1+2 (invoke role + exposure tier + READY admission + worker re-check); Req 2 (force_crash unchanged) → untouched, asserted by existing green tests; Req 3 + taxonomy + validator → Task 3; Req 4 (contract) → Task 1 Step 3; Req 5 (profile_examples) → Task 4; Req 6 (recovery note) → Task 5 Step 4; exposure/visibility (spec Authorization §) → Task 1 Step 1c; docs list → Tasks 1+5.
- **Reprovision opt-in regression** (spec test plan): already covered by the existing green test `tests/mcp/lifecycle/test_systems_tools.py::test_reprovision_without_profile_opt_in_denied` (a profile lacking `"reprovision"` is denied `profile_opt_in`). Since Task 3 keeps `REPROVISION` in `OPT_IN_DESTRUCTIVE_JOB_KINDS` and `_reprovision_opt_in` is unchanged, that test must stay green — no new test needed; run it in Task 3 Step 4 as a guard: `uv run python -m pytest tests/mcp/lifecycle/test_systems_tools.py::test_reprovision_without_profile_opt_in_denied -q`.
- **Ordering:** Task 1 (removes the power→gate construction) precedes Task 3 (removes `POWER` from `DESTRUCTIVE_JOB_KINDS`) so no code constructs `DestructiveOp(POWER)` against a set that no longer contains it. Task 2 depends on nothing but is grouped with the handler. Tasks 4–5 are docs.
- **Residual (#1078):** not implemented here; Task 5 keeps the design doc honest about scope.

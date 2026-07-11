# Provision lane + reprovision → contributor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclassify `systems.define`/`provision`/`provision_defined`, `artifacts.create_system_upload`, and `systems.reprovision` from `operator` to `contributor` leaseholder control, dropping `reprovision`'s destructive-op gate and `destructive_ops` opt-in.

**Architecture:** The two-place authz move (exposure map + runtime handler gate) established by ADR-0320, plus a taxonomy cleanup that removes `REPROVISION` from the destructive families and keeps the `jobs.cancel` allow-list honest. No DB migration.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. Spec: `docs/superpowers/specs/2026-07-10-provision-lane-contributor-1081-design.md`. ADR: `docs/adr/0326-provision-lane-contributor-lifecycle.md`.

## Global Constraints

- Branch: `feat/provision-lane-contributor-1081` off `main`. Never commit to `main`.
- Guardrails: `just lint`, `just type` (whole tree), `just test`; full gate `just ci` (adds lint-shell, lint-workflows, check-mermaid). Generated-doc gates: `just rbac-matrix-check`, `just docs-check`.
- ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict; zero-warning policy (no unused imports).
- No ADR references in any text that serializes into a tool schema (wrapper docstrings / `Field` descriptions) — `test_no_adr_leak`.
- Doc-style guard: **Milestone** not "Sprint"; no "critical/robust/comprehensive/elegant".
- Conventional-commit subjects ≤72 chars; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Stage explicit paths only — never `git add -A`.

---

### Task 1: Taxonomy — reprovision leaves the destructive families; provision lane becomes contributor-cancelable

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py:39-76` (three frozensets + docstrings)
- Test: `tests/mcp/jobs/test_jobs_tools.py:231-244` (cancel guard), `tests/services/systems/test_system_validation.py:181-206` (token validator)

**Interfaces:**
- Produces: `DESTRUCTIVE_JOB_KINDS = {TEARDOWN, FORCE_CRASH}`, `OPT_IN_DESTRUCTIVE_JOB_KINDS = {FORCE_CRASH}`, `CONTRIBUTOR_CANCELABLE_JOB_KINDS` gains `PROVISION`, `REPROVISION`. `services/systems/validation.py::_VALID_DESTRUCTIVE_OP_VALUES` derives from `OPT_IN_DESTRUCTIVE_JOB_KINDS` (auto-narrows to `{"force_crash"}` — no edit there).

- [ ] **Step 1: Flip the guard test** — in `tests/mcp/jobs/test_jobs_tools.py`, change the assertion `assert JobKind.PROVISION not in CONTRIBUTOR_CANCELABLE_JOB_KINDS  # provision lane out of scope` to assert PROVISION and REPROVISION **are** members, and update the L235 comment (drop "the operator-gated provision lane"):

```python
    assert JobKind.PROVISION in CONTRIBUTOR_CANCELABLE_JOB_KINDS  # leaseholder provision lane (#1081)
    assert JobKind.REPROVISION in CONTRIBUTOR_CANCELABLE_JOB_KINDS  # leaseholder reprovision (#1081)
```
Keep the `not CONTRIBUTOR_CANCELABLE_JOB_KINDS & DESTRUCTIVE_JOB_KINDS` line — it still holds because REPROVISION also leaves `DESTRUCTIVE_JOB_KINDS`.

- [ ] **Step 2: Flip the validator tests** — in `tests/services/systems/test_system_validation.py`: `test_reject_unknown_destructive_ops_accepts_known_directly` should accept only `["force_crash"]`; the `valid_destructive_ops` expectation (currently `["force_crash", "reprovision"]`) becomes `["force_crash"]`; and `test_reject_unknown_destructive_ops_rejects_non_opt_in_tokens` must add `"reprovision"` to its rejected `token` params (alongside `power`/`teardown`).

- [ ] **Step 3: Run tests to verify they fail**

Run: `just test 2>/dev/null` or targeted `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py::test_cancel_role_classification_covers_every_kind_and_fails_closed tests/services/systems/test_system_validation.py -q`
Expected: FAIL (frozensets not yet changed).

- [ ] **Step 4: Change the frozensets** — in `src/kdive/domain/operations/jobs.py`:
  - `DESTRUCTIVE_JOB_KINDS`: remove `JobKind.REPROVISION` → `frozenset({JobKind.TEARDOWN, JobKind.FORCE_CRASH})`.
  - `OPT_IN_DESTRUCTIVE_JOB_KINDS`: remove `JobKind.REPROVISION` → `frozenset({JobKind.FORCE_CRASH})`.
  - `CONTRIBUTOR_CANCELABLE_JOB_KINDS`: add `JobKind.PROVISION` and `JobKind.REPROVISION`.
  - Update all three docstrings: reprovision is now contributor leaseholder lifecycle (like power, #1081); the opt-in set governs only `force_crash`; the cancelable set now includes the provision lane a contributor can enqueue. No ADR ref requirement here (module-internal), but keep prose plain.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/domain/operations/jobs.py tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py
git commit -m "feat(security): reprovision leaves destructive families; provision lane cancelable (#1081)"
```

---

### Task 2: Exposure map — five tools become contributor-visible

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (L115, L219-222: `_OPERATOR → _CONTRIBUTOR`)
- Test: `tests/mcp/core/test_exposure.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: `required_scopes("systems.provision"|"systems.define"|"systems.provision_defined"|"systems.reprovision"|"artifacts.create_system_upload") == {PROJECT_CONTRIBUTOR}`.

- [ ] **Step 1: Update the exposure tests** — in `tests/mcp/core/test_exposure.py`:
  - Remove `systems.define`, `systems.provision`, `artifacts.create_system_upload` from the `_ABOVE_CONTRIBUTOR` frozenset (leave `images.upload`, `systems.teardown`, `control.force_crash`). Add the five tools to the contributor-visible set (`_CONTRIBUTOR_LOOP` or the appropriate positive set — follow the file's structure; `control.power` is the ADR-0320 precedent entry to mirror).
  - Rewrite `test_create_system_upload_stays_operator_but_run_upload_drops`: both `create_run_upload` and `create_system_upload` now assert `PROJECT_CONTRIBUTOR`. Rename it (e.g. `test_both_upload_kinds_are_contributor`).
  - Rewrite `test_project_tool_visible_honours_role_on_the_named_project`: `project_tool_visible("systems.provision", contributor, "a")` is now **True**; keep a still-operator tool (e.g. `images.upload`) to prove the per-project gate still discriminates, or adjust the assertion target.
  - Check `test_project_tool_visible_is_per_project_not_connection_union` (uses provision) and fix its expectation to contributor semantics.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: FAIL.

- [ ] **Step 3: Flip the exposure constants** — in `src/kdive/mcp/exposure.py`, change `_OPERATOR` → `_CONTRIBUTOR` for `artifacts.create_system_upload` (L115), `systems.define` (L219), `systems.provision` (L220), `systems.provision_defined` (L221), `systems.reprovision` (L222). Leave `systems.provision` in `CORE_TOOLS`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/exposure.py tests/mcp/core/test_exposure.py
git commit -m "feat(security): expose provision lane + reprovision to contributor (#1081)"
```

---

### Task 3: Runtime handler gates — the real authorization boundary

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (`required_role` at the `define`/`provision`/`provision_defined`/`reprovision` `with_runtime_for_*` sites — L187/241/279/469)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/uploads.py:374` (`_SYSTEM_UPLOAD.required_role`)
- Test: `tests/security/authz/test_rbac.py`, `tests/integration/test_systems_define_upload_provision.py`, plus any provision/define authz assertion in `tests/mcp/lifecycle/`

**Interfaces:**
- Consumes: nothing.
- Produces: all four `systems.*` tools and the system-upload seam enforce `Role.CONTRIBUTOR` at `require_role` (via `_runtime_resolution._authorized_kind`).

- [ ] **Step 1: Write/adjust the failing behavior tests** — find the tests asserting operator-required on these tools (grep `Role.OPERATOR`/`operator` in `tests/security/authz/` and `tests/integration/test_systems_define_upload_provision.py`). Change them to assert a **contributor** succeeds and a **viewer** is denied (`RoleDenied`/authz error). If a dedicated contributor-vs-viewer case does not exist for define/provision/provision_defined, add one following the existing operator-case pattern in the same file.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/security/authz/test_rbac.py tests/integration/test_systems_define_upload_provision.py -q`
Expected: FAIL (handlers still require operator).

- [ ] **Step 3: Flip the handler gates**
  - `registrar.py`: change `required_role=Role.OPERATOR` → `required_role=Role.CONTRIBUTOR` at the four `with_runtime_for_allocation`/`with_runtime_for_system` call sites for define (L187), provision (L241), provision_defined (L279), reprovision (L469).
  - `uploads.py:374`: `_SYSTEM_UPLOAD` `required_role=Role.OPERATOR` → `required_role=Role.CONTRIBUTOR`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/security/authz/ tests/integration/test_systems_define_upload_provision.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/registrar.py src/kdive/mcp/tools/catalog/artifacts/uploads.py tests/security/authz/test_rbac.py tests/integration/test_systems_define_upload_provision.py
git commit -m "feat(security): lower provision-lane + upload handler gates to contributor (#1081)"
```

---

### Task 4: Reprovision — remove the destructive gate and opt-in

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/admin.py` (remove `DestructiveOp` construction + `assert_destructive_allowed` + `DestructiveOpDenied` branch at L161-168; delete `_reprovision_opt_in` L197-199; remove unused gate imports at L42 and the `_REPROVISION` alias L51 if unused; **keep** `_audit_destructive_denied` L202 and `_authz_denied` — `teardown_system` L337-338 uses both)
- Test: `tests/security/authz/test_gate.py` (remove/adjust reprovision-through-gate cases), reprovision behavior test (contributor + empty `destructive_ops` on READY succeeds)

**Interfaces:**
- Consumes: Task 3's `require_role(CONTRIBUTOR)` at the registrar layer (the sole path to `_reprovision_in_lock`).
- Produces: `reprovision` gated only by contributor role + `READY`-only + no-live-run.

- [ ] **Step 1: Write the failing behavior test** — a contributor reprovisioning a `READY` System whose profile has **empty** `destructive_ops` succeeds (previously denied for missing opt-in). Follow the existing reprovision test fixtures in `tests/mcp/lifecycle/` / `tests/services/systems/`. Also assert a non-`READY` System still returns `configuration_error` and a live-run System is refused (guards preserved).

- [ ] **Step 2: Run to verify failure**

Run target the new test.
Expected: FAIL (gate still requires opt-in).

- [ ] **Step 3: Remove the gate machinery** — in `admin.py::_reprovision_in_lock`, delete the `op = DestructiveOp(...)`, the `try/except DestructiveOpDenied` around `assert_destructive_allowed`, and the denial return; the flow proceeds from project resolution straight to the `REPROVISIONING` dedup / `READY`-only / no-live-run guards. Delete `_reprovision_opt_in`. Remove the now-unused imports `DestructiveOp, DestructiveOpDenied, assert_destructive_allowed` (L42) and the `_REPROVISION` alias (L51) **only if** `ruff`/`ty` report them unused (they are distinct from `_REPROVISION_KIND`).

- [ ] **Step 4: Adjust the gate tests** — in `tests/security/authz/test_gate.py`, remove or repoint any case that drives `REPROVISION` through `assert_destructive_allowed`/`DestructiveOp` (the gate now governs only `force_crash`). Confirm `force_crash` gate cases are untouched.

- [ ] **Step 5: Run to verify pass + lint**

Run: `uv run python -m pytest tests/security/authz/test_gate.py <reprovision-test> -q && just lint && just type`
Expected: PASS, no unused-import warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/admin.py tests/security/authz/test_gate.py <reprovision-test-file>
git commit -m "feat(security): drop reprovision destructive gate + opt-in (#1081)"
```

---

### Task 5: Agent-facing contract — docstrings, Field, advertisements

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py:167,221,265,450` (wrapper docstrings) and `:442` (reprovision `profile` Field)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py:252` (create_system_upload wrapper docstring)
- Modify: `src/kdive/mcp/tools/jobs.py` (`jobs_cancel` wrapper docstring ~L406-411 and module docstring ~L11-16)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py:81` and `src/kdive/profiles/provisioning.py:116,165` (strike `reprovision` from `destructive_ops` advertisements)
- Test: `test_no_adr_leak` (already covered by suite) — must stay green

**Interfaces:** none produced; text-only.

- [ ] **Step 1: Update the five wrapper docstrings + the reprovision Field**
  - `registrar.py:167/221/265` (define/provision/provision_defined): replace "Operator only"/"Requires operator" with a contributor statement.
  - `registrar.py:450` (reprovision): replace "Requires operator and opt-in." with contributor, no opt-in.
  - `registrar.py:442` (reprovision `profile` Field): replace `"New provisioning profile; must opt in to reprovision."` with e.g. `"New provisioning profile to re-stage on the READY System."` (no opt-in claim).
  - `catalog/artifacts/registrar.py:252`: replace "Requires operator." with contributor.
  - No ADR refs in any of this text.

- [ ] **Step 2: Update jobs.cancel contract** — in `mcp/tools/jobs.py`, move `provision`/`reprovision` out of the "requires operator" sentence in both the `jobs_cancel` wrapper docstring and the module docstring; leave `teardown`/`force_crash` (and platform/internal kinds) as operator-only.

- [ ] **Step 3: Strike reprovision from destructive_ops advertisements**
  - `profile_examples.py:81`: the text "...reprovision only; leave it empty..." → advertise `force_crash` only.
  - `provisioning.py:116` and `:165`: remove `reprovision` so the field docstrings name `force_crash` as the sole opt-in token. (Leave `profile_examples.py:79` "without reprovisioning" and `provisioning.py:425` dedup-factor doc untouched.)

- [ ] **Step 4: Run the contract/schema tests**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/lifecycle/test_systems_profile_examples.py -q -k "adr or doc or example" ; just test 2>&1 | tail -3`
Expected: PASS (`test_no_adr_leak` green).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/registrar.py src/kdive/mcp/tools/catalog/artifacts/registrar.py src/kdive/mcp/tools/jobs.py src/kdive/mcp/tools/lifecycle/systems/profile_examples.py src/kdive/profiles/provisioning.py
git commit -m "docs(security): update provision-lane agent-facing contract to contributor (#1081)"
```

---

### Task 6: Regenerate generated docs

**Files:**
- Modify (generated): `docs/guide/safety-and-rbac.md`, the agent tool reference (via `just docs`), any doc-resource snapshots

- [ ] **Step 1: Regenerate**

Run: `just rbac-matrix && just docs`

- [ ] **Step 2: Verify the RBAC matrix now shows contributor for the five tools** — inspect the diff of `docs/guide/safety-and-rbac.md`; the five tools' rows move to contributor.

- [ ] **Step 3: Verify generated-doc gates**

Run: `just rbac-matrix-check && just docs-check`
Expected: both report in-sync.

- [ ] **Step 4: Commit**

```bash
git add docs/guide/safety-and-rbac.md docs/  # only the regenerated files that changed
git commit -m "docs(security): regenerate RBAC matrix + tool reference for #1081"
```

---

### Task 7: Full guardrail sweep

- [ ] **Step 1: Run the full gate**

Run: `just ci`
Expected: green (lint, type, lint-shell, lint-workflows, check-mermaid, test, and the generated-doc checks gated by `just test`).

- [ ] **Step 2: If any check is red, fix and fold the fix into the owning task's commit (or a `fix` commit), then re-run `just ci`.**

---

## Self-Review

- **Spec coverage:** Task 1 ↔ spec §4/§5 (taxonomy + validator) & §4 test-flip; Task 2 ↔ §1 (exposure); Task 3 ↔ §2 (gates); Task 4 ↔ §3 (reprovision gate removal, dead-import cleanup); Task 5 ↔ §6 (all agent-facing surfaces incl. jobs.cancel, profile_examples, provisioning, the :442 Field); Task 6 ↔ §7 (generated docs). Success criteria 1-6 map to Tasks 2/3 (1), 4 (2), 1 (3), 1 (4), untouched force_crash (5), 7 (6).
- **No migration:** confirmed — every change is a role constant, a frozenset, or text.
- **Ordering:** taxonomy first (Task 1) so the validator/cancel behavior is settled before the surface flips; docstrings (Task 5) before doc regen (Task 6) so the tool reference regenerates from corrected text.
- **Rollback:** revert the branch; no data/external state touched.

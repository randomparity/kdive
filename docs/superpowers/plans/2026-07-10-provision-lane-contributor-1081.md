# Provision lane + reprovision → contributor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reclassify `systems.define`/`provision`/`provision_defined`, `artifacts.create_system_upload`, and `systems.reprovision` from `operator` to `contributor` leaseholder control, dropping `reprovision`'s destructive-op gate and `destructive_ops` opt-in.

**Architecture:** The exposure-map + runtime-handler-gate move established by ADR-0320, plus a taxonomy cleanup that removes `REPROVISION` from the destructive families and keeps the `jobs.cancel` allow-list honest. The provision lane is gated in two enforcing layers (runtime-resolution wrapper + admission service) — both move. No DB migration.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. Spec: `docs/superpowers/specs/2026-07-10-provision-lane-contributor-1081-design.md`. ADR: `docs/adr/0326-provision-lane-contributor-lifecycle.md`.

## Global Constraints

- Branch: `feat/provision-lane-contributor-1081` off `main`. Never commit to `main`.
- Guardrails: `just lint`, `just type` (whole tree), `just test`; full gate `just ci`. Generated-doc gates: `just rbac-matrix-check`, `just docs-check` (both also gated by `just test`).
- ruff line length 100; lint set `E,F,I,UP,B,SIM`; `ty` strict; zero-warning policy (drop unused imports).
- No ADR references in wrapper docstrings / `Field` descriptions (`test_no_adr_leak`).
- Doc-style guard: **Milestone** not "Sprint"; no "critical/robust/comprehensive/elegant".
- Conventional-commit subjects ≤72 chars; end every commit body with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Stage explicit paths only — never `git add -A`. Each task's commit must leave `just ci` green (bisectable history).

---

### Task 1: Reprovision declassification (ATOMIC)

Removing `REPROVISION` from `DESTRUCTIVE_JOB_KINDS` makes `DestructiveOp(REPROVISION)` raise `ValueError` in `gate.py.__post_init__`, and adding it to `CONTRIBUTOR_CANCELABLE_JOB_KINDS` violates the guard `not (CANCELABLE & DESTRUCTIVE)` until the destructive-set edit lands. So the taxonomy edits, the `admin.py` gate removal, and every dependent test **must ship in one commit**.

**Files:**
- Modify: `src/kdive/domain/operations/jobs.py:39-76` (three frozensets + docstrings)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/admin.py` (remove gate machinery L161-168; add in-handler `require_role`; delete `_reprovision_opt_in` L197-199; drop unused gate imports L42 + `_REPROVISION` alias L51 if unused; **keep** `_audit_destructive_denied` L202 / `_authz_denied` — `teardown_system` L337-338 uses both)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py:469` (reprovision wrapper `required_role`)
- Test: `tests/mcp/jobs/test_jobs_tools.py` (cancel guard), `tests/services/systems/test_system_validation.py:181-206` (token validator), `tests/security/authz/test_gate.py:78,103,115` (reprovision gate constructs), `tests/mcp/lifecycle/test_systems_tools.py` (reprovision behavior + `_active_allocation_profile` fixture)

**Interfaces:**
- Produces: `DESTRUCTIVE_JOB_KINDS = {TEARDOWN, FORCE_CRASH}`; `OPT_IN_DESTRUCTIVE_JOB_KINDS = {FORCE_CRASH}`; `CONTRIBUTOR_CANCELABLE_JOB_KINDS` gains `PROVISION`, `REPROVISION`. `validation._VALID_DESTRUCTIVE_OP_VALUES` auto-narrows to `{"force_crash"}`. Reprovision handler enforces `require_role(ctx, system.project, Role.CONTRIBUTOR)`.

- [ ] **Step 1: Update the tests (make them describe the target state)**
  - `test_jobs_tools.py`: change `assert JobKind.PROVISION not in CONTRIBUTOR_CANCELABLE_JOB_KINDS` → assert `PROVISION` and `REPROVISION` **are** members; update the L235 comment (drop "operator-gated provision lane"). Keep `assert not CONTRIBUTOR_CANCELABLE_JOB_KINDS & DESTRUCTIVE_JOB_KINDS` (still true — REPROVISION also leaves DESTRUCTIVE).
  - `test_system_validation.py`: `test_reject_unknown_destructive_ops_accepts_known_directly` accepts only `["force_crash"]`; the `valid_destructive_ops` expectation → `["force_crash"]`; add `"reprovision"` to `test_reject_unknown_destructive_ops_rejects_non_opt_in_tokens`'s rejected `token` params.
  - `test_gate.py`: the reprovision-through-gate cases (`DestructiveOp(kind=JobKind.REPROVISION,...)` at L78/103/115) — remove them or repoint to `force_crash`; the L78 test `test_power_is_no_longer_a_destructive_job_kind` asserts REPROVISION still constructs, so update it to assert REPROVISION **no longer** constructs (raises `ValueError`), leaving `force_crash`/`teardown` as the members.
  - `test_systems_tools.py`:
    - `_active_allocation_profile()` (L1461-1464): drop the `destructive_ops = ["reprovision"]` line (leave the list empty) so the fixture is a valid contributor profile.
    - `test_reprovision_operator_may_invoke` (L1649): rename/repoint to a **contributor** actor (`_ctx(Role.CONTRIBUTOR)`) that still gets `queued`.
    - `test_reprovision_without_profile_opt_in_denied` (L1716): repurpose into "contributor with empty destructive_ops succeeds" (was: operator without opt-in → denied).
    - `test_reprovision_viewer_denied` (L1662) and `test_reprovision_viewer_denied_before_provider_rootfs_validation` (L1683): keep asserting a **viewer** is denied — now enforced by the new in-handler `require_role`; adjust the expectation to the shape `require_role` produces (raised `AuthorizationError`/`RoleDenied` vs the old `authorization_denied` envelope) to match how the handler surfaces it. Drop the `destructive_ops = ["reprovision"]` line at L1695.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py -q`
Expected: FAIL.

- [ ] **Step 3: Edit the taxonomy** — `domain/operations/jobs.py`: `DESTRUCTIVE_JOB_KINDS → frozenset({JobKind.TEARDOWN, JobKind.FORCE_CRASH})`; `OPT_IN_DESTRUCTIVE_JOB_KINDS → frozenset({JobKind.FORCE_CRASH})`; add `JobKind.PROVISION`, `JobKind.REPROVISION` to `CONTRIBUTOR_CANCELABLE_JOB_KINDS`. Update the three docstrings (reprovision is now contributor leaseholder lifecycle; opt-in governs only `force_crash`; cancelable set includes the provision lane).

- [ ] **Step 4: Remove the reprovision gate + add in-handler role check** — `admin.py::_reprovision_in_lock`: delete `op = DestructiveOp(...)`, the `try/except DestructiveOpDenied` + `assert_destructive_allowed` + denial return (L161-168); insert `require_role(ctx, system.project, Role.CONTRIBUTOR)` where the gate stood (after the system/allocation project-ownership resolution, before the `REPROVISIONING` dedup). Delete `_reprovision_opt_in` (L197-199). Flip `registrar.py:469` `required_role=Role.OPERATOR → Role.CONTRIBUTOR`. Run `just lint` and drop the now-unused imports `DestructiveOp, DestructiveOpDenied, assert_destructive_allowed` (L42) and the `_REPROVISION` alias (L51) if ruff reports them unused (distinct from `_REPROVISION_KIND`, which stays).

- [ ] **Step 5: Run to verify pass + lint/type**

Run: `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py -q && just lint && just type`
Expected: PASS, no warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/domain/operations/jobs.py src/kdive/mcp/tools/lifecycle/systems/admin.py src/kdive/mcp/tools/lifecycle/systems/registrar.py tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py
git commit -m "feat(security): reprovision is contributor leaseholder lifecycle (#1081)"
```

---

### Task 2: Provision-lane + upload handler gates (both enforcing layers)

**Files:**
- Modify: `src/kdive/services/systems/admission.py:419` (create_for_allocation) and `:621` (provision_defined) — `Role.OPERATOR → Role.CONTRIBUTOR`
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py:187,241,279` (define/provision/provision_defined wrapper `required_role`)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/uploads.py:374` (`_SYSTEM_UPLOAD.required_role`)
- Test: `tests/security/authz/test_rbac.py`, `tests/integration/test_systems_define_upload_provision.py`, `tests/mcp/lifecycle/test_create_upload_tool.py:756`, any handler-direct provision/define authz case in `tests/services/systems/test_admission.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces: define/provision/provision_defined and the system-upload seam admit `Role.CONTRIBUTOR` at both the wrapper and the admission service.

- [ ] **Step 1: Update the failing behavior tests** — grep `Role.OPERATOR`/`operator` in `tests/security/authz/` and the named files. Change the operator-required cases for these four tools to **contributor succeeds / viewer denied**. Specifically flip `test_create_upload_tool.py::test_create_system_upload_still_requires_operator` (L756) — a contributor now succeeds; add/keep a viewer-denied case. Do the same for any `test_admission.py` case asserting operator on `create_for_allocation`/`provision_defined`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/security/authz/test_rbac.py tests/integration/test_systems_define_upload_provision.py tests/mcp/lifecycle/test_create_upload_tool.py tests/services/systems/test_admission.py -q`
Expected: FAIL.

- [ ] **Step 3: Flip both gate layers**
  - `admission.py`: `require_role(ctx, alloc.project, Role.OPERATOR)` → `Role.CONTRIBUTOR` (L419); `require_role(ctx, system.project, Role.OPERATOR)` → `Role.CONTRIBUTOR` (L621).
  - `registrar.py`: `required_role=Role.OPERATOR → Role.CONTRIBUTOR` at define (L187), provision (L241), provision_defined (L279).
  - `uploads.py:374`: `_SYSTEM_UPLOAD` `required_role=Role.OPERATOR → Role.CONTRIBUTOR`.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/security/authz/ tests/integration/test_systems_define_upload_provision.py tests/mcp/lifecycle/test_create_upload_tool.py tests/services/systems/test_admission.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/services/systems/admission.py src/kdive/mcp/tools/lifecycle/systems/registrar.py src/kdive/mcp/tools/catalog/artifacts/uploads.py tests/security/authz/test_rbac.py tests/integration/test_systems_define_upload_provision.py tests/mcp/lifecycle/test_create_upload_tool.py tests/services/systems/test_admission.py
git commit -m "feat(security): lower provision-lane + upload handler gates to contributor (#1081)"
```

---

### Task 3: Exposure map — five tools become contributor-visible

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (L115, L219-222: `_OPERATOR → _CONTRIBUTOR`; and the stale jobs.cancel comment L173-174)
- Test: `tests/mcp/core/test_exposure.py`

**Interfaces:**
- Produces: `required_scopes(<each of the five tools>) == {PROJECT_CONTRIBUTOR}`.

- [ ] **Step 1: Update the exposure tests**
  - Remove `systems.define`, `systems.provision`, `artifacts.create_system_upload` from `_ABOVE_CONTRIBUTOR` (leave `images.upload`, `systems.teardown`, `control.force_crash`); add the five tools to the contributor-visible set (mirror the `control.power` ADR-0320 entry).
  - Rewrite `test_create_system_upload_stays_operator_but_run_upload_drops`: both upload kinds now `PROJECT_CONTRIBUTOR`; rename it.
  - Rewrite `test_project_tool_visible_honours_role_on_the_named_project`: `project_tool_visible("systems.provision", contributor, "a")` is now True; use a still-operator tool (e.g. `images.upload`) to prove the per-project gate still discriminates.
  - Fix `test_project_tool_visible_is_per_project_not_connection_union` (uses provision) for contributor semantics.

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: FAIL.

- [ ] **Step 3: Flip the exposure constants + fix the comment** — `exposure.py`: `_OPERATOR → _CONTRIBUTOR` for `artifacts.create_system_upload` (L115), `systems.define` (L219), `systems.provision` (L220), `systems.provision_defined` (L221), `systems.reprovision` (L222). Leave `systems.provision` in `CORE_TOOLS`. Update the jobs.cancel comment at L173-174 — the handler no longer "keeps operator for the provision lane"; it keeps operator only for the remaining destructive/platform kinds.

- [ ] **Step 4: Run to verify pass**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/exposure.py tests/mcp/core/test_exposure.py
git commit -m "feat(security): expose provision lane + reprovision to contributor (#1081)"
```

---

### Task 4: Agent-facing contract + stale prose

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py:167,221,265,450` (wrapper docstrings) and `:442` (reprovision `profile` Field)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py:252` (create_system_upload wrapper docstring)
- Modify: `src/kdive/mcp/tools/jobs.py` (`jobs_cancel` wrapper docstring ~L406-411 + module docstring ~L11-16)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py:81` and `src/kdive/profiles/provisioning.py:116,165` (strike `reprovision` from `destructive_ops` advertisements)
- Modify: `src/kdive/security/authz/gate.py` (module docstring L6-9 + `assert_destructive_allowed` docstring L70-71: drop "operator for reprovision" — the gate now governs `force_crash` only)

**Interfaces:** text-only.

- [ ] **Step 1: Update the five wrapper docstrings + the reprovision Field**
  - `registrar.py:167/221/265` (define/provision/provision_defined): "Operator only"/"Requires operator" → contributor.
  - `registrar.py:450` (reprovision): "Requires operator and opt-in." → contributor, no opt-in.
  - `registrar.py:442` (reprovision `profile` Field): `"New provisioning profile; must opt in to reprovision."` → e.g. `"New provisioning profile to re-stage on the READY System."`
  - `catalog/artifacts/registrar.py:252`: "Requires operator." → contributor.
  - No ADR refs in any of this.

- [ ] **Step 2: Update jobs.cancel contract** — `mcp/tools/jobs.py`: move `provision`/`reprovision` out of the "requires operator" sentence in the `jobs_cancel` wrapper docstring and the module docstring; leave `teardown`/`force_crash` (+ platform/internal kinds) operator-only.

- [ ] **Step 3: Strike reprovision from destructive_ops advertisements + gate prose**
  - `profile_examples.py:81`: advertise `force_crash` only (leave `:79` "without reprovisioning").
  - `provisioning.py:116` and `:165`: name `force_crash` as the sole opt-in token (leave `:425` dedup-factor doc).
  - `gate.py:6-9` and `:70-71`: state the gate governs `force_crash` (admin) only; remove the reprovision-is-operator sentences.

- [ ] **Step 4: Run the contract/schema tests**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/lifecycle/test_systems_profile_examples.py -q ; just lint && just type`
Expected: PASS (`test_no_adr_leak` green; no lint/type warnings).

- [ ] **Step 5: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/registrar.py src/kdive/mcp/tools/catalog/artifacts/registrar.py src/kdive/mcp/tools/jobs.py src/kdive/mcp/tools/lifecycle/systems/profile_examples.py src/kdive/profiles/provisioning.py src/kdive/security/authz/gate.py
git commit -m "docs(security): update provision-lane agent-facing contract to contributor (#1081)"
```

---

### Task 5: Regenerate generated docs

**Files:** `docs/guide/safety-and-rbac.md` (via `just rbac-matrix`), the agent tool reference (via `just docs`), any doc-resource snapshots the regen touches.

- [ ] **Step 1: Regenerate** — `just rbac-matrix && just docs`
- [ ] **Step 2: Verify the five tools show contributor** — inspect the `docs/guide/safety-and-rbac.md` diff.
- [ ] **Step 3: Verify gates** — `just rbac-matrix-check && just docs-check` (both in-sync).
- [ ] **Step 4: Commit**

```bash
git add docs/guide/safety-and-rbac.md docs/   # only regenerated files that changed
git commit -m "docs(security): regenerate RBAC matrix + tool reference for #1081"
```

---

### Task 6: Full guardrail sweep

- [ ] **Step 1: Run the full gate** — `just ci`. Expected: green.
- [ ] **Step 2:** If red, fix and fold into the owning task's commit (or a `fix` commit), then re-run `just ci`. Watch for any additional test that hard-asserts the old operator classification (e.g. `test_rbac_platform.py`, `test_docmeta.py`, `test_gateway_projection.py`, profile-schema snapshots) — flip each to contributor and rerun.

---

## Self-Review

- **Spec coverage:** Task 1 ↔ spec §3 (reprovision gate removal + in-handler require_role) & §4 (taxonomy) & §5 (validator) & the §4 test-flip; Task 2 ↔ §2 (both gate layers incl. admission.py:419/621); Task 3 ↔ §1 (exposure) + F5 jobs.cancel comment; Task 4 ↔ §6 (all agent-facing surfaces) + F5 gate.py prose; Task 5 ↔ §7. Success criteria 1↔Tasks 2/3, 2↔Task 1, 3↔Task 1, 4↔Task 1, 5↔untouched force_crash, 6↔Task 6.
- **Ordering/bisectability:** Task 1 is atomic (taxonomy + admin.py + tests) so no commit is red; the `DESTRUCTIVE_JOB_KINDS` edit never lands separately from the `admin.py` `DestructiveOp` removal. Tasks 2 and 3 are independent (handler vs exposure) and each green alone. Task 4 (docstrings) precedes Task 5 (regen from corrected text).
- **No migration:** every change is a role constant, a frozenset, or text.
- **Rollback:** revert the branch; no data/external state touched.

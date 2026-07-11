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
- Test: `tests/mcp/jobs/test_jobs_tools.py` (cancel guard), `tests/services/systems/test_system_validation.py:181-221` (token validator), `tests/security/authz/test_gate.py:78,103,115` (reprovision gate constructs), `tests/mcp/lifecycle/test_systems_tools.py` (reprovision behavior + `_active_allocation_profile` fixture + the L1810/L2015 rootfs tests that hardcode the token), `tests/integration/test_m1_allocation_accounting.py:1009,1022,1106,1115` (provision/reprovision calls passing `destructive_ops=["reprovision"]`), `tests/profiles/test_provisioning.py:106-119` (model-level `destructive_opt_in(…, REPROVISION) is True`)

Exhaustive token sweep: every test that hardcodes `destructive_ops=["reprovision"]` or `["force_crash", "reprovision"]` breaks once `OPT_IN_DESTRUCTIVE_JOB_KINDS` narrows (the token is rejected at `validate_profile_for_provider`, which runs on **both** the provision and reprovision paths, before the behavior each test names is exercised). All such sites must ship in this commit.

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
    - `test_reprovision_rejects_local_rootfs_outside_allowed_root_before_mutating_ready_system` (L1810) and `test_reprovision_rejects_upload_rootfs` (L2015): drop the `destructive_ops = ["reprovision"]` line so the rootfs-outside-root / upload-rootfs rejection each test names is actually reached (otherwise the token is rejected first and the test false-passes on the wrong `configuration_error`).
  - `tests/integration/test_m1_allocation_accounting.py`: drop `destructive_ops=["reprovision"]` from the four `provisioning_profile(...)` calls (L1009/1022/1106/1115) — reprovision needs no opt-in now, and provision never did.
  - `tests/profiles/test_provisioning.py:106-119`: these assert `destructive_opt_in(profile, JobKind.REPROVISION) is True` on a profile listing `"reprovision"`. The token is retired; repoint the example to `force_crash` (the live opt-in path) so the model test exercises a real op. (The `… is False` cases at L464/L729 stay — they test the negative and are unaffected.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py tests/integration/test_m1_allocation_accounting.py tests/profiles/test_provisioning.py -q`
Expected: FAIL.

- [ ] **Step 3: Edit the taxonomy** — `domain/operations/jobs.py`: `DESTRUCTIVE_JOB_KINDS → frozenset({JobKind.TEARDOWN, JobKind.FORCE_CRASH})`; `OPT_IN_DESTRUCTIVE_JOB_KINDS → frozenset({JobKind.FORCE_CRASH})`; add `JobKind.PROVISION`, `JobKind.REPROVISION` to `CONTRIBUTOR_CANCELABLE_JOB_KINDS`. Update the three docstrings (reprovision is now contributor leaseholder lifecycle; opt-in governs only `force_crash`; cancelable set includes the provision lane).

- [ ] **Step 4: Remove the reprovision gate + add in-handler role check** — `admin.py::_reprovision_in_lock`: delete `op = DestructiveOp(...)`, the `try/except DestructiveOpDenied` + `assert_destructive_allowed` + denial return (L161-168); insert `require_role(ctx, system.project, Role.CONTRIBUTOR)` where the gate stood (after the system/allocation project-ownership resolution, before the `REPROVISIONING` dedup). Delete `_reprovision_opt_in` (L197-199). Flip `registrar.py:469` `required_role=Role.OPERATOR → Role.CONTRIBUTOR`. Run `just lint` and drop the now-unused imports `DestructiveOp, DestructiveOpDenied, assert_destructive_allowed` (L42) and the `_REPROVISION` alias (L51) if ruff reports them unused (distinct from `_REPROVISION_KIND`, which stays).

- [ ] **Step 5: Run to verify pass + lint/type**

Run: `uv run python -m pytest tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py tests/integration/test_m1_allocation_accounting.py tests/profiles/test_provisioning.py -q && just lint && just type`
Expected: PASS, no warnings.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/domain/operations/jobs.py src/kdive/mcp/tools/lifecycle/systems/admin.py src/kdive/mcp/tools/lifecycle/systems/registrar.py tests/mcp/jobs/test_jobs_tools.py tests/services/systems/test_system_validation.py tests/security/authz/test_gate.py tests/mcp/lifecycle/test_systems_tools.py tests/integration/test_m1_allocation_accounting.py tests/profiles/test_provisioning.py
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

### Task 3: Exposure map + regenerated RBAC matrix

The RBAC matrix (`docs/guide/safety-and-rbac.md`) is generated from `exposure.py`, and `test_committed_doc_is_in_sync` (`tests/scripts/test_gen_rbac_tool_matrix.py:80`) runs under `just test`. So the exposure flip and the regen **must ship in one commit** or the commit is red. The file also carries hand-written prose (outside the generated markers) that no generator fixes.

**Files:**
- Modify: `src/kdive/mcp/exposure.py` (L115, L219-222: `_OPERATOR → _CONTRIBUTOR`; stale jobs.cancel comment L173-174)
- Modify (generated + hand-written): `docs/guide/safety-and-rbac.md`
- Test: `tests/mcp/core/test_exposure.py`, `tests/mcp/core/test_app.py:417-419` (spot-pin of `systems.define == PROJECT_OPERATOR`), `tests/mcp/lifecycle/test_allocations_tools.py` (next-action filter tests pinning `systems.provision` as absent for a contributor)

The exposure flip ripples through **three** test surfaces, all driven by the exposure map: direct `required_scopes` pins, `project_tool_visible` checks, and `suggested_next_actions` role-filters (ADR-0261). The last one lives in files with no `operator` token (`"systems.provision" not in contributor_actions`), so grep-for-operator misses it — every next-action test that omits `systems.provision` for a contributor must flip to **include** it (the contributor can now provision the slot it holds).

**Interfaces:**
- Produces: `required_scopes(<each of the five tools>) == {PROJECT_CONTRIBUTOR}`.

- [ ] **Step 1: Update the exposure tests**
  - Remove `systems.define`, `systems.provision`, `artifacts.create_system_upload` from `_ABOVE_CONTRIBUTOR` (leave `images.upload`, `systems.teardown`, `control.force_crash`); add the five tools to the contributor-visible set (mirror the `control.power` ADR-0320 entry).
  - Rewrite `test_create_system_upload_stays_operator_but_run_upload_drops`: both upload kinds now `PROJECT_CONTRIBUTOR`; rename it.
  - Rewrite `test_project_tool_visible_honours_role_on_the_named_project`: `project_tool_visible("systems.provision", contributor, "a")` is now True; use a still-operator tool (e.g. `images.upload`) to prove the per-project gate still discriminates.
  - Fix `test_project_tool_visible_is_per_project_not_connection_union` (uses provision) for contributor semantics.
  - `test_exposure.py::test_visible_next_actions_filters_preserves_order_no_dedup` (L326-337): the contributor expectation (L329) omits `systems.provision` — add it (contributor now sees all three actions; viewer/operator lines stay).
  - `test_app.py:417-419`: the spot-pin `required_scopes("systems.define") == {PROJECT_OPERATOR}` (comment "systems.define stays operator") must become `PROJECT_CONTRIBUTOR`, or repoint the spot-pin to a still-operator tool (`images.upload`); fix the L417 comment. `control.force_crash`/`systems.teardown`/`ops.reconcile_now` pins above it stay.
  - `test_allocations_tools.py`: the contributor next-action assertions that currently drop `systems.provision` must now include it — `test_request_grant_drops_systems_provision_for_contributor` (L273: rename; L281 `not in` → `in`; L282 exact list gains `systems.provision`), `test_get_granted_filters_next_actions_by_role` (L298 contributor exact list), `test_envelope_role_filters_success_next_actions` (L977 contributor exact list), `test_envelope_filter_is_per_project_not_connection_union` (L996 contributor-on-project), `test_renew_response_role_filters_success_next_actions` (L1011 contributor). Keep the **viewer** negative controls (viewer still never sees `systems.provision`) and the operator cases (unchanged).

- [ ] **Step 2: Run to verify failure**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py tests/mcp/core/test_app.py tests/mcp/lifecycle/test_allocations_tools.py -q`
Expected: FAIL.

- [ ] **Step 3: Flip the exposure constants + fix the comment** — `exposure.py`: `_OPERATOR → _CONTRIBUTOR` for `artifacts.create_system_upload` (L115), `systems.define` (L219), `systems.provision` (L220), `systems.provision_defined` (L221), `systems.reprovision` (L222). Leave `systems.provision` in `CORE_TOOLS`. Update the jobs.cancel comment at L173-174 — the handler no longer "keeps operator for the provision lane"; it keeps operator only for the remaining destructive/platform kinds.

- [ ] **Step 4: Regenerate the matrix + fix hand-written prose** — run `just rbac-matrix` (updates the generated table region so the five tools' rows show contributor). Then hand-edit the prose **outside** the generated markers in `docs/guide/safety-and-rbac.md`:
  - The `operator` capabilities bullet (~L14) lists "define and provision systems … upload system rootfs (`artifacts.create_system_upload`)" — move these to the `contributor` capabilities description.
  - The "### The two-check gate" section (~L198-205): it says `control.force_crash` **and** `systems.reprovision` pass through `assert_destructive_allowed` and "reprovision requires operator" — rewrite so the gate governs `force_crash` (admin) only.

- [ ] **Step 5: Run to verify pass (incl. the in-sync gate)**

Run: `uv run python -m pytest tests/mcp/core/test_exposure.py tests/mcp/core/test_app.py tests/mcp/lifecycle/test_allocations_tools.py tests/scripts/test_gen_rbac_tool_matrix.py -q && just rbac-matrix-check`
Expected: PASS, in sync.

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/exposure.py tests/mcp/core/test_exposure.py tests/mcp/core/test_app.py tests/mcp/lifecycle/test_allocations_tools.py docs/guide/safety-and-rbac.md
git commit -m "feat(security): expose provision lane + reprovision to contributor (#1081)"
```

---

### Task 4: Agent-facing contract + stale prose + regenerated tool reference

The wrapper docstrings render into `docs/guide/reference/systems.md` and `artifacts.md` (verified: they carry "Requires operator"/"Operator only"), and `just docs-check` runs under `just test`. So the docstring edits and `just docs` regen **must ship in one commit**.

**Files:**
- Modify: `src/kdive/mcp/tools/lifecycle/systems/registrar.py:167,221,265,450` (wrapper docstrings) and `:442` (reprovision `profile` Field)
- Modify: `src/kdive/mcp/tools/catalog/artifacts/registrar.py:252` (create_system_upload wrapper docstring)
- Modify: `src/kdive/mcp/tools/jobs.py` (`jobs_cancel` wrapper docstring ~L406-411 + module docstring ~L11-16)
- Modify: `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py:81` and `src/kdive/profiles/provisioning.py:116,165` (strike `reprovision` from `destructive_ops` advertisements)
- Modify: `src/kdive/security/authz/gate.py` (module docstring L6-9 + `assert_destructive_allowed` docstring L70-71: drop "operator for reprovision" — the gate now governs `force_crash` only)
- Modify (generated): `docs/guide/reference/systems.md`, `docs/guide/reference/artifacts.md`
- Test: `tests/mcp/lifecycle/test_systems_profile_examples.py:155` (asserts `"reprovision" in note` — must drop that clause)

**Interfaces:** text-only.

- [ ] **Step 1: Update the five wrapper docstrings + the reprovision Field**
  - `registrar.py:167/221/265` (define/provision/provision_defined): "Operator only"/"Requires operator" → contributor.
  - `registrar.py:450` (reprovision): "Requires operator and opt-in." → contributor, no opt-in.
  - `registrar.py:442` (reprovision `profile` Field): `"New provisioning profile; must opt in to reprovision."` → e.g. `"New provisioning profile to re-stage on the READY System."`
  - `catalog/artifacts/registrar.py:252`: "Requires operator." → contributor.
  - No ADR refs in any of this.

- [ ] **Step 2: Update jobs.cancel contract** — `mcp/tools/jobs.py`: move `provision`/`reprovision` out of the "requires operator" sentence in the `jobs_cancel` wrapper docstring and the module docstring; leave `teardown`/`force_crash` (+ platform/internal kinds) operator-only.

- [ ] **Step 3: Strike reprovision from destructive_ops advertisements + gate prose + fix the note test**
  - `profile_examples.py:81`: advertise `force_crash` only (leave `:79` "without reprovisioning").
  - `provisioning.py:116` and `:165`: name `force_crash` as the sole opt-in token (leave `:425` dedup-factor doc).
  - `gate.py:6-9` and `:70-71`: state the gate governs `force_crash` (admin) only; remove the reprovision-is-operator sentences.
  - `tests/mcp/lifecycle/test_systems_profile_examples.py:155`: drop the `and "reprovision" in note` clause (assert only `"force_crash" in note`).

- [ ] **Step 4: Regenerate the tool reference** — `just docs` (updates `docs/guide/reference/systems.md` + `artifacts.md` from the corrected docstrings).

- [ ] **Step 5: Run the contract/schema tests + the in-sync gate**

Run: `uv run python -m pytest tests/mcp/core/test_tool_docs.py tests/mcp/lifecycle/test_systems_profile_examples.py -q && just docs-check && just lint && just type`
Expected: PASS (`test_no_adr_leak` green; in sync; no lint/type warnings).

- [ ] **Step 6: Commit**

```bash
git add src/kdive/mcp/tools/lifecycle/systems/registrar.py src/kdive/mcp/tools/catalog/artifacts/registrar.py src/kdive/mcp/tools/jobs.py src/kdive/mcp/tools/lifecycle/systems/profile_examples.py src/kdive/profiles/provisioning.py src/kdive/security/authz/gate.py tests/mcp/lifecycle/test_systems_profile_examples.py docs/guide/reference/systems.md docs/guide/reference/artifacts.md
git commit -m "docs(security): update provision-lane agent-facing contract to contributor (#1081)"
```

---

### Task 5: Full guardrail sweep

- [ ] **Step 1: Run the full gate** — `just ci`. Expected: green (Tasks 3 and 4 already regenerated their own doc outputs).
- [ ] **Step 2:** If red, fix and fold into the owning task's commit (or a `fix` commit), then re-run `just ci`. Watch for any residual surface that hard-asserts the old operator classification or embeds the retired docstrings: `test_rbac_platform.py`, `test_docmeta.py`, `test_gateway_projection.py`, `tests/mcp/resources/test_doc_exposure.py`, profile-schema snapshots, and any committed doc-resource snapshot. Regenerate/flip and rerun until green.

---

## Self-Review

- **Spec coverage:** Task 1 ↔ spec §3 (reprovision gate removal + in-handler require_role) & §4 (taxonomy) & §5 (validator) & the §4 test-flip; Task 2 ↔ §2 (both gate layers incl. admission.py:419/621); Task 3 ↔ §1 (exposure) + F5 jobs.cancel comment + the generated matrix + hand-written prose; Task 4 ↔ §6 (all agent-facing surfaces) + F5 gate.py prose + the generated tool reference; Task 5 ↔ §7 sweep. Success criteria 1↔Tasks 2/3, 2↔Task 1, 3↔Task 1, 4↔Task 1, 5↔untouched force_crash, 6↔Task 5.
- **Ordering/bisectability:** every task's commit leaves `just ci` green. Task 1 is atomic (taxonomy + admin.py + every `destructive_ops=["reprovision"]` test) so the `DESTRUCTIVE_JOB_KINDS` edit never lands apart from the `admin.py` `DestructiveOp` removal, and no token-rejection test is orphaned. Task 3 regenerates `safety-and-rbac.md` in the same commit as the `exposure.py` flip (the in-sync gate runs under `just test`); Task 4 regenerates the `reference/*.md` in the same commit as the docstring edits (`docs-check` runs under `just test`). Task 2 (handler gates) is independent of Task 3 (exposure) — each green alone, since no test cross-asserts exposure==handler-role.
- **No migration:** every change is a role constant, a frozenset, or text.
- **Rollback:** revert the branch; no data/external state touched.

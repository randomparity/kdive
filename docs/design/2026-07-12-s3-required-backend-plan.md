# S3 required backend — Implementation Plan

> **For agentic workers:** Execute this plan **serially and directly** (not via
> parallel subagents). It is one coherent refactor: the object-store type flips
> from `ObjectStore | None` to `ObjectStore`, and that change ripples across ~15
> files. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ratify S3 as a required backend — make config validation fail fast when
S3 is unset/blank, and remove the no-S3 accommodation branches so the object
store is non-optional end to end.

**Architecture:** (1) Config: `KDIVE_S3_ENDPOINT_URL`/`KDIVE_S3_BUCKET` get
`required_when=_always` + a strip-then-reject-blank parse. (2) The store source
(`store/assembly.py`, `processes/reconciler.py`, `__main__.py`) stops returning
`None`. (3) Each consumer narrows its store param to non-optional and deletes its
dead `if store is None` branch. (4) Docs + comment rewording.

**Tech Stack:** Python 3.14, `uv`, `ruff`, `ty`, `pytest`. Guardrail: `just ci`
(= `just lint`, `just type`, `just lint-shell`, `just lint-workflows`,
`just check-mermaid`, `just test`), each recipe run individually in CI.

**Spec:** `docs/design/2026-07-12-s3-required-backend.md`
**ADR:** `docs/adr/0337-s3-required-backend.md`

## Global Constraints

- **Green per commit.** Run **whole-tree** `just type` and `just test` before
  every commit (not a path-scoped subset). `just type` type-checks the whole tree
  including `tests/` (justfile deliberately unscoped), and a removed dataclass
  field/attr surfaces as a `ty` error in *any* test module that still uses it — so
  a scoped test run can mask a red `just type`. The commit order below is designed
  so each step is independently green: narrowing the store *source* to
  non-optional first (Task 2) is safe because a non-`None` `ObjectStore` is
  assignable to a still-`Optional` consumer param; then each consumer narrows +
  deletes its dead branch (Tasks 3–10) with all callers already passing non-`None`.
- **`ty` does NOT catch dead `if store is None` branches.** Narrowing a param to
  `ObjectStore` and forgetting to delete its `if store is None` block leaves a
  dead branch that `ty` reports as clean (verified against ty 0.0.53) and the
  symbol grep below does not match. Deleting each branch is manual discipline;
  Task 11 adds a residual-`is None` grep as the real backstop. The type change
  surfaces removed *fields/attributes* (`unresolved-attribute`/`unknown-argument`),
  not dead guards on a narrowed param.
- **Keep class-(b) and class-(c) sites.** Do NOT touch: staged-path store-free
  resolution (`images/rootfs/fetch.py` `staged-path` branch,
  `providers/local_libvirt/lifecycle/rootfs/rootfs_catalog_fetch.py`) — reword its
  comment only; `services/runs/complete_build.py` chunked-store `None` gating;
  `providers/*/retrieve.py` lazy-init; `materialize.py` unwired-lane guard; the
  fail-open `try/except` transient-store-error handling in `kernel_config/fetch.py`
  and the `artifacts`/`raw_fetch` read tools (drop only the `store_unconfigured`
  *sentinel*, keep outage handling).
- **No migration, no schema change, no new tool/response field.**
- **Doc style:** never "Sprint"; avoid "critical/crucial/essential/significant/
  comprehensive/robust/elegant" in ADRs, specs, comments, commit messages.
- **Removal backstop** (run after Task 10):
  `rg -n 'optional_object_store|s3_env_is_absent|_AbsentImageStore|optional_reconciler_object_store|_unconfigured_image_build_handler|store_unconfigured|RequiredObjectStore|_S3_OPTIONAL_ENV_NAMES|_required_store_error|optional_upload_store|optional_image_store|optional_ops_image_store' src/ tests/`
  must return nothing.

---

### Task 1: Config — reject unset AND blank S3 settings

**Files:**
- Modify: `src/kdive/config/core_settings.py` (S3_ENDPOINT_URL, S3_BUCKET; add a `_nonempty` parse)
- Test: `tests/config/` (find the existing settings/validate test module)

**Interfaces:**
- Produces: `S3_ENDPOINT_URL`/`S3_BUCKET` now `required_when=_always` with a
  `parse` that raises `ValueError` on a whitespace-only value.

- [ ] **Step 1: Write the failing tests.** In the config-validate test module, add
  cases asserting `config.validate(<process>)` raises `CategorizedError` with
  `CONFIGURATION_ERROR` naming `KDIVE_S3_ENDPOINT_URL` / `KDIVE_S3_BUCKET` when,
  respectively: (a) the vars are absent, (b) `KDIVE_S3_ENDPOINT_URL=""`, (c)
  `KDIVE_S3_ENDPOINT_URL="   "`. Cover **all three store-user processes** named in
  success criterion 1 — parametrize over `"server"`, `"worker"`, `"reconciler"`
  (at minimum the absent variant for each, plus the empty/blank variants on one).
  Use the existing test's env-override fixture pattern (grep the module for how it
  sets a `config.env_snapshot`/monkeypatches env).

- [ ] **Step 2: Run to verify they fail.**
  `uv run python -m pytest tests/config -k s3 -q` → FAIL (validate passes today).

- [ ] **Step 3: Implement.** Add near the other parse helpers:

```python
def _nonempty(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("must not be blank")
    return value
```

  Change `S3_ENDPOINT_URL` and `S3_BUCKET` to `parse=_nonempty` and add
  `required_when=_always`. Leave `S3_REGION` as `parse=_str` (it has a default).

- [ ] **Step 4: Run to verify they pass**, and confirm no regression:
  `uv run python -m pytest tests/config -q`.

- [ ] **Step 5: Commit.**
  `git add src/kdive/config/core_settings.py tests/config`
  `git commit -m "feat: require non-blank KDIVE_S3_ENDPOINT_URL/BUCKET (#1133)"`

---

### Task 2: Collapse the store source to non-optional

Flip `store/assembly.py`, `processes/reconciler.py`, and the `reconcile-systems`
CLI so the store is never `None`. Consumers keep their `Optional` params for now
(non-`None` is assignable), so this task is green on its own.

**Files:**
- Modify: `src/kdive/store/assembly.py`
- Modify: `src/kdive/mcp/assembly/tool_registration.py` (reads the removed fields at ~:105,106,117,185)
- Modify: `src/kdive/jobs/assembly.py` (reads the removed fields at ~:67,85,99,125,152 — see step 2 for the Task-3 interleaving)
- Modify: `src/kdive/processes/reconciler.py`
- Modify: `src/kdive/__main__.py` (`_handle_reconcile_systems`, import at ~:29)
- Test: `tests/store/`, `tests/reconciler/test_main.py`, `tests/mcp/ops/test_reconcile_now.py`, `tests/processes/test_worker.py`, **`tests/mcp/core/test_app.py`**

> Note: `mcp/assembly/app.py` only *calls* `build_object_store_assembly()` (~:66);
> it does not read the removed fields, so it needs no edit.

**Interfaces:**
- Produces: `ObjectStoreAssembly` with a single non-optional store field (see
  step 3) + `request_time_store_factory: ObjectStoreFactory`. Downstream code
  reads `assembly.<store_field>` as a non-`None` `ObjectStore`.

- [ ] **Step 1: Update tests first.** In `tests/mcp/ops/test_reconcile_now.py`
  delete/rewrite `test_register_resolves_upload_store_off_without_s3_env` (no-S3 is
  gone) and keep `test_register_reraises_partial_s3_config` as a "requires S3" case
  (partial/blank config raises). Update any `tests/store` / `tests/reconciler` /
  `tests/processes` assertions that expect a `None` store or the removed helpers.
  In **`tests/mcp/core/test_app.py`**: rewrite the two `ObjectStoreAssembly(...)`
  constructions (~:238, ~:345) to the collapsed `store=`/`request_time_store_factory=`
  field set; fix the attribute reads `object_stores.optional_*_store` /
  `required_image_build_store` (~:326-329) to `object_stores.store`; delete
  `test_object_store_assembly_preserves_configured_store_error` (~:257-275) and the
  `None`-field assertions in
  `test_build_handler_registry_derives_worker_ports_from_one_composition`
  (~:293-329). (The image-build config-error arm at ~:332-364 is removed in Task 3.)

- [ ] **Step 2: Rewrite `store/assembly.py`.** Remove `optional_object_store`,
  `s3_env_is_absent`, `_required_store_error`, `_S3_OPTIONAL_ENV_NAMES`, and the
  `RequiredObjectStore` alias. Collapse `ObjectStoreAssembly` to:

```python
@dataclass(frozen=True, slots=True)
class ObjectStoreAssembly:
    """Object-store roles assembled once for app and worker wiring."""

    store: ObjectStore
    request_time_store_factory: ObjectStoreFactory


def build_object_store_assembly(
    store_factory: ObjectStoreFactory | None = None,
) -> ObjectStoreAssembly:
    """Resolve the process object store; raises if S3 is unconfigured."""
    store_factory = store_factory or object_store_from_env
    return ObjectStoreAssembly(
        store=store_factory(),
        request_time_store_factory=store_factory,
    )
```

  Update the `mcp/assembly/tool_registration.py` and `jobs/assembly.py` call
  sites that read `optional_upload_store`/`optional_image_store`/
  `optional_ops_image_store`/`required_image_build_store` to read `assembly.store`
  (keep their local param types `Optional` for now). **Interleaving:**
  `jobs/assembly.py:152` currently does `isinstance(store, CategorizedError)` and
  falls back to `_unconfigured_image_build_handler`; in Task 2 rewire its read to
  `assembly.store` but leave that (now-dead) `isinstance` branch and the stub in
  place — Task 3 deletes them and drops the then-unused `CategorizedError` import
  (ruff F401).

- [ ] **Step 3: Rewrite `processes/reconciler.py`.** Remove
  `optional_reconciler_object_store` and its `s3_env_is_absent` use; build the
  store via `object_store_from_env()` (raising if unconfigured) and pass it to
  `ReconcileConfig(upload_store=store, image_store=store)`.

- [ ] **Step 4: Rewrite `__main__.py` `_handle_reconcile_systems`.** Drop the
  `optional_reconciler_object_store` import; add `import sys` and import
  `CategorizedError` (from `kdive.domain.errors`) if not already in scope. Call
  `object_store_from_env()` inside a `try/except CategorizedError` that prints the
  error message and returns a non-zero exit code (preserve the friendly-failure
  UX; match the module's existing `print` style):

```python
try:
    store = object_store_from_env()
except CategorizedError as error:
    print(f"error: {error}", file=sys.stderr)
    return 1
```

- [ ] **Step 5: Run guardrails.** `just type` and
  `uv run python -m pytest tests/store tests/reconciler tests/processes tests/mcp/ops -q`
  → green.

- [ ] **Step 6: Commit.**
  `git commit -m "refactor: object store is non-optional at the source (#1133)"`

---

### Task 3: Remove the deferred image-build stub (`jobs/assembly.py`)

**Files:**
- Modify: `src/kdive/jobs/assembly.py` (`_image_build_handler_registrar`, `_unconfigured_image_build_handler`)
- Test: `tests/jobs/test_worker_main.py`, **`tests/mcp/core/test_app.py`** (`test_image_build_handler_preserves_store_config_error` and the arm at ~:332-364)

- [ ] **Step 1: Update/remove tests** that assert an IMAGE_BUILD job fails with the
  unconfigured-store config error (in both `tests/jobs/test_worker_main.py` and
  `tests/mcp/core/test_app.py`).
- [ ] **Step 2: Delete `_unconfigured_image_build_handler`** and the
  `isinstance(store, CategorizedError)` branch in `_image_build_handler_registrar`;
  register the real image-build handler unconditionally with `assembly.store`.
  Drop the now-unused `CategorizedError` import from `jobs/assembly.py` (ruff F401).
- [ ] **Step 3: Run** `just type` and `uv run python -m pytest tests/jobs -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: always register image-build handler (#1133)"`

---

### Task 4: Narrow job handlers — systems, diagnostic_sysrq, boot_evidence, console_rotate

Each handler narrows its store param to `ObjectStore` and deletes its
`if store is None` branch. Group into one commit (all are job handlers reached
from `jobs/assembly.py`, which now passes non-`None`).

**Files:**
- Modify: `src/kdive/jobs/handlers/systems.py` (`_commit_uploaded_rootfs` ~:140-145; teardown reclaim ~:506-515)
- Modify: `src/kdive/jobs/handlers/control/diagnostic_sysrq.py` (~:259-267)
- Modify: `src/kdive/jobs/handlers/runs/boot_evidence.py` (`_capture_console_artifact` ~:80-88)
- Modify: `src/kdive/jobs/handlers/console/console_rotate.py` (`console_rotate_handler` ~:219-221)
- Test: the mirrored test modules under `tests/jobs/handlers/`

- [ ] **Step 1: Update tests.** Delete the no-S3 cases
  (`_commit_uploaded_rootfs` raise-on-None, teardown skip-reclaim, sysrq
  raise-on-None, boot-evidence skip-capture, console-rotate no-op) in the mirrored
  test files. Keep the store-present behavior tests.
- [ ] **Step 2: Narrow + delete branches.** In each site: change the param/attr
  type from `ObjectStore | None` to `ObjectStore` and remove the `if ... is None:`
  raise/skip/no-op block, dedenting the store-present body. For the teardown
  reclaim, always reclaim (remove the `if artifact_store is not None` guard).
- [ ] **Step 3: Run** `just type` and
  `uv run python -m pytest tests/jobs/handlers -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: job handlers require the object store (#1133)"`

---

### Task 5: Narrow the reconciler loop passes (`reconciler/loop.py`)

Make the reconciler store fields **required** (operator decision: full removal,
not narrow-at-entry). This is the largest task — the store fields feed an
aggregate (`ReconcileConfig`) whose zero-arg default cascades to ~13 store-free
test call sites. Use `kw_only=True` (avoids reordering 22 fields) and a shared
test helper (keeps the churn mechanical). **All edits land in one commit** to keep
whole-tree `just type`/`just test` green.

**Files:**
- Modify: `src/kdive/reconciler/loop.py` (`ReconcileConfig` :209-217; the eight pass gates :245-310; `_DEFAULT_RECONCILE_CONFIG` :238; `reconcile_once` config default :454; the `Reconciler.__init__` config default :517)
- Modify: `src/kdive/mcp/tools/ops/reconcile/reconcile.py` (`ReconcileRepairPorts.upload_store`/`image_store` :60-61 — drop `| None`; :98-99 feed)
- Modify: `src/kdive/processes/reconciler.py` (`run_reconciler_with_composition` `upload_store` param :109; `build_reconcile_config` `upload_store` param :142 — `ObjectStore | None` → `ObjectStore`)
- Test: `tests/reconciler/{test_loop,test_loop_telemetry,test_promotion_sweep,test_runtime_resource_reaping,test_orphaned_active_sweep,test_expiry_sweep}.py`, `tests/services/test_pcie_claim_release.py`, `tests/integration/{test_reconcile_inventory,test_m1_allocation_accounting}.py`, `tests/mcp/ops/test_reconcile_now.py`

**Interfaces:**
- Produces: `ReconcileConfig(*, upload_store: UploadStore, image_store: ImageSweepStore, ...)` — the two stores now required kw args. `_DEFAULT_RECONCILE_CONFIG` removed; `reconcile_once`/`Reconciler.__init__` take `config` with no default.

- [ ] **Step 1: Add a shared test helper.** In the reconciler test package's
  `conftest.py` (or a shared `tests/reconciler/_helpers.py` imported where needed),
  add `make_reconcile_config(**overrides) -> ReconcileConfig` that supplies fake
  stores by default (a `unittest.mock.create_autospec(UploadStore)` /
  `ImageSweepStore`, or the existing test fakes) so store-unrelated passes stay
  isolated. This replaces the deleted zero-arg default.
- [ ] **Step 2: Update tests first.** (a) Remove
  `test_loop_inventory_pass_absent_when_no_image_store`
  (`tests/integration/test_reconcile_inventory.py`) and the `# default config: no
  image store` call at :1066 (rewrite to pass a config without an image store via
  the helper, or delete if it only asserted the skip). (b) Replace every bare
  `reconcile_once(pool, reaper)` / `reconcile_once(pool, NullReaper())` call (the
  ~13 sites listed in the Files test set) with
  `reconcile_once(pool, reaper, config=make_reconcile_config())`. (c) Replace every
  store-free `ReconcileConfig(...)` construction with `make_reconcile_config(...)`.
  Keep the s3-unreachable / no-digest degrade tests (store-*outage*, store present).
- [ ] **Step 3: Make the fields required.** In `loop.py`: add `kw_only=True` to
  the `@dataclass(frozen=True, slots=True)` decorator; change
  `upload_store: UploadStore | None = None` → `upload_store: UploadStore` and
  `image_store: ImageSweepStore | None = None` → `image_store: ImageSweepStore`;
  delete `_DEFAULT_RECONCILE_CONFIG` and drop the `= _DEFAULT_RECONCILE_CONFIG`
  default on `reconcile_once` (:454) and `Reconciler.__init__` (:517), making
  `config` a required kw arg.
- [ ] **Step 4: Delete the eight gates.** In each pass function delete the
  `if <store> is None: return None` block, using `config.image_store` /
  `config.upload_store` directly: **four `image_store`** —
  `_reconcile_inventory_repair` (:245), `_leaked_images_repair` (:254),
  `_dangling_images_repair` (:263), `_expired_private_images_repair` (:272); **four
  `upload_store`** — `_abandoned_uploads_repair` (:281), `_report_artifacts_gc_repair`
  (:290), `_investigation_artifacts_gc_repair` (:299),
  `_expired_build_artifacts_gc_repair` (:310).
- [ ] **Step 5: Narrow the feeders.** In `reconcile.py` drop `| None` on
  `ReconcileRepairPorts.upload_store` (:60) and `image_store` (:61); trace the
  `reconcile_now` ports plumbing that supplies them and narrow those to
  non-optional too (confirm `ObjectStore` satisfies `UploadStore`/`ImageSweepStore`
  — it does; the pass functions already consume it). In `processes/reconciler.py`
  narrow `run_reconciler_with_composition`'s `upload_store` (:109) and
  `build_reconcile_config`'s `upload_store` (:142) from `ObjectStore | None` to
  `ObjectStore`.
- [ ] **Step 6: Run whole-tree** `just type` and `just test` → green.
- [ ] **Step 7: Commit.**
  `git commit -m "refactor: reconciler config requires the object store (#1133)"`

---

### Task 6: Remove `_AbsentImageStore` (`ops/reconcile/reconcile_systems.py`)

**Files:**
- Modify: `src/kdive/mcp/tools/ops/reconcile/reconcile_systems.py` (~:52-69, :112)
- Test: `tests/mcp/ops/test_reconcile_systems.py`

- [ ] **Step 1: Update tests.** `test_absent_default_systems_toml_is_quiet_no_op`
  should still pass (absent *inventory file* is a no-op — unrelated to S3); remove
  only the arm that substitutes `_AbsentImageStore` for a `None` store.
- [ ] **Step 2: Delete `_AbsentImageStore`** and the `image_store is None`
  substitution; the tool uses the passed non-`None` `image_store` directly.
- [ ] **Step 3: Run** `just type` and
  `uv run python -m pytest tests/mcp/ops/test_reconcile_systems.py -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: reconcile_systems requires the object store (#1133)"`

---

### Task 7: Narrow ops.images tools (upload + prune)

**Files:**
- Modify: `src/kdive/mcp/tools/ops/images/upload.py` (~:78-79)
- Modify: `src/kdive/mcp/tools/ops/images/registrar.py` (~:135-137)
- Test: `tests/mcp/ops/` image upload/prune tests

- [ ] **Step 1: Update tests.** Remove the `store is None → _config_error` cases
  for `upload` and `images_prune_expired`.
- [ ] **Step 2: Narrow the store params** to `ObjectStore` and delete the
  `_config_error` early returns.
- [ ] **Step 3: Run** `just type` and `uv run python -m pytest tests/mcp/ops -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: ops.images tools require the object store (#1133)"`

---

### Task 8: Drop the `store_unconfigured` sentinel (`artifacts/reads.py`), keep outage handling

**Files:**
- Modify: `src/kdive/mcp/tools/catalog/artifacts/reads.py` (`_load_redacted_plaintext` ~:349-352; `_find_response_data` ~:431-442)
- Test: `tests/mcp/catalog/test_artifacts_tools.py`

- [ ] **Step 1: Confirm retained behavior.** Keep
  `test_artifacts_get_degrades_when_store_unconfigured` renamed/repurposed to a
  live-outage case, and keep `..._on_store_error`, `..._on_presign_error`,
  `test_artifacts_find_store_outage_omits_match_found`. Only the *unconfigured*
  sentinel is removed; a live store outage still degrades to `content_unavailable`.
- [ ] **Step 2: Implement.** Remove the `store_unconfigured` reason branch; on a
  `CategorizedError` from a live store fault keep returning the
  `content_unavailable` body (map to a generic outage reason, not
  `store_unconfigured`). Do not remove the `try/except` — it guards transient
  faults.
- [ ] **Step 3: Run** `just type` and
  `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: drop store_unconfigured degrade sentinel (#1133)"`

---

### Task 9: Reword staged-path "no-S3 lane" comments (class-(b), no behavior change)

**Files:**
- Modify: `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_catalog_fetch.py` (docstring ~:33-36)
- Modify: `src/kdive/images/rootfs/fetch.py` (docstring ~:195-199)

- [ ] **Step 1: Reword.** Replace "keeps staged-path provisioning working when no
  object storage is configured (the no-S3 lane)" and "no object store, no cache,
  no digest" framing with "staged-path resolves from a host-local file and never
  touches the object store (a cost optimization; ADR-0228, ADR-0337)". No code
  change.
- [ ] **Step 2: Run** `just lint` and
  `uv run python -m pytest tests/images tests/providers/local_libvirt/lifecycle/test_rootfs_catalog_fetch.py -q`
  (behavior unchanged, tests still pass;
  `test_sync_fetch_staged_path_returns_validated_path_without_store` remains green).
- [ ] **Step 3: Commit.**
  `git commit -m "docs: reword staged-path store-free comments (#1133)"`

---

### Task 10: Operator docs — state S3 as required

**Files:**
- Modify: `docs/operating/index.md`, `docs/operating/install.md`, `docs/operating/kubernetes.md`, `docs/operating/local-stack.md` (make the S3 requirement explicit; remove any "optional" framing)

- [ ] **Step 1: Edit** each operating doc to state an S3-compatible object store
  is a required backend (cite ADR-0337), and that `KDIVE_S3_ENDPOINT_URL` /
  `KDIVE_S3_BUCKET` must be set (non-blank) for server/worker/reconciler. No Helm
  chart change (the demo path already derives a working endpoint; the external
  default now fails fast when omitted).
- [ ] **Step 2: Run** `just check-mermaid` and scan for banned prose words.
- [ ] **Step 3: Commit.**
  `git commit -m "docs: state S3 as a required operator backend (#1133)"`

---

### Task 11: Full guardrail sweep + removal backstop

- [ ] **Step 1: Run the removal backstop grep** (Global Constraints) → expect no
  output. If any symbol remains, remove it and re-run.
- [ ] **Step 1b: Run the residual-branch grep** over the touched trees and eyeball
  each hit against the kept class-(b)/(c) list (staged-path, complete_build chunked
  gating, retrieve lazy-init, materialize guard, kernel_config fail-open):
  `rg -n 'store is (not )?None|artifact_store is (not )?None|image_store is (not )?None|upload_store is (not )?None' src/kdive/jobs src/kdive/reconciler src/kdive/mcp/tools/ops src/kdive/mcp/tools/catalog/artifacts`
  Every remaining hit must be a deliberately-kept class-(b)/(c) site; delete any
  leftover class-(a) dead guard.
- [ ] **Step 2: Run** `just ci` → all recipes green.
- [ ] **Step 3: Commit** any lint/format fixups if needed (`git commit -m "chore:
  guardrail fixups (#1133)"`), otherwise nothing to do.

---

## Self-review

- **Spec coverage:** Task 1 = spec §Approach.1 (config fail-fast); Task 2 =
  §Approach.2 + §5 (assembly collapse, reconciler, CLI); Task 3 = the deferred
  image-build stub; Tasks 4–8 = §Approach.3 (the class-(a) branch removals) + §4
  (drop `store_unconfigured` sentinel, keep outage handling); Task 9 =
  §Approach.6 comment rewording (class-(b) kept); Task 10 = §Approach.6 operator
  docs (no Helm change per the review); Task 11 = success criteria 2 & 6.
  Criteria 1 (config fail-fast, three cases) = Task 1; criterion 3 (non-optional
  types, `ty` green) = Tasks 2–8; criterion 4 (staged-path still store-free) =
  Task 9; criterion 5 (live-outage degrade retained) = Task 8.
- **Kept sites (not removed):** complete_build chunked gating, retrieve lazy-init,
  materialize unwired-lane guard, `kernel_config/fetch` fail-open — none appear in
  any removal task. Confirmed against the audit's class-(c) list.
- **Line numbers are approximate** (`~:`); the implementer greps the named symbol
  in the named file rather than trusting the offset.

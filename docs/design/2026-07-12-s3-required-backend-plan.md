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

- **Green per commit.** `just type` and `just test` must pass at every commit.
  The commit order below is designed so each step is independently green:
  narrowing the store *source* to non-optional first (Task 2) is safe because a
  non-`None` `ObjectStore` is assignable to a still-`Optional` consumer param;
  then each consumer narrows + deletes its dead branch (Tasks 3–10) with all
  callers already passing non-`None`.
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
  three cases asserting `config.validate("server")` (and one for `worker`) raises
  `CategorizedError` with `CONFIGURATION_ERROR` naming `KDIVE_S3_ENDPOINT_URL` /
  `KDIVE_S3_BUCKET` when, respectively: (a) the vars are absent, (b)
  `KDIVE_S3_ENDPOINT_URL=""`, (c) `KDIVE_S3_ENDPOINT_URL="   "`. Use the existing
  test's env-override fixture pattern (grep the module for how it sets a
  `config.env_snapshot`/monkeypatches env).

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
- Modify: `src/kdive/processes/reconciler.py`
- Modify: `src/kdive/__main__.py` (`_handle_reconcile_systems`, import at ~:29)
- Test: `tests/store/`, `tests/reconciler/test_main.py`, `tests/mcp/ops/test_reconcile_now.py`, `tests/processes/test_worker.py`

**Interfaces:**
- Produces: `ObjectStoreAssembly` with a single non-optional store field (see
  step 3) + `request_time_store_factory: ObjectStoreFactory`. Downstream code
  reads `assembly.<store_field>` as a non-`None` `ObjectStore`.

- [ ] **Step 1: Update tests first.** In `tests/mcp/ops/test_reconcile_now.py`
  delete/rewrite `test_register_resolves_upload_store_off_without_s3_env` (no-S3 is
  gone) and keep `test_register_reraises_partial_s3_config` as a "requires S3" case
  (partial/blank config raises). Update any `tests/store` / `tests/reconciler` /
  `tests/processes` assertions that expect a `None` store or the removed helpers.

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

  Update `mcp/assembly/app.py`, `mcp/assembly/tool_registration.py`, and
  `jobs/assembly.py` call sites that read `optional_upload_store`/
  `optional_image_store`/`optional_ops_image_store`/`required_image_build_store` to
  read `assembly.store` (keep their local param types `Optional` for now).

- [ ] **Step 3: Rewrite `processes/reconciler.py`.** Remove
  `optional_reconciler_object_store` and its `s3_env_is_absent` use; build the
  store via `object_store_from_env()` (raising if unconfigured) and pass it to
  `ReconcileConfig(upload_store=store, image_store=store)`.

- [ ] **Step 4: Rewrite `__main__.py` `_handle_reconcile_systems`.** Drop the
  `optional_reconciler_object_store` import; call `object_store_from_env()` inside
  a `try/except CategorizedError` that prints the error message and returns a
  non-zero exit code (preserve the friendly-failure UX):

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
- Test: `tests/jobs/test_worker_main.py` (and any test asserting the stub)

- [ ] **Step 1: Update/remove tests** that assert an IMAGE_BUILD job fails with the
  unconfigured-store config error.
- [ ] **Step 2: Delete `_unconfigured_image_build_handler`** and the
  `CategorizedError` branch in `_image_build_handler_registrar`; register the real
  image-build handler unconditionally with `assembly.store`.
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

**Files:**
- Modify: `src/kdive/reconciler/loop.py` (~:245-310 — the `config.image_store is None` / `config.upload_store is None` pass gates)
- Modify: `src/kdive/reconciler/*` `ReconcileConfig` definition (narrow `image_store`/`upload_store` to `ObjectStore`)
- Test: `tests/integration/test_reconcile_inventory.py`, `tests/reconciler/`

- [ ] **Step 1: Update tests.** Remove
  `test_loop_inventory_pass_absent_when_no_image_store`; keep the s3-unreachable /
  no-digest degrade tests (those are store-*outage*, not no-S3 — the store is
  present but returns bad HEADs).
- [ ] **Step 2: Narrow `ReconcileConfig.image_store`/`upload_store`** to
  `ObjectStore` and delete the four inventory-pass and four GC-pass `is None`
  gates so the passes are always scheduled.
- [ ] **Step 3: Run** `just type` and
  `uv run python -m pytest tests/reconciler tests/integration/test_reconcile_inventory.py -q`.
- [ ] **Step 4: Commit.**
  `git commit -m "refactor: reconciler passes require the object store (#1133)"`

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

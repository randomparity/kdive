# Mutation sweep — coverage status and deferred targets

A repository-wide mutation-testing sweep (`just mutate`, mutmut 3.6) ran against the
container-free source modules on 2026-06-21. This note records what was covered, two
reusable tooling workarounds discovered during the run, and the targets deferred to a
follow-up sweep. See `mutation-testing.md` for how `just mutate` itself works.

## Result

- **407** source modules → **273** container-free "fast" targets (have at least one
  covering test that does not use the Postgres fixtures), **112** Postgres-backed targets,
  and the modules with no direct unit test (originally ~22; a reproducible scan found **25** on
  `main`). The no-direct-test bucket was closed by #665, reopened by post-sweep modules, and
  re-closed by #1298 / #1304 — see below.
- The 273 fast targets were swept in 30 weight-balanced buckets, each killing surviving
  mutants and then passing an adversarial `/challenge` review of the added tests.
- **~3,700 mutants killed across ~210 commits.** Every bucket's added tests pass the full
  gate: `just lint`, `just type` (whole-tree), and `just test` (6,349 passed).
- **46** fast targets could not be swept in that run (categorized below); the 112 PG-backed and
  the no-direct-test targets were out of scope for the 2026-06-21 sweep (the latter first closed
  by #665, then re-closed by #1298 / #1304 after post-sweep modules reopened it).

## Reusable tooling workarounds

> **Folded into the recipe (ADR-0229).** `just mutate` now applies both workarounds below
> automatically — it generates a per-run `sitecustomize.py` shim on a unique temp dir, prepends it
> to `PYTHONPATH`, and sets `UV_NO_SYNC=1` for the spawned mutmut/pytest subprocesses. No manual
> `export` is needed; the detail below is retained as the rationale.

Two environment issues block `just mutate` on parts of the tree. Both are worked around
without editing repo source — apply them when sweeping cli/mcp/security/config modules.

### 1. beartype.claw circular import in mutmut workers

`key_value` (via `py-key-value-aio`) calls `beartype_this_package()` at import, installing
a meta-path import hook. In a freshly *spawned* mutmut Pool worker that hook can intercept a
stdlib/pytest import while `beartype.claw._clawstate` is still initializing, raising
`ImportError: cannot import name 'claw_state'` and aborting the baseline before any mutant
runs (Python 3.14, beartype 0.22.9).

Workaround — a `sitecustomize.py` on `PYTHONPATH` that eagerly completes the imports at
interpreter startup, before the hook can fire:

```python
# /tmp/kdive-mut-pyhook/sitecustomize.py
import multiprocessing.connection, multiprocessing.context, multiprocessing.pool
import multiprocessing.popen_spawn_posix, multiprocessing.queues, multiprocessing.reduction
import multiprocessing.resource_sharer, multiprocessing.resource_tracker, multiprocessing.spawn
import multiprocessing.synchronize, multiprocessing.util
try:
    import beartype.claw._clawstate
    import beartype.claw._importlib._clawimpload
    import pytest
except Exception:
    pass
```

Run with: `PYTHONPATH=/tmp/kdive-mut-pyhook just mutate <source> <tests...>`

### 2. Shared-venv editable-install contention (parallel runs only)

The venv carries an editable kdive install (`.venv/.../kdive.pth`). Every `uv run` re-points
that `.pth` at the current working directory's `src`. When several worktrees share one venv
(symlink), concurrent `uv run` invocations rewrite each other's `kdive.pth`, intermittently
breaking imports. mutmut mutates its own `mutants/` copy regardless, so the editable pointer
only needs to stay valid — set `export UV_NO_SYNC=1` so `uv run` never rewrites it. (If it
was already corrupted, repoint `kdive.pth` at the real `src` and re-verify
`uv run --no-sync python -c "import kdive"`.)

## Deferred / blocked targets

### Postgres/container-backed (112) — sweep in progress (#1306)

Their only covering tests use container fixtures — the `migrated_url`/`pg_conn`/`postgres_url`
Postgres fixtures, or the `minio_store` MinIO fixture. Sweeping these spins up testcontainers
per run (slow, can leak/collide under parallelism); run them serially in a dedicated session.
Subsystems: `services/`, `store/`, `db/`, most `jobs/handlers/`, and the Postgres-backed
`inventory/`/`reconciler/` paths. #1306 is the tracking epic; the remaining subsystem buckets
are its sub-issues.

**Swept so far:**

- `store/objectstore.py` — 569 mutants, 23 → 0 surviving. The survivors were assertion gaps
  in the `_infrastructure_error` mapping (op label, key, and carried S3 error code went
  unasserted), the `err.response.get("ResponseMetadata", {})` defensive default (never
  exercised), and the `put_artifact` `content-encoding` metadata key (a case-mutation that a
  live MinIO round-trip cannot distinguish, because S3 lowercases user-metadata keys — killed
  with a fake-client exact-key assertion). Run with all four covering test files, including the
  `minio_store`-gated round-trips: one read (`head`'s `content-encoding`) is attributable only
  through the container round-trip, so the bucket's mutation run must keep the gated files in
  scope to legitimately reach 0-surviving.
- `store/assembly.py` — a no-direct-test module (previously imported only cross-file by
  conftest/`test_app`), not itself container-backed; gained a direct mirror unit test
  (`tests/store/test_assembly.py`, #1405, #665 pattern) pinning both branches of
  `build_object_store_assembly` (provided `store_factory` used verbatim; default falls
  through to `object_store_from_env`). 3 mutants, 0 surviving.
- `db/` bucket (#1401) — the container-backed repository / lock / idempotency / migration
  modules, swept serially against their `pg_conn`/`migrated_url`/`postgres_url` covering
  tests (plus `tests/adversarial/test_{idempotency_concurrency,lock_key_properties}.py`).
  Per module: `db/pool.py` 19 mutants, 6 → 0; `db/idempotency.py` 145, 31 → 21 equivalent;
  `db/locks.py` 76, 8 → 8 equivalent; `db/migrate.py` 75, 7 → 5 equivalent;
  `db/probe_fence.py` 0 mutants generated (import-time-only, per #665). Assertion gaps
  killed by pinning the created pool's `min_size`/`max_size`, the run-step error path
  (`run_id`/`step` threaded into the non-JSON, unknown-state, and completion messages),
  `complete_run_step`'s returned value, and both colliding filenames in the duplicate-migration
  error. The **surviving mutants are equivalent** and cluster into six DB-specific classes,
  none killable by a behavioral assertion:
  1. **Postgres case-folded SQL** — mutating keyword/identifier case (`SELECT`→`select`,
     `run_steps`→`RUN_STEPS`) is a no-op: keywords are case-insensitive and unquoted
     identifiers fold to lowercase.
  2. **Error-message `path`/`run_id`/`step` on DB-sourced values** — these arguments feed
     only `ensure_json_value` / `parse_persisted_run_step_state` *error* messages, which never
     fire for a value read back from `jsonb` (always valid JSON) or a CHECK-constrained column,
     so setting them to `None` at those call sites is unobservable.
  3. **`int.from_bytes(..., signed=True)` dropping `"big"`** — Python 3.11+ defaults the
     byteorder to `"big"`, so the advisory-lock-key digest→int mapping is identical.
  4. **Dead defensive `else` on `row is None`** — the `pg_try_advisory_lock` / `count(*)`
     queries always return exactly one row, so flipping the unreachable `else False`→`else True`
     is unobservable.
  5. **`decode("utf-8")`→`decode("UTF-8")`** — the codec name is case-insensitive; same decoder.
  6. **Behavior-preserving restructure** — `run_step`'s `inserted = None` skips the early
     `RETURNING` return and falls through to an identical follow-up `SELECT` on the same
     connection, yielding the same result. `db/repositories.py` remains tooling-blocked (below).

- `services/allocation/` bucket (#1398) — the admission / lease / capacity-debit modules,
  swept serially against their `migrated_url` covering tests (`tests/services/allocation/*`,
  `tests/services/test_allocation_{admission,enqueue,sizing}.py`,
  `tests/services/test_{admission_budget_quota,pcie_claim,pcie_claim_release}.py`, plus the
  MCP-level renew tests). Per module (mutants, surviving before → after):
  `error_details.py` 5, 0→0; `lease_bounds.py` 28, 0→0; `admission/affinity.py` 5, 0→0;
  `admission/request.py` 164, 4→0; `admission/sizing.py` 56, 10→1; `admission/metrics.py`
  93, 21→3; `admission/placement.py` 119, 3→1; `admission/pcie_claim.py` 30, 6→2;
  `idempotency.py` 70, 6→5; `release.py` 98, 22→2; `renew.py` 253, 35→8; `promotion.py`
  533, 121→39; `admission/core.py` 489, 51→20. Assertion gaps killed by pinning the full
  grant/enqueue/release/renew snapshot + audit rows (tool/object_kind/transition/args/project),
  lease-window arithmetic, error messages + details on every fail-closed path, and the
  funding-gate + clamp boundaries. The **surviving mutants are equivalent** and cluster into
  the same classes as the `db/` bucket plus a few allocation-specific ones:
  1. **Postgres case-folded SQL** — keyword / unquoted-identifier case (as in `db/`).
  2. **Advisory-lock key args** — mutating a `LockScope.*` lock key to `None` only changes
     which key serializes; a serial (single-connection) test cannot observe it.
  3. **Defense-in-depth placement filters** — `promotion._candidate_hosts` drops the
     project/arch/pcie placement filter, but `admission_gate` re-checks affinity
     (`project_may_place`), arch (`_reserve_accel`), and PCIe (`_resolve_pcie_claim`), so the
     final grant/deny outcome is identical (placement is an optimization, the gate is
     authoritative).
  4. **`model_copy` update keys the ledger never reads** — mutating the `state`/`lease_expiry`/
     `pcie_claim` keys of the in-memory copy passed to `accounting.reserve` is unobservable:
     `reserve` reads only `id`/`project`/`resource_id`/`created_at`.
  5. **`getattr` falsy default / discarded value** — `_enabled=False`→`None` (every use is a
     truthiness guard) and `getattr(denial, "queueable", False)`→`None` are both falsy through
     every branch; a `CategorizedError` message blanked at a call site whose only consumer is
     `categorized_details` (which keeps details, not the message) never surfaces.
  6. **OTel case-insensitive instrument names / naive-vs-UTC datetime / cosmetic
     `operation_label` / `devices=[]` on denial `GateResult`s** — the metric name is
     normalized to lowercase by OpenTelemetry; `datetime.now(None)` equals UTC on a UTC host
     and only feeds a relative lease window; the `resolve_replay` operation label only shapes
     an error string; and a denial `GateResult`'s `devices` list is never read (only the grant
     path reads it).
  `admission/core.py` is partially swept: the grant snapshot/audit cluster and the
  denial-enrichment boundaries are done, but ~8 killable survivors on the **queue-on-capacity
  enqueue path** (`_enqueue`, `_deny_or_enqueue`, `_host_cap_check`/`admission_gate` queueable)
  and a **PCIe-busy** `_resolve_pcie_claim` read need the `_enqueue` persistence + PCIe-busy
  covering assertions; that deeper sweep of the large `core.py` module is a #1398 follow-up
  (the "split a large module" path).

- `services/runs/` bucket (#1399) — the run admission / bind / state-transition /
  build-finalization modules, swept serially against their `migrated_url` covering tests
  (`tests/services/runs/*`, plus `tests/adversarial/test_runs_bind_races.py`). Per module
  (mutants, surviving before → after): `states.py` 0 (module-level constants, no mutable
  surface); `liveness.py` 68, 11→0; `host_admission.py` 91, 2→0; `bind.py` 215, 35→13
  equivalent; `steps.py` 76, 12→7 equivalent; `complete_build.py` 261, 65→9 equivalent;
  `admission.py` 503, 48→18 equivalent. (Note the naming trap: the adversarial
  `test_admission_*` files cover `services/allocation/admission`, not this bucket's
  `services/runs/admission.py`; a fast direct cover — `tests/services/runs/test_create_flow.py`
  — was added for the runs create flow.) Assertion gaps killed by pinning the storm-hit
  threshold, argument passthrough in the liveness fakes, the full bind/create/complete_build
  success snapshots + audit rows (tool/object_kind/transition/args-digest), reject-path error
  object_id/category/details, the chunked-reassembly copy/final-key + cleanup, and the
  cmdline_for branch matrix. The **surviving mutants are equivalent** and cluster into these
  classes, none killable by a behavioral assertion:
  1. **Postgres case-folded SQL** — keyword / unquoted-identifier case (as in `db/`).
  2. **Advisory-lock key args** — mutating a `LockScope.*` lock key to `None` only changes
     which key serializes; a serial (single-connection) test cannot observe it.
  3. **Defense-in-depth re-checks** — a pre-lock reject (run-bindable, alloc-hostable,
     cross-project) dropped in `_resolve_*`/`_bind_locked` is re-caught under the lock by
     `check_host_preconditions` / the `IS NULL` compare-and-set, so the outcome is identical.
  4. **Coupled-condition tautologies** — `precond is not None or ok is None` (and the
     `chunked and store is not None`) are logically equal to the mutated `and`/`or` form
     because the two operands are always coupled (`ok is None` ⟺ `precond is not None`;
     `store` is set iff `chunked`).
  5. **`typing.cast` / `model_dump(mode=…)` no-ops** — `cast(t, v)` returns `v` unchanged
     regardless of `t`, and `model_dump`'s mode is unobservable for a payload with no
     non-JSON-native fields.
  6. **Naive-vs-UTC + DB-overwritten timestamp** — `datetime.now(None)` equals UTC on a UTC
     host and the value is overwritten by the DB on insert (ADR-0016).
  7. **Redundant / unreachable guards** — `_optional_provenance_map`'s cast, the `kind = None`
     vs `""` in `_validate_unbound_target_kind` (both fail the membership check identically),
     the FK-unreachable `else "missing"` allocation-missing literal, and the log-context-only
     `bind_context(principal=…)`.
  8. **Protocol-stub default args** — the `arch="x86_64"` default on the
     `CompleteBuildValidation.__call__` Protocol (a `...` body never executed).

- `services/` remaining container-backed bucket (#1400) — the reports / debug / investigations
  / images / systems / accounting service modules, swept serially against their `migrated_url`
  covering tests (`tests/services/{reports,debug,images,systems,accounting}/*`, plus
  `tests/services/test_accounting*.py`, `tests/reconciler/test_image_sweeps.py`,
  `tests/mcp/debug/test_debug_session_read.py`). Per module (mutants, surviving before → after):
  `reports/sections.py` 215, 65→28 equivalent; `debug/detach.py` 26, 2→2 equivalent;
  `debug/sessions.py` 19, 0 (already clean); `systems/validation.py` 59, 12→0;
  `images/retention.py` 76, 19→18 equivalent (killed the `pruned += 1`→`= 1` count mutant);
  `images/publish.py` 266, 37→18 equivalent; `images/upload.py` 295, 76→8 equivalent;
  `accounting/ledger.py` 430, 34→24 equivalent; `systems/admission.py` 336, 70→28 (equivalents
  **plus a deferred killable remainder**, below). The earlier commits on this branch swept the
  idempotency / artifacts-listing / debug-lifecycle / investigations (`read`/`view`/`lifecycle`/
  `metadata`/`common`) / reports (`render`/`artifacts`/`core`) modules to 0-or-equivalent. Gaps
  killed by pinning: the section scope-isolation / cap-truncation / activity effective-window /
  costs value+window paths; the fail-closed upload quota / oversize / clamp-expiry pure decisions
  and their error+audit contracts; the publish digest-mismatch / HEAD-gate error contracts; the
  reconcile missing-size + started-but-unended active-hours guard; the provision persisted-System
  fields + audit + job + quota boundary + recycle failure. The **surviving mutants are
  equivalent**, in the same classes as `#1399` (Postgres case-folded SQL; advisory-lock key
  args; `cast`/`model_dump` no-ops; naive-vs-UTC DB-overwritten `now`) plus:
  1. **`cap + 1` fetch sentinel** — the section pagers fetch `cap + 1` to detect truncation;
     any value `≥ cap + 1` (including `None` = `LIMIT NULL` = no limit, or `cap + 2`) yields an
     identical `_capped` result (`rows[:cap]`, `truncated = len > cap`) — only `< cap + 1`
     (e.g. `cap - 1`) is observable, and that is killed.
  2. **Empty-clause accumulator** — the first `clause += …` on an empty `clause` equals `=`.
  3. **`_log.*` log-statement mutations** — argument/message mutations on `_log.info`/`_log.warning`
     calls (the retention / publish best-effort legs); no behavioral effect, matching the
     campaign's `do_not_mutate_patterns=logger.\w+` intent (missed here as the modules use `_log`).
  4. **Unreachable fallbacks / no-op labels** — the costs `principal or ""` fallback (grouped
     principals are always non-empty), the upload identity-component error label casing (the
     security decision is pinned by the traversal guard), the cosmetic tempdir prefix, and the
     `_validate_staged(None, …)` source under the stubbed inspect seam.
  5. **Local-profile binding no-ops (`systems/admission.py`)** — for the local-libvirt test
     profile the resource resolves `accel=None`/`resolved_cpu=None` and requests neither fadump
     nor a pinned CPU, so the fadump / cpu-pin / `resolved_cpu` binding mutants are unobservable.

  **`systems/admission.py` deferred killable remainder (needs a follow-up).** Restricting the
  covering set to `tests/services/systems/` (the root `test_admission_*` cover
  `services/allocation`, not this module) leaves a killable remainder that needs new fixtures,
  not just assertions: (a) the `accel`-resolution mutants in `_resolve_new_system_bindings` /
  `_insert_*` need a KVM/TCG caps-bearing resource whose `capability_view` resolves a non-`None`
  accel (the `FakeLibvirtConn` resource yields `accel=None`); (b) `_provision_create_response`'s
  `is FAILED` branch converges with the recycle branch unless a **failed provision job**
  (dedup-keyed) is seeded so `_failed_system_retry_failure` diverges; (c) the enqueue dedup-key
  `allocation_id=None` mutant needs a job idempotency/dedup-key assertion.

- `jobs/handlers/` bucket (#1402) — the durable worker job handlers (provision / build / install /
  boot / capture / control), swept serially against their `migrated_url` / `minio_store` covering
  tests (`tests/jobs/handlers/**`, `tests/jobs/test_{image_build_handler,capture_telemetry,
  diagnostic_sysrq}.py`, `tests/adversarial/test_vmcore_capture_idempotency.py`). Per module
  (mutants, surviving before → after): `runs/registrar.py` 26, 0→0; `diagnostics.py` 28, 0→0;
  `runs/{common,install,ports}.py` and the async-frame `runs_*` modules 0 mutants (import-time /
  deeper than `max_stack_depth`, per #665; `ports.py` mirror-tested by #1304);
  `console/capture_telemetry.py` 57, 3→3 equivalent; `console/console_evidence.py` 50, 22→17
  equivalent; `console/console_rotate.py` 207, 29→25 equivalent; `runs/boot.py` 59, 32→0;
  `image_build.py` 14, 2→0; `artifacts/vmcore.py` 170, 35→3 equivalent;
  `connectivity/ssh_authorize.py` 143, 41→0; `connectivity/ssh_reachable.py` 152, 31→11;
  `control/control.py` 113, 18→2 equivalent; `control/watch_for_crash.py` 232, 54→20;
  `control/diagnostic_sysrq.py` 324, 57→14. Assertion gaps killed by pinning: collaborator-arg
  passthrough into the `boot_evidence` / `redacted_console_tail` / retriever / probe seams; the
  full audit rows (tool / object_kind / transition / args_digest / project); the boot/capture
  result dicts + returned ids; provider-kind tags (`take_provider_kind`); the SSH argv hardening
  options + fail-closed error messages + remediation strings; the console decode-error handler on
  raw guest bytes (invalid-UTF-8 round-trips); and the watch/sysrq settle-poll and byte-cap
  boundaries. The **surviving mutants are equivalent**, in the same classes as `#1399`/`#1400`
  (Postgres case-folded SQL; advisory-lock key args; codec-name case; naive-vs-UTC `now`;
  log-statement `_log.*` mutations; defense-in-depth re-checks masked by a sibling guard) plus:
  1. **Codec error-handler on already-valid UTF-8** — `redacted_console_tail` decodes the bytes
     `read_redacted_console` already re-encoded to valid UTF-8, so its `"replace"` handler never
     fires; the mutant is unobservable (the *source* read, over raw disk bytes, is killed).
  2. **Value only in a fail-closed error detail** — `ensure_method_match`'s `run_id` and similar
     feed only a not-taken error path (method-mismatch), killable only with a cross-method fixture.
  3. **`_real_probe` deadline/backoff/read-timeout arithmetic + socket params** — the SSH-reachable
     banner probe's timing (`<0` vs `<=0`, `suppress(None)`, `open_connection(None)`→loopback) is
     either an unobservable boundary or killable only via a fake-clock socket harness.
  4. **Poll-loop mutants caught as a timeout** — `watch_console_for_crash`'s `match = None` makes
     the loop never fire, so every fired-path test fails or times out (detected, not a clean kill).

  **Tooling note (fixtures sandbox).** `image_build.py` resolves its rootfs catalog by a
  `__file__`-relative path into the repo-root `fixtures/` tree, which mutmut's `source_paths=
  src/kdive` sandbox does not copy, so its baseline aborts. Sweep it with a
  `mutants/fixtures -> ../fixtures` symlink (mutmut reuses the copy, so the symlink survives across
  runs); no source change is needed. This joins the ADR-0229 env shims as a `just mutate`
  workaround for `__file__`-relative resource loads outside the package.

  **Deferred killable remainder (needs a follow-up).** Three large handlers were not swept in this
  session and carry killable survivors in the same audit / message / arg-passthrough / core-logic
  clusters handled above (not equivalents): `control/capture_traffic.py` (356 mutants, 99
  surviving), `runs/boot_evidence.py` (290, 105), and `systems.py` (766, 143). A couple of smaller
  deferrals also remain: `console_log_path(None)` in `watch_for_crash` / `diagnostic_sysrq` needs a
  system-id-dependent console-read fixture, and the `_real_probe` timing cluster needs a fake-clock
  socket harness.

**Not yet swept:** the remaining subsystem buckets (`inventory/`, `reconciler/`) and the deferred
`jobs/handlers/` remainder above (`capture_traffic` / `boot_evidence` / `systems`) — filed as
#1306 sub-issues / a #1402 follow-up.

### No direct unit test — DONE (#665; reopened, re-closed by #1298 / #1304)

**The invariant.** Every source module is imported by at least one test that mirrors it, so mutmut
can attribute a killing test to the module: a mutant in a module that no test imports directly
survives unattributably even when cross-file tests exercise its behavior. This invariant
originates in #665 — *not* ADR-0229, which only folds the mutmut env shims into the `just mutate`
recipe (its clean "0 mutants generated" report for import-time-only modules is referenced below).

**#665 (2026-06-21).** A reproducible AST scan (no test under `tests/` imports the module by
dotted path) found **25** such modules on `main` (the original "22" was approximate;
`config/manifest.py` had since gained a test, and the scan surfaced a few small contract
modules). Each gained a direct unit test; per module:

- **Mutated to 0 surviving (function-body targets):** `mcp/middleware/shared` (12),
  `mcp/middleware/telemetry` (126), `mcp/middleware/usage` (77), `mcp/middleware/exposure` (19),
  `mcp/middleware/denial_audit` (78), `mcp/tools/ops/_reads` (36),
  `providers/local_libvirt/lifecycle/rootfs_catalog_fetch` (16), `services/runs/bind` (23, its
  pure `_run_bindable_error`). `services/runs/admission` (pure helpers: 145 generated, **8
  surviving — all equivalent**: the `cast` runtime no-op, `model_dump` `mode=` variants identical
  for an all-`str` model, the unobservable `<`/`<=` lease-expiry boundary, the `kind=None`→`""`
  sentinel that rejects identically, and a `detail=detail` drop that re-defaults to the same
  string). The async Postgres-locked admission/bind create flow stays a bucket-1 target.
- **Covered, 0 mutatable mutants (import-time-only declarations):** the three provider
  `settings.py`, `services/runs/states`, `domain/lifecycle/rules`, `providers/shared/build_timeouts`,
  `domain/catalog/{image_format,ownership}`, `db/probe_fence`, `providers/ports/handles`,
  `domain/_records`, `diagnostics/provider_contracts`, `domain/profile_documents`, `profiles/types`.
  Their code runs only at import / in a class body, so `mutate_only_covered_lines` (under
  `max_stack_depth=8`) records nothing to mutate; the direct tests still catch a changed default,
  dropped state, renamed enum value, or altered field set. `just mutate` now reports this as a
  clean "0 mutants generated — no covered, mutatable lines" (ADR-0229) rather than a baseline
  failure.
- **Covered, but reclassified to "could not be swept" (below):** `inventory/_row_typing` and
  `mcp/middleware/binding_errors`.

**Reopened by post-sweep modules; re-closed by #1298 / #1304.** Between 2026-06-27 and 2026-07-16,
**13** new modules landed with no test importing them directly (verified by git add-dates + an
import scan on 2026-07-19). They were behaviorally covered *indirectly* (89–100% line coverage via
MCP-layer tests), so the gap was mutation-attributability, not behavior. `images/rootfs/stage_volume_wiring.py`
— also a real coverage gap — was closed by #1298; the other **12** gained direct mirror unit tests
in #1304 (each imports its module by dotted path; PG-independent "fast" targets):
`services/investigations/{metadata,lifecycle,view}`, `mcp/tools/ops/audit/{read_pipeline,registrar}`,
`mcp/tools/ops/inventory/registrar`, `mcp/tools/_vmcore_kdump_gate`,
`images/cataloging/{object_keys,read_model}`, `images/rootfs/baseline`, `providers/shared/host_cpu`,
`jobs/handlers/runs/ports`. The bucket is closed again.

### Could not be swept this run (46)

- **mutmut copy-scope / baseline (≈15):** the module's covering test reads files mutmut does
  not copy into `mutants/` (e.g. the top-level `docs/` tree), or a `tests/conftest.py`
  re-import fails in the copy. Examples: `mcp/resources/registrar.py`, `mcp/assembly/app.py`,
  `config/external_env.py`, `security/secrets/secret_registry.py`,
  `version.py`, and `mcp/middleware/binding_errors` (its import chain resolves a source path that
  404s as `mutants/<frozen importlib._bootstrap>` in the copy — covered by a direct test, but the
  baseline cannot run).
- **mutmut import-time trampoline crash (`db/repositories.py`, re-investigated by #1401):**
  `repositories.py` builds ~15 module-level repository singletons (`RESOURCES =
  StatefulRepository(...)`, …) at import, invoking its own now-trampolined `Repository` /
  `StatefulRepository` / `KeyedRepository` constructors during module import. mutmut's
  `record_trampoline_hit` walks the caller stack under `max_stack_depth=8` and calls
  `Path(co_filename).resolve(strict=True)` on each frame; at import time one frame is
  `<frozen importlib._bootstrap>`, which resolves to a nonexistent
  `mutants/<frozen importlib._bootstrap>` and raises `FileNotFoundError`, aborting the baseline
  before any mutant runs (the `deploy`/other-module collection errors seen intermittently are a
  downstream artifact of the coverage phase's module-unload, not the root cause). This is the
  same trampoline/frozen-bootstrap class as `mcp/middleware/binding_errors`, and it is a mutmut
  limitation with import-time-invoked mutated code — not a `repositories.py` defect. The module
  stays behaviorally covered by `tests/db/test_repositories.py`; the fix belongs upstream (guard
  the `<frozen …>` pseudo-filename before `resolve(strict=True)`) or would require moving the
  singleton construction out of import time (a source change out of scope for a test sweep).
- **mutmut cannot attribute a covering test (≈1):** `inventory/_row_typing` reaches 100% line
  coverage and mutmut generates mutants for its `@dataclass(frozen=True, slots=True)` `RowTyper`
  methods, but the per-mutant coverage map finds no covering test at any `max_stack_depth`, so it
  stops early. Covered by a direct test (all validator accept/reject paths); not unit-mutatable
  here.
- **No covered/mutatable lines (≈17):** logic is reached only through async event-loop
  frames (deep `asyncio.run` stacks exceed `max_stack_depth`, so `mutate_only_covered_lines`
  records nothing) or only via PG-backed/cross-file tests. Examples:
  `services/allocation/admission/core.py`, `jobs/handlers/runs_*.py`,
  `inventory/reconcile*.py`.
- **No mutable surface (≈8):** contract-only modules — `Protocol`s with `...` bodies, frozen
  dataclasses, bare Pydantic field declarations. mutmut generates nothing to mutate; the
  primary tests already pin field sets / frozenness / structural checks behaviorally.
  Examples: `providers/ports/{debug,retrieve,build_transport}.py`,
  `domain/lifecycle/shapes.py`, `domain/operations/jobs.py`.
- **Cross-file kill deferred (≈1):** the killing test belongs in a non-primary test file
  that another bucket owned (skipped to avoid a cross-agent merge conflict).

## Resuming

The mapping is reproducible. Re-running a bucket is cheap: already-clean modules report
0 surviving and are skipped. The "no direct unit test" bucket is closed (#665, re-closed by
#1298 / #1304); the next sweep's remaining work is the 112 PG-backed targets (serial, dedicated
run with `docker ps` cleanup afterward) and the tooling/structure-blocked set above.

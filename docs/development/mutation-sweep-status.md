# Historical mutation sweep results

This record preserves the mutation counts, test-gap discoveries and follow-up attribution from
the campaign that began on 2026-06-21, with later results through #1419. It is evidence for
[ADR-0229](../adr/0229-mutation-shim-fold-in.md), not a current coverage report or work queue.
Paths, test selections and issue dispositions below describe the recorded runs. Use the
[mutation-testing guide](mutation-testing.md) for the current procedure.

## Scope and interpretation

The initial inventory reported 407 source modules, 273 container-free candidate targets and
112 Postgres-backed targets; a later direct-import scan found 25 modules rather than the initial
approximation of 22. These are different checkpoints, not a reconciled partition of today's tree.
The record reports 30 buckets, approximately 3,700 killed mutants across roughly 210 commits, and a
6,349-test gate pass. It also reports 46 fast targets that could not be swept in the initial run.
The no-direct-test follow-ups were #665, then #1298 / #1304; backend follow-ups are recorded below.

Counts are retained as reported, not re-measured. A survivor means the selected run did not kill
that mutation. Earlier blanket “equivalent” classifications overstated this evidence: serial
lock-key tests, UTC-only time tests, bounded-output assertions over unbounded queries, omitted
profiles and timeouts leave behavior unproven. The explanations below are observations about
those selections, not exemptions from concurrency, resource, logging or error-path contracts.
“Zero” results and completed follow-ups apply only to their recorded scope.

## Tooling decision

The campaign's manual import shim and shared-environment workaround were folded into the wrapper
by [ADR-0229](../adr/0229-mutation-shim-fold-in.md). The current guide owns their scope and cleanup;
the obsolete manual shim and editable-install repair commands are omitted here. The observed
failure was on Python 3.14 with beartype 0.22.9:
`ImportError: cannot import name 'claw_state'`.

## Recorded follow-up runs and limitations

### Postgres/container-backed targets (#1306)

Their only covering tests use container fixtures — the `migrated_url`/`pg_conn`/`postgres_url`
Postgres fixtures, or the `minio_store` MinIO fixture. Sweeping these spins up testcontainers
per run; the recorded sweep used serial runs in a dedicated session.
Subsystems: `services/`, `store/`, `db/`, most `jobs/handlers/`, and the Postgres-backed
`inventory/`/`reconciler/` paths. #1306 was the tracking epic for these subsystem buckets.

**Recorded results:**

- `store/objectstore.py` — 569 mutants, 23 → 0 surviving. The survivors were assertion gaps
  in the `_infrastructure_error` mapping (op label, key, and carried S3 error code went
  unasserted), the `err.response.get("ResponseMetadata", {})` defensive default (never
  exercised), and the `put_artifact` `content-encoding` metadata key (a case-mutation that a
  live MinIO round-trip cannot distinguish, because S3 lowercases user-metadata keys — killed
  with a fake-client exact-key assertion). The recorded selection included all four covering test
  files and the
  `minio_store`-gated round-trips: one read (`head`'s `content-encoding`) is attributable only
  through the container round-trip, so the reported 0-surviving result depends on including those
  gated files.
- `store/assembly.py` — a no-direct-test module (previously imported only cross-file by
  conftest/`test_app`), not itself container-backed; gained a direct mirror unit test
  (`tests/store/test_assembly.py`, #1405, #665 pattern) pinning both branches of
  `build_object_store_assembly` (provided `store_factory` used verbatim; default falls
  through to `object_store_from_env`). 3 mutants, 0 surviving.
- `db/` bucket (#1401) — the container-backed repository / lock / idempotency / migration
  modules, swept serially against their `pg_conn`/`migrated_url`/`postgres_url` covering
  tests (plus `tests/adversarial/test_{idempotency_concurrency,lock_key_properties}.py`).
  Per module: `db/pool.py` 19 mutants, 6 → 0; `db/idempotency.py` 145, 31 → 21 residual;
  `db/locks.py` 76, 8 → 8 residual; `db/migrate.py` 75, 7 → 5 residual;
  `db/probe_fence.py` 0 mutants generated (import-time-only, per #665). Assertion gaps
  killed by pinning the created pool's `min_size`/`max_size`, the run-step error path
  (`run_id`/`step` threaded into the non-JSON, unknown-state, and completion messages),
  `complete_run_step`'s returned value, and both colliding filenames in the duplicate-migration
  error. The recorded residuals fell into six DB-specific classes:
  1. **Postgres case-folded SQL** — mutating keyword/identifier case (`SELECT`→`select`,
     `run_steps`→`RUN_STEPS`) is a no-op: keywords are case-insensitive and unquoted
     identifiers fold to lowercase.
  2. **Error-message `path`/`run_id`/`step` on DB-sourced values** — these arguments feed
     only `ensure_json_value` / `parse_persisted_run_step_state` *error* messages, which the
     selected tests did not trigger for values read back from `jsonb` or a
     CHECK-constrained column,
     so they did not establish those error-message contracts.
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
  funding-gate + clamp boundaries. The recorded residuals included the `db/` classes and these
  allocation-specific observations:
  1. **Postgres case-folded SQL** — keyword / unquoted-identifier case (as in `db/`).
  2. **Advisory-lock key args** — changing a lock key can change which operations serialize.
     The serial selection did not establish the concurrency contract; later #1417 fixtures
     below demonstrate how identity-lock mutations can be killed.
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
     normalized to lowercase by OpenTelemetry; `datetime.now(None)` produces a naive local time,
     unlike aware `now(UTC)`; the UTC-only
     relative-window tests did not establish other timezone behavior; the `resolve_replay` operation
     label only shapes
     an error string; and a denial `GateResult`'s `devices` list is never read (only the grant
     path reads it).
  `admission/core.py` received a follow-up (#1410, the #1398 follow-up): the deferred
  queue-on-capacity enqueue path was taken 36→17 (17 residuals recorded) by pinning the
  `_enqueue` persistence snapshot (`requested_arch`/`shape`/`requested_pcie_specs`) and the full
  enqueue audit row (tool/object_kind/transition/args_digest/project) in
  `tests/services/test_allocation_enqueue.py`. The PCIe-busy `_resolve_pcie_claim` read was
  already killed by the existing busy-device denial + enqueue tests; its lone survivor is the
  `devices=[]`→`None` on a *denial* result (class 6, never read). The residuals were
  the same classes as the grant sweep: Postgres case-folded SQL (5), denial-`GateResult`
  `devices=[]`→`None` (6), cosmetic `operation_label` (3), `datetime.now(UTC)`→`now(None)` on a
  UTC host (1), and the removed `resource_id=None`/`lease_expiry=None`/`pcie_claim=[]` kwargs
  that equal the `Allocation` field defaults (2).

- `services/runs/` bucket (#1399) — the run admission / bind / state-transition /
  build-finalization modules, swept serially against their `migrated_url` covering tests
  (`tests/services/runs/*`, plus `tests/adversarial/test_runs_bind_races.py`). Per module
  (mutants, surviving before → after): `states.py` 0 (module-level constants, no mutable
  surface); `liveness.py` 68, 11→0; `host_admission.py` 91, 2→0; `bind.py` 215, 35→13
  residual; `steps.py` 76, 12→7 residual; `complete_build.py` 261, 65→9 residual;
  `admission.py` 503, 48→18 residual. (Note the naming trap: the adversarial
  `test_admission_*` files cover `services/allocation/admission`, not this bucket's
  `services/runs/admission.py`; a fast direct cover — `tests/services/runs/test_create_flow.py`
  — was added for the runs create flow.) Assertion gaps killed by pinning the storm-hit
  threshold, argument passthrough in the liveness fakes, the full bind/create/complete_build
  success snapshots + audit rows (tool/object_kind/transition/args-digest), reject-path error
  object_id/category/details, the chunked-reassembly copy/final-key + cleanup, and the
  cmdline_for branch matrix. The recorded residuals were grouped as follows:
  1. **Postgres case-folded SQL** — keyword / unquoted-identifier case (as in `db/`).
  2. **Advisory-lock key args** — changing a lock key can change which operations serialize.
     The serial selection did not establish the concurrency contract; later #1417 fixtures
     below demonstrate how identity-lock mutations can be killed.
  3. **Defense-in-depth re-checks** — a pre-lock reject (run-bindable, alloc-hostable,
     cross-project) dropped in `_resolve_*`/`_bind_locked` is re-caught under the lock by
     `check_host_preconditions` / the `IS NULL` compare-and-set in those tests. That final
     outcome does not establish behavior under concurrent changes between checks.
  4. **Coupled-condition tautologies** — `precond is not None or ok is None` (and the
     `chunked and store is not None`) are logically equal to the mutated `and`/`or` form
     because the two operands are always coupled (`ok is None` ⟺ `precond is not None`;
     `store` is set iff `chunked`).
  5. **`typing.cast` / `model_dump(mode=…)` no-ops** — `cast(t, v)` returns `v` unchanged
     regardless of `t`, and `model_dump`'s mode is unobservable for a payload with no
     non-JSON-native fields.
  6. **Naive-vs-UTC + DB-overwritten timestamp** — the run observed database-overwritten
     timestamps (ADR-0016). A UTC host alone does not make naive local and aware UTC values
     equivalent; the timestamp must actually be overwritten before any observable use.
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
  `reports/sections.py` 215, 65→28 residual; `debug/detach.py` 26, 2→2 residual;
  `debug/sessions.py` 19, 0 (already clean); `systems/validation.py` 59, 12→0;
  `images/retention.py` 76, 19→18 residual (killed the `pruned += 1`→`= 1` count mutant);
  `images/publish.py` 266, 37→18 residual; `images/upload.py` 295, 76→8 residual;
  `accounting/ledger.py` 430, 34→24 residual; `systems/admission.py` 336, 70→28 (residuals
  **plus a deferred killable remainder**, below). The earlier commits on this branch swept the
  idempotency / artifacts-listing / debug-lifecycle / investigations (`read`/`view`/`lifecycle`/
  `metadata`/`common`) / reports (`render`/`artifacts`/`core`) modules to zero-or-residual outcomes.
  Gaps
  killed by pinning: the section scope-isolation / cap-truncation / activity effective-window /
  costs value+window paths; the fail-closed upload quota / oversize / clamp-expiry pure decisions
  and their error+audit contracts; the publish digest-mismatch / HEAD-gate error contracts; the
  reconcile missing-size + started-but-unended active-hours guard; the provision persisted-System
  fields + audit + job + quota boundary + recycle failure. Residuals included the `#1399`
  classes (Postgres case-folded SQL; advisory-lock key
  args; `cast`/`model_dump` no-ops; naive-vs-UTC DB-overwritten `now`) plus:
  1. **`cap + 1` fetch sentinel** — the section pagers fetch `cap + 1` to detect truncation;
     any value `≥ cap + 1` (including `None` = `LIMIT NULL` = no limit, or `cap + 2`) yields an
     identical `_capped` output (`rows[:cap]`, `truncated = len > cap`) in those assertions.
     This does not establish equivalent query cost or memory use: removing the limit can
     fetch an unbounded result. The `< cap + 1` output-changing mutant was killed.
  2. **Empty-clause accumulator** — the first `clause += …` on an empty `clause` equals `=`.
  3. **`_log.*` log-statement mutations** — argument/message mutations on `_log.info`/`_log.warning`
     calls (the retention / publish best-effort legs) were not asserted, matching the
     campaign's `do_not_mutate_patterns=logger.\w+` intent (missed here as the modules use `_log`).
     That selection does not establish that changed
     diagnostics are harmless.
  4. **Unreachable fallbacks / no-op labels** — the costs `principal or ""` fallback (grouped
     principals are always non-empty), the upload identity-component error label casing (the
     security decision is pinned by the traversal guard), the cosmetic tempdir prefix, and the
     `_validate_staged(None, …)` source under the stubbed inspect seam.
  5. **Local-profile binding gaps (`systems/admission.py`)** — for the local-libvirt test
     profile the resource resolves `accel=None`/`resolved_cpu=None` and requests neither fadump
     nor a pinned CPU, so those tests did not observe fadump / cpu-pin / `resolved_cpu` binding
     changes. Other supported profiles still require their own evidence.

  **`systems/admission.py` follow-up (#1413).** Restricting the
  covering set to `tests/services/systems/` (the root `test_admission_*` cover
  `services/allocation`, not this module) left a killable remainder that needed new fixtures,
  not just assertions: (a) the `accel`-resolution mutants in `_resolve_new_system_bindings` /
  `_insert_*` need a KVM/TCG caps-bearing resource whose `capability_view` resolves a non-`None`
  accel (the `FakeLibvirtConn` resource yields `accel=None`); (b) `_provision_create_response`'s
  `is FAILED` branch converges with the recycle branch unless a **failed provision job**
  (dedup-keyed) is seeded so `_failed_system_retry_failure` diverges; (c) the enqueue dedup-key
  `allocation_id=None` mutant needs a job idempotency/dedup-key assertion. #1413 added
  `tests/services/systems/test_admission_host_bindings.py` (a `guest_arches`-bearing resource
  seeded onto the bound Resource; a `failed` System plus a dedup-keyed failed provision job;
  a `{allocation_id}:provision` dedup-key assertion), taking `systems/admission.py` from
  **28 → 13 surviving** — all 15 killed mutants are the three deferred clusters (accel 13,
  `is FAILED` 1, dedup-key 1). The follow-up left 13 survivors in `systems/admission.py` — the
  local-profile fadump / cpu-pin / `resolved_cpu` binding
  gaps (item 5 above; the local test profile does not establish other profiles),
  the Postgres case-folded SQL, and the DB-overwritten naive-vs-UTC `now`.

- `jobs/handlers/` bucket (#1402) — the durable worker job handlers (provision / build / install /
  boot / capture / control), swept serially against their `migrated_url` / `minio_store` covering
  tests (`tests/jobs/handlers/**`, `tests/jobs/test_{image_build_handler,capture_telemetry,
  diagnostic_sysrq}.py`, `tests/adversarial/test_vmcore_capture_idempotency.py`). Per module
  (mutants, surviving before → after): `runs/registrar.py` 26, 0→0; `diagnostics.py` 28, 0→0;
  `runs/{common,install,ports}.py` and the async-frame `runs_*` modules 0 mutants (import-time /
  deeper than `max_stack_depth`, per #665; `ports.py` mirror-tested by #1304);
  `console/capture_telemetry.py` 57, 3→3 residual; `console/console_evidence.py` 50, 22→17
  residual; `console/console_rotate.py` 207, 29→25 residual; `runs/boot.py` 59, 32→0;
  `image_build.py` 14, 2→0; `artifacts/vmcore.py` 170, 35→3 residual;
  `connectivity/ssh_authorize.py` 143, 41→0; `connectivity/ssh_reachable.py` 152, 31→11;
  `control/control.py` 113, 18→2 residual; `control/watch_for_crash.py` 232, 54→20;
  `control/diagnostic_sysrq.py` 324, 57→14. Assertion gaps killed by pinning: collaborator-arg
  passthrough into the `boot_evidence` / `redacted_console_tail` / retriever / probe seams; the
  full audit rows (tool / object_kind / transition / args_digest / project); the boot/capture
  result dicts + returned ids; provider-kind tags (`take_provider_kind`); the SSH argv hardening
  options + fail-closed error messages + remediation strings; the console decode-error handler on
  raw guest bytes (invalid-UTF-8 round-trips); and the watch/sysrq settle-poll and byte-cap
  boundaries. The recorded residuals included the same classes as `#1399`/`#1400`
  (Postgres case-folded SQL; advisory-lock key args; codec-name case; naive-vs-UTC `now`;
  log-statement `_log.*` mutations; defense-in-depth re-checks masked by a sibling guard) plus:
  1. **Codec error-handler on already-valid UTF-8** — `redacted_console_tail` decodes the bytes
     `read_redacted_console` already re-encoded to valid UTF-8, so its `"replace"` handler never
     fires; the mutant is unobservable (the *source* read, over raw disk bytes, is killed).
  2. **Value only in a fail-closed error detail** — `ensure_method_match`'s `run_id` and similar
     feed only a not-taken error path (method-mismatch), killable only with a cross-method fixture.
  3. **`_real_probe` deadline/backoff/read-timeout arithmetic + socket params** — the SSH-reachable
     banner probe's timing (`<0` vs `<=0`, `suppress(None)`, `open_connection(None)`→loopback) is
     unresolved by that selection; a later fake-clock socket harness killed these survivors.
  4. **Poll-loop mutants caught as a timeout** — `watch_console_for_crash`'s `match = None` makes
     the loop never fire, and was reported as failure or timeout. A timeout remains an execution
     outcome to investigate, not evidence of equivalence or a deterministic assertion kill.

  **Tooling note (fixtures sandbox).** `image_build.py` resolves its rootfs catalog by a
  `__file__`-relative path into the repo-root `fixtures/` tree, which mutmut's `source_paths=
  src/kdive` sandbox does not copy, so its baseline aborts. The campaign used a
  `mutants/fixtures -> ../fixtures` symlink to supply those resources. That was a local
  workaround, not wrapper behavior; a target/test-selection change discards the cache and
  its symlink. The current guide explains cache lifetime.

  **Follow-up on deferred survivors (#1415).** Three large handlers were deferred from this
  session and carried killable survivors in the same audit / message / arg-passthrough / core-logic
  clusters handled above; the #1415 follow-up reported 0 further killable survivors under
  that selection, with residuals in the same classes. Per module (mutants, surviving before →
  after): `control/
  capture_traffic.py` 356, 99→29 residual; `runs/boot_evidence.py` 290, 105→26 residual;
  `systems.py` 766, 143→27 residual (a further ~57 mutants intermittently report `[timeout]`
  under container contention; a serial rerun reportedly killed them). The differing outcomes
  remain a determinism limitation; rerunning does not explain or resolve the contention.
  The kills pinned the changed-state / unsupported-provider /
  unwritten-pcap / no-snapshot error messages + details; the audit-row tuples (tool / object_kind /
  object_id / transition / args_digest / project) across provision / reprovision / restore /
  teardown / capture; the returned ids and their run_id / artifact columns; the provider-kind tags;
  the collaborator-arg passthrough into the capture loop, the provider provision/reprovision call,
  the boot-evidence record/capture seams, and the snapshot attach/detach/create/revert/delete calls;
  the derived-domain fallback; the DB-backed job-cancel re-check; the resolved-cpu / billing /
  fingerprint / upload-rootfs-commit + manifest-delete transitions; and the console-part object
  reclaim. The recorded residuals were grouped with the sibling
  buckets: Postgres case-folded SQL keywords, advisory-lock key args, `_log.*` statement mutations,
  the `search_text` before/after/max-matches params (they change only discarded context lines or a
  capped `>0`), `max_polls`' dead `max(1, …)` floor (a positive-int duration is always ≥ the poll
  interval), the codec error-handler on already-valid UTF-8, `_unlink_quietly`'s `missing_ok`
  masked by `suppress(OSError)`, the `operation=` arg that feeds only a failure-path log line, and
  defense-in-depth re-checks masked by a sibling guard. The two smaller fixture-dependent deferrals
  were also **SWEPT by #1415**: `control/watch_for_crash.py` and `control/diagnostic_sysrq.py` gained
  a recording `console_log_path` fixture so the handler is pinned to read *its own* System's console
  (killing the `console_log_path(None)` mutant in each — watch_for_crash 20→19, diagnostic_sysrq
  14→13, residuals the same poll-loop / byte-cap / log residuals), and `connectivity/
  ssh_reachable.py` gained a fake-clock socket harness (a manually-driven coroutine over a patched
  asyncio clock / connect / read / sleep) that pins every `_real_probe` connect/backoff/read timeout,
  the exact deadline boundary, the derived host/port, the `suppress(OSError)` on close, and the
  handler's `datetime.now(UTC)` stamp — **11→0 surviving**.

- `inventory/` bucket (#1403) — the override-ledger / serializer / writeback / CLI / loader modules
  and the `inventory/reconcile/` merge-reconcile passes, swept serially against their `migrated_url`
  covering tests (`tests/inventory/*`, `tests/integration/test_reconcile_{inventory,coefficients}.py`).
  Per module (mutants, surviving before → after): `path.py` 19, 0→0; `reconcile/pipeline.py` 36, 0→0;
  `reconcile/{locks,records}.py` 0 mutants (import-time / dataclass-only, per #665); `loader.py` 27,
  1→1 residual; `cli.py` 41, 14→0; `overrides.py` 103, 9→8 residual; `serialize.py` 381, 39→8
  residual; `writeback.py` 263, 11→8 residual; `reconcile/coefficients.py` 66, 10→7 residual;
  `reconcile/overrides.py` 114, 11→3 residual; `reconcile/images.py` 482, ~56→54 (residuals plus a
  deferred killable remainder, since **follow-up recorded in #1417**, below);
  `reconcile/resources.py` 568,
  70→53 (residuals plus a deferred killable remainder, since **follow-up recorded in #1417**,
  below).
  Assertion gaps killed by pinning: the `reconcile-systems` CLI diff
  headers + em-dash detail suffix + absent-default no-op line; the override-ledger `created_at`
  round-trip; the serializer's DB read-path field mapping across every image source shape and resource
  kind (incl. `_cap_int`/`_opt_cap_int` bool/non-int rejection); the writeback `api_base` trailing-slash
  strip + client write-timeout; the coefficient created/updated record content; the override-GC cleared
  **count** (settled/converged/retained/two-entry/empty); the image realize/prune/provenance paths
  (registered-row preservation on a file edit, absent-object stays-defined, fresh-build defined
  placeholder, staged-path sidecar build-verified-not-attested, prune record label, idempotent
  no-spurious-UPDATE); and the resource deterministic name (+ empty-cleaned fallback), record labels,
  local-libvirt overlay + idempotency, and fault-inject field-change update. Residuals included
  the sibling classes (Postgres case-folded SQL; `_log.*`
  log-statement mutations; `cast` no-ops; `.lower()`-normalized `off` default; `fdopen`/`read_text`
  encoding no-ops on a UTF-8 host; a sort key over a unique cost-class name; a no-op `+=` on a zero
  accumulator; a convergent insert-conflict-reread; a `_S3Head` literal-case / registered-preserve
  masked by a sibling check; build/s3 `volume` always-`None`; an ownerless-config-key `visibility`
  arg) plus:
  1. **mutmut-unattributable `RowTyper` callers** — `resources._{upsert,local,discovered}_row`
     map columns through the frozen-slots `RowTyper` (`inventory/_row_typing`, the documented
     can't-attribute case): mutmut's per-mutant covered-lines map finds no covering test for these
     call sites even when a test that would kill them is the sole covering test, so they survive
     unattributably (the row helpers are behaviorally pinned by the field assertions above).
  2. **ConfigMap default-name literal (`writeback`)** — `resolve_writeback_target`'s
     `or "kdive-systems"` default is behaviorally pinned by `test_factory_configmap_in_a_pod_...`
     (asserts `_name == "kdive-systems"`), but mutmut cannot attribute that test to the literal
     (same limitation class as (1)).

  **`inventory/reconcile/` follow-up (#1417).** The deferred killable remainder was killed by new
  fixtures in `tests/integration/test_reconcile_inventory.py` (no source change). `reconcile/images.py`
  **54 → 52 surviving**: killed the `_update_entry` config-upload store passthrough (a staged-path row
  whose `.config` sibling appears only *after* creation, so the upload runs on the update pass — a
  store arg dropped to `None` there raises on `put_artifact`) and the `_prune_departed` `continue`→
  `break` (a kept config row seeded before a departed one, so a `break` would spare the departed row).
  `reconcile/resources.py` **55 → 31 surviving**: killed the four `_needs_config_adoption` `or`→`and`
  boundaries (single-condition adoption fixtures — managed_by-only, stale-lease-only, owner-only, and
  null-name-only via host-adopt, which also killed the previously RowTyper-unattributable `_upsert_row`
  lease/owner/name reads), the three `_overlay_one_local` change-detector `or`→`and` boundaries
  (single-field overlay changes — name-only, cost_class-only, pool-only, which also killed the
  `_record` arg-drop), the `_fault_inject_upsert` host-adopt edge (a stray null-named row at the
  shared synthetic host_uri must NOT be adopted), the `_remote_libvirt_upsert` removed/detached
  disposition edges (remote-libvirt ledger fixtures), the `_prune_departed` `continue`→`break`, and
  the `_prune_departed` `name`→`None` passthrough into `prune_or_cordon_*` (two concurrency fixtures
  proving the file-departure and removed-ledger prune paths serialize on the `(kind, name)` identity
  lock — otherwise a serial test cannot observe a lock-key-only mutation). The residual survivors in
  both modules were grouped as follows — the same classes above (Postgres case-folded SQL; `_log.*`
  log-statement mutations; `cast`/RowTyper no-ops; the ownerless-config-key `visibility` arg; the
  `_S3Head` registered-preserve masked by a sibling check; build/s3 `volume` always-`None`) plus the
  falsy-default no-ops (`adopt_by_host=None`, `detached: bool = True` default never reached, the
  diff-arg drop in the append-free `_name_unconfigured_discovered`) and the `_deterministic_name`
  `.strip` string-literal quirk.

- `reconciler/` bucket (#1404) — the periodic drift-repair loop, swept serially against its
  `migrated_url` covering tests (`tests/reconciler/*`, plus `tests/integration/test_reconcile_inventory.py`
  for the inventory pass). Per module (mutants, surviving before → after): `repairs/jobs.py` 60, 14→13
  residual; `repairs/debug_sessions.py` 107, 27→27 residual; `repairs/console_rotation.py` 67, 11→6
  residual; `cleanup/uploads.py` 106, 20→19 residual; `loop_telemetry.py` 108, 25→25 residual;
  `cleanup/images.py` 84, 22→19 residual; `cleanup/provider_reaping.py` 121, 39→35 residual;
  `inventory.py` 56, 21→16 (residuals plus a chapter of chdir-tooling-blocked mutants, below);
  `cleanup/runtime_resources.py` 124, 39→36 residual; `fleet.py` 143, 38→30 residual;
  `repairs/systems.py` 275, 61→~56 residual; `cleanup/gc.py` 248, 94→87 residual;
  `repairs/allocations.py` 208, 52→50 residual; `loop.py` 184, 36→20 (residuals — the deferred
  repair-factory arg-passthrough remainder was killed by #1419, below). Assertion gaps killed by pinning the **swept-count** of every repair with a
  multi-candidate seed (each `+= 1` counter proven to sum, not fix at 1), the **skip-does-not-halt**
  contract (a skipped/failed candidate seeded before a reapable one, killing the `continue`→`break`
  mutants in the console-rotation, provider-domain, dump-volume, image-dangling, runtime-resource, and
  console-collector loops), the console-rotation `boot_id` stat identity, the reap-order state guard
  (`state not in gone_states`), the fleet capacity-total continue + all-valid no-warning + gauge units,
  the inventory pass's parsed-doc/real-path flow and drift-repair-every-pass cache, and the loop's
  console-registry wiring + config-driven publish grace. Residuals included the sibling
  classes (Postgres case-folded SQL; `_log.*` log-statement mutations incl.
  `exc_info`; advisory-lock key args; `row_factory=None`/`cast` no-ops on un-indexed results; OTel
  case-normalized metric names + advisory bucket bounds; `bool`→`None` falsy assignments; unreachable
  `else` fallbacks on always-one-row queries; defense-in-depth `or`→`and` re-check guards whose
  delete-between-select-and-lock race is unreachable in a single-connection test; audit-event field
  mutations; and the `mtime >= cutoff` naive boundary an exact-epoch match cannot reach) plus:
  1. **inventory `_cwd_inventory_shadowed` — chdir-tooling-blocked (7).** The CWD-shadow detection is
     inherently CWD-relative (`Path("systems.toml")`), so its tests use `monkeypatch.chdir`; mutmut's
     trampoline resolves the mutated source file relative to the process CWD, so a `chdir` mid-test
     aborts its baseline. Those tests live in `test_inventory_pass_cwd.py`, kept out of the sweep but
     run in the suite, so the shadow-guard / warn-once / `_cwd_shadow_warned=True` mutants are behaviorally
     covered though not mutmut-attributable. (The baseline also required a `loop`-free covering test —
     `loop.py`'s module-level `_INVENTORY_PASS = InventoryReconcilePass()` singleton runs mutated code at
     import time and trips the same trampoline — so `test_inventory_pass.py` imports only
     `kdive.reconciler.inventory`.)
  2. **`inventory.__init__`/`reset` sentinel observation.** The cache is keyed by a sha256 hex
  digest
     that can never equal `""`, so the initial/reset sentinel value is always overwritten before a
     hash-match can read it.

  **`loop.py` follow-up (#1419).** The nine repair-factory arg-passthrough mutants
  (`config.upload_store`/`config.image_store`/`conn`/`image_publish_grace` → `None` in the
  `_leaked_images`/`_dangling_images`/`_expired_private_images`/`_abandoned_uploads`/`_report`/
  `_investigation`/`_expired_build` gc / `_reconcile_inventory` factory lambdas) survived because the
  store-consuming repairs short-circuit on an empty DB (the store arg was never dereferenced). #1419
  killed them with a `reconcile_once` seed fixture (`tests/reconciler/test_reconcile_once_stores.py`)
  that seeds one live candidate per store-consuming repair and hands the pass real recording stores,
  asserting each repair's count is 1 and `report.failures == ()` (a `None`-threaded factory arg makes
  the repair raise, landing it in `failures` with a 0 count). The `_reconcile_inventory` factory's
  `config.image_store` passthrough — only dereferenced for an object-HEAD-gated s3 image — is killed by
  `test_loop_inventory_pass_threads_image_store_into_head_gated_s3` in
  `tests/integration/test_reconcile_inventory.py`. The recorded selection added those two files to
  the
  `tests/reconciler` + `tests/integration/test_reconcile_inventory.py` covering set: 184 mutants, 29→20
  surviving, **0 in the arg-passthrough cluster**. The 20 residuals concerned the timing/log/
  best-effort machinery in `_sleep_until_stop`, `_run_repair_plan`'s `_log.warning`, `_refresh_fleet_snapshot`
  (best-effort snapshot read), `_pass_loop`'s lag/interval arithmetic, and `_tick_until_stop` — the same
  classes as the sibling buckets.

**Recorded jobs follow-up:** #1415 covered the three large modules
(`capture_traffic` / `boot_evidence` / `systems`) and both smaller fixture-dependent deferrals
(`console_log_path(None)` in `watch_for_crash` / `diagnostic_sysrq`, and the `ssh_reachable`
`_real_probe` timing cluster) with no further killable survivors reported for that selection (see
above).

### Direct-import follow-ups (#665, #1298, #1304)

The campaign used tests that imported each target directly to improve mutation attribution.
This was the #665 selection strategy, not an ADR-0229 requirement or proof that a direct import
alone makes a mutant killable. ADR-0229 owns the environment shim, not a new testing policy.

**#665 (2026-06-21).** A reproducible AST scan (no test under `tests/` imports the module by
dotted path) found **25** such modules on `main` (the original "22" was approximate;
`config/manifest.py` had since gained a test, and the scan surfaced a few small contract
modules). Each gained a direct unit test; per module:

- **Mutated to 0 surviving (function-body targets):** `mcp/middleware/shared` (12),
  `mcp/middleware/telemetry` (126), `mcp/middleware/usage` (77), `mcp/middleware/exposure` (19),
  `mcp/middleware/denial_audit` (78), `mcp/tools/ops/_reads` (36),
  `providers/local_libvirt/lifecycle/rootfs_catalog_fetch` (16), `services/runs/bind` (23, its
  pure `_run_bindable_error`). `services/runs/admission` (pure helpers: 145 generated, **8
  surviving**: the `cast` runtime no-op, `model_dump` `mode=` variants identical
  for an all-`str` model, the `<`/`<=` lease-expiry boundary not reached by the selected clocks, the
  `kind=None`→`""`
  sentinel that rejects identically, and a `detail=detail` drop that re-defaults to the same
  string). The async Postgres-locked admission/bind create flow stays a bucket-1 target.
- **Covered, 0 mutatable mutants (import-time-only declarations):** the three provider
  `settings.py`, `services/runs/states`, `domain/lifecycle/rules`, `providers/shared/build_timeouts`,
  `domain/catalog/{image_format,ownership}`, `db/probe_fence`, `providers/ports/handles`,
  `domain/_records`, `diagnostics/provider_contracts`, `domain/profile_documents`, `profiles/types`.
  Their code runs only at import / in a class body, so `mutate_only_covered_lines` (under
  `max_stack_depth=8`) records nothing to mutate; the direct tests still catch a changed default,
  dropped state, renamed enum value, or altered field set. The campaign recorded the wrapper
  reporting "0 mutants generated — no covered, mutatable lines" rather than a baseline failure.
- **Covered, but reclassified to "could not be swept" (below):** `inventory/_row_typing` and
  `mcp/middleware/binding_errors`.

**Reopened by post-sweep modules; re-closed by #1298 / #1304.** Between 2026-06-27 and 2026-07-16,
**13** new modules landed with no test importing them directly (verified by git add-dates + an
import scan on 2026-07-19). They were behaviorally covered *indirectly* (89–100% line coverage via
MCP-layer tests), so the gap was mutation-attributability, not behavior. `images/rootfs/stage_volume_wiring.py`
— also a real coverage gap — was closed by #1298; the other **12** gained direct mirror unit tests
in #1304 (each imports its module by dotted path; PG-independent "fast" targets):
`services/investigations/{metadata,lifecycle,view}`, `mcp/tools/ops/audit/{read_pipeline,registrar}`,
`mcp/tools/ops/inventory/registrar`, `mcp/tools/lifecycle/vmcore/_vmcore_kdump_gate`,
`images/cataloging/{object_keys,read_model}`, `images/rootfs/baseline`, `providers/shared/host_cpu`,
`jobs/handlers/runs/ports`. That follow-up recorded the bucket as closed at that checkpoint.

### Initial unswept targets and tooling limitations (46)

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

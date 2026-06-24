# Plan — `build-log` artifact capture (#770)

Derived from `docs/superpowers/specs/2026-06-24-build-log-artifact-capture.md` and
[ADR-0238](../../adr/0238-build-log-artifact-capture.md). Implemented in this session, TDD,
on branch `feat/build-log-artifact-770`. No DB migration. Pre-assigned ADR 0238.

## Guardrails (run before every commit)

- `just lint` — `ruff check` + `ruff format --check`
- `just type` — `ty check` (whole tree). LOCAL CAVEAT: whole-tree `ty` fails on pre-existing
  unused drgn/libguestfs `# ty: ignore` comments; if and only if that exact error appears, run
  the commit with `SKIP=ty` and note it in the PR. Never mask a real type error with it.
- `just test` (focused subsets during TDD: the build-host, runs_build, and artifacts test
  modules; full suite once before push).
- `prek run` before commits.

Hard limits: ≤100 lines/function, complexity ≤8, ≤5 positional params, 100-char lines, absolute
imports, Google-style docstrings on non-trivial public APIs.

## File scope (do not stray)

`src/kdive/providers/shared/build_host/execution.py`,
`.../build_host/orchestration.py`, `.../build_host/transports/transport_seams.py`,
`src/kdive/providers/local_libvirt/build.py` (and its remote sibling iff it diverges),
`src/kdive/jobs/handlers/runs_build.py`, `src/kdive/mcp/tools/lifecycle/runs/common.py`, plus the
mirroring tests. Do NOT touch `sessions_lifecycle.py`, `profile_examples.py`, `provisioning.py`,
`registrar.py` (other agents own those). `docs/adr/README.md` additive only (already done).

## Task 1 — `CapturedStep` result + capture constant

**Where it fits:** Spec §1 (result-carrying build step). The foundation: a value that carries the
exit code plus the redacted, tail-capped combined output, replacing the bare `int` the
`make`/`olddefconfig` seams return.

**Files:** `execution.py` (new `CapturedStep`, `BUILD_LOG_TAIL_BYTES`, a `redact_and_cap` helper
or reuse), `orchestration.py` (`RunStep` type + the two call sites).

**Steps (TDD):**
1. Failing unit test: a `CapturedStep` built from sample stdout+stderr keeps the **tail** when
   over `BUILD_LOG_TAIL_BYTES`, and runs the `Redactor` over the text (a registered secret →
   `[REDACTED]`). Assert combined ordering (stdout then stderr, or interleaved-by-stream as
   implemented) and the 16 KiB cap.
2. Implement `CapturedStep` (frozen dataclass: `returncode: int`, `output: str`) and the
   redact+cap helper. Reuse `Redactor`/`SecretRegistry`; `BUILD_LOG_TAIL_BYTES = 16 * 1024`.
3. Change `RunStep` type alias to return `CapturedStep`. Update `transport_run_step`,
   `transport_run_make`, `transport_run_olddefconfig`, `real_run_make`, `real_run_olddefconfig`,
   `run_make_target` to return `CapturedStep`. The transport path builds it from `CommandResult`;
   the local path adds `capture_output=True, text=True` and builds it from the
   `CompletedProcess`. On `TimeoutExpired` (now carrying partial output under capture), build a
   `CapturedStep`-less but output-carrying error (see Task 3) — partial `.stdout`/`.stderr`.

**Acceptance:** the two seams return `CapturedStep`; `.returncode` is correct; `.output` is
redacted and ≤ 16 KiB, keeping the tail. `just lint type test` green on the build-host module.

**Rollback:** the change is additive to a value type; revert the seam signatures to `int` if
abandoned. No persisted state.

## Task 2 — orchestrator attaches output to the non-zero-exit failure

**Where it fits:** Spec §2 (non-zero raise site). `build_workspace` checks `!= 0` on the two
seams; on failure it must put the captured output on the raised `build_failure`.

**Files:** `orchestration.py`, `execution.py` (`build_failure` gains an optional build-log arg).

**Steps (TDD):**
1. Failing test: an orchestrator with a `run_make` stub returning `CapturedStep(2, "ld: error")`
   raises a `BUILD_FAILURE` whose details carry the captured `"ld: error"` text under a known key
   (e.g. `details["build_log"]`). Same for `run_olddefconfig`.
2. Implement: `build_workspace` reads `result = self.run_make(workspace)`; on
   `result.returncode != 0` raises `build_failure("make exited non-zero", run_id,
   build_log=result.output)`. `build_failure` puts `build_log` into `details["build_log"]` when
   present.
3. Keep the no-output paths (the dropped-symbol / missing-config `_validate_final_config` raises)
   unchanged — they carry no build log.

**Acceptance:** non-zero make/olddefconfig raises BUILD_FAILURE carrying the captured output.

**Rollback:** drop the `build_log` kwarg; `build_failure` stays back-compatible.

## Task 3 — capture partial output on the make timeout

**Where it fits:** Spec §2 (timeout raise site) + edge case "Timeout".

**Files:** `execution.py` (`run_make_target` / `real_run_make` TimeoutExpired handlers).

**Steps (TDD):**
1. Failing test: a `run_make_target` whose subprocess raises `TimeoutExpired` with
   `output`/`stderr` set raises `"make exceeded the build timeout"` BUILD_FAILURE carrying the
   partial output in `details["build_log"]`.
2. Implement: in the `except subprocess.TimeoutExpired as exc` arm, read `exc.stdout`/`exc.stderr`
   (bytes or str depending on `text=`), redact+cap, attach to the raised error's
   `details["build_log"]`. Guard for `None` (timeout before any output).

**Acceptance:** a timed-out make surfaces partial output on its error.

**Rollback:** revert the handler arms; the timeout error stays as today (no build_log).

## Task 4 — builder persists the build-log object, threads the key

**Where it fits:** Spec §2 builder half. The off-thread builder PUTs the redacted bytes
`REDACTED` and re-raises carrying the object key.

**Files:** a NEW shared helper `.../build_host/publishing/build_log.py`
(`persist_build_log(store, run_id, output) -> str | None` PUTting
`<tenant>/runs/<run_id>/build-log` with `sensitivity=REDACTED, retention_class="build-log"`,
`owner_kind="runs"`, `name="build-log"`), called from BOTH `local_libvirt/build.py` and
`remote_libvirt/build.py`. **Both builders are separate classes** (`LocalLibvirtBuild`,
`RemoteLibvirtBuild`) with their own `build()` wrapping `build_workspace` in try/finally — there
is NO shared `build()`, so the helper avoids duplicating the PUT logic across the two `build()`
bodies.

**Steps (TDD):**
1. Failing test (helper): `persist_build_log(fake_store, run_id, "ld: error")` PUTs to
   `<tenant>/runs/<run_id>/build-log` with `Sensitivity.REDACTED` and
   `retention_class="build-log"` and returns the object key; empty/blank output → `None`, no PUT.
2. Failing test (each builder): a `build()` whose `build_workspace` raises a BUILD_FAILURE
   carrying `details["build_log"]` and a fake store → the store received the build-log PUT and the
   re-raised BUILD_FAILURE carries `details["build_log_artifact"]` = the key. A BUILD_FAILURE with
   **no** `build_log` (e.g. modules_install failure, missing build-id) → no PUT, error unchanged.
3. Failing test: a store PUT that raises → the original BUILD_FAILURE still propagates (swallow),
   no different error.
4. Implement `persist_build_log`; in each `build()`, wrap the `build_workspace` call so a caught
   `CategorizedError` carrying `details.get("build_log")` calls the helper (best-effort) and
   re-raises with `details["build_log_artifact"]` set when a key came back; PUT errors are logged +
   swallowed (re-raise the original). Preserve the existing `finally: cleanup_workspace`. Scope the
   catch to `build_workspace` only, so post-make BUILD_FAILUREs (modules/build-id/publish) are not
   misclassified as having a build log — they carry no `build_log` detail anyway, so the helper
   no-ops, but keeping the catch tight is clearer.

**Acceptance:** a failed build PUTs a REDACTED Run-keyed build-log object and the propagating error
carries its key. No captured output → no PUT.

**Rollback:** remove the try/except wrapper; `build()` reverts to direct propagation.

## Task 5 — worker registers the artifacts row (upsert) + records the id

**Where it fits:** Spec §2 worker half + Retry/idempotency.

**Files:** `runs_build.py` (`_fail_build` / `_build_and_record` except arm). Reuse
`register_artifact_row` + `ARTIFACTS` repo; upsert-by-key like
`_upsert_run_console_row`.

**Steps (TDD):**
1. Failing test: `_fail_build` (or the except arm) given a propagating BUILD_FAILURE carrying
   `details["build_log_artifact"]=<key>` registers an `artifacts` row (owner_kind='runs',
   owner_id=run_id, REDACTED) and writes the artifact id into the failing job's failure context as
   `failure_detail_build_log_artifact`. A second failed attempt with the same key upserts (one row,
   refreshed etag), not a duplicate.
2. Failing test: a BUILD_FAILURE with no `build_log_artifact` → no artifacts row, failure recorded
   as today.
3. Failing test: a row-insert error is swallowed; the Run still transitions to FAILED.
4. Implement: in the except arm, after `_fail_build`, if the error carries the key, upsert the
   `artifacts` row by `(owner_kind, owner_id, object_key)` and stash the resulting id so the
   worker's `_failure_context` surfaces it (either set `details["build_log_artifact"]` to the
   artifact **id** so `_failure_context` maps it, or write it directly into the recorded failure
   context). Pick whichever keeps `_failure_context`'s redaction intact — prefer letting the
   existing `details → failure_detail_*` mapping carry the **id**.

**Acceptance:** a failed build has exactly one Run-owned REDACTED `build-log` artifacts row, its id
on the failing job's failure context; retries don't duplicate it.

**Rollback:** remove the upsert block; the failure path reverts to category-only.

## Task 6 — surface `refs["build-log"]` on the failed Run

**Where it fits:** Spec §3 surface.

**Files:** `mcp/tools/lifecycle/runs/common.py` (`_failed_envelope`).

**Steps (TDD):**
1. Failing test: `envelope_for_run` for a FAILED Run whose `failing_job.failure_context` carries
   `failure_detail_build_log_artifact=<id>` returns `refs["build-log"] == <id>`. A failed Run
   without that key has no `build-log` ref. A no-leak category still suppresses the surface.
2. Implement: in `_failed_envelope`, when `failing_job` is present and not no-leak, read
   `context.get("failure_detail_build_log_artifact")`; if set, add it to the `refs` passed to
   `ToolResponse.failure`. Keep it out of `data` (don't double-surface) or leave the existing
   `failure_detail_*` flow and additionally promote into refs — decide for a single surface; the
   spec says promote into `refs`.

**Acceptance:** `runs.get` on a failed internal build advertises `refs["build-log"]`, resolvable
via `artifacts.get`.

**Rollback:** drop the refs addition.

## Task 7 — integration assertion (acceptance)

**Where it fits:** the issue acceptance — "a failed internal build exposes a retrievable build log
with the actual compiler error."

**Steps (TDD):** an integration-level test that drives the worker build handler with a make seam
that fails emitting a known compiler error, then asserts (a) a REDACTED `build-log` artifacts row
exists for the Run, (b) `runs.get` surfaces `refs["build-log"]`, (c) `artifacts.get` on that id
returns the redacted content containing the compiler error. Exercise BOTH the local seam and the
transport seam (or assert the transport seam path at the unit level if a full transport harness is
out of reach — state the limitation).

**Acceptance:** end-to-end retrievability proven for at least the local path; transport path
covered at unit level.

## Sequencing

1 → 2 → 3 (execution/orchestration capture) → 4 (builder PUT) → 5 (worker row) → 6 (surface) →
7 (integration). Each task is committed independently with green focused guardrails. Run the full
`just test` before push (Task 7 + architecture/doc tests).

## Cross-cutting cleanup

- Every `RunStep` consumer must be updated to the new `CapturedStep` return (Task 1 is the
  blast-radius task — grep all `run_make`/`run_olddefconfig`/`transport_run_step` callers).
- No new env var, dependency, or migration. `retention_class="build-log"` is a new string value
  only (no enum to extend — retention classes are free strings per the codebase).

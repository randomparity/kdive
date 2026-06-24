# Capture internal-build compiler output as a retrievable `build-log` artifact

- **Issue:** #770
- **ADR:** [ADR-0238](../../adr/0238-build-log-artifact-capture.md)
- **Status:** Accepted
- **Date:** 2026-06-24

## Problem

When an internal kernel build fails, the agent gets only `"make exited non-zero"` plus the
`run_id` (`providers/shared/build_host/orchestration.py:116`). The actual compiler error — the
one piece of information needed to fix the build — is lost:

- The **local** path (`execution.py`, `real_run_make` / `run_make_target`) spawns `make` with
  inherited stdio (no `capture_output`), so `make`'s stdout/stderr stream to the worker's own
  fds and are never captured.
- The **transport/SSH** path (`transports/transport_seams.py`, `transport_run_step`) *captures*
  stdout+stderr into a `CommandResult` but throws it away — only `.returncode` is read.

There is no `build-log` artifact. `runs.get refs` advertises `kernel` and `debuginfo` (and, after
ADR-0226, `console`) but has no build-log slot, and `artifacts.get` has nothing to fetch. The
only build-failure detail an agent sees today is pre-compile stderr (git/rsync/patch), surfaced
truncated to 2000 chars via `CategorizedError.details` → the failed-Run envelope.

## Goal / acceptance

A failed internal build exposes a retrievable build log containing the actual compiler error:

1. `make` (and `make olddefconfig`) stdout+stderr are **captured on both the local and transport
   paths**.
2. On a build-step failure the captured output is stored as a `build-log` artifact — **redacted**
   (project redaction helper) and **size-capped** — owned by the failing Run.
3. The artifact id is surfaced on `runs.get` for the FAILED Run and is fetchable via
   `artifacts.get`, which serves its redacted bytes inline (subject to the existing inline cap).

## Constraints discovered in the codebase

These shape the design; violating them breaks existing contracts.

- **`RunStep = Callable[[Path], int]` discards output.** The orchestrator's `make`/`olddefconfig`
  seams return only an exit code. To carry captured output up to the failure site without a
  second out-of-band channel, the seam must return the output alongside the code.
- **The builder runs off the event loop with no DB connection.** `build()` (and the whole
  transport session) execute inside `asyncio.to_thread`; they can PUT to the object store
  (`build()` already does, for `kernel`/`vmlinux`) but cannot register an `artifacts` **row**
  (no `conn`). Row registration must happen at the worker seam that holds `conn`.
- **`artifacts.get` serves by `artifacts` table id, gated on `Sensitivity.REDACTED`, and was
  `owner_kind='systems'`-only.** The successful build artifacts (`kernel`/`vmlinux`/`modules`)
  are NOT in the `artifacts` table — they are surfaced as `runs.*_ref` object keys and stored
  `SENSITIVE`. For the build-log to be fetchable via `artifacts.get`, it must (a) have a DB row
  and (b) be stored `REDACTED` — mirroring the per-Run console evidence (ADR-0235). But the
  handler's authorization read hard-codes `owner_kind='systems'` and resolves the project through
  the `systems` table, so a Run-owned artifact was unreachable. Since a build can have no System
  (next bullet), `artifacts.get` must be extended to admit `owner_kind='runs'` and resolve the
  project through `runs.project`.
- **A build failure produces a FAILED Run, surfaced via `_failed_envelope`** in
  `mcp/tools/lifecycle/runs/common.py`, NOT the SUCCEEDED `runs.get` path that ADR-0226 extended.
  `_failed_envelope` already surfaces the failing job's worker-redacted `failure_detail_*` keys
  into `data`. The worker turns `CategorizedError.details` into `failure_detail_*` keys
  (`jobs/worker.py:_failure_context`). So an artifact **id** placed in the error details flows to
  the failed-Run surface automatically — but a build-log id is an artifact reference, and the
  envelope already has a `refs` slot for exactly that.
- **A build can have no bound System (`system_id` nullable, ADR-0169).** So the build-log artifact
  is owned by the **Run** (`owner_kind='runs'`, `owner_id=run_id`), like the existing
  `kernel`/`vmlinux` object keys, not by a System.

## Design

### 1. Capture: a result-carrying build step

Introduce a small captured-output result and change the orchestrator's `make`/`olddefconfig`
seams to return it instead of a bare `int`.

- New `CapturedStep` value (returncode + combined redacted-capped output text), and the
  orchestrator's `run_make` / `run_olddefconfig` seams typed to return it. The orchestrator
  checks `.returncode` exactly as before; on non-zero it now also has `.output` in hand.
- **Local path** (`execution.py`): `run_make_target` / `real_run_make` pass
  `capture_output=True, text=True` to the (sandbox) subprocess and build a `CapturedStep` from
  `proc.stdout` + `proc.stderr`. Inherited stdio is replaced by capture; the build no longer
  streams to the worker's fds (acceptable — the worker logs are not the build log).
- **Transport path** (`transport_seams.py`): `transport_run_step` already has a `CommandResult`
  with `stdout`/`stderr`; build a `CapturedStep` from it instead of returning only `.returncode`.

The output is **redacted and capped at capture** (reuse the redaction helper and a tail cap, as
`redacted_tail` does for stderr today). Capping at capture bounds memory and the stored object;
the cap keeps the **tail**, where the compiler error and the failing recipe line live.

### 2. Carry the captured output to the failure site

Two raise sites must carry the captured output:

- **Non-zero exit:** `build_failure(message, run_id)` in the orchestrator (the `make` /
  `olddefconfig` exit-code check). Add the captured build-log text to the raised error.
- **Timeout:** `run_make_target` / `real_run_make` raise a *distinct* `"make exceeded the build
  timeout"` `CategorizedError` on `subprocess.TimeoutExpired`, which is the most common
  real-world build hang — exactly when an agent needs the partial log. With
  `capture_output=True`, `TimeoutExpired` carries the partial `.stdout` / `.stderr`; capture and
  attach them to that error too, so the timeout path is not a build-log black hole. (The
  transport path enforces its own `timeout_s`; if the transport raises rather than returning a
  `CommandResult`, no partial bytes exist and behavior is unchanged.)

The orchestrator/execution layer raises with the captured output attached; the builder
(`build.py`) catches the `BUILD_FAILURE`, persists the build-log object (it already owns a
store), and re-raises an error that carries the **artifact object key** so the worker can
register the row.

Because the builder has the object store but no `conn`, the split is:

- **Builder (`build.py`, off-thread):** on a build-step `BUILD_FAILURE` that carries captured
  output, PUT the redacted bytes to the object store under the Run-keyed build-log key, stored
  `REDACTED`, and re-raise a `BUILD_FAILURE` whose `details` carry the stored object key (and the
  inline output tail for the existing `failure_detail_*` surface). On any path with no captured
  output (e.g. a pre-compile checkout failure), behavior is unchanged.
- **Worker (`runs_build.py` `_fail_build`, holds `conn`):** when the propagating
  `CategorizedError` carries a build-log object key, register the `artifacts` row
  (`owner_kind='runs'`, `owner_id=run_id`, `REDACTED`) and record the artifact id on the failing
  job's failure context so the failed-Run surface can advertise it.

**Retry / idempotency.** BUILD jobs retry up to `max_attempts` (3); each attempt rebuilds while
`existing_build_result` is `None`, so a failing build can capture a log on each attempt. The
build-log object key is **Run-keyed** (`<tenant>/runs/<run_id>/build-log`), so a re-capture
*overwrites* the same object — never accumulates objects. The row is registered **upsert-by-key**
(mirroring the per-Run console row, ADR-0235): if a `build-log` row already exists for this Run's
key, its etag is refreshed in place rather than inserting a duplicate, so a Run has at most one
`build-log` artifact row no matter how many attempts failed, and `refs["build-log"]` is stable.

**Persistence must not mask the build error.** The new object PUT and row insert/upsert are on the
already-terminal failure path. A failure of either (store outage, DB error) is logged and
swallowed — the original `BUILD_FAILURE` must still propagate so the Run fails for the real
reason. A build-log-persistence error never converts a build failure into a different failure or a
success.

This keeps the object write where the bytes are (off-thread, in the builder) and the row write
where the connection is (worker), mirroring ADR-0005 write-before-commit: the object is PUT
first, the row committed after.

### 3. Surface on `runs.get` and `artifacts.get`

- **`artifacts.get`**: extend the authorization read to admit `owner_kind='runs'` (not just
  `'systems'`) and resolve the project through the owner's table (`runs.project` for a Run-owned
  artifact). Once a `REDACTED` Run-owned `artifacts` row exists, the handler then serves it by id
  (inline content subject to `KDIVE_ARTIFACT_INLINE_MAX_BYTES`). Cross-project access stays
  not-found-shaped on both owner kinds.
- **`runs.get`** (FAILED Run): surface the build-log artifact id as `refs["build-log"]` in
  `_failed_envelope`. The id reaches the envelope through the failing job's failure context
  (`failure_detail_build_log_artifact`), which `_failed_envelope` already reads; promote that one
  recognized key into `refs` (instead of leaving it only in `data`), because it is an artifact
  pointer and `refs` is the slot for artifact pointers (consistent with ADR-0226's reasoning).

### Sizing and redaction

- **Cap:** keep the trailing `BUILD_LOG_TAIL` bytes of combined output (default the same 2000-char
  order as `STDERR_TAIL`, raised to a build-log-appropriate size — see ADR for the exact value and
  rationale). The tail is where the error is; a head cap would truncate it away.
- **Redaction:** run the project `Redactor` (secret registry + key/value patterns) over the
  captured text before it is stored or surfaced, exactly as `redacted_tail` does. The object is
  stored `REDACTED` so `artifacts.get` will serve it.

## Edge cases (must be tested)

- **Empty output**: a build that fails with no captured bytes (e.g. `make` killed before emitting)
  — no build-log artifact is registered; the failed Run surfaces as before. No empty-object row.
- **Oversized output**: output far exceeding the cap is truncated to the tail; the stored object
  and the surfaced inline detail are bounded.
- **Secret-looking lines**: a line containing a registered secret or a `key=value` secret pattern
  is `[REDACTED]` in the stored object and in any inline surface.
- **Both paths**: a local-path failure and a transport-path failure each produce a retrievable
  build-log artifact containing the captured stderr.
- **Pre-compile failure (no captured output)**: a checkout/config failure with no make output
  behaves exactly as today — no build-log artifact, existing `failure_detail_stderr` preserved.
- **Olddefconfig failure**: `make olddefconfig` non-zero captures and surfaces its output the same
  way as a `make` failure.
- **Timeout**: a `make` that exceeds `MAKE_TIMEOUT_S` captures the partial output carried on the
  `TimeoutExpired` and surfaces it as a build-log artifact.
- **Retry**: two failed attempts of the same Run produce exactly one `build-log` artifact row
  (upsert-by-key), and `refs["build-log"]` resolves to it.
- **Persistence failure**: a build-log object PUT or row write that errors is swallowed; the Run
  still fails with the original `BUILD_FAILURE`, not a store/DB error.

## Out of scope

- No change to the success path: a successful build registers no build-log artifact (the compiler
  output on success is not retained — issue #770 is about *failure* visibility).
- No new MCP tool, request schema, or DB migration: the build-log uses the existing `artifacts`
  table, object store, and `runs.get`/`artifacts.get` tools.
- No streaming/live build-log tail: the artifact is the terminal captured output, not a live feed.

# ADR 0238 — Capture internal-build compiler output as a retrievable `build-log` artifact

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-24
- **Deciders:** kdive maintainers
- **Builds on:** [ADR-0235](0235-per-run-console-evidence.md) (the per-Run
  redacted-evidence artifact pattern this mirrors: PUT redacted bytes, register an
  `artifacts` row, surface the id on `runs.get`),
  [ADR-0226](0226-runs-get-console-ref.md) (the `runs.get` `refs` artifact-pointer
  convention), [ADR-0141](0141-failed-run-reason-surfacing.md) (the failed-Run
  envelope and the failing job's `failure_detail_*` surface this rides on),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (the worker redaction the build-log
  passes through).

## Context

On an internal-builder `make` failure the agent gets only `"make exited non-zero"` and
the `run_id` (`providers/shared/build_host/orchestration.py`). The compiler error — the
one thing needed to fix the build — is discarded:

- The **local** build path (`providers/shared/build_host/execution.py`) spawns `make`
  with inherited stdio (no `capture_output`), so its stdout/stderr go to the worker's
  own fds and are never captured.
- The **transport/SSH** path (`providers/shared/build_host/transports/transport_seams.py`)
  *captures* stdout+stderr into a `CommandResult`, then reads only `.returncode` and
  discards the text.

No `build-log` artifact exists. `runs.get refs` advertises `kernel`/`debuginfo` (and,
after ADR-0226, `console`) but no build log, and `artifacts.get` has nothing to fetch.
With external upload now the default kernel-build path (ADR-0234), the internal builders
remain a supported lane but are a black box on failure (black-box review defect; #770).

Four constraints bound the design:

1. **`RunStep = Callable[[Path], int]` discards output.** The orchestrator's
   `make`/`olddefconfig` seams return only an exit code, so the captured output has no
   channel to the failure site.
2. **The builder runs off the event loop with no DB connection.** `build()` and the whole
   transport session run inside `asyncio.to_thread`. They can PUT to the object store
   (`build()` already does, for `kernel`/`vmlinux`) but cannot register an `artifacts`
   **row** — there is no `conn`.
3. **`artifacts.get` serves by `artifacts`-table id and is gated on `Sensitivity.REDACTED`.**
   The successful build artifacts are *not* in that table (they are surfaced as
   `runs.*_ref` object keys, stored `SENSITIVE`). A fetchable build-log therefore needs a
   DB row and `REDACTED` storage — exactly what the per-Run console evidence does (ADR-0235).
4. **A build failure yields a FAILED Run**, surfaced via `_failed_envelope`
   (`mcp/tools/lifecycle/runs/common.py`), not the SUCCEEDED `runs.get` path ADR-0226
   extended. The failed-Run envelope already surfaces the failing job's worker-redacted
   `failure_detail_*` keys. A build can have no bound System (`system_id` nullable,
   ADR-0169), so the build-log is owned by the **Run** (`owner_kind='runs'`).

## Decision

We will capture `make` and `make olddefconfig` stdout+stderr on both build paths,
redact and tail-cap it, store it as a Run-owned `REDACTED` `build-log` artifact on a
build-step failure, and surface its id as `refs["build-log"]` on the failed Run's
`runs.get`, fetchable via the unchanged `artifacts.get`.

The mechanism splits the object write (where the bytes are) from the row write (where
the connection is):

1. **Result-carrying build step.** The orchestrator's `run_make`/`run_olddefconfig` seams
   return a `CapturedStep` (returncode + redacted, tail-capped combined output) instead of a
   bare `int`. The orchestrator still branches on `.returncode`; on non-zero it attaches
   `.output` to the raised `build_failure`. The **local** seam adds `capture_output=True`
   to the (sandboxed) subprocess; the **transport** seam builds the `CapturedStep` from the
   `CommandResult` it already captures.

2. **Builder persists the object (off-thread).** On a build-step `BUILD_FAILURE` that
   carries captured output, the builder PUTs the redacted bytes to the object store under
   the Run-keyed build-log key, stored `REDACTED`, and re-raises a `BUILD_FAILURE` whose
   `details` carry the stored object key (and the inline output tail). A failure that
   carries no captured output (a pre-compile checkout/config failure) is unchanged.

3. **Worker registers the row (holds `conn`).** When the propagating `CategorizedError`
   carries a build-log object key, `_fail_build` registers the `artifacts` row
   (`owner_kind='runs'`, `owner_id=run_id`, `REDACTED`) via the existing
   `register_artifact_row` + `ARTIFACTS.insert`, and records the artifact id in the failing
   job's failure context. Object-PUT-before-row-commit follows ADR-0005.

4. **Surface.** `_failed_envelope` promotes the recognized build-log artifact id from the
   failing job's failure detail into `refs["build-log"]`, because an artifact id belongs in
   the `refs` slot (ADR-0226). `artifacts.get` is unchanged: the `REDACTED` row is served by
   id, inline content bounded by `KDIVE_ARTIFACT_INLINE_MAX_BYTES`.

The captured output is redacted (project `Redactor`) and tail-capped at capture. The cap
keeps the **trailing** `BUILD_LOG_TAIL_BYTES` (16 KiB) — eight times the 2000-char
`STDERR_TAIL` used for pre-compile stderr, because a compiler error is preceded by many
lines of recipe echo, yet still well under the 64 KiB inline-serve cap so the whole log is
returned in one `artifacts.get`. The tail is kept, not the head, because the failing recipe
line and the error live at the end of the output.

## Consequences

- A failed internal build (local or transport) exposes a `build-log` artifact reachable in
  two hops: `runs.get` → `refs["build-log"]` → `artifacts.get`. The acceptance criterion of
  #770 is met for both paths.
- The local build no longer streams `make` output to the worker's own stdio; it is captured
  instead. The worker's logs are not the build log, so this is not a regression — and a
  16 KiB tail bounds the captured memory per build.
- No new MCP tool, request schema, authz role, or DB migration: the build-log reuses the
  `artifacts` table, object store, and the existing `runs.get`/`artifacts.get` tools. The
  failed-Run envelope gains one optional `refs` key, omitted when no log was captured.
- A new obligation: the build-log object is `REDACTED` at capture (not `SENSITIVE` then
  healed), so the redaction runs in the builder before the PUT — the registry must be in
  scope there (it already is, for git/patch error redaction). An unredacted secret in build
  output would otherwise be served by `artifacts.get`; the redaction + `REDACTED` sensitivity
  is the gate.
- A successful build registers no build-log artifact: #770 is about failure visibility, and
  retaining every successful build's compiler output is unbounded storage with no consumer.
- The failure path now does one object PUT and one row insert it did not before. Both are on
  the already-slow terminal failure path, not the hot path; a PUT or insert failure is logged
  and swallowed so it never masks the original build failure (the build error must still
  propagate).

## Alternatives considered

- **Register the row in the builder.** Rejected: the builder runs inside `asyncio.to_thread`
  with no DB connection and must stay synchronous and connection-free (it is reused over a
  transport with no worker DB access). Opening a connection there would couple the build seam
  to the worker's pool and break the off-thread contract. The object/row split keeps each
  write where its resource already lives.
- **Stream the captured output back through the result instead of the exception.** Rejected:
  `build()` returns a `BuildOutput` only on success; a failed build raises. There is no
  success result to hang a build-log on, and threading a side-channel return for the failure
  case duplicates the exception path. The `CategorizedError.details` already flow to the
  failed-Run surface via `_failure_context`, so the exception is the natural carrier.
- **Store the build-log inline in the job's `failure_context` only (no artifact).** Rejected:
  `failure_context` is worker-redacted free text capped for an envelope `detail`; a 16 KiB log
  does not belong inline in every `runs.get`/`jobs.get` envelope, and it would not be
  fetchable via `artifacts.get` (the issue's explicit acceptance). The artifact + `refs`
  pointer keeps the envelope thin and the log addressable.
- **Store the build-log `SENSITIVE` and heal to `REDACTED` later (the quarantine pattern,
  [ADR-0075](0075-objectstore-quarantine-pre-registration-writes.md)).** Rejected: build
  output is text we fully control the capture of, so we can
  redact synchronously at capture (as `redacted_tail` already does for stderr) and store
  `REDACTED` directly. The quarantine pattern exists for raw binary artifacts (vmcores) that
  cannot be redacted inline; a text build-log does not need it.
- **Head-cap the output.** Rejected: the compiler error and the failing recipe line are at the
  *end* of the build output; a head cap would discard exactly the bytes the agent needs.
- **Surface the id in `data` instead of `refs`.** Rejected: it is an object-store artifact
  pointer, which is what `refs` is for (the same reasoning ADR-0226 applied to `refs.console`).

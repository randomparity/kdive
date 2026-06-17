# ADR 0149 — Actionable retry against a failed System on `systems.provision`

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** KDIVE maintainers

## Context

Retrying `systems.provision` against an Allocation whose System already reached a terminal
`failed` state returns a bare, unactionable failure. Found during black-box MCP evaluation
(defect #3, issue [#512](https://github.com/randomparity/kdive/issues/512)).

The relevant facts (confirmed against code):

- One System per Allocation is intended (`src/kdive/domain/models.py` — `System` "one per
  Allocation"). The admission path resolves the Allocation's existing System under the
  ALLOCATION lock (`_find_system_for_allocation`) and never mints a second one for the same
  Allocation. So once provisioning fails, that Allocation's System is **sticky in `failed`**
  for the life of the Allocation.
- `_provision_create_response` (`src/kdive/services/systems/admission.py`) special-cases
  `existing is None` (mint), `DEFINED` (route to `provision_defined`), and `PROVISIONING`
  (re-enqueue). Every **other** state — including `failed` — falls through to a single
  `_failure(existing.id, data={"current_status": existing.state.value})`.
- That fallthrough emits `category=configuration_error`, `detail=None` (the `_failure`
  default), no `suggested_next_actions`. `configuration_error` is **not** a suppressed category
  (`src/kdive/domain/errors.py` `_SUPPRESSED_DETAIL`), so `suppressed_detail` passes `None`
  through — the retry envelope carries a null detail and no next action.
- The first call's precise reason is **not lost data**: the failed provision job carries it in
  `failure_context["failure_message"]`, written and **secret-redacted at the worker boundary**
  (`src/kdive/jobs/worker.py` `_failure_context`). The provision job has a deterministic
  `dedup_key` of `f"{allocation_id}:provision"` (`admission.py` `_enqueue_provision_job` /
  `_insert_provisioning_system`), and `jobs` rows are never deleted, so the job is reachable by
  that natural key from the admission path.
- ADR-0141 established the pattern of surfacing a failed object's reason by reading the linked
  job's already-redacted `failure_context` rather than re-deriving or re-redacting it.

The constraint is the no-leak/redaction seam (ADR-0123): `detail` flows through
`suppressed_detail` at envelope construction; `configuration_error` is diagnostic (not
suppressed), and the message surfaced is the worker-redacted `failure_message`, never raw
exception text re-read in the admission path.

## Decision

We will **not re-mint** a System on a retry against a `failed` System — one-System-per-Allocation
is intended — and we will instead return an **actionable, idempotent failure** that names the
state, surfaces the original redacted reason, and tells the caller the exact next step.

1. **Explicit `failed` branch in `_provision_create_response`.** Add a branch (before the
   catch-all fallthrough) for `existing.state is SystemState.FAILED` that returns a
   `_failure(existing.id, …)` with:
   - `detail`: the failed provision job's worker-redacted `failure_context["failure_message"]`,
     prefixed with a fixed actionable sentence stating the System is in `failed` and that a
     fresh System requires releasing and re-requesting the Allocation. When no job/message is
     found, the fixed sentence is returned alone (never `None`).
   - `suggested_next_actions`: `("allocations.release", "allocations.request")` — the intended
     re-provision path (release the spent Allocation, request a fresh one).
   - `data`: `current_status="failed"` (unchanged) plus `failing_job_id` and any
     `failure_detail_*` keys the worker recorded, mirroring ADR-0141 so the caller can
     `jobs.get` for full context.

2. **Read the job by its deterministic `dedup_key`.** A new connection-scoped
   `queue.get_by_dedup_key(conn, dedup_key)` returns the job for `f"{allocation_id}:provision"`
   (or `None`). No new column, no migration — the link is the natural key that
   `_enqueue_provision_job` already uses.

3. **No new redaction logic.** The surfaced `failure_message` / `failure_detail_*` are the
   **same already-redacted bytes** `jobs.get` returns. The admission path does not run the
   redactor (it owns no secret set) and does not read `str(exc)`.

4. **The remaining catch-all keeps current behavior but gains the release next-action.** The
   non-`failed` terminal/other states (`torn_down`, `crashed`, `ready`, `reprovisioning`) keep
   `configuration_error` + `current_status`, with `suggested_next_actions` pointing at the
   release path so no fallthrough is ever a bare null-detail dead end. Surfacing a provision job
   reason is `failed`-only (the others did not fail provisioning).

5. **Document the Allocation↔System cardinality** in the `systems.provision` tool docstring so
   the "a fresh Allocation yields a fresh System" rule is explicit, not guessed.

## Consequences

- **A retry against a failed System returns the same actionable detail every time** (the
  reason is read from the persisted job on each call), and names `allocations.release` /
  `allocations.request` as the exact next step. Satisfies the acceptance criteria.
- **The re-mint-vs-instruct decision is now explicit** (instruct-to-release), not an accidental
  fallthrough. One-System-per-Allocation is preserved.
- **No migration.** The job is reached by its existing `dedup_key`; no schema change, no new
  field on any model.
- **No-leak seam unchanged.** `detail` flows through `suppressed_detail`; `configuration_error`
  is diagnostic, and the surfaced message is the worker-redacted `failure_message`. The
  `data` extras (`failing_job_id`, `failure_detail_*`) bypass `suppressed_detail`, but the
  branch only runs for `configuration_error` (a non-suppressed category) and the values are the
  worker-redacted context, so no resource-existence leak is introduced.
- **One extra read on the failed branch only.** The admission path already holds a connection
  inside the lock; the job lookup is one indexed `SELECT` on the unique `dedup_key`, run only
  when `existing.state is failed`.
- **Stale-reason window is not a concern.** For the `provisioning->failed` path the provision job
  is already terminal by the time a retry is attempted (the System reached `failed` *because* the
  job dead-lettered), so `failure_message` is present on every retry.
- **Reprovision-failed Systems are handled.** A System can also reach `failed` via
  `reprovisioning->failed` (`jobs/handlers/systems.py`), which leaves the original `provision` job
  `succeeded`. The job surface (`failing_job_id` + reason) is therefore gated on
  `job.state is failed`, so a succeeded provision job is never advertised as the failing one; the
  caller still gets the fixed guidance + release/re-request actions. (Surfacing the *reprovision*
  job's reason is `systems.reprovision`'s domain, out of scope here.)

## Alternatives considered

- **Re-mint a fresh System for the same Allocation on retry.** Rejected: violates the intended
  one-System-per-Allocation invariant (`models.py`), and the failed Allocation's accounting
  (`granted -> active`) is already spent; minting again would silently consume quota and obscure
  that the *Allocation* is the unit to recycle. Instructing the caller to release+re-request
  keeps the cardinality honest.
- **Add a `runs.failing_job_id`-style `failed_provision_job_id` column to `systems`.** Rejected:
  unnecessary. The provision job's `dedup_key` is deterministic from the Allocation id, so the
  link already exists as a natural key; a new column + migration would add machinery for a link
  we can compute.
- **Surface only a fixed sentence, not the original reason.** Rejected: the issue explicitly
  wants the precise first-call reason carried forward; it already exists, secret-redacted, on the
  job. Reading it costs one `SELECT` and removes the "user had to guess" failure mode.
- **Echo `str(exc)` from a re-validation in the admission path.** Rejected: the admission path
  owns no resolved secret set, so it cannot redact correctly; the worker is the only boundary
  that does. Surfacing the worker's already-redacted `failure_context` is the correct seam
  (same rule as ADR-0141).

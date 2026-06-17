# Actionable retry against a failed System on `systems.provision` (#512)

- **Status:** Draft
- **Date:** 2026-06-17
- **ADR:** [0149](../adr/0149-failed-system-provision-retry-ergonomics.md)
- **Issue:** [#512](https://github.com/randomparity/kdive/issues/512)

## Problem

Retrying `systems.provision` against an Allocation whose System already reached terminal
`failed` returns a bare `configuration_error` with `detail: null`, no `suggested_next_actions`,
and the **same failed System id** — never an actionable path forward. The first call's precise
reason (e.g. "base image volume … is not staged …") is dropped on every retry, and the caller
had to *guess* that a fresh Allocation is required to get a fresh System.

The bare envelope comes from the catch-all fallthrough in `_provision_create_response`
(`src/kdive/services/systems/admission.py`): `existing is None` mints, `DEFINED` routes,
`PROVISIONING` re-enqueues, and **every other state — including `failed` — returns
`_failure(existing.id, data={"current_status": existing.state.value})`** with `_failure`'s
default `detail=None`. `configuration_error` is not a suppressed category, so the null detail
passes straight through `suppressed_detail`.

The reason is **not lost**: it lives on the failed provision job's worker-redacted
`failure_context["failure_message"]` (`src/kdive/jobs/worker.py:_failure_context`), and the
provision job has a deterministic `dedup_key` of `f"{allocation_id}:provision"`.

## Acceptance criteria (from the issue)

- A retry against a failed System returns the **same actionable `detail` every time** (not
  `detail: null`) and tells the user the **exact next action**.
- The re-mint-vs-instruct decision is made **explicitly**, not left as an accidental fallthrough.

## Design

See ADR-0149 for the decision and rejected alternatives. In summary:

1. **No re-mint.** One-System-per-Allocation is intended (`models.py`); the failed System stays.
   The retry returns an actionable failure that instructs the caller to recycle the *Allocation*.

2. **Explicit `failed` branch in `_provision_create_response`.** Before the catch-all, add a
   branch for `existing.state is SystemState.FAILED` that returns `_failure(existing.id, …)`:
   - `detail` = a fixed actionable sentence ("System is `failed`; release and re-request the
     allocation for a fresh System") + the failed provision job's redacted `failure_message`
     when present (fixed sentence alone when absent — never `None`);
   - `suggested_next_actions = ("allocations.release", "allocations.request")`;
   - `data` = `current_status="failed"` (unchanged) + `failing_job_id` + any `failure_detail_*`
     keys the worker recorded (mirrors ADR-0141).

3. **Read the job by its deterministic `dedup_key`.** New `queue.get_by_dedup_key(conn, key)`
   returns the job for `f"{alloc.id}:provision"` or `None`. No column, no migration.

4. **Catch-all keeps current shape + release next-action.** Non-`failed` other states
   (`torn_down`, `crashed`, `ready`, `reprovisioning`) keep `configuration_error` +
   `current_status`, now with `suggested_next_actions=("allocations.release",
   "allocations.request")` so no fallthrough is a bare null-detail dead end. The job-reason
   surface is `failed`-only.

5. **No new redaction.** The surfaced `failure_message` / `failure_detail_*` are the same bytes
   `jobs.get` returns; the admission path owns no secret set and runs no redactor.

6. **Document the Allocation↔System cardinality** in the `systems.provision` tool docstring.

## Behavioural contract

| `existing` state | provision job `failure_context` | `systems.provision` retry envelope |
|------------------|---------------------------------|------------------------------------|
| `failed` | provision job `failed` w/ `{failure_message: "..."}` | `configuration_error`, `detail=<sentence> + "..."`, `data.failing_job_id` set, next=release/request |
| `failed` | provision job absent / no message | `configuration_error`, `detail=<sentence>` (alone), no `failing_job_id`, next=release/request |
| `failed` | provision job `succeeded` (System failed during reprovision) | `configuration_error`, `detail=<sentence>` (alone), **no** `failing_job_id`, next=release/request |
| `torn_down` / `crashed` / `ready` / `reprovisioning` | — | `configuration_error`, `current_status` set, next=release/request, `detail=None` |
| `defined` | — | unchanged (route to `systems.provision_defined`) |
| `provisioning` | — | unchanged (re-enqueue) |
| none | — | unchanged (mint) |

For the `provisioning->failed` path the retry is always against a *terminal* provision job (the
System reached `failed` because the job dead-lettered), so unlike a failed Run (ADR-0141) there
is no mid-retry empty-`detail` window. A System reached via `reprovisioning->failed` leaves the
original provision job `succeeded`; the job surface is gated on `job.state is failed`, so that
succeeded job is never advertised — the caller still gets the guidance + release/re-request
actions.

## Edge / error paths to test (behaviour, not implementation)

- Failed System retry, provision job present with a redacted `failure_message`: envelope
  `detail` contains the actionable sentence **and** that message; `data.failing_job_id` is the
  job id; `suggested_next_actions == ["allocations.release", "allocations.request"]`.
- Failed System retry is **idempotent**: two/three retries return the identical envelope
  (same detail, same actions, same System id) — no re-mint, no second System row for the
  Allocation.
- Failed System retry with **no provision job row** (defensive — can't normally happen):
  `detail` is the fixed sentence alone, never `None`; no `failing_job_id` key.
- Failed System whose provision job **succeeded** (System failed during reprovision): the
  succeeded job is not surfaced (`failing_job_id` absent); `detail` is the fixed sentence alone.
- A non-`failed` other-state System (`torn_down`) retry: `configuration_error` +
  `current_status="torn_down"` + release/request next actions (no job reason).
- `failure_detail_*` keys the worker recorded are copied verbatim into `data`.
- The surfaced `detail`/`failure_detail_*` equal what `jobs.get` on the same job returns (no
  second redaction, no divergence).

## Out of scope

- Changing one-System-per-Allocation cardinality or adding a re-provision-in-place path for a
  failed System (that is `systems.reprovision`'s domain and requires a `ready`/`crashed`
  System, not `failed`).
- Surfacing reasons for non-`failed` terminal states (they did not fail provisioning).
- Any schema change — the job link is the deterministic `dedup_key`.

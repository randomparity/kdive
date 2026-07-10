# Spec — `runs.boot` explicit re-boot + replayed-job marker (#1063)

- **Status:** Draft
- **Date:** 2026-07-09
- **Issue:** #1063 (`BLACK_BOX_REVIEW.md` pain point P3)
- **ADR:** [0321-runs-boot-force-reboot](../adr/0321-runs-boot-force-reboot.md)
- **Branch:** `feat/runs-boot-force-reboot-1063` (base `main`)

## Problem

Calling `runs.boot` on a Run whose `boot` step already `succeeded` returns the
**prior** job envelope (`status: succeeded`, same `object_id`) and does **not**
re-boot. When a guest is wedged this reads to an agent as "I rebooted" while
nothing happened.

The mechanism is **not** idempotency-key replay (the black-box review's inferred
cause). It is per-step **ledger dedup**:

- `boot_run` (`src/kdive/mcp/tools/lifecycle/runs/steps.py`) → `_enqueue_step` →
  `_locked_enqueue`. The recycle decision is ledger-driven:
  `recycle = not await _has_step_row(conn, run.id, "boot")`, then
  `queue.enqueue(..., f"{run.id}:boot", recycle_terminal=recycle)`.
- When the `boot` `run_steps` row already exists as `succeeded`, `recycle=False`
  and `queue.enqueue` returns the **existing terminal job** unchanged — regardless
  of whether the caller passed an `idempotency_key`. The stable per-step
  `dedup_key` `{run.id}:boot` is what dedups.
- No `force` parameter exists on `boot_run`/`runs.boot`. Today a fresh boot
  requires a re-stage via `runs.install` with a *changed* cmdline/crashkernel,
  which deletes the `install` **and** `boot` step rows and recycles.
- The response envelope has no marker distinguishing a replayed prior job from a
  freshly-enqueued boot (`run_job_envelope` in `runs/common.py`).

Two defects:

1. **Missing re-boot semantics** — no way to request a fresh boot of the same
   installed variant without a re-stage.
2. **Replayed response is indistinguishable from a fresh boot** — the envelope
   shows `status: succeeded` with no replayed/deduped marker.

## Goals

1. Add an explicit re-boot path on `runs.boot` — a `force: bool` parameter that
   recycles the `boot` step (deletes the settled `run_steps` row so
   `recycle_terminal` resets the succeeded boot job to a fresh `queued` attempt),
   reusing the `delete_run_step("boot")` machinery the `runs.install` re-stage
   path already uses.
2. Add a `replayed` marker to the boot job envelope so a replayed prior job is
   visibly distinct from a freshly-enqueued (or recycled) boot.
3. Document that, absent `force`, a fresh boot of the same variant requires a
   re-stage — and that `force` with a reused `idempotency_key` replays the stored
   envelope (use a distinct or no key to re-boot).

## Non-goals

- No change to the idempotency-key contract (`keyed_mutation`): a repeated
  `(principal, key, kind)` still returns the stored envelope verbatim, args
  notwithstanding — this already governs `runs.install` cmdline sweeps. `force`
  is not folded into an args digest.
- No `replayed` marker on the `runs.install` envelope (out of scope; the same
  `run_job_envelope` helper stays unchanged for install — the marker is opt-in per
  call site).
- No DB migration and no new `run_steps` column: the replayed signal is the
  existing in-process `recycle` boolean at enqueue time, not persisted state.
- No change to `queue.enqueue`, `_has_step_row`, or the worker boot handler.

## Decision (summary; full rationale in ADR-0321)

### `force` re-boot path

Add `force: bool = False` to `boot_run` and thread it into `_enqueue_step`
(boot-only; the install path re-stages separately). Inside the existing per-Run
advisory-lock transaction, before enqueue:

- If `force` and the `boot` step is `running` → reject with
  `configuration_error` `data.reason = "step_in_progress"` (mirrors the
  `runs.install` re-stage guard — an in-flight boot must not be recycled).
- If `force` and the `boot` step is `succeeded` → `delete_run_step(conn, run.id,
  "boot")`. `_has_step_row` then misses → `recycle=True` →
  `recycle_terminal=True` resets the succeeded boot job (`attempt=0`, lease/
  worker/failure cleared, `queued`) and the same `{run.id}:boot` dedup key
  returns that recycled job. A fresh boot runs.
- If `force` and the `boot` step is absent (`pending`) or already recycled
  (a terminally-failed boot deletes its own row, ADR-0185) → nothing to delete;
  the enqueue is already a fresh boot (`recycle=True`).

`force=False` is byte-unchanged from today.

### `replayed` marker

`_enqueue_step` already learns the `recycle` decision. Return it from
`_locked_enqueue` (now `-> tuple[Job, bool]`) so the boot path can surface it.
The boot envelope becomes `run_job_envelope(job, run.id, replayed=not recycle)`:

- `replayed=True` — the returned job is a pre-existing one (`recycle=False`): a
  settled `succeeded` boot returned unchanged, or an in-flight `queued`/`running`
  boot deduped. **No new boot was enqueued.**
- `replayed=False` — a fresh boot was enqueued or a terminal job recycled
  (first boot, retry after failure, or `force`).

`run_job_envelope(job, run_id, *, replayed: bool | None = None)` injects
`data["replayed"]` only when `replayed is not None`. The `runs.install` call site
passes nothing → its envelope is unchanged (no `replayed` key). Boot always
passes a bool → the boot envelope always carries `replayed`.

The idempotency-key replay layer (`keyed_mutation`) is unchanged: a stored
envelope replays with whatever `replayed` value it was recorded with.

## Acceptance criteria

- [ ] `runs.boot(force=True)` on a Run whose `boot` step `succeeded` recycles the
      boot job to `queued` (`attempt=0`, `error_category=None`), deletes+recreates
      no duplicate job (still one `{run_id}:boot` row), and returns
      `data.replayed = false`, `status = queued`.
- [ ] `runs.boot()` (no force) on a Run whose `boot` step `succeeded` returns the
      prior job unchanged (`status = succeeded`, same `object_id`, one job) and
      `data.replayed = true`.
- [ ] First `runs.boot()` after a succeeded install enqueues a fresh boot job
      (`status = queued`) with `data.replayed = false`.
- [ ] `runs.boot(force=True)` while the `boot` step is `running` is rejected with
      `configuration_error`, `data.reason = "step_in_progress"`, and enqueues no
      new/recycled job.
- [ ] `runs.boot(force=True)` on a never-booted Run (install succeeded, no boot
      row) enqueues a fresh boot (`data.replayed = false`), same as `force=False`.
- [ ] `runs.install` envelope is unchanged — no `replayed` key
      (`run_job_envelope` default path).
- [ ] `_locked_enqueue` returning `(job, recycle)` leaves the install re-stage
      path behaviorally unchanged.
- [ ] The `runs.boot` wrapper exposes `force` with an agent-facing `Field`
      description covering the re-stage-free re-boot, the `data.replayed` marker,
      and the reused-idempotency_key caveat.
- [ ] `just resources-docs` refreshes any packaged agent-doc snapshot and
      `just resources-docs-check` passes (if `runs.boot` is snapshotted).
- [ ] `just ci` is green.

## Failure modes and edge cases

- **`force` + reused `idempotency_key`.** `keyed_mutation` replays the stored
  envelope before `do_work` runs, so the force re-boot does **not** fire and the
  stored (possibly `replayed=true`) envelope is returned. This matches the
  existing `runs.install` cmdline-sweep contract; the wrapper `Field` and
  docstring instruct callers to use a distinct or no key to re-boot. Not a code
  change — an existing, documented contract.
- **`force` while boot `running`.** Rejected (`step_in_progress`) rather than
  deleting an in-flight `run_steps` claim, which would corrupt the worker's
  `complete_run_step` fence. Mirrors `runs.install`.
- **Terminally-failed boot + `force`.** The boot row was already deleted on
  failure (ADR-0185), so `force` finds nothing to delete and the enqueue recycles
  the failed job exactly as a plain retry does — `force` is a harmless no-op there,
  `replayed=false` either way.
- **`replayed` on an in-flight dedup.** A second `runs.boot()` while the first
  boot is `queued`/`running` returns the in-flight job with `replayed=true` (no
  new boot enqueued) — correct: the marker means "no fresh boot was triggered,"
  not "the prior boot is terminal."
- **Concurrent `force` calls.** Both serialize on the per-Run advisory lock; the
  first deletes the row and recycles, the second finds the row absent (or a fresh
  `queued` job) and enqueues idempotently on the same dedup key — one job.

## Rollback

Pure additive. `force` defaults `False` (unchanged behavior); the `replayed` key
is a new additive envelope field consumers may ignore. No migration, no persisted
state, no schema change. Rollback is reverting the branch.

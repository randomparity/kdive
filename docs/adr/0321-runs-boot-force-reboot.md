# ADR 0321 — `runs.boot` explicit re-boot (`force`) + replayed-job marker

- **Status:** Accepted
- **Date:** 2026-07-09
- **Deciders:** kdive maintainers
- **Spec:** [`../specs/2026-07-09-runs-boot-force-reboot-1063.md`](../specs/2026-07-09-runs-boot-force-reboot-1063.md)
- **Follows:** [ADR-0299](0299-install-cmdline-iteration.md) — the ledger-driven recycle
  (`_locked_enqueue`, `delete_run_step`) this change reuses for the boot step.

## Context

`runs.boot` on a Run whose `boot` step already `succeeded` returns the prior job
envelope (`status: succeeded`, same `object_id`) and does not re-boot (#1063,
`BLACK_BOX_REVIEW.md` P3). The black-box review inferred idempotency-key replay;
the real mechanism is per-step **ledger dedup**. `_locked_enqueue` sets
`recycle = not _has_step_row(run.id, "boot")` and calls
`queue.enqueue(..., dedup_key="{run.id}:boot", recycle_terminal=recycle)`. A
present `succeeded` boot row → `recycle=False` → `queue.enqueue` returns the
existing terminal job unchanged, whether or not the caller passed an
`idempotency_key`.

Two consequences:

1. There is no way to request a fresh boot of the same installed variant. The
   only path to a fresh boot today is a `runs.install` re-stage with a *changed*
   cmdline/crashkernel, which deletes both step rows — an agent that just wants to
   reboot a wedged guest must perturb the install variant to do it.
2. The envelope for the returned prior job is byte-identical to a fresh boot's, so
   an agent cannot tell "rebooted" from "nothing happened."

## Decision

Two additive changes, both scoped to the boot lane.

**1 — `force` re-boot.** Add `force: bool = False` to `boot_run` and the
`runs.boot` wrapper, threaded into `_enqueue_step` (boot-only). Inside the
existing per-Run advisory-lock transaction, before enqueue:

- `force` + `boot` step `running` → reject `configuration_error`
  `data.reason="step_in_progress"` (an in-flight boot must not be recycled;
  mirrors the `runs.install` re-stage guard).
- `force` + `boot` step `succeeded` → `delete_run_step(conn, run.id, "boot")`.
  `_has_step_row` then misses → `recycle=True` → `recycle_terminal=True` resets
  the succeeded boot job in place (`attempt=0`, lease/worker/failure/result
  cleared, `queued`) and the same `{run.id}:boot` dedup key returns it. A fresh
  boot runs — no duplicate job, no re-stage, no Run-state flip.
- `force` + boot absent/failed (already recycled) → nothing to delete; the
  enqueue is already fresh.

`force=False` is byte-unchanged from today.

**2 — `replayed` marker.** `_locked_enqueue` returns `(Job, recycle)`; the boot
path surfaces `run_job_envelope(job, run.id, replayed=not recycle)`.
`run_job_envelope(job, run_id, *, replayed: bool | None = None)` injects
`data["replayed"]` only when the argument is not `None`. `replayed=True` means the
returned job pre-existed (`recycle=False`: settled `succeeded` returned unchanged,
or in-flight `queued`/`running` deduped — no new boot enqueued); `replayed=False`
means a fresh boot was enqueued or a terminal job recycled (first boot, retry,
`force`). The `runs.install` call site passes no `replayed` and is unchanged.

**No DB migration.** The replayed signal is the in-process `recycle` boolean at
enqueue time, derived from the `run_steps` row's presence — not persisted state.
No `run_steps` column is added.

**Idempotency key unchanged.** `keyed_mutation` still replays a stored envelope on
a repeated `(principal, key, kind)` before `do_work` runs, so `force` with a
reused key returns the stored envelope without re-booting — the same contract that
already governs `runs.install` cmdline sweeps. The wrapper `Field`/docstring
instruct callers to use a distinct or no key to re-boot.

## Consequences

- `runs.boot` gains an agent-facing `force` parameter and its envelope gains a
  `data.replayed` boolean — both additive; a consumer ignoring them sees today's
  behavior when it never passes `force`.
- An agent can re-boot a wedged guest directly (`force=True`) instead of
  perturbing the install variant, and can distinguish a real boot from a deduped
  no-op via `data.replayed`.
- `_locked_enqueue`'s return type widens to `tuple[Job, bool]`; the install
  re-stage caller discards the flag (`job, _ = ...`) and is behaviorally unchanged.
- `force` while boot is `running` fails closed (`step_in_progress`) rather than
  corrupting an in-flight claim.
- The `replayed` marker rides the same in-flight `recycle` decision, so it cannot
  drift from the actual enqueue outcome.

## Considered & rejected

- **Fold `force` into an idempotency args digest so a reused key with `force=True`
  re-boots.** Changes the surface-wide `keyed_mutation` contract (currently
  key-only, shared by `runs.create`/`install`/`boot`) for one call site, and
  makes replay behavior depend on args in a way agents would have to reason about
  per tool. Rejected — keep the uniform key-only replay; document the caveat.
- **A new `run_steps` boot column / persisted "replayed" flag.** The signal is
  already available in-process at enqueue time; persisting it adds a migration and
  a write path for zero added information. Rejected (spec/campaign: no migration).
- **Always emit `replayed` on every job envelope (install too).** Widens the
  contract of the shared `run_job_envelope` beyond the issue's scope and churns
  install tests for no requirement. Rejected — opt in per call site.
- **A separate `runs.reboot` tool.** Duplicates the boot preconditions
  (SUCCEEDED, bound, install-first) and the enqueue lane for a one-boolean
  difference. Rejected — a `force` param on the existing tool is smaller surface.
- **Auto-recycle a succeeded boot on every `runs.boot` (no `force`).** Would make
  the tool non-idempotent by default and silently re-boot on an accidental repeat
  call. Rejected — re-boot must be explicit.

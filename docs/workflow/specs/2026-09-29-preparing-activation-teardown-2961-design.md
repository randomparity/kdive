# Teardown of a preparing external-boot activation (#2961) — design

Governing decisions: [ADR 0620](../../adr/0620-authority-owned-system-teardown.md) (authority-owned
System teardown, amended 2026-09-28), [ADR 0608](../../adr/0608-run-external-boot-preparation-under-provider-authority.md)
(preparation runs under the activate job's authority). No new decision record: this change applies
ADR 0620 to a state it already names.

## Problem

`systems.teardown` refuses every System whose newest external-boot activation is `preparing`.
The refusal is `configuration_error` with reason `external_boot_teardown_authority_unresolved` and
detail "a preparing activation requires its durable preparation plan". The System then has no
supported exit (live trigger: the #2865 native ppc64le proof, recorded in #2951).

Two code facts cause it:

1. `build_external_boot_payload` (`src/kdive/jobs/handlers/external_boot/admission.py`) requires a
   preparation plan for every purpose when the activation is `preparing`. No teardown caller has
   a plan to pass, and `TeardownPayload` has no field for one. So the refusal applies to every
   preparing activation, not only to one whose activate job has no plan.
2. The shared marked-operation runner (`src/kdive/jobs/handlers/external_boot/runner.py`) runs
   `_debit_preparing` (reservation `pending` → `ready`) and `_materialize_preparing` (authority
   `materialize` and `prepare` from the job's plan) for every operation on a `preparing`
   activation. A teardown that passed admission would first debit capacity and prepare, and only
   then tear the System down.

The rest of the teardown path already admits this state. `teardown_handler` admits every
activation state except `torn_down`. The authority's `teardown_proof` gives `complete_pending`
for a pending reservation. `finalize_external_boot_authority_teardown` (migration 0147) admits a
`preparing` activation. For `complete_pending` it deletes the pending reservation, records
`pending_system_teardown` cleanup evidence, and sets the activation and the System to
`torn_down`. ADR 0620: "pending reservations never credit". A teardown allocation fences every
other purpose (migration 0161).

## Design

1. `admission.py`: the `preparing` plan requirement and the plan-identity check apply only when
   `purpose != "teardown"`. Every other purpose keeps both checks unchanged.
2. `runner.py`: one predicate, `_prepares(activation, marker)`, is true when the activation is
   `preparing` and `marker.purpose != "teardown"`. `_debit_preparing`, `_materialize_preparing`,
   and the "no preparation executor" refusal use it. A teardown of a preparing activation
   therefore goes from authority allocation and acknowledgement straight to the authority
   System teardown and its receipt.
3. `systems.teardown` (`src/kdive/mcp/tools/lifecycle/systems/admin.py`) needs no change. With
   admission fixed, it enqueues the ordinary authority-marked teardown job.

The authority, not the server, destroys the System and ends the reservation. The server never
retires the activation or deletes the reservation on its own: that needs authority terminal and
cleanup evidence (0147 constraints), and the System's host state is private to the authority.
This is the deviation from the issue's "without a provider call" wording: the change makes no
preparation call, but the System teardown call remains.

## Failure model

1. **Actors and deployments** — a project ADMIN calling `systems.teardown`; the worker that runs
   the marked teardown job; the provider authority that destroys the System; the still-running
   activate job of the same activation, which re-claims at each lease lapse; the reconciler.
2. **Invariants and assets at stake** — the activation state machine (`preparing` → `torn_down`
   is an existing edge); exactly-once reservation accounting (a pending reservation is never
   credited, a ready one credits once); ADR 0584/0620 authority fences; no provider preparation
   mutation performed only to be destroyed.
3. **Concurrency** — the teardown allocation supersedes the activate authority and fences later
   activate allocation (0161). If the activate job debited the reservation before the takeover,
   the authority sees a `ready` reservation and proves `complete_ready`, which credits once
   through the existing 0147 receipt. If not, it proves `complete_pending`. The receipt's
   reservation-state checks reject a proof that disagrees with the row, so neither ordering
   credits twice. After the receipt the activation is `torn_down`, and the activate job's next
   attempt is refused by the runner's state check.
4. **Accepted failure classes** — an incomplete authority enumeration gives
   `retained_quarantine` and requeues the teardown, as for every other state. A preparing
   activation with no `current` or `retired` authority row still gets "no unambiguous authority
   route" from `systems.teardown` (unchanged; see follow-up candidates).
5. **Covered elsewhere** — the unbounded acknowledged-retry grant (#2960); the release denial
   hint text (#2962); reuse of a System after release (#2968); the `journal-conflict` trigger
   (#2952).

## Success

- `systems.teardown` on a System whose newest activation is `preparing` returns `queued` with an
  authority-marked teardown job that carries no plan (MCP test).
- The teardown handler on a `preparing` activation calls no preparation executor, calls the
  authority teardown once, and leaves the activation and System `torn_down` with
  `pending_system_teardown` cleanup evidence, no reservation row, and no reservation release row
  (handler test against the real 0147 receipt).
- `build_external_boot_payload` still refuses `activate` on a `preparing` activation with no plan
  (admission test).

## Validation

Focused tests above, `just lint`, `just type`, `just test-changed`, and the pre-push `just ci`.
No live host run: the proof is the database-backed receipt; the #2865 trigger re-run belongs to
#2960 and #2968.

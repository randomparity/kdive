# Exhausted preactivation teardown recycle (#3617) — implementation plan

Goal: `systems.teardown` restarts an authority-owned preactivation teardown whose final attempt's
lease lapsed without a receipt, so an operator can recover the System once the cause is fixed.

Architecture: `enqueue_preactivation_teardown` hands every admissible policy to
`queue.enqueue_with_status`, whose `UPDATE` alone decides the restart, and refuses the two
policies that could restart a `succeeded` row. `systems.teardown`'s preactivation branch passes
`FAILED_OR_LAPSED_EXHAUSTED`. Docs and an ADR-0620 amendment record the behavior.
Spec: `docs/workflow/specs/2026-11-13-exhausted-preactivation-teardown-recycle-3617-design.md`.

Tech stack: Python 3.14, psycopg 3, pytest against real Postgres (`migrated_url` fixture).

Expected implementation size: 170–230 changed lines (M) — service ~15, admin 1, tests ~130,
runbooks ~20, ADR amendment ~30.

## Global Constraints

- No migration, no new ADR file, no new dependency. No request/response schema change, so the
  lifecycle identity hash does not move.
- Do not change `jobs.cancel`, the reconciler, `ops.force_teardown`'s policy, or the queue SQL.
- Ruff line length 100; `ty` strict. Prose: plain and factual; never "critical", "robust",
  "comprehensive", "elegant".
- Guardrails: `just lint`, `just type`, `just test-changed`, `prek run`.

## File map

| File | Owns now | Change |
|---|---|---|
| `src/kdive/services/systems/authority_owned.py` | preactivation teardown admission | drop short-circuit; refuse terminal policies |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | `systems.teardown` routing | preactivation branch passes `FAILED_OR_LAPSED_EXHAUSTED` |
| `tests/services/systems/test_authority_admission.py` | preactivation admission tests | two new tests |
| `docs/adr/0620-authority-owned-system-teardown.md` | teardown decisions | append amendment |
| `docs/operating/runbooks/exhausted-authority-system-provision.md` | operator recovery | rewrite the "Authority unavailable" limit |
| `docs/operating/runbooks/live-testing.md` | #3611 subsection | replace "nothing restarts it" |

`src/kdive/mcp/tools/jobs.py` (cancel fence, `NEVER`) is checked and unchanged.

## Task 1: Recycle the lapsed exhausted row from `systems.teardown`

Files: modify `src/kdive/services/systems/authority_owned.py`,
`src/kdive/mcp/tools/lifecycle/systems/admin.py`; test
`tests/services/systems/test_authority_admission.py`.

Interfaces: consumes `queue.enqueue_with_status(conn, kind, payload, authorizing, dedup_key, *,
recycle) -> tuple[Job, bool]` and `queue.JobRecyclePolicy` (existing, `jobs/queue.py`);
`authority_owned.preactivation_absence_is_proven(conn, system_id) -> bool` (existing). Signature
of `enqueue_preactivation_teardown` is unchanged.

Verification:

- Contract: lapsed exhausted row restarts with kept attempt counter, raised budget and one audit
  row; release stays fenced until a new attempt's receipt. Mode: focused-test.
  `test_exhausted_preactivation_teardown_recycles_only_after_lease_lapse[lapsed]`; red before the
  change: response object is the running job and `state == "running"`. Green:
  `uv run pytest tests/services/systems/test_authority_admission.py -k exhausted -q`.
- Contract: live final attempt and automatic `NEVER` callers do not restart. Mode: focused-test.
  `...[live]` plus the `NEVER` assertion in both params; passes before and after (regression
  guard; bite is checked by the controlled fault in step 6).
- Contract: `TERMINAL` / `TERMINAL_OR_CANCELED` refused. Mode: focused-test.
  `test_preactivation_teardown_refuses_terminal_recycle`; red before: no `ValueError`.

Steps:

1. Add the tests below the existing `test_failed_capped_teardown_restarts_and_completes`. They
   reuse the module's `_profile`, `_DIGEST`, `_ROOT`, `systems_support`, `_seed_direct_terminal_attempt`
   and `_RoleDsns`. Helper `_authority_system(pool)` provisions an authority System and returns its
   id (same body as the provisioning prelude in the existing tests). The test:
   - provisions, calls `teardown_system` as `user-1` (admin) → job queued;
   - registers worker incarnation `old`, sets the job `running`, `attempt=3`, `max_attempts=3`,
     `worker_id=old`, lease `clock_timestamp() - 1s` (lapsed) or `+ 5 minutes` (live);
   - `enqueue_control_teardown(..., recycle=NEVER)` returns state `running`;
   - `teardown_system` as `user-2` returns the same job id;
   - lapsed: row is `('queued', 3, 6)`, `heartbeat_worker_job(job, old_credential, 3, 5 min)` is
     false, one `teardown:recycled` audit row by `user-2`, ownership `teardown-requested`,
     `preactivation_absence_is_proven` false; provision canceled, a new incarnation claims
     `(job, 4)` via `claim_worker_job`, `_seed_direct_terminal_attempt(...,
     disposition="preactivation-absent", generation=5)`, `finalize_authority_system_attempt`
     with attempt 4 returns `('applied','succeeded','torn_down')`, and absence is proven;
   - live: row is `('running', 3, 3)` and no audit row.
   The terminal-policy test calls `enqueue_preactivation_teardown` directly with each of
   `TERMINAL` and `TERMINAL_OR_CANCELED` and expects `ValueError`.
2. Run the focused command; expect `[lapsed]` and the terminal-policy test to fail.
3. In `enqueue_preactivation_teardown`, before computing the dedup key:

   ```python
   if recycle not in _PREACTIVATION_RECYCLE_POLICIES:
       raise ValueError(f"preactivation teardown cannot recycle with policy {recycle.value!r}")
   ```

   with a module constant `_PREACTIVATION_RECYCLE_POLICIES = frozenset({NEVER, FAILED,
   FAILED_OR_LAPSED_EXHAUSTED})`. Replace the `if prior is not None and prior.state is not
   JobState.FAILED: job = prior / else:` block with an unconditional `enqueue_with_status` call;
   `recycled = prior is not None and changed`. Update the docstring: the queue decides which
   prior row restarts; operator callers pass `FAILED_OR_LAPSED_EXHAUSTED` or `FAILED`, automatic
   ones `NEVER`. Update `enqueue_control_teardown`'s docstring sentence that says "any other
   authority-marked prior row replays".
4. In `admin.py` `_enqueue_preactivation_authority_teardown`, pass
   `recycle=queue.JobRecyclePolicy.FAILED_OR_LAPSED_EXHAUSTED`, with a comment citing ADR-0620's
   #3617 amendment.
5. Run the focused command (green) and the whole module:
   `uv run pytest tests/services/systems/test_authority_admission.py -q`; then `just lint`,
   `just type`. Commit `fix(system-authority): recycle an exhausted preactivation teardown (#3617)`.
6. Controlled fault: revert only the `admin.py` policy to `FAILED`, run `[lapsed]`, observe red,
   restore with `git checkout -- src/kdive/mcp/tools/lifecycle/systems/admin.py` (committed).
   Then temporarily delete `AND lease_expires_at < clock_timestamp()` in `jobs/queue.py`:
   `[live]` must go red; restore with `git checkout -- src/kdive/jobs/queue.py`.

Acceptance: both tests green, existing module tests green, faults observed red.

## Task 2: Record the decision and correct the runbooks

Files: `docs/adr/0620-authority-owned-system-teardown.md`,
`docs/operating/runbooks/exhausted-authority-system-provision.md`,
`docs/operating/runbooks/live-testing.md`.

Verification: Mode: task-test-not-applicable — operator prose and an ADR amendment with no
executable consumer; `just lint` (markdown and records checks) runs over them.

Steps:

1. Append to ADR-0620, after the last amendment, `## Amendment (2026-11-13): an exhausted
   preactivation teardown restarts on request (#3617)`: the problem (worker leaves the row
   `running`; claim stops at `max_attempts`; the dedup returned non-`failed` rows), the decision
   (spec's Decision items 1-4 in prose), why it is safe (spec's safety list), and
   `Rejected for this amendment:` with tagged bullets:
   - **A reconciler lane that fails the exhausted teardown.** judgment: the #2889 amendment
     rejected terminalizing lapsed marked jobs in the reconciler; a `failed` teardown would still
     need the same operator request.
   - **Let `jobs.cancel` end it.** verified: `cancel_job` refuses the marker
     (`authority_system_preactivation_teardown_not_cancelable`, `mcp/tools/jobs.py`), because a
     canceled stable dedup row leaves no teardown the queue admits.
   - **Also recycle from `ops.force_teardown`.** judgment: its policy also governs ordinary
     teardown rows through `enqueue_control_teardown`; `systems.teardown` is the supported path.
2. Runbook "Limits" first bullet: the teardown retries each time its lease lapses (about five
   minutes) up to three attempts, then stops `running`; restore the authority, then run
   `systems.teardown` again, which restarts the same job with a fresh budget of `max_attempts`
   more attempts (normally three); link
   `live-testing.md#recovering-an-allocation-stranded-by-a-failed-authority-owned-provision`.
   Add: if the reconciler log shows `authority System teardown attempt ... still has core
   cleanup` or `authority System teardown repair failed` for the teardown, the authority already
   proved absence and the reconciler holds the receipt; fix that cleanup instead of rerunning
   `systems.teardown`, which cannot finish while that receipt is pending.
3. `live-testing.md` paragraph "While the cause remains...": after its last attempt the job stays
   `running`; `jobs.cancel` refuses it and `ops.force_teardown` returns it unchanged; once the
   cause is fixed, `systems.teardown` restarts it with a fresh budget of attempts (normally
   three), with the same reconciler-log pre-check as step 2. Keep "Find and fix the
   cause before you run `systems.teardown`."
4. `just lint`; commit `docs(adr): record the exhausted preactivation teardown recycle (#3617)`.

Acceptance: ADR amendment present with tagged rejections; both runbooks state the three-attempt
stop and the `systems.teardown` restart.

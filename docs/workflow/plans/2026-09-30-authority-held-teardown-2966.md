# Plan: ordinary teardown and release refuse an authority-held domain (#2966)

Goal: no unmarked teardown reports success for a System with external-boot history,
`allocations.release` keeps such a System's allocation `active` until the System is torn down,
and `systems.teardown` can replace a refused ordinary job with the authority-marked teardown.

Architecture: widen the worker fence in `teardown_handler` from "restricting activation" to
"any activation"; add one release check in `_release_locked` and `reclaim_under_lock`; let the
public authority-teardown route recycle a `failed` or `canceled` ordinary prior.
Spec: `docs/workflow/specs/2026-09-30-authority-held-teardown-2966-design.md`.
Decision: ADR-0620 amendment (2026-09-30, #2966).

Tech stack: Python 3, psycopg async, pytest with the migrated Postgres fixture (`migrated_url`).

Expected implementation size: 180–260 changed lines (M) — three source hunks of 10–30 lines
and six test cases of 25–40 lines in two existing test files.

## Global Constraints

- Guardrails: `just lint`, `just type`, `just test-changed`, `just records` (after
  `git fetch origin main`). On macOS run with Homebrew bash >= 4.4 and gnubin on PATH.
- 100-character lines; ruff format. No new dependency, migration, or schema change.
- Keep the worker refusal's contract: `CategorizedError`, category `conflict`, `terminal=True`,
  `details.reason == "external_boot_teardown_not_supported"`, message prefix
  `ordinary teardown is fenced by external-boot`.
- Do not change `guard_external_boot_release`: the expiry sweep's `_expire_one`
  (`reconciler/repairs/allocations.py`) calls it directly, and expiry is out of scope.

## File map

| File | Owns now | Change |
|---|---|---|
| `src/kdive/jobs/handlers/systems.py` | `teardown_handler`; refuses a restricting activation | refuse any activation (`get_latest_for_system`) |
| `src/kdive/services/allocation/release.py` | `_release_locked`, `reclaim_under_lock`; release admission | new `_require_system_teardown` check after the guard in both |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | `_enqueue_authority_teardown`; conflict on any ordinary prior | recycle a `failed` or `canceled` ordinary prior (policy `TERMINAL_OR_CANCELED`) |
| `tests/mcp/lifecycle/test_systems_tools.py` | handler and public teardown tests | three cases |
| `tests/services/external_boot/test_allocation_release.py` | release admission tests | three cases |

No caller migration: every changed function keeps its signature.

## Task 1: the worker refuses any external-boot history

Files: `src/kdive/jobs/handlers/systems.py`; test `tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: consumes `ExternalBootActivationRepository.get_latest_for_system(conn, system_id)
-> ExternalBootActivation | None` (`src/kdive/db/external_boot_activations.py`).

Verification:
- Contract "unmarked teardown refuses clean history". Mode: focused-test.
  `test_ordinary_teardown_refuses_completed_external_boot_history`, parametrized over
  `ExternalBootActivationState.RECOVERED` and `ABANDONED` with `cleanup_complete=True`. Red:
  `DID NOT RAISE`. Green: `uv run pytest tests/mcp/lifecycle/test_systems_tools.py -k
  completed_external_boot_history -q`.
- Contract "no activation tears down as before". Mode: focused-test. Existing
  `test_teardown_handler_destroys_and_sets_torn_down` stays green.

Steps:
1. Add the test after `test_queued_ordinary_teardown_refuses_activation_created_before_claim`:
   `granted_allocation`, `seed_system(pool, alloc_id, SystemState.READY)`, `_enqueue_teardown`,
   `_seed_run(pool, system_id, RunState.SUCCEEDED)`, `seed_activation(conn, state=<state>,
   cleanup_complete=True, system_id=UUID(system_id), run_id=UUID(run_id))`; call
   `systems_handlers.teardown_handler(conn, job, resolver=provider_resolver(provisioner=prov),
   artifact_store=INERT_OBJECT_STORE)` inside `pytest.raises(CategorizedError, match="ordinary
   teardown is fenced by external-boot")`. Assert category `CONFLICT`, the reason,
   `details["activation_state"] == state.value`, System state `READY`, `prov.torn_down == []`.
2. Run; expect `DID NOT RAISE`.
3. In `teardown_handler` replace `get_restricting_for_system` with `get_latest_for_system` and
   set the message to `"ordinary teardown is fenced by external-boot authority: the provider-host
   authority owns this System's host domain (ADR-0620); run systems.teardown"`. Add one docstring
   sentence naming the fence.
4. Run both focused commands; expect pass. Commit
   `fix(jobs): refuse ordinary teardown for any external-boot history`.

## Task 2: release keeps the allocation until the System is torn down

Files: `src/kdive/services/allocation/release.py`; test
`tests/services/external_boot/test_allocation_release.py`.

Interfaces: consumes `ExternalBootDenied(message, *, details, next_actions, project)`
(`src/kdive/services/external_boot/admission.py`, already imported in `release.py`) and
`release_with_backstops(pool, uid, *, project, audit_writer) -> ReleaseOutcome`, which maps a
`CategorizedError` to `ReleaseOutcome(released=False, category, details=categorized_details(exc))`.

Verification:
- Contract "release refuses while a non-torn-down System has history". Mode: focused-test.
  `test_release_refuses_a_system_with_external_boot_history`. Red: `outcome.released is True`.
  Green: `uv run pytest tests/services/external_boot/test_allocation_release.py -q`.
- Contract "release succeeds once the System is torn down". Mode: focused-test.
  `test_release_admits_a_torn_down_system_with_external_boot_history` (green before and after;
  it pins the `torn_down` exemption).
- Contract "the orphaned-active reaper retains a failed System with history". Mode:
  focused-test. `test_reconciler_reclaim_retains_a_failed_system_with_external_boot_history`.
  Red: `outcome.released is True`. Green: same file command.
- Contract "expiry is unchanged". Mode: focused-test. Existing
  `test_expiry_without_a_restricting_activation_logs_no_denial` stays green.

Steps:
1. Add both tests with the `seeded_activation` fixture (`state=RECOVERED`,
   `cleanup_complete=True`; the fixture seeds a `ready` System on a `granted` allocation in
   project `proj`). The first calls `release_with_backstops(pool, allocation_id, project="proj",
   audit_writer=_noop_audit)` and asserts `released is False`, `CONFLICT`,
   `outcome.details["reason"] == "external_boot_system_teardown_required"`, allocation still
   `granted`. The second first runs `UPDATE systems SET state = 'torn_down'` and asserts
   `released is True`. The third runs `UPDATE systems SET state = 'failed'`, then
   `reclaim_under_lock(conn, _noop_audit, allocation_id, project="proj")`, and asserts
   `released is False` and `CONFLICT`.
2. Run; expect the first and third to fail (`released is True`).
3. Add to `release.py`:

   ```python
   _UNTORN_EXTERNAL_BOOT_SYSTEM_SQL = (
       "SELECT s.id FROM systems s WHERE s.allocation_id = %s AND s.state <> 'torn_down' "
       "AND EXISTS (SELECT 1 FROM external_boot_activations e WHERE e.system_id = s.id) "
       "ORDER BY s.id LIMIT 1"
   )


   async def _require_system_teardown(conn, allocation_id, *, project) -> None:
       """Keep the allocation until the authority tears down each System with history (#2966)."""
       row = await (await conn.execute(_UNTORN_EXTERNAL_BOOT_SYSTEM_SQL, (allocation_id,))).fetchone()
       if row is None:
           return
       raise ExternalBootDenied(
           f"allocations.release is denied while System {row[0]} has external-boot history "
           "and is not torn down; run systems.teardown first (ADR-0620)",
           details={"reason": "external_boot_system_teardown_required", "system_id": str(row[0])},
           next_actions=["systems.teardown", "systems.get"],
           project=project,
       )
   ```

   and call it in `_release_locked` right after `await guard_external_boot_release(...)`, which
   already holds every System lock of the allocation. In `reclaim_under_lock` call it inside
   the existing `try` after `guard_external_boot_release`, so the existing `except
   ExternalBootDenied` maps it to `ReleaseOutcome(released=False, category=CONFLICT)`.
4. Run the file; expect pass. Commit
   `fix(allocation): refuse release while a System needs authority teardown`.

## Task 3: the public teardown replaces a failed ordinary job

Files: `src/kdive/mcp/tools/lifecycle/systems/admin.py`; test
`tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: consumes `queue.JobRecyclePolicy.TERMINAL_OR_CANCELED`, `_AUTHORITY_MARKER`
(`"external_boot_authority_v1"`), `_ordinary_teardown_job(pool, state)`,
`_seed_retired_teardown_authority(conn, seeded)`, `_teardown(pool, ctx, system_id, *,
resolver)` and `_JOB_COLUMNS` from the test module.

Verification:
- Contract "a failed or canceled ordinary prior becomes the marked teardown, same job id".
  Mode: focused-test. `test_teardown_replaces_failed_ordinary_job_for_external_boot_history`,
  parametrized over `failed` and `canceled`. Red:
  `status == "error"`, reason `ordinary_teardown_fenced_by_external_boot`. Green:
  `uv run pytest tests/mcp/lifecycle/test_systems_tools.py -k failed_ordinary_job_for_external_boot -q`.
- Contract "a queued ordinary prior still conflicts". Mode: focused-test. Existing
  `test_teardown_activation_fence_preempts_keyed_ordinary_replay` stays green.

Steps:
1. Add the test: `_ordinary_teardown_job(pool, SystemState.READY)`; `UPDATE jobs SET state =
   <failed|canceled>`; `_seed_run`; `seed_activation(conn, state=RECOVERED, cleanup_complete=True,
   ready_reservation=True, system_id=..., run_id=...)`; `_seed_retired_teardown_authority(conn,
   seeded)`; `_teardown(pool, ctx(Role.ADMIN), system_id,
   resolver=provider_resolver(external_boot=ExternalBootOperations()))`. Assert `status ==
   "queued"`, `object_id == job_id`, and the payload's `external_boot_authority_v1.activation_id`
   equals the seeded activation id.
2. Run; expect the conflict.
3. In `_enqueue_authority_teardown` compute `ordinary = prior is not None and
   _AUTHORITY_MARKER not in prior.payload and "authority_system_v1" not in prior.payload`. When
   `ordinary and prior.state in {JobState.FAILED, JobState.CANCELED}`, skip the marker conflict
   and the marker-equality replay, and enqueue with `JobRecyclePolicy.TERMINAL_OR_CANCELED`
   (the row is failed or canceled under the System lock, so `succeeded` cannot match);
   otherwise keep today's flow.
4. Run the focused commands and the whole file; expect pass. Commit
   `fix(mcp): replace a refused ordinary teardown with the authority route`.

## Follow-up candidates

- Route `enqueue_control_teardown` to the authority teardown (needs a `ProviderResolver` in
  `JobOperations`, break-glass and the reconciler lane).
- `repair_stalled_tearing_down_systems` re-enqueues an unmarked teardown for a pre-fix
  `tearing_down` System with history.

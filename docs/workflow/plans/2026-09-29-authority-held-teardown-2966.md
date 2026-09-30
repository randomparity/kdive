# Plan: ordinary teardown refuses an authority-held domain (#2966)

Goal: an unmarked TEARDOWN job never reports success for a System with external-boot history,
and `systems.teardown` can replace the refused job with the authority-marked teardown.

Architecture: widen one fence in the worker's `teardown_handler` from "restricting activation"
to "any activation", and let the public authority-teardown route recycle a `failed` ordinary
prior. Spec: `docs/workflow/specs/2026-09-29-authority-held-teardown-2966-design.md`.
Decision: ADR-0620 amendment (2026-09-29, #2966).

Tech stack: Python 3, psycopg async, pytest with the migrated Postgres fixture (`migrated_url`).

Expected implementation size: 90–140 changed lines (M) — two source hunks of about 10 lines
each, plus three test cases of 30–40 lines each in one test file.

## Global Constraints

- Guardrails: `just lint`, `just type`, `just test-changed`, `just records` (after
  `git fetch origin main`). On macOS run with Homebrew bash >= 4.4 and gnubin on PATH.
- 100-character lines; ruff format. No new dependency, migration, or public field.
- Keep the refusal's existing contract: `CategorizedError`, category `conflict`,
  `terminal=True`, `details.reason == "external_boot_teardown_not_supported"`, message prefix
  `ordinary teardown is fenced by external-boot` (matched by the existing test at
  `tests/mcp/lifecycle/test_systems_tools.py`).

## File map

| File | Owns now | Change |
|---|---|---|
| `src/kdive/jobs/handlers/systems.py` | `teardown_handler`: refuses on a restricting activation | refuse on any activation (`get_latest_for_system`) |
| `src/kdive/mcp/tools/lifecycle/systems/admin.py` | `_enqueue_authority_teardown`: conflict on any ordinary prior | recycle a `failed` ordinary prior (policy `FAILED`) |
| `tests/mcp/lifecycle/test_systems_tools.py` | handler and public teardown tests | three new cases |

No caller migration: both functions keep their signatures.

## Task 1: the worker refuses any external-boot history

Files: modify `src/kdive/jobs/handlers/systems.py`; test `tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: consumes `ExternalBootActivationRepository.get_latest_for_system(conn, system_id)
-> ExternalBootActivation | None` (`src/kdive/db/external_boot_activations.py`). Task 2 relies
on the refusal happening before `SYSTEMS.update_state` and the provisioner call.

Verification:
- Contract "unmarked teardown refuses a System whose newest activation is clean history".
  Mode: focused-test. Test
  `test_ordinary_teardown_refuses_completed_external_boot_history[recovered|abandoned]`. Red:
  `DID NOT RAISE` (the handler tears down). Green:
  `uv run pytest tests/mcp/lifecycle/test_systems_tools.py -k completed_external_boot_history -q`.
- Contract "a System with no activation tears down as before". Mode: focused-test. Existing
  `test_teardown_handler_destroys_and_sets_torn_down` stays green (same command, `-k
  destroys_and_sets_torn_down`).

Steps:
1. Add the test beside `test_queued_ordinary_teardown_refuses_activation_created_before_claim`:
   seed a `READY` System, enqueue with `_enqueue_teardown`, seed a Run and
   `seed_activation(conn, state=<RECOVERED|ABANDONED>, cleanup_complete=True, system_id=...,
   run_id=...)`, call `teardown_handler` with `FakeProvisioning()`, and assert the `conflict`
   category, the reason, `details["activation_state"]`, state `READY`, and
   `provisioner.torn_down == []`.
2. Run it; expect `DID NOT RAISE`.
3. In `teardown_handler`, replace `get_restricting_for_system` with `get_latest_for_system`, and
   set the message to: `"ordinary teardown is fenced by external-boot authority: the System has
   external-boot history, so the provider-host authority owns its host domain (ADR-0620); run
   systems.teardown"`. Update the docstring with one sentence naming the fence.
4. Run both focused commands; expect pass. Commit
   `fix(jobs): refuse ordinary teardown for any external-boot history`.

## Task 2: the public teardown replaces a failed ordinary job

Files: modify `src/kdive/mcp/tools/lifecycle/systems/admin.py`; test
`tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: consumes `queue.JobRecyclePolicy.FAILED` and `_AUTHORITY_MARKER`
(`"external_boot_authority_v1"`). An `authority_system_v1` prior is not ordinary and keeps its
current conflict.

Verification:
- Contract "a failed ordinary prior becomes the marked teardown, same job id". Mode:
  focused-test. Test `test_teardown_replaces_failed_ordinary_job_for_external_boot_history`.
  Red: `response.status == "error"` with reason `ordinary_teardown_fenced_by_external_boot`.
  Green: `uv run pytest tests/mcp/lifecycle/test_systems_tools.py -k
  failed_ordinary_job_for_external_boot -q`.
- Contract "a queued ordinary prior still conflicts". Mode: focused-test. Existing
  `test_teardown_activation_fence_preempts_keyed_ordinary_replay` stays green.

Steps:
1. Add the test: `_ordinary_teardown_job(pool, SystemState.READY)`, set that job `failed`,
   seed a Run, a `RECOVERED` `cleanup_complete` activation and `_seed_retired_teardown_authority`,
   then `_teardown(..., resolver=provider_resolver(external_boot=ExternalBootOperations()))`.
   Assert `status == "queued"`, the same `object_id`, and the job payload carries
   `external_boot_authority_v1` with the activation id.
2. Run it; expect the conflict red.
3. In `_enqueue_authority_teardown`, compute
   `ordinary_prior = prior is not None and _AUTHORITY_MARKER not in prior.payload and
   "authority_system_v1" not in prior.payload`. Skip the marker conflict when
   `ordinary_prior and prior.state is JobState.FAILED`; skip the marker-equality replay for an
   ordinary prior; enqueue with `JobRecyclePolicy.FAILED` for an ordinary prior.
4. Run the focused commands and the whole file; expect pass. Commit
   `fix(mcp): replace a refused ordinary teardown with the authority route`.

## Deferrals and follow-up candidates

- Route `enqueue_control_teardown` to the authority teardown (needs a `ProviderResolver` in
  `JobOperations`, break-glass and the reconciler lane). Follow-up candidate.
- `repair_stalled_tearing_down_systems` re-enqueues an unmarked teardown for a pre-fix
  `tearing_down` System with history. Follow-up candidate (spec failure model, entry 3).

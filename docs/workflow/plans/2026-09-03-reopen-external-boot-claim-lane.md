# Plan — reopen the worker claim path for authority-marked external-boot payloads

Goal: make a job whose payload carries `external_boot_authority_v1` claimable and counted,
while proving the generic finalization fence survives untouched.

Architecture: one new forward-only SQL migration reverses the claim-side half of the `DO`
block at `src/kdive/db/schema/0122_external_boot_authority.sql:275-320`, using the same
`pg_get_functiondef` + `replace` + `EXECUTE` idiom, then asserts the finalizer-side half is
still installed. No Python source changes. Tests are `psycopg` integration tests against the
disposable-Postgres fixtures in `tests/db/conftest.py`.

Spec: `docs/workflow/specs/2026-09-03-reopen-external-boot-claim-lane-design.md`.

Expected implementation size: 250–350 changed lines (M) — derived from the file map below: one ~70-line migration, one ~200-line test module, seven one-line list appends with two slice-index bumps, and one amended test in the 0122 suite.

## Global Constraints

- Python 3.14, managed with `uv`. Ruff line length **100**, lint set `E,F,I,UP,B,SIM`.
  `ty` runs whole-tree with strict defaults.
- Migration number **0127** is assigned by the dispatching orchestrator. Do not pick another.
  `0126_remote_module_attempt_obligations.sql` is the highest on `main` at
  `ee8659b5ab6bd00e7dbd0697595e201896e243d5`.
- Applied migrations are **byte-immutable** (ADR-0015). `just schema-guard` diffs against
  `origin/main` and fails any modify/delete/rename of an existing `src/kdive/db/schema/*.sql`.
  Get 0127 right before it lands; it cannot be amended afterwards.
- `just migration-order-check` requires a new migration to be numbered strictly above the
  highest version on `origin/main`.
- Guardrail suite: `just ci`, run **bare** — no pipes, no `|| true`, no trailing `; echo $?`.
  Capture with a redirect: `just ci > <file> 2>&1 < /dev/null`.
- `.github/scripts/mermaid-check/node_modules` is gitignored and per-worktree; run
  `npm ci` there once or `just check-mermaid` aborts `just ci` with `ERR_MODULE_NOT_FOUND`
  before the test recipe runs (#2156).
- Doc-style guard: use "Milestone", never "Sprint"; avoid "critical", "robust",
  "comprehensive", "elegant" in prose, ADRs, commit messages, and code comments.
- Never pass a PR or issue body as a shell string; use `--body-file`.

## File map

| path | action | answerable for |
|---|---|---|
| `src/kdive/db/schema/0127_reopen_external_boot_claim_lane.sql` | create | reversing the claim-side fence; asserting the finalizer-side fence survives |
| `tests/db/test_migration_0127_reopen_external_boot_claim_lane.py` | create | every acceptance criterion for 0127 |
| `tests/db/test_external_boot_authority_migration.py` | modify | drop the now-superseded claim-exclusion assertion |
| `tests/db/test_migrate.py` | modify | three hardcoded version lists + one slice index |
| `tests/db/test_migration_0091_system_object_sweep_cursors.py` | modify | one hardcoded version list |
| `tests/db/test_migration_0102_build_gc_cursors.py` | modify | one hardcoded version list |
| `tests/db/test_migration_0115_capture_reap_state.py` | modify | one hardcoded version list + one slice index |

Interfaces consumed from `tests/db/external_boot_authority_support.py` (all confirmed to
exist with these signatures):

- `_seed_case(conn: psycopg.Connection, *, purpose: str = "activate", operation: str | None = None, worker_protocol: int = 4, worker_suffix: str = "a", legacy_recovery_point: bool = False) -> _AuthorityCase` (line 135) — seeds resource → allocation → system → investigation → run → activation → `worker_incarnations` → a `jobs` row with `payload = {"external_boot_authority_v1": marker}`, `state='running'`, `attempt=1`, `max_attempts=3`.
- `_AuthorityCase` (line 51) — fields used here: `job_id`, `worker_id`, `credential`.
- `_RoleDsns.__call__(role: str) -> str` (line 41) — conninfo string for a login in `role`.
- `authority_role_dsns` fixture (line 85) — must be re-exported into the test module as
  `from tests.db.external_boot_authority_support import authority_role_dsns as authority_role_dsns  # noqa: F401`.
- `migrated_url: str` fixture (`tests/db/conftest.py:318`) — fully-migrated per-worker DB.

`SET ROLE` does **not** work for these functions: they check
`pg_has_role(session_user, 'kdive_worker', 'member')`, and `SET ROLE` does not change
`session_user`. Connect over a separate LOGIN DSN from `authority_role_dsns`.

## Task 1 — the migration

Creates `src/kdive/db/schema/0127_reopen_external_boot_claim_lane.sql`.

Two `DO` blocks. The first loops over `claim_worker_job(text,bytea,interval,text[])` and
`count_claimable_worker_jobs(text[])`; for each it reads `pg_get_functiondef`, requires the
exact injected text immediately followed by its anchor marker, replaces that pair with the
marker alone, requires the result to contain no `external_boot_authority_v1`, and `EXECUTE`s
it. The second loops over `complete_worker_job(uuid,bytea,integer,text)` and
`fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)` and raises if either has lost its
own exclusion.

Exact constants:

```sql
v_injected constant text := E'AND NOT (j.payload ? ''external_boot_authority_v1'')\n          ';
v_marker   constant text := 'AND j.attempt < j.max_attempts';
```

Substring tests use `position(... in ...)`, not `LIKE`: both `max_attempts` and
`external_boot_authority_v1` contain `_`, a `LIKE` single-character wildcard.

Acceptance: `uv run python -c "from kdive.db import migrate; print(migrate.discover_migrations()[-1].filename)"` prints `0127_reopen_external_boot_claim_lane.sql`.

## Task 2 — the 0127 test module

Creates `tests/db/test_migration_0127_reopen_external_boot_claim_lane.py`.

1. `test_claim_functions_no_longer_exclude_authority_marked_payloads` — `pg_get_functiondef`
   for both claim-side functions contains no `external_boot_authority_v1`.
2. `test_generic_finalizers_still_exclude_authority_marked_payloads` — `pg_get_functiondef`
   for `complete_worker_job` and `fail_worker_job` still contains it. **Required
   deliverable.** This is the only test that catches a reversal that over-matched into the
   finalizer pair.
3. `test_worker_claims_and_counts_an_authority_marked_job` — seed with `_seed_case`, flip the
   row to `queued` with `attempt=0`, then as `kdive_worker`: `count_claimable_worker_jobs`
   returns ≥ 1 and `claim_worker_job` returns the seeded `job_id`.
4. `test_generic_finalizers_still_refuse_an_authority_marked_job` — after that claim, both
   `complete_worker_job` and `fail_worker_job` return no row and the job's state is still
   `running`.
5. `test_reapplying_the_migration_raises_on_an_already_reversed_database` — re-executing
   0127's SQL against the migrated DB raises `psycopg.errors.RaiseException` matching
   `unexpected source shape`.
6. `test_grants_and_authority_vocabulary_are_unchanged` — `has_function_privilege` for the
   two authority functions is True for the `kdive_worker` login and False for the
   `kdive_server`, `kdive_reconciler` and `kdive_provider_authority` logins; the four
   external-boot tables give `kdive_worker` `SELECT` but not `UPDATE`; and the two CHECK
   constraint definitions still pin the five `p_purpose` values and the `boot`/`teardown`
   kinds.

Criterion "idempotent on re-run" is covered by the existing `test_rerun_is_a_noop`
(`tests/db/test_migrate.py:128`), whose version list Task 3 extends — no new test.

Acceptance: `uv run python -m pytest tests/db/test_migration_0127_reopen_external_boot_claim_lane.py -q` passes.

## Task 3 — the hardcoded migration lists

Seven sites append `("0127", "0127_reopen_external_boot_claim_lane.sql")` (or the bare
version string where the list holds only versions), and two slices widen by one:

- `tests/db/test_migrate.py:255` (in `test_rerun_is_a_noop`)
- `tests/db/test_migrate.py:294` and the slice `migrations[-28:]` → `migrations[-29:]` at line 265
- `tests/db/test_migrate.py:1007`
- `tests/db/test_migrate.py:1410`
- `tests/db/test_migration_0102_build_gc_cursors.py:45`
- `tests/db/test_migration_0091_system_object_sweep_cursors.py:40`
- `tests/db/test_migration_0115_capture_reap_state.py:30` and the slice `migrations[-11:]` → `migrations[-12:]`

Acceptance: `uv run python -m pytest tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py -q` passes.

## Task 4 — amend the superseded 0122 assertion

`tests/db/test_external_boot_authority_migration.py:2398`,
`test_marked_jobs_are_not_claimable_and_no_readiness_switch_exists`, asserts a marked job is
not claimable. Issue #2201 deliberately inverts that contract, so the assertion is superseded
rather than broken.

Rename it to `test_no_external_boot_readiness_switch_exists`, keep the
`%external_boot%enable%` assertion (still true — 0127 adds no readiness switch), and remove
the claim half together with its now-unused `kdive_worker` connection and the `queued` flip
that only existed to feed it. Add a one-line comment pointing at the 0127 module for the
claim behaviour. Its docstring and the module's remain accurate for 0122's other fences.

Acceptance: `uv run python -m pytest tests/db/test_external_boot_authority_migration.py -q` passes.

## Task 5 — prove the non-regression test bites

Commit tasks 1–4 first. Then inject a controlled fault: extend the migration's first `DO`
block array to include the two finalizer signatures and generalize the injected text so the
finalizer exclusion is removed too. Re-run the 0127 module and require
`test_generic_finalizers_still_exclude_authority_marked_payloads` to fail as an **assertion
failure**, not a collection or connection error. Revert, and verify the migration file is
byte-identical to the committed version with `sha256sum` before and after.

Acceptance: the fault run shows a failed assertion in that test; the post-revert `sha256sum`
matches the pre-fault value; `git status --short` is clean.

## Guardrails

`just ci > <file> 2>&1 < /dev/null` after Task 4 and again after Task 5's revert. Run
`npm ci` in `.github/scripts/mermaid-check/` first. `just schema-guard` and
`just migration-order-check` need `git fetch origin main` beforehand; both are in `just ci`.

## Deferrals carried

None recorded at plan time.

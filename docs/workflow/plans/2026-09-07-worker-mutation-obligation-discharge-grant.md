# Worker-granted System mutation-obligation discharge — implementation plan

**Goal.** Let the System-teardown reclaim path discharge a System's open remote-module mutation
obligations under `kdive_worker` and `kdive_reconciler`, neither of which holds `UPDATE` on
`remote_module_attempt_obligations` (#2302).

**Architecture.** A new migration adds one `SECURITY DEFINER` function whose whole body is that
bulk discharge, gated on `kdive_worker`-or-`kdive_reconciler` membership and granted `EXECUTE`
to exactly those two roles. A new `worker_discharge_system_mutation_obligations` repository
method calls it, and only the teardown reclaim path switches to that method. This is the
variant-method split ADR-0609 already uses three times in the same class
(`worker_record_terminal_evidence`, `worker_record_restored_evidence`,
`worker_discharge_reap_obligation`). The shared `discharge_system_mutation_obligations` and its
two `kdive_server` call sites in `src/kdive/db/external_boot_activations.py` stay as they are.

**Tech stack.** Python 3.14 managed with `uv`; psycopg 3 async; PostgreSQL; pytest with
testcontainers-backed disposable Postgres.

Spec: `docs/workflow/specs/2026-09-07-worker-mutation-obligation-discharge-grant-design.md`.
Decision: `docs/adr/0629-worker-fenced-system-mutation-obligation-discharge.md`.

Expected implementation size: 190–270 changed lines (M) — derived from the file map below: one
new ~40-line migration, a ~20-line repository method, a 3-line caller switch, and one new test
module carrying six arms plus its role fixtures.

## Global Constraints

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs with strict defaults over the
  **whole tree** (`src` and `tests`), via `just type`.
- Doc-style guard, project-wide, including code comments and commit messages: use
  **Milestone**, never "Sprint"; avoid the words "critical", "robust", "comprehensive",
  "elegant".
- Migration files under `src/kdive/db/schema/` are immutable once merged. This change adds
  exactly one, numbered **0152** — the campaign-assigned number. Do not renumber it.
- The ADR number is **0629**, campaign-assigned. `docs/adr/README.md` carries no index table:
  the index-sync invariant went with the index itself under ADR-0504, as
  `scripts/guards/check_adr_status.py:17-18` states. Do not add a row. That guard also rejects
  a `Proposed` ADR cited from `src/` or `tests/`, so ADR-0629 is written `Accepted
  (2026-09-07)` from the start and its first citation lands in the same commit.
- Guardrails: `just lint`, `just type`, `just test`, `just ci`, plus `just
  migration-order-check` and `just schema-guard`, which CI gates individually and which read
  the local `origin/main`, so `git fetch origin main` precedes either. Run each bare — never
  through `tail`/`head` and never with a trailing `; echo $?`. Capture with
  `just ci > FILE 2>&1 < /dev/null`; the `< /dev/null` is required because `lint-ansible`
  aborts on non-blocking stdin.
- Before `git commit`: `just format` for a Python-only change; otherwise stage, run `prek run`,
  then re-add exactly the paths that were staged — never `git add -A` or `git add -u`.
- The db tests need a reachable Docker daemon and **skip** without one. Every run offered as
  evidence sets `KDIVE_REQUIRE_DOCKER=1`, which turns the skip into a hard failure.
- A `just type` `warning[unused-ignore-comment]` for
  `src/kdive/providers/local_libvirt/lifecycle/boot/session_mechanisms.py:564` or
  `src/kdive/providers/local_libvirt/retrieve/guestfs.py:121` is a known host-only divergence,
  not this diff: move the unowned `guestfs.py` and `libguestfsmod*.so` symlinks in
  `.venv/lib/python3.14/site-packages/` aside and re-run. Do not edit `pyproject.toml` or those
  two files.
- `BASE_BRANCH` is `main`; the branch is `feat/worker-discharge-grant-2302`.

## File map

| Path | Disposition | Answerable for |
|---|---|---|
| `src/kdive/db/schema/0152_worker_system_mutation_discharge.sql` | create | the `SECURITY DEFINER` function, its `REVOKE`, and its two `EXECUTE` grants |
| `src/kdive/db/remote_module_attempt_obligations.py` | modify | the new `worker_discharge_system_mutation_obligations` method |
| `src/kdive/jobs/handlers/system_reclaim.py` | modify | calling the worker variant from the teardown reclaim path |
| `tests/db/test_worker_system_mutation_discharge.py` | create | the six role-grant proofs |

`src/kdive/db/external_boot_activations.py`, `src/kdive/jobs/handlers/systems.py`, and
`src/kdive/reconciler/repairs/jobs.py` are read but not changed.

## Task 1 — Migration 0152, the worker method, and the caller switch

**Where this fits.** This is the whole production change. Task 2 proves it.

### Interfaces

Consumed from the existing codebase, each confirmed present with the signature assumed here:

- `RemoteModuleAttemptObligationRepository.discharge_system_mutation_obligations(self, conn: AsyncConnection, system_id: UUID) -> int`
  — `src/kdive/db/remote_module_attempt_obligations.py:305-325`. Left unchanged.
- `advisory_xact_lock(conn: AsyncConnection, scope: LockScope, key: UUID | str)`, an async
  context manager — `src/kdive/db/locks.py:94-121`. `LockScope.SYSTEM` — `locks.py:69`.
- The variant-method precedent: `worker_discharge_reap_obligation(self, conn, context, attempt) -> bool`,
  which issues `SELECT public.commit_worker_remote_module_evidence(...)` and reads the single
  returned column — `remote_module_attempt_obligations.py:442-460`.
- The migration precedent: `public.commit_worker_remote_module_evidence`, declared `LANGUAGE
  plpgsql SECURITY DEFINER SET search_path = ''` with a `pg_has_role(session_user,
  'kdive_worker', 'member')` gate raising SQLSTATE `42501`, then `REVOKE ALL … FROM PUBLIC` and
  `GRANT EXECUTE … TO kdive_worker` — `src/kdive/db/schema/0134_remote_module_worker_evidence.sql`
  (137 lines).
- The table's column shapes: `mutation_discharged_at timestamptz` and
  `mutation_discharge_reason text`, neither an enum, so `SET search_path = ''` needs no type
  qualification — `src/kdive/db/schema/0126_remote_module_attempt_obligations.sql:68-73`.

Relied on by Task 2: `public.discharge_system_mutation_obligations(uuid) RETURNS integer`, and
`RemoteModuleAttemptObligationRepository.worker_discharge_system_mutation_obligations(self, conn: AsyncConnection, system_id: UUID) -> int`.

### Verification

- **Contract: `kdive_worker` and `kdive_reconciler` can discharge a System's open mutation
  obligations without table-level `UPDATE`.** Mode: `focused-test`. Observable: the reclaim
  helper completes under a real role login and the rows carry `mutation_discharge_reason =
  'terminal_escape'`. Tests: `tests/db/test_worker_system_mutation_discharge.py`, written in
  Task 2. Expected red before this task's SQL exists:
  `psycopg.errors.UndefinedFunction`; expected red with the caller left on the shared method:
  `psycopg.errors.InsufficientPrivilege`. Green command:
  `KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_worker_system_mutation_discharge.py -q`.
- **Contract: the shared method still works under `kdive_server`.** Mode: `focused-test`. Test
  case `test_server_role_shared_discharge_still_works` in the same module. Expected red if the
  shared method were rerouted through the function instead of a variant being added:
  `psycopg.errors.InsufficientPrivilege: permission denied for function`.
- **Contract: migration ordering and immutability.** Mode: `task-test-not-applicable`. The
  changed surface is a new numbered file under `src/kdive/db/schema/`. `just
  migration-order-check` and `just schema-guard` are the executable consumers of that contract;
  a task-local test would restate their git-diff comparison against a base ref the test cannot
  fix.

### Steps

1. Create `src/kdive/db/schema/0152_worker_system_mutation_discharge.sql` with exactly this
   content:

   ```sql
   -- 0152_worker_system_mutation_discharge.sql — worker/reconciler bulk terminal-escape discharge
   --
   -- ADR-0629. `remote_module_attempt_obligations` grants kdive_worker and kdive_reconciler
   -- SELECT only (0126:230-232), but both roles reach the bulk discharge on the System teardown
   -- reclaim path, so the direct UPDATE failed with 42501 (#2302). 0132 and 0147 composed this
   -- same write into a definer function they already had; that path has none, so it gets one.

   CREATE FUNCTION public.discharge_system_mutation_obligations(p_system_id uuid)
   RETURNS integer
   LANGUAGE plpgsql
   SECURITY DEFINER
   SET search_path = ''
   AS $$
   DECLARE
       v_discharged integer;
   BEGIN
       -- The EXECUTE grant below already restricts the callers. This gate is the second layer,
       -- so a later grant widened by accident does not by itself widen who may write.
       IF NOT (pg_catalog.pg_has_role(session_user, 'kdive_worker', 'member')
               OR pg_catalog.pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
           RAISE EXCEPTION 'worker or reconciler authority is required' USING ERRCODE = '42501';
       END IF;
       IF p_system_id IS NULL THEN
           RAISE EXCEPTION 'system id is required' USING ERRCODE = '22023';
       END IF;

       -- The caller holds the System advisory lock for the surrounding transaction; the lock
       -- helper's key space is not reachable from SQL, so it is deliberately not retaken here.
       UPDATE public.remote_module_attempt_obligations
       SET mutation_discharged_at = pg_catalog.now(),
           mutation_discharge_reason = 'terminal_escape'
       WHERE system_id = p_system_id AND mutation_discharged_at IS NULL;
       GET DIAGNOSTICS v_discharged = ROW_COUNT;
       RETURN v_discharged;
   END
   $$;

   REVOKE ALL ON FUNCTION public.discharge_system_mutation_obligations(uuid) FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION public.discharge_system_mutation_obligations(uuid)
       TO kdive_worker, kdive_reconciler;
   ```

2. In `src/kdive/db/remote_module_attempt_obligations.py`, leave
   `discharge_system_mutation_obligations` (lines 305-325) exactly as it is, and insert this new
   method immediately after it, before `record_terminal_evidence`:

   ```python
       async def worker_discharge_system_mutation_obligations(
           self, conn: AsyncConnection, system_id: UUID
       ) -> int:
           """The worker/reconciler form of :meth:`discharge_system_mutation_obligations`.

           Same write, same first-write-wins predicate, same System lock. It goes through the
           SECURITY DEFINER function migration 0152 grants to ``kdive_worker`` and
           ``kdive_reconciler`` (ADR-0629), because neither role holds ``UPDATE`` on the table
           and the teardown reclaim path runs under one of them (#2302). The direct-write
           sibling stays for the ``kdive_server`` activation edges that already hold it.
           """
           async with advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
               row = await conn.execute(
                   "SELECT public.discharge_system_mutation_obligations(%s)", (system_id,)
               )
               value = await row.fetchone()
           return 0 if value is None else int(value[0])
   ```

3. In `src/kdive/jobs/handlers/system_reclaim.py`, change the call at lines 110-113 from
   `discharge_system_mutation_obligations` to `worker_discharge_system_mutation_obligations`,
   so the block reads:

   ```python
           if discharge_mutation_obligations:
               await RemoteModuleAttemptObligationRepository().worker_discharge_system_mutation_obligations(
                   conn, system_id
               )
   ```

   That line exceeds 100 characters, so bind the repository to a local name first:

   ```python
           if discharge_mutation_obligations:
               obligations = RemoteModuleAttemptObligationRepository()
               await obligations.worker_discharge_system_mutation_obligations(conn, system_id)
   ```

4. Run `just format`, then `just lint`. Expect `All checks passed!` and exit 0.
5. Run `just type`. Expect `All checks passed` and exit 0.
6. Run `git fetch origin main`, then `just migration-order-check` and `just schema-guard`,
   each bare. Expect exit 0 from both: 0152 is strictly above 0151, and no already-merged
   migration file changed.

### Acceptance criteria

- `src/kdive/db/schema/0152_worker_system_mutation_discharge.sql` exists, defines exactly one
  function, and contains exactly one `GRANT` naming `kdive_worker` and `kdive_reconciler` and
  no `GRANT` on any table.
- The function is `SECURITY DEFINER` with `SET search_path = ''`, and every object it names is
  schema-qualified.
- `discharge_system_mutation_obligations` is byte-identical to its state on `main`.
- `system_reclaim.py` is the only file calling the new worker method.
- `just lint`, `just type`, `just migration-order-check`, and `just schema-guard` are green.

## Task 2 — The role-grant proofs

**Where this fits.** This is the evidence #2302 item 3 asks for: without it a missing grant is
found by a live teardown rather than by the suite.

### Interfaces

Consumed, each confirmed present with the signature assumed here:

- `authority_role_dsns`, a pytest fixture over `migrated_url` yielding a `_RoleDsns` whose
  `__call__(role: str) -> str` returns a DSN for a freshly created `LOGIN` principal that is a
  member of `role`; it serves exactly `kdive_server`, `kdive_worker`, `kdive_reconciler`, and
  `kdive_provider_authority`, and drops them on teardown —
  `tests/db/external_boot_authority_support.py:86-109`, with `_RoleDsns` at `:37-47`. Its
  `parameters` field holds the base connection parameters, so an ad-hoc principal outside those
  four is connected by building a DSN from `psycopg.conninfo.make_conninfo(**{**dsns.parameters,
  "user": <name>, "password": <password>})` rather than through `__call__`.
- `_seed(conn: psycopg.AsyncConnection) -> tuple[UUID, UUID]`, inserting the
  resource/allocation/system/investigation/run spine and returning `(system_id, run_id)` —
  `tests/db/remote_module_attempt_obligations_support.py:22-53`.
- `_attempt(system_id: UUID, run_id: UUID, nonce: str = "0" * 32) -> ModuleAttempt` — same file,
  `:55-56`.
- `RemoteModuleAttemptObligationRepository.open_mutation_obligation(conn, attempt) -> bool`,
  which inserts the obligation row — `src/kdive/db/remote_module_attempt_obligations.py:208-221`.
- `reclaim_system_core_after_provider_teardown(conn, artifact_store, system_id, *, reclaim_snapshot_ledger: bool, discharge_mutation_obligations: bool) -> None`
  — `src/kdive/jobs/handlers/system_reclaim.py:98-113`.
- `RetiredKeyBatchDeleter`, a `Protocol` with
  `delete_retired_key_batch(self, key: str, limit: int) -> bool` — same file, `:33-36`.

### Verification

Each entry below is `Mode: focused-test`, in
`tests/db/test_worker_system_mutation_discharge.py`, with green command
`KDIVE_REQUIRE_DOCKER=1 uv run python -m pytest tests/db/test_worker_system_mutation_discharge.py -q`.

- **Worker-role teardown reclaim discharges.** `test_worker_role_teardown_reclaim_discharges`.
  Expected red before Task 1: `psycopg.errors.UndefinedFunction: function
  public.discharge_system_mutation_obligations(uuid) does not exist`.
- **Reconciler-role teardown reclaim discharges**, with the `reclaim_snapshot_ledger=True` that
  `src/kdive/reconciler/repairs/jobs.py:102` passes.
  `test_reconciler_role_teardown_reclaim_discharges`. Same expected red.
- **No table-level `UPDATE` was granted.** `test_worker_role_direct_update_still_denied`; a
  direct `UPDATE` under the worker login must raise `psycopg.errors.InsufficientPrivilege`.
  Expected red if the fix were a table grant: no exception raised.
- **The in-body role gate refuses a non-member.** `test_non_member_execute_is_refused`; a
  `LOGIN` principal that is a member of neither role but has been granted `EXECUTE` directly
  must raise `psycopg.errors.InsufficientPrivilege` carrying `worker or reconciler authority is
  required`. Expected red if the gate were left to the grant alone: no exception raised.
- **First-write-wins is preserved.** `test_second_discharge_is_a_noop`; the stored
  `mutation_discharged_at` is equal after a second reclaim. Expected red if the
  `mutation_discharged_at IS NULL` predicate were dropped: the timestamp advances.
- **The shared method still works under `kdive_server`.**
  `test_server_role_shared_discharge_still_works`; calling
  `discharge_system_mutation_obligations` under the server login returns 1 and discharges the
  row. Expected red if the shared method were rerouted through the function: `permission denied
  for function`.

### Steps

1. Create `tests/db/test_worker_system_mutation_discharge.py`. Import `authority_role_dsns`
   re-exported with a `noqa: F401` alias, exactly as
   `tests/db/test_remote_module_worker_evidence.py:31-33` does, plus `_seed` and `_attempt` from
   `tests.db.remote_module_attempt_obligations_support`, and `asyncio`, `psycopg`, `pytest`,
   `uuid4`, `psycopg.conninfo.make_conninfo`, and `psycopg.sql.SQL`/`Identifier`/`Literal`.
2. Add a module-level stub store, since the reclaim helper requires one:

   ```python
   class _NoStore:
       """The reclaim helper's object-store port; these arms assert on database state only."""

       def delete_retired_key_batch(self, key: str, limit: int) -> bool:
           return True
   ```

3. Add a helper that seeds one System with one open mutation obligation on a privileged
   connection and returns its `system_id`, using `_seed`, `_attempt`, and
   `RemoteModuleAttemptObligationRepository().open_mutation_obligation`. Commit before any role
   connection reads it.
4. Add a helper that opens a non-autocommit `psycopg.AsyncConnection` for a given DSN and runs
   `reclaim_system_core_after_provider_teardown(conn, _NoStore(), system_id,
   reclaim_snapshot_ledger=<flag>, discharge_mutation_obligations=True)`, then commits.
5. Write `test_worker_role_teardown_reclaim_discharges`: seed, run the helper under
   `authority_role_dsns("kdive_worker")` with `reclaim_snapshot_ledger=False` — the flag
   `src/kdive/jobs/handlers/systems.py:730-736` passes — then assert on a privileged connection
   that `mutation_discharged_at` is not null and `mutation_discharge_reason ==
   "terminal_escape"`.
6. Write `test_reconciler_role_teardown_reclaim_discharges`: the same body under
   `authority_role_dsns("kdive_reconciler")` with `reclaim_snapshot_ledger=True`. If that flag
   makes the arm fail on a *different* missing grant — `delete_snapshots_for_system` writes to
   `snapshots` — that is a separate defect: report it, and do not widen this change to fix it.
7. Write `test_worker_role_direct_update_still_denied`: under the worker login, execute
   `UPDATE remote_module_attempt_obligations SET mutation_discharged_at = now(),
   mutation_discharge_reason = 'terminal_escape' WHERE system_id = %s` inside
   `pytest.raises(psycopg.errors.InsufficientPrivilege)`.
8. Write `test_non_member_execute_is_refused`: on a privileged connection create a `LOGIN` role
   with a unique name and no `IN ROLE` clause, `GRANT EXECUTE ON FUNCTION
   public.discharge_system_mutation_obligations(uuid)` to it, connect with a DSN built by
   `make_conninfo(**{**dsns.parameters, "user": name, "password": password})`, and assert the
   call raises `psycopg.errors.InsufficientPrivilege` whose message contains `worker or
   reconciler authority is required`. Drop the role in a `finally`. Build every identifier with
   `SQL(...).format(Identifier(...))`, never string interpolation.
9. Write `test_second_discharge_is_a_noop`: run the worker-role reclaim twice, read the stored
   `mutation_discharged_at` after each, and assert the two values are equal.
10. Write `test_server_role_shared_discharge_still_works`: under
    `authority_role_dsns("kdive_server")`, call
    `RemoteModuleAttemptObligationRepository().discharge_system_mutation_obligations(conn,
    system_id)` inside a transaction and assert it returns 1.
11. Run the green command above, bare. Expect `6 passed` and exit 0. A line reporting skips
    means the daemon was unreachable and the run is not evidence.
12. Run `just lint` and `just type`, each bare. Expect exit 0 from both.

### Acceptance criteria

- All six cases pass with `KDIVE_REQUIRE_DOCKER=1` set, and the run reports no skips.
- Every arm connects through a real `LOGIN` principal; no arm asserts anything while connected
  as the backend superuser, for which `pg_has_role` is true against every role.
- Both a permitted arm and a denied arm exist for the same call, so a call broken for everyone
  cannot pass as a proof of the gate.
- The test module creates no role it does not drop.

## Task 3 — Full gate

### Verification

- **Contract: the repository's PR gate.** Mode: `task-test-not-applicable`. This task adds no
  contract of its own; it runs the aggregate gate the two tasks above satisfied piecewise.

### Steps

1. Merge `origin/main` into the branch so the gate reflects the base the PR will merge onto,
   then `git fetch origin main` and run `just ci > /tmp/ci-2302.log 2>&1 < /dev/null` bare.
   Expect exit 0; read the log for the failing recipe if it is not.

### Acceptance criteria

- `just ci` exits 0 on a branch whose base is current `origin/main`.

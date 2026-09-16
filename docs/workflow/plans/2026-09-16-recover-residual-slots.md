# Make `recover` reach the residual slots — implementation plan

**Goal.** Give `recover` a fence-release path keyed on the `worker_incarnations` row rather than on
a retained `state.json`, so #2533's four recoverable residual slots clear their on-disk facts and
release their fence in one call, and the fifth is refused with its own disposition.

**Architecture.** A new `SECURITY DEFINER` read function returns the active local rows whose
incarnation carries a slot's derived prefix. The coordinator proves those rows dead against a
current systemd observation and releases each by echoing its own stored binding back into the
existing `public.terminate_worker_incarnation`, whose exact-match comparison is then satisfied by
construction. Files are cleared by a new unconditional `SlotStore` method needing no `SlotState`.

**Tech stack.** Python 3.13, pydantic v2, psycopg 3 (async), PostgreSQL 17, pytest. Migrations are
plain numbered `.sql` files under `src/kdive/db/schema/`.

Spec: `docs/workflow/specs/2026-09-16-recover-residual-slots-design.md`.
Decision: `docs/adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md`.
Governing: `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`.

Expected implementation size: 450–620 changed lines (M) — from the file map: a ~55-line migration,
~55 lines in `authority_store.py`, ~70 in `systemd_worker_state.py`, ~135 in
`systemd_worker_lifecycle.py`, ~240 across three test modules. The range exceeds the frozen M band;
it is reported as measured, not trimmed, and no task may be dropped to make it agree.

## Global Constraints

- **Migration `0155` is reserved.** Mint no other. `just migration-order-check` compares against
  `origin/main`, so `git fetch origin main` first.
- **The protocol identity must not move.** Add no `Operation` value and no
  `LifecycleRequest`/`LifecycleResponse` field. `SlotResult.code` is
  `Annotated[str, StringConstraints(max_length=64)]` — free-form and bounded, so a new *value*
  leaves `model_json_schema()` and `lifecycle_protocol_identity()` untouched; a new *field* would
  not. If a change alters either model's JSON schema, stop: the approach is wrong for this issue.
- **`CURRENT_WORKER_FENCE_PROTOCOL` stays `4`.** Do not alter `worker_incarnations` or
  `public.terminate_worker_incarnation`.
- **ADR-0657 forbids recovering a slot whose invocation identity is unreadable.** Refuse, never
  clear. Concretely that case is a `BootObservation` whose `boot_id` equals the registered one.
- **`ruff format` rewrites a tuple `except (A, B):` inside an indented Markdown fence into
  `except A, B:`, invalid Python.** Never write one here; use separate arms or a named constant.
  Verified with `ruff format --diff` against this repo's pinned ruff.
- **The installer pre-creates all eight slot directories**
  (`deploy/systemd/install-live-worker-lifecycle.sh`, the `for slot in {1..8}` loop). On a
  provisioned host `SlotResidue.EMPTY` therefore never occurs and must never be load-bearing for
  control flow: decide "did this call retire anything" from what was released or removed, never
  from the residue value.
- Guardrails: `just lint`, `just type`, `just test-changed`; failures rerun with `just test-lf`;
  `prek run` after staging (hooks mutate). Full gate `just ci > <file> 2>&1 < /dev/null` — the
  redirect is required, Ansible blocks on stdin. Run gates bare; the exit code is the truth.
- 100-character lines; ≤100 lines and cyclomatic complexity ≤8 per function.
- `tests/worker_lifecycle/` needs live PostgreSQL: the `migrated_url` fixture re-exported from
  `tests/db/conftest.py`, and `SET SESSION AUTHORIZATION` to take a role.

## File map

| File | Now | After |
|---|---|---|
| `src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql` | — | new witness-only read function |
| `src/kdive/worker_lifecycle/authority_store.py` | register/authenticate/terminate | + `recoverable_worker_incarnations` |
| `.../systemd/systemd_worker_state.py` | `load` collapses absent and malformed | + `SlotResidue`, `SlotInspection`, `inspect`, `discard_unrecoverable` |
| `.../systemd/systemd_worker_lifecycle.py` | `recover` keyed on retained state | + row-keyed residual path, identity helpers, case-5 refusal |
| `tests/worker_lifecycle/test_authority_store.py` | — | + 5 accessor cases |
| `tests/.../test_systemd_worker_state.py` | — | + 6 inspect/discard cases |
| `tests/.../test_systemd_worker_lifecycle.py` | 14 `recover` cases | + 8 residual cases, 3 amended |

No caller migrates and no compatibility path is retained: `_terminal_observation` keeps its
signature and every existing call site is untouched.

---

## Task 1 — The witness-only read accessor

Creates `src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql`; modifies
`src/kdive/worker_lifecycle/authority_store.py`; tests
`tests/worker_lifecycle/test_authority_store.py`. Everything downstream needs a way to name a
slot's fence row without a `state.json`; nothing else in this change touches SQL.

**Interfaces provided.** `async def recoverable_worker_incarnations(conn: AsyncConnection, unit:
str) -> tuple[LocalWorkerIncarnation, ...]`. `LocalWorkerIncarnation` already exists in this
module: a frozen slots dataclass of `incarnation: str`, `authority_kind: Literal["local"]`,
`authority_binding: LocalAuthorityBinding`, `fence_protocol: int`. `LocalAuthorityBinding` is a
`TypedDict` requiring `unit`, `generation`, `boot_id`, `invocation_id`, `host`. `cast`,
`require_top_level_transaction` and `_validated_binding` are already imported here.

**Verification inventory.** Five entries, all `Mode: focused-test` in
`tests/worker_lifecycle/test_authority_store.py`, all green under `just test-verbose
tests/worker_lifecycle/test_authority_store.py -k recoverable`; the first four red with
`AttributeError` on the missing wrapper before step 5.

- `::test_recoverable_returns_only_the_active_local_rows_for_the_slot` — a terminated row and
  another slot's active row are both excluded.
- `::test_recoverable_rejects_a_unit_name_outside_the_fixed_slot_shape` — pass a unit holding `%`
  and `_`; red: foreign rows returned instead of a raise.
- `::test_recoverable_ignores_an_incarnation_that_only_extends_the_slot_prefix` — red: the longer
  row is returned.
- `::test_recoverable_binding_terminates_the_row_it_came_from` — red:
  `terminate_worker_incarnation` returns `False`.
- `::test_recoverable_is_denied_to_the_other_runtime_roles` — red: the call succeeds as
  `kdive_worker`.

**Steps.**

1. Write the migration:

   ```sql
   -- Name a slot's fence row without its retained state.json (ADR-0667, #2533).
   --
   -- Recovery has no incarnation string when state.json is absent or malformed, and the one
   -- existing read is keyed on the worker's own credential, which cleanup_terminated unlinks.
   -- SlotState.incarnation is derived as 'local-systemd:<unit>:<generation>', so a fixed slot
   -- yields an exact prefix. Read-only: this releases nothing, and the caller still passes the
   -- binding it returns to terminate_worker_incarnation unchanged.
   CREATE FUNCTION public.recoverable_worker_incarnations(p_unit text)
   RETURNS TABLE (incarnation text, authority_binding jsonb, fence_protocol integer)
   LANGUAGE plpgsql
   STABLE
   SECURITY DEFINER
   SET search_path = ''
   AS $$
   DECLARE
       v_prefix text;
   BEGIN
       IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
           RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
       END IF;
       -- The unit is derived from the fixed slot index, never from a request. Validating the
       -- shape keeps the prefix free of pattern metacharacters, so the match below stays narrow
       -- by construction rather than by correct escaping.
       IF p_unit IS NULL OR p_unit !~ '^kdive-live-worker@[1-8]\.service$' THEN
           RAISE EXCEPTION 'recoverable lookup requires a fixed worker unit'
               USING ERRCODE = '22023';
       END IF;
       v_prefix := 'local-systemd:' || p_unit || ':';
       RETURN QUERY
       SELECT w.incarnation, w.authority_binding, w.fence_protocol
       FROM public.worker_incarnations AS w
       WHERE starts_with(w.incarnation, v_prefix)
         -- The generation is exactly 32 lowercase hex characters, so an incarnation that only
         -- extends the prefix is excluded by length before the pattern is applied.
         AND octet_length(w.incarnation) = octet_length(v_prefix) + 32
         AND substr(w.incarnation, octet_length(v_prefix) + 1) ~ '^[0-9a-f]{32}$'
         AND w.authority_kind = 'local'
         AND w.state = 'active'
       ORDER BY w.recorded_at, w.incarnation
       LIMIT 17;
   END
   $$;

   REVOKE ALL ON FUNCTION public.recoverable_worker_incarnations(text) FROM PUBLIC;
   REVOKE ALL ON FUNCTION public.recoverable_worker_incarnations(text)
       FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
   GRANT EXECUTE ON FUNCTION public.recoverable_worker_incarnations(text)
       TO kdive_lifecycle_witness;
   ```

   The function does not filter on `host`; the binding stores one and the caller filters on it in
   Task 4. Keeping it out of the predicate leaves the accessor a pure prefix read and puts the host
   decision beside the `socket.gethostname()` the binding was built from.

2. `git fetch origin main && just migration-order-check` — exit 0, no complaint about 0155.

3. Write the five tests, following the file's existing shape: an `async def _run()` driven by
   `asyncio.run(_run())`, a witness from `await _role_connection(migrated_url,
   "kdive_lifecycle_witness")`, `try/finally: await witness.close()`, rows created with the
   existing `register_worker_incarnation(conn, incarnation, "local", binding, credential_hash,
   _PROTOCOL)`. Import `LocalAuthorityBinding` from `kdive.worker_lifecycle.authority_store`. Give
   each row a distinct hash — `credential_hash` is `UNIQUE`, and `_credential_hash(_credential(
   "..."))` already does that. For the denial case open a second connection as `kdive_worker` and
   assert `pytest.raises(errors.InsufficientPrivilege)`.

4. Run the inventory command — expect red.

5. Add the wrapper after `authenticate_worker_incarnation`:

   ```python
   _MAX_RECOVERABLE_ROWS = 16


   async def recoverable_worker_incarnations(
       conn: AsyncConnection, unit: str
   ) -> tuple[LocalWorkerIncarnation, ...]:
       """Return the active local rows one fixed slot holds, named by its derived prefix."""
       require_top_level_transaction(conn, "recoverable_worker_incarnations")
       async with conn.transaction():
           rows = await (
               await conn.execute(
                   "SELECT incarnation, authority_binding, fence_protocol "
                   "FROM public.recoverable_worker_incarnations(%s)",
                   (unit,),
               )
           ).fetchall()
       if len(rows) > _MAX_RECOVERABLE_ROWS:
           raise RuntimeError(f"slot unit {unit} holds an implausible number of active fences")
       return tuple(
           LocalWorkerIncarnation(
               cast(str, row[0]),
               "local",
               cast(LocalAuthorityBinding, _validated_binding("local", row[1])),
               cast(int, row[2]),
           )
           for row in rows
       )
   ```

   The SQL `LIMIT 17` against a bound of 16 makes an overflow detectable rather than silently
   truncated. `_validated_binding` raises on a binding whose keys or values are malformed, so a
   corrupt row surfaces as a `RuntimeError` here and never reaches the coordinator as a usable
   record — Task 4 step 4 relies on that and must not duplicate the check.

6. Rerun step 4 — 5 passed. Then `just lint` and `just type` — exit 0.

7. Commit: `feat(lifecycle): name a slot's fence row by its derived incarnation prefix`.

---

## Task 2 — Raw slot inspection and unconditional fact clearing

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py`; tests
`tests/processes/lifecycle/systemd/test_systemd_worker_state.py`. Cases 1 and 2 are invisible to
every existing operation because `load` collapses "absent" into `None` and "malformed" into a
raised `StateConflict`.

**Interfaces provided.** Add `from enum import StrEnum` and `from dataclasses import dataclass`;
neither is imported in this module yet. `SlotResidue` is a `StrEnum` of `EMPTY = "empty"`,
`STATE_ABSENT = "state-absent"`, `STATE_UNREADABLE = "state-unreadable"`,
`STATE_VALID = "state-valid"`. `SlotInspection` is a `@dataclass(frozen=True, slots=True)` of
`residue: SlotResidue` and `state: SlotState | None`. `SlotStore` gains
`inspect(self) -> SlotInspection` and `discard_unrecoverable(self) -> bool`.

`discard_unrecoverable` returns **whether it removed anything**. That return is the only honest
signal that a slot held residue: the installer pre-creates all eight slot directories, so
`SlotResidue.EMPTY` does not occur on a provisioned host and cannot be used to decide it.

**Verification inventory.** Six entries, all `Mode: focused-test` in
`tests/processes/lifecycle/systemd/test_systemd_worker_state.py`, all red with `AttributeError`
before the code lands, all green under `just test-verbose
tests/processes/lifecycle/systemd/test_systemd_worker_state.py -k "inspect or
discard_unrecoverable"`.

- `::test_inspect_reports_each_slot_residue` — all four residues.
- `::test_inspect_returns_unreadable_instead_of_raising_for_a_malformed_document`.
- `::test_discard_unrecoverable_clears_a_slot_with_no_parseable_state`.
- `::test_discard_unrecoverable_reports_whether_it_removed_anything` — `True` for a slot holding
  files, `False` for an existing but empty slot directory. The installer's pre-created directories
  make this the load-bearing case.
- `::test_discard_unrecoverable_requires_root`.
- `::test_discard_unrecoverable_accepts_an_already_empty_slot` — a no-op returning `False`, not an
  error.

**Steps.**

1. Read this module's `load` and `cleanup_terminated` first, so the descriptor discipline below
   matches: `self._slot_descriptor(create=...)` returns `int | None`, reads go through
   `self._read(descriptor, name)`, and every path closes the descriptor in a `finally`.

2. Add `SlotResidue` and `SlotInspection` above `class SlotStore`, per the Interfaces block, with a
   one-line docstring each.

3. Add `inspect` immediately after `load`:

   ```python
   def inspect(self) -> SlotInspection:
       """Report what this slot holds without letting an unreadable document raise.

       ``load`` collapses "no slot", "no document" and "unparseable document" into ``None`` or a
       raised ``StateConflict``. Recovery has to tell them apart to report which residual case it
       met; it must not branch on ``EMPTY``, which a provisioned host never produces.
       """
       descriptor = self._slot_descriptor(create=False)
       if descriptor is None:
           return SlotInspection(SlotResidue.EMPTY, None)
       try:
           try:
               data = self._read(descriptor, "state.json")
           except FileNotFoundError:
               return SlotInspection(SlotResidue.STATE_ABSENT, None)
           try:
               return SlotInspection(SlotResidue.STATE_VALID, SlotState.model_validate_json(data))
           except ValueError:
               return SlotInspection(SlotResidue.STATE_UNREADABLE, None)
       finally:
           os.close(descriptor)
   ```

4. Add `discard_unrecoverable` immediately after `cleanup_terminated`:

   ```python
   def discard_unrecoverable(self) -> bool:
       """Remove every retained file for this slot, reporting whether anything was removed.

       ``cleanup_terminated`` is the evidenced path and keeps its guards. This is the unevidenced
       one ADR-0657 allows for a slot proven dead: it keeps the root requirement and the
       slot-permission validation ``_slot_descriptor`` performs, and drops only the comparison
       against a retained state that, in #2533's cases 1 and 2, does not exist. The caller
       releases the fence first, so a crash here leaves files with no fence rather than a fence
       with no files.
       """
       self._require_root()
       descriptor = self._slot_descriptor(create=False)
       if descriptor is None:
           return False
       removed = False
       try:
           for name in ("worker.env", "worker-incarnation.credential", "release", "state.json"):
               try:
                   os.unlink(name, dir_fd=descriptor)
               except FileNotFoundError:
                   continue
               removed = True
           os.fsync(descriptor)
       finally:
           os.close(descriptor)
       return removed
   ```

5. Write the six tests. Match the file's existing root-owned-slot fixture, and find its existing
   `_require_root` refusal test and follow that shape rather than inventing a monkeypatch. For the
   unreadable case write `b"{ not json"` into `state.json` through the store's own root path.

6. Run the inventory command — 6 passed. Then `just lint` and `just type` — exit 0.

7. Commit: `feat(lifecycle): distinguish and clear unreadable slot residue`.

---

## Task 3 — Prove death against an identity pair, not a `SlotState`

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`; tests
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`. `_terminal_observation` owns
both "which identity is authoritative" and "what does this observation prove about it"; Task 4
needs the second half against the *row's* identity. Behaviour is unchanged, so a transcription
error is visible on its own before the behavioural task lands. Add `from dataclasses import
dataclass` to this module.

**Verification inventory.** One entry, `Mode: focused-test`:
`::test_identity_outcome_matches_the_state_rules`, parametrized over the same observations driven
through both functions — foreign unit, boot mismatch, `BootObservation` on the retained boot,
successor invocation, unknown membership, populated membership, each terminal `result` value —
asserting identical returns and identical raised types. Red: `AttributeError` on
`_identity_outcome`. Green: `just test-verbose
tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -k identity_outcome`.

**Steps.**

1. Add the identity record and the state reader above `_terminal_observation`:

   ```python
   @dataclass(frozen=True, slots=True)
   class _InvocationIdentity:
       """One exact systemd invocation, from retained state or from the registered binding."""

       unit: str
       slot: int
       boot_id: str
       invocation_id: str


   def _state_identity(state: SlotState) -> _InvocationIdentity | None:
       """Return the retained invocation identity, or ``None`` for an unbound phase."""
       if state.boot_id is None or state.invocation_id is None:
           return None
       return _InvocationIdentity(state.unit, state.slot, state.boot_id, state.invocation_id)
   ```

2. Replace `_terminal_observation`'s body with the identity-selecting half:

   ```python
   def _terminal_observation(
       state: SlotState, observation: UnitObservation | BootObservation
   ) -> TerminationOutcome | None:
       identity = _state_identity(state)
       if identity is None:
           raise LifecycleConflict("bound lifecycle phase has no exact invocation")
       return _identity_outcome(identity, observation)
   ```

   The foreign-unit check moves into `_identity_outcome`, where it still runs first for every
   existing caller.

3. Add `_identity_outcome` directly beneath, carrying the whole body that used to live in
   `_terminal_observation` — foreign-unit raise, boot-ID branch, `BootObservation` branch,
   successor-invocation branch with its `_log.warning`, membership rules, `_outcome` call —
   substituting `identity.unit`, `identity.slot`, `identity.boot_id`, `identity.invocation_id` for
   the `state.*` reads. **Keep the existing comment above the successor-invocation branch
   verbatim**: it cites ADR-0657 and ADR-0574 and is why that branch returns `killed`.

4. Write the parametrized equivalence test.

5. `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` — every
   pre-existing case still passes. This task changes no behaviour, so a failure is a transcription
   error in step 3, not an expected red.

6. `just lint` and `just type` — exit 0.

7. Commit: `refactor(lifecycle): derive a terminal outcome from an invocation identity`.

---

## Task 4 — The row-keyed residual path and the case-5 refusal

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`; tests
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`. The behavioural change. It
leaves #2532's proven happy path as it is and adds a fallback engaging only where that path fails
in one of the two ways #2533 names, or where there was never a parseable state.

**Interfaces consumed.** `recoverable_worker_incarnations` (Task 1); `SlotInspection`,
`SlotResidue`, `SlotStore.inspect`, `SlotStore.discard_unrecoverable` (Task 2);
`_InvocationIdentity`, `_state_identity`, `_identity_outcome` (Task 3).

**Three chores first, or none of the code below type-checks.**

- Extend this module's import from `...systemd_worker_state` — currently `SlotState` and
  `StateConflict` — with `SlotInspection` and `SlotResidue`; import `LocalWorkerIncarnation` and
  `recoverable_worker_incarnations` from `kdive.worker_lifecycle.authority_store`; import `socket`.
- Extend the `SlotStorage` **Protocol** with `def inspect(self) -> SlotInspection: ...` and
  `def discard_unrecoverable(self) -> bool: ...`. The coordinator calls every store through that
  Protocol, not through `SlotStore`, so omitting these is a `just type` failure. Give the test
  file's store double both methods, and a real notion of "has files" so the `bool` can be false.
- Make `_authority_call` return its operation's value, so a read can use it. Change it to the PEP
  695 generic form `async def _authority_call[T](self, deadline: Deadline, operation: Callable[[],
  Coroutine[Any, Any, T]]) -> T:` and `return` the `await asyncio.wait_for(...)` result, keeping
  the existing `TimeoutError` handling and the trailing `_require_time(deadline)`. Existing callers
  ignore the value and are unchanged.

**Verification inventory.** All `Mode: focused-test` in
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`; all green under
`just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -k
"residual or unreadable or protocol_identity or foreign_host"`.

- Cases 1–4, four entries: `::test_recover_retires_a_residual_slot_with_no_state_document`,
  `..._with_an_unreadable_state_document`, `..._whose_retained_binding_drifted`,
  `..._whose_evidence_the_authority_rejected`. Each asserts the cleared store *and* the released
  row. Red: the failure the spec's Problem section names for that case.
- `::test_recover_refuses_a_slot_whose_registered_invocation_is_absent_on_the_retained_boot` —
  asserts `code == "recovery_refused_unreadable_identity"`, no release, no file removed, **and
  that the sweep continues**: pair it with a second slot retired in the same call. Red: the sweep
  ends with `dependency_unavailable`/`restore_systemd` and the second slot is untouched.
- `::test_recover_refuses_a_live_unit_in_every_residual_case` — parametrized over the five faults
  with `membership="populated"`. Each arm must assert the fault was actually constructed — files
  still present, row still active — not merely that nothing happened, or every arm passes for the
  same uninteresting reason.
- `::test_recover_skips_a_row_registered_by_another_host` — a row whose binding `host` is not this
  host's is neither released nor counted, and the slot is not reported as retired.
- `::test_recover_refuses_a_slot_before_releasing_any_of_its_rows` — two active rows, the second
  unrecoverable; asserts `authority.released == []`, so a slot is never left half-released.
- `::test_recover_residual_support_does_not_move_the_protocol_identity` — asserts
  `lifecycle_protocol_identity()` equals the literal captured in step 1.

**Steps.**

1. Capture the protocol identity for the last test. This change does not touch
   `systemd_worker_contract.py`, so the current value *is* the base value — no stashing:

   ```sh
   uv run python -c "from kdive.processes.lifecycle.systemd.systemd_worker_contract import lifecycle_protocol_identity as f; print(f())"
   ```

   Paste the printed value in as a literal. Do not compute it at test time from the code under
   test — that would assert nothing. Cross-check it against the literal
   `tests/scripts/test_live_stack_scripts.py` already pins; they must agree.

2. Add the refusal disposition, its message map, and the fallback tuple beside `_RECOVERY_REFUSED`:

   ```python
   # ADR-0657 (`docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`, lines 62-66, the
   # "clears facts, never evidence" paragraph) forbids running for a slot whose invocation
   # identity is unreadable: recovery may not fabricate a `TerminationOutcome` nor attribute one
   # invocation's exit facts to another. ADR-0574 makes absence within the retained boot a
   # non-event, so systemd reporting no invocation on that boot leaves nothing able to prove the
   # registered invocation ended. That slot is refused, with its own code so an operator and a log
   # filter can tell it from a live-process refusal, which is transient. Relaxing this takes an ADR
   # amendment; the operator's remedy is a reboot, which yields a different boot ID and so real
   # evidence.
   _RECOVERY_REFUSED_IDENTITY = "recovery_refused_unreadable_identity"
   _REFUSAL_MESSAGES = {
       _RECOVERY_REFUSED: "fixed worker unit still has live processes",
       _RECOVERY_REFUSED_IDENTITY: (
           "registered invocation identity is unreadable; ADR-0657 forbids recovering it"
       ),
   }
   _REFUSALS = frozenset(_REFUSAL_MESSAGES)

   # The two failures #2533 names for cases 3 and 4: the retained binding no longer matches the
   # row, so the evidenced path cannot commit and recovery falls back to the row's own binding.
   # Deliberately broad -- every `StateConflict` out of the evidenced path means the retained
   # facts cannot be trusted, which is exactly when the row should be believed instead. A named
   # constant, not an inline `except (A, B)`, because `ruff format` rewrites a tuple `except`
   # inside an indented Markdown fence into invalid Python.
   _RESIDUAL_FALLBACK: tuple[type[Exception], ...] = (EvidenceRejected, StateConflict)


   @dataclass(frozen=True, slots=True)
   class _Recovery:
       """What retiring one slot produced, before it is rendered as a result."""

       state: SlotState | None = None
       cleared: bool = False
       refusal: str | None = None
   ```

3. Extend the `IncarnationAuthority` Protocol with `async def recoverable(self, unit: str) ->
   tuple[LocalWorkerIncarnation, ...]` and `async def release(self, record:
   LocalWorkerIncarnation, outcome: TerminationOutcome) -> None`, each with a one-line docstring
   and `...` body. Implement them on `PostgresAuthority` beside `terminate`: `recoverable` opens
   `async with self.pool.connection()` and returns `await recoverable_worker_incarnations(
   connection, unit)`; `release` does the same and calls `terminate_worker_incarnation(connection,
   record.incarnation, "local", record.authority_binding, outcome)`, raising
   `EvidenceRejected(f"database rejected termination evidence for {record.incarnation}")` when it
   returns false — the same shape as `terminate`, passing the row's stored binding unchanged.

4. Add the two identity predicates beside `_state_identity`:

   ```python
   def _identity_is_unreadable(
       identity: _InvocationIdentity, observation: UnitObservation | BootObservation
   ) -> bool:
       """Report the one case ADR-0657:62-66 forbids recovering.

       A ``BootObservation`` on the *retained* boot means systemd has no invocation identity for
       this unit, and ADR-0574 forbids reading that absence as termination. Nothing can prove the
       registered invocation ended, so recovery refuses rather than clearing. A different boot ID
       is not this case: that is real evidence, and ``_identity_outcome`` maps it to ``killed``.
       """
       return isinstance(observation, BootObservation) and observation.boot_id == identity.boot_id


   def _registered_identity(
       store: SlotStorage, record: LocalWorkerIncarnation
   ) -> _InvocationIdentity | None:
       """Return the invocation identity the fence claims, or ``None`` if the row is incoherent.

       ``_validated_binding`` has already rejected a binding with missing or empty members, so the
       only inconsistency left is a stored ``unit`` disagreeing with the incarnation prefix the row
       was found by. Such a row cannot be trusted to name an invocation, so it is refused.
       """
       binding = record.authority_binding
       if binding["unit"] != store.unit:
           return None
       return _InvocationIdentity(store.unit, store.slot, binding["boot_id"], binding["invocation_id"])
   ```

5. Add the residual retirement and its two authority helpers as coordinator methods. **Every row is
   classified before any row is released**, so a slot is refused whole rather than half-released:

   ```python
   async def _retire_residual_slot(
       self,
       store: SlotStorage,
       inspection: SlotInspection,
       observation: UnitObservation | BootObservation,
       deadline: Deadline,
       stop_deadline: Deadline,
   ) -> _Recovery:
       releasable: list[tuple[LocalWorkerIncarnation, TerminationOutcome]] = []
       for record in await self._authority_records(store, deadline):
           if record.authority_binding["host"] != socket.gethostname():
               # Another host's fence. The incarnation prefix carries no host, so a shared
               # database can surface one; it is not this host's to release (ADR-0667).
               _log.warning(
                   "recovery skipped a foreign-host fence unit=%s slot=%d", store.unit, store.slot
               )
               continue
           identity = _registered_identity(store, record)
           if identity is None or _identity_is_unreadable(identity, observation):
               return _Recovery(refusal=_RECOVERY_REFUSED_IDENTITY)
           outcome = _identity_outcome(identity, observation)
           if outcome is None:
               return _Recovery(refusal=_RECOVERY_REFUSED)
           releasable.append((record, outcome))
       for record, outcome in releasable:
           await self._release(record, outcome, deadline)
       removed = self._store_call(stop_deadline, store.discard_unrecoverable)
       return _Recovery(cleared=bool(releasable) or removed)


   async def _authority_records(
       self, store: SlotStorage, deadline: Deadline
   ) -> tuple[LocalWorkerIncarnation, ...]:
       try:
           return await self._authority_call(deadline, lambda: self._authority.recoverable(store.unit))
       except LifecycleDeadlineExceeded:
           raise
       except Exception as exc:
           raise _AuthorityUnavailable("worker recovery authority unavailable") from exc


   async def _release(
       self, record: LocalWorkerIncarnation, outcome: TerminationOutcome, deadline: Deadline
   ) -> None:
       try:
           await self._authority_call(deadline, lambda: self._authority.release(record, outcome))
       except EvidenceRejected:
           raise
       except LifecycleDeadlineExceeded:
           raise
       except IncarnationConflict:
           raise
       except Exception as exc:
           raise _AuthorityUnavailable("worker termination authority unavailable") from exc
   ```

   The separate `except` arms mirror `_terminate` directly above. Do not collapse them into a tuple
   — see the Global Constraint on `ruff format`.

6. Add the dispatcher. It refuses an unreadable identity on the evidenced path too, so case 5 gets
   one disposition wherever it is met:

   ```python
   async def _retire_inspected_slot(
       self,
       store: SlotStorage,
       inspection: SlotInspection,
       observation: UnitObservation | BootObservation,
       deadline: Deadline,
       stop_deadline: Deadline,
   ) -> _Recovery:
       if inspection.state is not None:
           identity = _state_identity(inspection.state)
           if identity is not None and _identity_is_unreadable(identity, observation):
               return _Recovery(refusal=_RECOVERY_REFUSED_IDENTITY)
           try:
               retired = await self._retire_slot(
                   store, inspection.state, observation, deadline, stop_deadline
               )
           except _RESIDUAL_FALLBACK as exc:
               # #2533 cases 3 and 4: the retained binding no longer matches the row, so the
               # evidenced path cannot commit. The row is the fence holder, so fall back to
               # proving *its* identity dead and releasing it with its own stored binding.
               _log.warning(
                   "recovery falling back to the registered binding unit=%s slot=%d cause=%s",
                   store.unit,
                   store.slot,
                   type(exc).__name__,
               )
           else:
               return _Recovery(state=retired)
       return await self._retire_residual_slot(store, inspection, observation, deadline, stop_deadline)
   ```

7. In `_recover_slot`, keep the opening block unchanged — the `observe` call, the foreign-unit
   raise, `retained_identity = isinstance(observation, UnitObservation)`, and the `unknown` and
   `populated` membership branches. Replace everything from the
   `state = self._store_call(..., store.load)` line to the end of the method with:

   ```python
   inspection = self._store_call(stop_deadline, store.inspect)
   recovery = await self._retire_inspected_slot(
       store, inspection, observation, deadline, stop_deadline
   )
   if recovery.refusal is not None:
       return SlotResult(
           slot=store.slot,
           unit=store.unit,
           code=recovery.refusal,
           message=_REFUSAL_MESSAGES[recovery.refusal],
       )
   if retained_identity:
       # Only a unit systemd still accounts for can be holding an identity to release; a
       # BootObservation is already the inactive, empty-identity state `require_inactive`
       # wants, so resetting it would be a no-op that hides which slots this call touched.
       self._systemd_call(stop_deadline, self._runtime.reset_failed, store.unit, stop_deadline)
   if recovery.state is not None:
       return _result(recovery.state)
   if recovery.cleared:
       return SlotResult(slot=store.slot, unit=store.unit, message="retired the residual worker slot")
   if retained_identity:
       return SlotResult(
           slot=store.slot, unit=store.unit, message="cleared the retained unit identity"
       )
   return None
   ```

8. Widen the two places comparing against `_RECOVERY_REFUSED` alone, using `_REFUSALS`: in
   `recover`, `any(result.code == _RECOVERY_REFUSED ...)` becomes `any(result.code in _REFUSALS
   ...)` with its message changed to `"recovery refused one or more fixed worker slots"`; in
   `_with_completed_slots`, `merged[result.slot].code != _RECOVERY_REFUSED` becomes
   `merged[result.slot].code not in _REFUSALS`, so an identity refusal is not overwritten by a
   reload.

9. Extend `FakeAuthority` in the test file with `recoverable` and `release` mirroring the real
   adapter: `self.rows: dict[str, list[LocalWorkerIncarnation]]` keyed by unit,
   `self.released: list[tuple[str, TerminationOutcome]]`, and a `self.fail_recoverable` flag.
   **`release` must assert the binding it is given is the one stored for that incarnation** — that
   assertion is what proves the echo-the-row design holds, and a fake skipping it would pass while
   the real adapter failed. Do **not** give the fake a way to return a malformed binding:
   `_validated_binding` makes that unreachable in production, and a fake allowing it would test a
   path that cannot happen.

10. Amend the three shipped `recover` cases whose pinned behaviour this issue changes, saying so in
    each docstring. First run `rg -n "recovery refused a fixed worker unit with live processes"
    tests/` and update **every** hit — step 8 changes that message and more than one case asserts
    it.
    - `test_recover_clears_a_failed_unit_whose_slot_facts_stop_already_removed` — now also releases
      the fence when a row exists. Keep an arm with **no** row, still asserting
      `authority.released == []`; with a pre-created directory and nothing to remove, that arm now
      expects the `"cleared the retained unit identity"` message, not a retirement.
    - `test_recover_reports_a_rejected_binding_without_clearing_the_slot` — now retires through the
      row; rename to `..._recovers_a_rejected_binding_through_the_registered_row`.
    - `test_recover_refuses_a_slot_whose_invocation_identity_is_unreadable` — still refuses, but now
      with `recovery_refused_unreadable_identity` as a per-slot code instead of a sweep-ending
      `dependency_unavailable`. This is criterion 2 landing.

11. Write the eight new cases. Run `just test-verbose
    tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` — every case passes. Then
    `just lint`, `just type`, `just test-changed` — exit 0 each.

12. Commit: `feat(lifecycle): recover the residual slots through the registered row`.

---

## Closing verification

1. `git fetch origin main && just records` — exit 0.
2. `just lint`, `just type`, `just test-changed`, run bare — exit 0 each.
3. `just lock-check`, `just lint-workflows`, `just container-arch-check` — no workflow runs these
   three (#2582), so run them locally before hand-off. Exit 0 each.
4. `just ci > <file> 2>&1 < /dev/null` — exit 0. 15–20 minutes.
5. The spec's live-host criterion needs a provisioned systemd host and `sudo`, denied to this
   session. Record which proof arms ran and which did not; do not assert the host proof.

## Deferrals and rejected findings from design review

Two independent `$gauntlet` passes ran over this set (lenses `downstream-reader` and
`operator-cold-resume`). Every correctness finding either pass raised is fixed above; three were
blocking and are worth naming, because each one was a real defect in an earlier draft of this
design: the case-5 refusal was unreachable through the real accessor, a same-boot absence ended
the whole sweep, and `SlotResidue.EMPTY` was load-bearing for control flow despite never occurring
on a provisioned host.

- **Proportionality: the design set is 4.1x the frozen M denominator** (1013 lines / 250) —
  `rejected-with-evidence`, and reported rather than trimmed. The remedy the rule names is cutting
  the design, and this set was already cut twice, from 1124 lines, before the two review passes;
  the growth since is the three blocking fixes above. The premise that fails is the denominator,
  not the design: M maps to 250 changed lines, while this plan's own file map measures the
  implementation at 450-620, so the set is roughly 1.7x the implementation it specifies — an
  ordinary ratio for a full-spec plan carrying complete code. `$spellcraft` forbids re-sizing a
  frozen denominator from a plan estimate, so the band stands as recorded and this is logged as a
  known, reported tension for the operator rather than resolved by shrinking the artifact below
  what its implementer needs. The dispatching orchestrator's instruction for this row was explicit:
  report the real number rather than trimming to hit the band.

- **`fence_protocol` is read but never checked** — `rejected-with-evidence`.
  `public.terminate_worker_incarnation` does not key on `fence_protocol`, so its value cannot
  affect whether a release is accepted. The accessor returns it because `LocalWorkerIncarnation`
  carries it, not because recovery branches on it.
- **`_RESIDUAL_FALLBACK` catches every `StateConflict`, not only binding drift** —
  `rejected-with-evidence`. Any `StateConflict` from the evidenced path means the retained facts
  cannot be trusted, which is precisely when the row should be believed instead. Narrowing it would
  strand the slots this issue exists to reach. Recorded in the constant's own comment.
- **#2489's `operator_recovery` runbook paragraph goes stale once this ships** —
  `follow-up-candidate`, owner #2489, which owns that documentation under charter exclusion 3. Not
  fixed here.

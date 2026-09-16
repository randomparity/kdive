# Make `recover` reach the residual slots — implementation plan

**Goal.** Give `recover` a fence-release path keyed on the `worker_incarnations` row rather than
on a retained `state.json`, so the four recoverable residual slots of issue #2533 clear their
on-disk facts and release their fence in one call, and the fifth is refused with its own
disposition.

**Architecture.** A new `SECURITY DEFINER` read function returns the active local rows whose
incarnation carries a slot's derived prefix. The coordinator proves those rows dead against a
current systemd observation and releases each by echoing its own stored binding back into the
existing `public.terminate_worker_incarnation`, whose exact-match comparison is then satisfied by
construction. On-disk facts are cleared by a new unconditional `SlotStore` method that needs no
parseable `SlotState`.

**Tech stack.** Python 3.13, pydantic v2, psycopg 3 (async), PostgreSQL, pytest. SQL migrations are
plain numbered files under `src/kdive/db/schema/`.

Spec: `docs/workflow/specs/2026-09-16-recover-residual-slots-design.md`.
Decision: `docs/adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md`.
Governing: `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`.

Expected implementation size: 450–620 changed lines (M) — derived from the file map below: one
new ~55-line migration, ~55 lines in `authority_store.py`, ~70 in `systemd_worker_state.py`, ~130
in `systemd_worker_lifecycle.py`, and ~240 across three test modules including four amended
`recover` cases.

## Global Constraints

- **Migration number `0155` is reserved for this issue.** Do not mint another. `just
  migration-order-check` compares against `origin/main` and needs `git fetch origin main` first.
- **The lifecycle protocol identity must not move.** Add no `Operation` value and no
  `LifecycleRequest`/`LifecycleResponse` field. `SlotResult.code` is
  `Annotated[str, StringConstraints(max_length=64)]`, a free-form bounded string — adding a new
  *value* does not change `model_json_schema()` and therefore does not change
  `lifecycle_protocol_identity()`. Adding a *field* would. If a change you are about to make
  alters either model's JSON schema, stop: the approach is wrong for this issue.
- **`CURRENT_WORKER_FENCE_PROTOCOL` stays `4`.** Do not change `worker_incarnations`, do not
  change `public.terminate_worker_incarnation`.
- **ADR-0657 forbids recovering a slot whose invocation identity is unreadable.** That case is
  refused, never cleared. Do not relax it.
- Guardrails: `just lint`, `just type`, `just test-changed`; rerun failures with `just test-lf`;
  `prek run` after staging (hooks mutate). Full local gate: `just ci > <file> 2>&1 < /dev/null`
  (the `< /dev/null` is required — Ansible blocks on stdin). Never pipe a gate through
  `tail`/`head`; the exit code is the truth.
- `ruff format` covers Python inside Markdown fences, so code fences in these design docs must be
  formatted or `just lint` goes red.
- Line limit 100 characters. Prefer ≤100 lines per function and cyclomatic complexity ≤8.
- The `tests/worker_lifecycle/` suite needs a live PostgreSQL; it uses the `migrated_url` fixture
  re-exported from `tests/db/conftest.py` and `SET SESSION AUTHORIZATION` to take a role.

## File map

| File | Now | After |
|---|---|---|
| `src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql` | — | new: one witness-only read function |
| `src/kdive/worker_lifecycle/authority_store.py` | register/authenticate/terminate wrappers | + `recoverable_worker_incarnations` wrapper |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` | `load` collapses absent and malformed | + `SlotResidue`, `SlotInspection`, `inspect`, `discard_unrecoverable` |
| `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` | `recover` keyed on retained state | + row-keyed residual path, identity helper, case-5 refusal |
| `tests/worker_lifecycle/test_authority_store.py` | — | + 4 accessor cases |
| `tests/processes/lifecycle/systemd/test_systemd_worker_state.py` | — | + 5 inspect/discard cases |
| `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` | 14 `recover` cases | + 7 residual cases; 2 amended |

No caller migrates and no compatibility path is retained: `_terminal_observation` keeps its
current signature and every existing caller is untouched.

---

## Task 1 — The witness-only read accessor

Creates `src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql`.
Modifies `src/kdive/worker_lifecycle/authority_store.py`.
Tests `tests/worker_lifecycle/test_authority_store.py`.

**Where this fits.** Everything downstream needs a way to name a slot's fence row without a
`state.json`. This task is that way, and nothing else in the change touches SQL.

**Interfaces — provided to later tasks.**

```python
async def recoverable_worker_incarnations(
    conn: AsyncConnection, unit: str
) -> tuple[LocalWorkerIncarnation, ...]: ...
```

`LocalWorkerIncarnation` already exists in this module as a frozen slots dataclass with fields
`incarnation: str`, `authority_kind: Literal["local"]`, `authority_binding: LocalAuthorityBinding`,
`fence_protocol: int`. `LocalAuthorityBinding` is a `TypedDict` with required keys `unit`,
`generation`, `boot_id`, `invocation_id`, `host`.

**Verification inventory.**

- *Only active local rows carrying the exact slot prefix are returned.* `Mode: focused-test` —
  `tests/worker_lifecycle/test_authority_store.py::test_recoverable_returns_only_the_active_local_rows_for_the_slot`.
  Red: `AttributeError: module ... has no attribute 'recoverable_worker_incarnations'`. Green:
  `just test-verbose tests/worker_lifecycle/test_authority_store.py -k recoverable`.
- *A crafted unit name cannot widen the match.* `Mode: focused-test` —
  `::test_recoverable_rejects_a_unit_name_outside_the_fixed_slot_shape`. Red: rows returned for a
  `%` unit. Green: as above.
- *A longer incarnation sharing the prefix does not match.* `Mode: focused-test` —
  `::test_recoverable_ignores_an_incarnation_that_only_extends_the_slot_prefix`. Red: the extra row
  is returned. Green: as above.
- *A binding read back from the accessor satisfies the fence's exact match.* `Mode: focused-test` —
  `::test_recoverable_binding_terminates_the_row_it_came_from`. Red: `terminate_worker_incarnation`
  returns `False`. Green: as above.
- *The function is unreachable from the non-witness runtime roles.* `Mode: focused-test` —
  `::test_recoverable_is_denied_to_the_other_runtime_roles`. Red: the call succeeds as
  `kdive_worker`. Green: as above.

**Steps.**

1. Write `src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql`:

   ```sql
   -- Name a slot's fence row without its retained state.json (ADR-0667, #2533).
   --
   -- Recovery has no incarnation string when state.json is absent or malformed, and the one
   -- existing read is keyed on the worker's own credential, which cleanup_terminated unlinks.
   -- SlotState.incarnation is derived as 'local-systemd:<unit>:<generation>', so a fixed slot
   -- yields an exact prefix. This function is read-only: it releases nothing, and the caller
   -- still passes the binding it returns to terminate_worker_incarnation unchanged.
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
       -- shape here keeps the prefix free of any pattern metacharacter, so the match below
       -- needs no escaping to stay narrow.
       IF p_unit IS NULL OR p_unit !~ '^kdive-live-worker@[1-8]\.service$' THEN
           RAISE EXCEPTION 'recoverable lookup requires a fixed worker unit'
               USING ERRCODE = '22023';
       END IF;
       v_prefix := 'local-systemd:' || p_unit || ':';
       RETURN QUERY
       SELECT w.incarnation, w.authority_binding, w.fence_protocol
       FROM public.worker_incarnations AS w
       WHERE starts_with(w.incarnation, v_prefix)
         -- The generation is exactly 32 lowercase hex characters, so an incarnation that merely
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

2. Run `git fetch origin main && just migration-order-check`. Expect exit 0 and no complaint about
   `0155`.

3. Write the five failing tests named in the inventory above, in
   `tests/worker_lifecycle/test_authority_store.py`. Follow the file's existing shape: an
   `async def _run()` body driven by `asyncio.run(_run())`, a witness connection from
   `await _role_connection(migrated_url, "kdive_lifecycle_witness")`, `try/finally: await
   witness.close()`. Register rows with the existing `register_worker_incarnation(conn,
   incarnation, "local", binding, credential_hash, _PROTOCOL)`. Build local bindings as
   `LocalAuthorityBinding(unit=..., generation=..., boot_id=..., invocation_id=..., host=...)` —
   import it from `kdive.worker_lifecycle.authority_store`. Use a distinct `credential_hash` per
   row (the column is `UNIQUE`); `_credential_hash(_credential("..."))` already does this.
   For the denial case, open a second connection with
   `await _role_connection(migrated_url, "kdive_worker")` and assert
   `pytest.raises(errors.InsufficientPrivilege)`.

4. Run `just test-verbose tests/worker_lifecycle/test_authority_store.py -k recoverable`. Expect
   red with `AttributeError` for the four wrapper cases.

5. Add the wrapper to `src/kdive/worker_lifecycle/authority_store.py`, directly after
   `authenticate_worker_incarnation`:

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

6. Run the same command. Expect five passed.

7. Run `just lint` and `just type`. Expect exit 0 from each.

8. Commit: `feat(lifecycle): name a slot's fence row by its derived incarnation prefix`.

**Acceptance.** `0155` applies cleanly, the accessor returns only the slot's active local rows,
a binding it returns terminates the row it came from, and no other runtime role may call it.

---

## Task 2 — Raw slot inspection and unconditional fact clearing

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py`.
Tests `tests/processes/lifecycle/systemd/test_systemd_worker_state.py`.

**Where this fits.** Cases 1 and 2 are invisible to every existing operation because `load`
collapses "absent" into `None` and "malformed" into a raised `StateConflict`. This task makes the
two distinguishable and gives recovery a way to remove the residue either way.

**Interfaces — provided to later tasks.**

```python
class SlotResidue(StrEnum):
    EMPTY = "empty"  # no slot directory
    STATE_ABSENT = "state-absent"  # slot directory, no state.json (case 1)
    STATE_UNREADABLE = "state-unreadable"  # state.json present, unparseable (case 2)
    STATE_VALID = "state-valid"


@dataclass(frozen=True, slots=True)
class SlotInspection:
    residue: SlotResidue
    state: SlotState | None


class SlotStore:
    def inspect(self) -> SlotInspection: ...
    def discard_unrecoverable(self) -> None: ...
```

`StrEnum` comes from `enum` and `dataclass` from `dataclasses`; neither is imported in this module
yet, so add both imports.

**Verification inventory.**

- *`inspect` distinguishes the four residues.* `Mode: focused-test` —
  `test_systemd_worker_state.py::test_inspect_reports_each_slot_residue`. Red:
  `AttributeError: 'SlotStore' object has no attribute 'inspect'`. Green:
  `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_state.py -k inspect`.
- *`inspect` never raises for a malformed document.* `Mode: focused-test` —
  `::test_inspect_returns_unreadable_instead_of_raising_for_a_malformed_document`. Red:
  `StateConflict` propagates. Green: as above.
- *`discard_unrecoverable` removes every slot file without a parseable state.* `Mode:
  focused-test` — `::test_discard_unrecoverable_clears_a_slot_with_no_parseable_state`. Red:
  `AttributeError`. Green: as above, `-k discard_unrecoverable`.
- *`discard_unrecoverable` still requires root.* `Mode: focused-test` —
  `::test_discard_unrecoverable_requires_root`. Red: `AttributeError`. Green: as above.
- *`discard_unrecoverable` on an empty slot is a no-op, not an error.* `Mode: focused-test` —
  `::test_discard_unrecoverable_accepts_an_already_empty_slot`. Red: `AttributeError`. Green: as
  above.

**Steps.**

1. Read `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` around `load`
   (the `def load` method) and `cleanup_terminated`, so the descriptor discipline below matches
   what is already there: `self._slot_descriptor(create=...)` returns an `int | None`, every
   read goes through `self._read(descriptor, name)`, and every path closes the descriptor in a
   `finally`.

2. Add the two module-level types immediately above `class SlotStore`:

   ```python
   class SlotResidue(StrEnum):
       """What a slot directory holds, before any validation is attempted."""

       EMPTY = "empty"
       STATE_ABSENT = "state-absent"
       STATE_UNREADABLE = "state-unreadable"
       STATE_VALID = "state-valid"


   @dataclass(frozen=True, slots=True)
   class SlotInspection:
       """A validation-free reading of one slot, with its state where one parses."""

       residue: SlotResidue
       state: SlotState | None
   ```

3. Add `inspect` immediately after `load`:

   ```python
   def inspect(self) -> SlotInspection:
       """Report what this slot holds without letting an unreadable document raise.

       ``load`` answers one question -- is there a usable state -- and collapses "no slot",
       "no document" and "unparseable document" into ``None`` or a raised ``StateConflict``.
       Recovery has to tell them apart: the first is nothing to do, and the other two are
       #2533's cases 1 and 2, which hold a fence that only the database can now name.
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
   def discard_unrecoverable(self) -> None:
       """Remove every retained file for this slot without requiring a parseable state.

       ``cleanup_terminated`` is the evidenced path and keeps its guards. This is the
       unevidenced one ADR-0657 allows for a slot proven dead: it keeps the root requirement
       and the slot-permission validation ``_slot_descriptor`` performs, and drops only the
       comparison against a retained state that, in #2533's cases 1 and 2, does not exist.
       The caller releases the fence first, so a crash here leaves files with no fence rather
       than a fence with no files.
       """
       self._require_root()
       descriptor = self._slot_descriptor(create=False)
       if descriptor is None:
           return
       try:
           for name in ("worker.env", "worker-incarnation.credential", "release", "state.json"):
               with suppress(FileNotFoundError):
                   os.unlink(name, dir_fd=descriptor)
           os.fsync(descriptor)
       finally:
           os.close(descriptor)
   ```

5. Write the five tests named above. Match the file's existing fixture style for a root-owned
   slot root and its non-root refusal; find the existing `_require_root` refusal test in this file
   and follow it rather than inventing a new monkeypatch shape. For the unreadable case, write
   `b"{ not json"` into `state.json` through the store's own root path.

6. Run `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_state.py -k
   "inspect or discard_unrecoverable"`. Expect five passed.

7. Run `just lint` and `just type`. Expect exit 0.

8. Commit: `feat(lifecycle): distinguish and clear unreadable slot residue`.

**Acceptance.** `inspect` returns each of the four residues and never raises on a malformed
document; `discard_unrecoverable` clears a slot with no parseable state, refuses without root, and
is a no-op on an empty slot.

---

## Task 3 — Prove death against an identity pair, not a `SlotState`

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`.
Tests `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

**Where this fits.** `_terminal_observation` owns both "which identity is authoritative" and "what
does this observation prove about it". Task 4 needs the second half against the *row's* identity.
This task separates them and changes no behaviour, so a regression here is visible on its own
before the behavioural task lands.

**Interfaces — provided to later tasks.**

```python
@dataclass(frozen=True, slots=True)
class _InvocationIdentity:
    unit: str
    slot: int
    boot_id: str
    invocation_id: str


def _identity_outcome(
    identity: _InvocationIdentity, observation: UnitObservation | BootObservation
) -> TerminationOutcome | None: ...
```

`_terminal_observation(state, observation) -> TerminationOutcome | None` keeps its exact current
signature and every existing call site. `TerminationOutcome` is already imported in this module
from `kdive.worker_lifecycle.contracts`. `dataclass` is not yet imported here — add it.

**Verification inventory.**

- *The extracted helper preserves every rule `_terminal_observation` applied.* `Mode:
  focused-test` — `test_systemd_worker_lifecycle.py::test_identity_outcome_matches_the_state_rules`,
  a parametrized case driving both functions over the same observations (foreign unit, boot
  mismatch, `BootObservation` on the retained boot, successor invocation, unknown membership,
  populated membership, each terminal `result`) and asserting identical outcomes and identical
  raised types. Red: `ImportError`/`AttributeError` on `_identity_outcome`. Green:
  `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -k
  identity_outcome`.

**Steps.**

1. Add the dataclass above `_terminal_observation`:

   ```python
   @dataclass(frozen=True, slots=True)
   class _InvocationIdentity:
       """One exact systemd invocation, from retained state or from the registered binding."""

       unit: str
       slot: int
       boot_id: str
       invocation_id: str
   ```

2. Rewrite `_terminal_observation` as the identity-selecting half, delegating the rules. Keep its
   docstring-free shape and its existing guards in the same order:

   ```python
   def _terminal_observation(
       state: SlotState, observation: UnitObservation | BootObservation
   ) -> TerminationOutcome | None:
       if state.boot_id is None or state.invocation_id is None:
           raise LifecycleConflict("bound lifecycle phase has no exact invocation")
       return _identity_outcome(
           _InvocationIdentity(state.unit, state.slot, state.boot_id, state.invocation_id),
           observation,
       )
   ```

   Note the foreign-unit check moves into `_identity_outcome` below, so it still runs first for
   every existing caller.

3. Add `_identity_outcome` directly beneath it, carrying the whole body that used to live in
   `_terminal_observation` — including the `_log.warning` for an out-of-band restart — with
   `state.unit` becoming `identity.unit`, `state.slot` becoming `identity.slot`, `state.boot_id`
   becoming `identity.boot_id`, and `state.invocation_id` becoming `identity.invocation_id`.
   Keep the existing comment block above the successor-invocation branch verbatim: it cites
   ADR-0657 and ADR-0574 and is the reason that branch returns `killed`.

4. Write the parametrized equivalence test named above.

5. Run `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
   Expect every pre-existing case in the file to still pass — this task changes no behaviour, so
   a failure here is a transcription error in step 3, not an expected red.

6. Run `just lint` and `just type`. Expect exit 0.

7. Commit: `refactor(lifecycle): derive a terminal outcome from an invocation identity`.

**Acceptance.** Both functions agree on every observation shape, and the file's existing
`recover`, `stop`, `start`, and `status` cases pass unchanged.

---

## Task 4 — The row-keyed residual path and the case-5 refusal

Modifies `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`.
Tests `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

**Where this fits.** This is the behavioural change. It leaves #2532's proven happy path exactly
as it is and adds a fallback that engages only where that path fails in one of the two ways
#2533 names — `EvidenceRejected` or `StateConflict` — or where there was never a parseable state
to start from.

**Interfaces — consumed from earlier tasks.** `recoverable_worker_incarnations(conn, unit) ->
tuple[LocalWorkerIncarnation, ...]` (Task 1); `SlotStore.inspect() -> SlotInspection`,
`SlotStore.discard_unrecoverable() -> None`, `SlotResidue` (Task 2); `_InvocationIdentity`,
`_identity_outcome` (Task 3).

**Two import-and-Protocol chores this task owns, before any of the code below type-checks.**

- Extend the module's existing import from
  `kdive.processes.lifecycle.systemd.systemd_worker_state` — which currently names `SlotState`
  and `StateConflict` — with `SlotInspection` and `SlotResidue`.
- Extend the `SlotStorage` Protocol in this module with the two new methods. The coordinator calls
  every store through that Protocol, not through `SlotStore`, so omitting these is a `just type`
  failure, not a runtime one:

  ```python
  def inspect(self) -> SlotInspection: ...


  def discard_unrecoverable(self) -> None: ...
  ```

  The test file's store double implements the same Protocol, so give it both methods too.

**Verification inventory.**

- *Each of cases 1–4 clears the slot and releases the fence in one call.* `Mode: focused-test` —
  four cases in `test_systemd_worker_lifecycle.py` named
  `test_recover_retires_a_residual_slot_with_no_state_document`,
  `..._with_an_unreadable_state_document`, `..._whose_retained_binding_drifted`,
  `..._whose_evidence_the_authority_rejected`. Each asserts both the cleared store and the
  released row. Red: the failure named for that case in the spec's Problem section. Green:
  `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -k
  residual`.
- *Case 5 is refused with its own disposition.* `Mode: focused-test` —
  `::test_recover_refuses_a_residual_slot_whose_registered_identity_is_unreadable`, asserting
  `code == "recovery_refused_unreadable_identity"`, no termination, and no file removed. Red: the
  refusal is `dependency_unavailable`, indistinguishable from the cgroup-unreadable refusal.
  Green: as above, `-k unreadable_identity`.
- *A live unit is refused in each of the five cases.* `Mode: focused-test` —
  `::test_recover_refuses_a_live_unit_in_every_residual_case`, parametrized over the five faults
  with `membership="populated"`, asserting no termination and no file removed in every arm.
  Green: as above, `-k live_unit_in_every_residual`.
- *The protocol identity is unchanged.* `Mode: focused-test` —
  `::test_recover_residual_support_does_not_move_the_protocol_identity`, asserting
  `lifecycle_protocol_identity()` equals the literal recorded from the base commit. Red: any
  request/response field change. Green: as above, `-k protocol_identity`.

**Steps.**

1. Capture the base-commit identity for the test above:
   `git stash && uv run python -c "from kdive.processes.lifecycle.systemd.systemd_worker_contract
   import lifecycle_protocol_identity; print(lifecycle_protocol_identity())" && git stash pop`.
   Record the printed value and paste it into the test as a literal. Do not compute it at test
   time from the code under test — that would assert nothing.

2. Add the second refusal disposition beside the existing one, near `_RECOVERY_REFUSED`:

   ```python
   _RECOVERY_REFUSED = "recovery_refused"
   # ADR-0657 (`docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`, the "clears facts,
   # never evidence" paragraph) forbids running for a slot whose invocation identity is
   # unreadable: recovery may not fabricate a `TerminationOutcome` nor attribute one invocation's
   # exit facts to another. A slot holding a fence we cannot prove dead is therefore refused, and
   # it gets its own code so an operator and a log filter can tell it from a live-process refusal,
   # which is transient, and from a systemd outage, which is not this slot's fault. Relaxing this
   # takes an ADR amendment.
   _RECOVERY_REFUSED_IDENTITY = "recovery_refused_unreadable_identity"

   # The two failures #2533 names for cases 3 and 4: the retained binding no longer matches the
   # row, so the evidenced path cannot commit and recovery falls back to the row's own binding.
   # Kept as a named tuple constant rather than an inline `except (A, B)` because `ruff format`
   # rewrites a tuple `except` inside an indented Markdown fence into invalid Python.
   _RESIDUAL_FALLBACK: tuple[type[Exception], ...] = (EvidenceRejected, StateConflict)
   ```

3. Add the outcome record used to keep `_recover_slot` flat:

   ```python
   @dataclass(frozen=True, slots=True)
   class _Recovery:
       """What retiring one slot produced, before it is rendered as a result."""

       state: SlotState | None = None
       residue_cleared: bool = False
       released: int = 0
       refusal: str | None = None
   ```

   `released` and `residue_cleared` are separate because a slot can hold a fence with no files
   left on disk at all. Recovery is the operator escape hatch, so the result has to say which of
   the two it actually did rather than reporting one as the other.

4. Extend the `IncarnationAuthority` Protocol with the two row-keyed operations, and implement
   them on `PostgresAuthority`:

   ```python
   # in IncarnationAuthority
   async def recoverable(self, unit: str) -> tuple[LocalWorkerIncarnation, ...]:
       """Return the active local rows one fixed slot still holds."""
       ...


   async def release(self, record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None:
       """Commit terminal evidence using the row's own stored binding."""
       ...
   ```

   ```python
   # in PostgresAuthority
   async def recoverable(self, unit: str) -> tuple[LocalWorkerIncarnation, ...]:
       """Return the active local rows one fixed slot still holds."""
       async with self.pool.connection() as connection:
           return await recoverable_worker_incarnations(connection, unit)


   async def release(self, record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None:
       """Commit terminal evidence using the row's own stored binding (ADR-0667)."""
       async with self.pool.connection() as connection:
           accepted = await terminate_worker_incarnation(
               connection, record.incarnation, "local", record.authority_binding, outcome
           )
       if not accepted:
           raise EvidenceRejected(f"database rejected termination evidence for {record.incarnation}")
   ```

   Import `LocalWorkerIncarnation` and `recoverable_worker_incarnations` from
   `kdive.worker_lifecycle.authority_store` alongside the existing imports.

5. Add the residual retirement, which releases every row the slot holds before any file is
   removed:

   ```python
   async def _retire_residual_slot(
       self,
       store: SlotStorage,
       inspection: SlotInspection,
       observation: UnitObservation | BootObservation,
       deadline: Deadline,
       stop_deadline: Deadline,
   ) -> _Recovery:
       records = await self._authority_records(store, deadline)
       for record in records:
           identity = _registered_identity(store, record)
           if identity is None:
               return _Recovery(refusal=_RECOVERY_REFUSED_IDENTITY)
           outcome = _identity_outcome(identity, observation)
           if outcome is None:
               return _Recovery(refusal=_RECOVERY_REFUSED)
           await self._release(record, outcome, deadline)
       if inspection.residue is SlotResidue.EMPTY:
           return _Recovery(released=len(records))
       self._store_call(stop_deadline, store.discard_unrecoverable)
       return _Recovery(residue_cleared=True, released=len(records))
   ```

   with the two small helpers beside it:

   ```python
   async def _authority_records(
       self, store: SlotStorage, deadline: Deadline
   ) -> tuple[LocalWorkerIncarnation, ...]:
       records: tuple[LocalWorkerIncarnation, ...] = ()

       async def _read() -> None:
           nonlocal records
           records = await self._authority.recoverable(store.unit)

       try:
           await self._authority_call(deadline, _read)
       except LifecycleDeadlineExceeded:
           raise
       except Exception as exc:
           raise _AuthorityUnavailable("worker recovery authority unavailable") from exc
       return records


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

   The four separate `except` arms mirror `_terminate` directly above, which is written the same
   way. Do not collapse them into one parenthesized tuple: `ruff format` rewrites a tuple `except`
   inside an indented Markdown fence into `except A, B, C:`, which is a syntax error, so the
   guardrail would corrupt this plan rather than check it.

   and the identity reader, which is where case 5 is actually decided:

   ```python
   def _registered_identity(
       store: SlotStorage, record: LocalWorkerIncarnation
   ) -> _InvocationIdentity | None:
       """Read the invocation identity the fence itself claims, or ``None`` if it is unreadable.

       ADR-0657 forbids running for a slot whose invocation identity is unreadable, so a
       binding that does not name a readable invocation on this slot's own unit yields no
       identity and the caller refuses instead of clearing.
       """
       binding = record.authority_binding
       if binding["unit"] != store.unit or not binding["boot_id"] or not binding["invocation_id"]:
           return None
       return _InvocationIdentity(store.unit, store.slot, binding["boot_id"], binding["invocation_id"])
   ```

6. Rewrite `_recover_slot` to use `inspect`, keep #2532's path first, and fall back on exactly
   the two named failures:

   ```python
   async def _recover_slot(
       self, store: SlotStorage, deadline: Deadline, stop_deadline: Deadline
   ) -> SlotResult | None:
       observation = self._systemd_call(
           stop_deadline, self._runtime.observe, store.unit, stop_deadline
       )
       if observation.unit != store.unit:
           raise LifecycleConflict("systemd returned a foreign unit observation")
       retained_identity = isinstance(observation, UnitObservation)
       if retained_identity:
           if observation.membership == "unknown":
               raise SystemdUnavailable("worker cgroup membership is unavailable")
           if observation.membership == "populated":
               return SlotResult(
                   slot=store.slot,
                   unit=store.unit,
                   code=_RECOVERY_REFUSED,
                   message="fixed worker unit still has live processes",
               )
       inspection = self._store_call(stop_deadline, store.inspect)
       recovery = await self._retire_inspected_slot(
           store, inspection, observation, deadline, stop_deadline
       )
       if recovery.refusal is not None:
           return _refusal_result(store, recovery.refusal)
       if retained_identity:
           # Only a unit systemd still accounts for can be holding an identity to release; a
           # BootObservation is already the inactive, empty-identity state `require_inactive`
           # wants, so resetting it would be a no-op that hides which slots this call touched.
           self._systemd_call(stop_deadline, self._runtime.reset_failed, store.unit, stop_deadline)
       if recovery.state is not None:
           return _result(recovery.state)
       if recovery.residue_cleared:
           return SlotResult(
               slot=store.slot, unit=store.unit, message="cleared the unrecoverable slot residue"
           )
       if recovery.released:
           return SlotResult(
               slot=store.slot, unit=store.unit, message="released the retained worker fence"
           )
       if retained_identity:
           return SlotResult(
               slot=store.slot, unit=store.unit, message="cleared the retained unit identity"
           )
       return None


   async def _retire_inspected_slot(
       self,
       store: SlotStorage,
       inspection: SlotInspection,
       observation: UnitObservation | BootObservation,
       deadline: Deadline,
       stop_deadline: Deadline,
   ) -> _Recovery:
       if inspection.state is not None:
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

   with the small renderer beside `_result`:

   ```python
   def _refusal_result(store: SlotStorage, refusal: str) -> SlotResult:
       messages = {
           _RECOVERY_REFUSED: "fixed worker unit still has live processes",
           _RECOVERY_REFUSED_IDENTITY: (
               "registered invocation identity is unreadable; ADR-0657 forbids recovering it"
           ),
       }
       return SlotResult(slot=store.slot, unit=store.unit, code=refusal, message=messages[refusal])
   ```

7. Widen the sweep's refusal check in `recover` so the new disposition also fails the response,
   replacing the single-code comparison:

   ```python
   refusals = {_RECOVERY_REFUSED, _RECOVERY_REFUSED_IDENTITY}
   if any(result.code in refusals for result in results):
       return LifecycleResponse(
           ok=False,
           code="conflict",
           message="recovery refused one or more fixed worker slots",
           retry_action="operator_recovery",
           slots=tuple(results),
       )
   ```

   Also widen `_with_completed_slots`, which currently compares `!= _RECOVERY_REFUSED` when
   deciding which result wins for a slot: change that comparison to `not in refusals` using the
   same set, so an identity refusal is not overwritten by a reload either.

8. Extend `FakeAuthority` in the test file with `recoverable` and `release`, mirroring the real
   adapter: a `self.rows: dict[str, list[LocalWorkerIncarnation]]` keyed by unit, a
   `self.released: list[tuple[str, TerminationOutcome]]`, and a `self.fail_recoverable` flag.
   `release` must assert the binding it is given is the one stored for that incarnation — that
   assertion is what proves the echo-the-row design actually holds, and a fake that skips it
   would pass while the real adapter failed.

9. Amend the two #2532 cases whose pinned behaviour this issue changes, and say so in each
   docstring:
   - `test_recover_clears_a_failed_unit_whose_slot_facts_stop_already_removed` — now also
     releases the fence when a row exists. Keep an arm with **no** row, which still asserts
     `authority.released == []` and the `"cleared the retained unit identity"` message.
   - `test_recover_reports_a_rejected_binding_without_clearing_the_slot` — now retires the slot
     through the row. Rename to `..._recovers_a_rejected_binding_through_the_registered_row`.

10. Write the seven new cases named in the inventory. Run
    `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
    Expect every case in the file to pass.

11. Run `just lint`, `just type`, then `just test-changed`. Expect exit 0 from each.

12. Commit: `feat(lifecycle): recover the residual slots through the registered row`.

**Acceptance.** Cases 1–4 clear facts and release the fence in one `recover` call; case 5 is
refused with `recovery_refused_unreadable_identity` and touches nothing; a populated cgroup is
refused in all five; `lifecycle_protocol_identity()` is unchanged.

---

## Closing verification

1. `git fetch origin main && just records` — expect exit 0.
2. `just lint`, `just type`, `just test-changed` — expect exit 0 from each, run bare.
3. `just lock-check`, `just lint-workflows`, `just container-arch-check` — no workflow runs these
   three (#2582), so run them locally before hand-off. Expect exit 0 from each.
4. `just ci > /tmp/ci-2533.log 2>&1 < /dev/null` — expect exit 0. Takes 15–20 minutes.
5. Live proof on a provisioned systemd host is the spec's criterion for the induced cases. `sudo`
   is denied to this session, so record which arms ran and which did not rather than asserting
   the host proof.

## Deferrals carried from design review

None recorded yet; the design review runs after this plan is written. Any deferral it produces is
appended here with its owning record path or tracker issue, because this plan is the artifact the
implementer reads.

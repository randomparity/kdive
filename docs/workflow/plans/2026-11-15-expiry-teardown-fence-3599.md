# Lease expiry teardown fence (#3599) — implementation plan

Spec: [expiry teardown fence](../specs/2026-11-15-expiry-teardown-fence-3599-design.md).

Goal: reconciler lease expiry holds an allocation whose System has external-boot history and is
not `torn_down`, exactly as `allocations.release` does. Architecture: reuse the release fence in
`_expire_one`; drop the narrower settled-only helper it subsumes. Stack: Python 3, psycopg 3,
pytest against real Postgres (`migrated_url`, `authority_role_dsns`).

Expected implementation size: 90–130 changed lines (M) — one fence call and helper removal ~25,
rename ~4, two tests ~70, test edits ~15.

## Global Constraints

- No migration (spec, Decision); `0199` stays unused.
- ADR-0816 is append-only (`just records`): only the 2026-11-15 amendment block is added.
- Guardrails: `just test-verbose <paths>`, `just lint`, `just type`.

## Task 1 — expiry applies the release fence

Files: modify `src/kdive/services/allocation/release.py`,
`src/kdive/reconciler/repairs/allocations.py`, `src/kdive/services/external_boot/settlement.py`,
`tests/services/external_boot/test_allocation_release.py`,
`tests/db/test_migration_0194_stranded_activation_settlement.py`.

Interfaces: produces `require_system_teardown(conn: AsyncConnection, allocation_id: UUID, *,
project: str) -> None` (raises `ExternalBootDenied` with `details["reason"] ==
SYSTEM_TEARDOWN_REQUIRED_REASON` and `details["system_id"]`). Consumes the existing
`guard_external_boot_release`, `ExternalBootDenied`, `seeded_activation` fixture
(`state=`, `cleanup_complete=`), `_repair_records`, `_strand` and `authority_role_dsns`.

Verification:
- Completed unsettled history is held — `focused-test`:
  `test_expiry_holds_a_completed_unsettled_history_until_teardown[recovered|abandoned]`. Red on
  main: `assert await sweep_expired_allocations(conn) == 0` sees 1. Green:
  `just test-verbose tests/services/external_boot/test_allocation_release.py`.
- Torn-down System expires with no warning — `focused-test`: the second half of the same test and
  the edited `test_expiry_without_a_restricting_activation_logs_no_denial`; same command.
- Fence runs under the reconciler role — `focused-test`:
  `test_the_reconciler_reads_the_release_fence`; red: `ImportError` before the rename. Green:
  `just test-verbose tests/db/test_migration_0194_stranded_activation_settlement.py -k release_fence`.

Steps:

1. Add to `test_allocation_release.py`:

   ```python
   @pytest.mark.parametrize(
       "state", [ExternalBootActivationState.RECOVERED, ExternalBootActivationState.ABANDONED]
   )
   def test_expiry_holds_a_completed_unsettled_history_until_teardown(
       migrated_url: str,
       seeded_activation: SeedActivation,
       caplog: pytest.LogCaptureFixture,
       state: ExternalBootActivationState,
   ) -> None:
       """The worker refuses this System's orphan teardown, so expiry holds it (#3599)."""

       async def body() -> None:
           async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
               seeded = await seeded_activation(conn, state=state, cleanup_complete=True)
               cursor = await conn.execute(
                   "UPDATE allocations SET state = 'active', lease_expiry = now() - interval '1s' "
                   "WHERE id = (SELECT allocation_id FROM systems WHERE id = %s) RETURNING id",
                   (seeded.system_id,),
               )
               row = await cursor.fetchone()
               assert row is not None
               allocation_id = row[0]

               with caplog.at_level(logging.INFO, logger=allocation_repairs.__name__):
                   assert await sweep_expired_allocations(conn) == 0
               held = _repair_records(caplog, logging.WARNING)
               assert len(held) == 1
               message = held[0].getMessage()
               assert str(allocation_id) in message
               assert str(seeded.system_id) in message
               assert SYSTEM_TEARDOWN_REQUIRED_REASON in message

               await conn.execute(
                   "UPDATE systems SET state = 'torn_down' WHERE id = %s", (seeded.system_id,)
               )
               assert await sweep_expired_allocations(conn) == 1
               state_row = await conn.execute(
                   "SELECT state FROM allocations WHERE id = %s", (allocation_id,)
               )
               assert await state_row.fetchone() == ("expired",)

       asyncio.run(body())
   ```

   Import `SYSTEM_TEARDOWN_REQUIRED_REASON` from `kdive.services.allocation.release`. Run the
   focused command; expect the new cases to fail at `== 0`.
2. In `release.py` rename `_require_system_teardown` to `require_system_teardown` (definition and
   both call sites in `reclaim_under_lock` and the pool release path).
3. In `_expire_one`, replace the `try` body and the settled block:

   ```python
   try:
       await allocation_release.guard_external_boot_release(conn, allocation_id, project=project)
       await allocation_release.require_system_teardown(conn, allocation_id, project=project)
   except allocation_release.ExternalBootDenied as denied:
       ...  # existing comment, now naming the #3599 fence too
       _log.warning(
           "reconciler: allocation %s not expired — the external-boot release fence refused "
           "the release; retried next pass: %s %s",
           allocation_id,
           denied,
           denied.details,
       )
       return False
   ```

   Delete the `settled_system_awaiting_teardown` block and its import.
4. Delete `settled_system_awaiting_teardown` from `services/external_boot/settlement.py` (no other
   caller: `rg -n settled_system_awaiting_teardown src tests`).
5. In `test_expiry_without_a_restricting_activation_logs_no_denial`, set the seeded System to
   `torn_down` before the sweep (`UPDATE systems SET state = 'torn_down' WHERE id = %s`) and say
   so in its docstring.
6. Replace `test_the_reconciler_reads_a_settled_system_awaiting_teardown` with
   `test_the_reconciler_reads_the_release_fence`: strand a case, then under
   `authority_role_dsns("kdive_reconciler")` call `require_system_teardown(conn,
   case.allocation_id, project="proj")` (the project only names the denial) and assert
   `ExternalBootDenied` with `details["reason"] == SYSTEM_TEARDOWN_REQUIRED_REASON` and `details["system_id"] ==
   str(case.system_id)`, before and after settlement.
7. Run both focused commands (green), `just lint`, `just type`; commit
   `fix(reconciler): hold expiry for Systems with external-boot history`.

Rollback: revert the commit; no persisted state changes.

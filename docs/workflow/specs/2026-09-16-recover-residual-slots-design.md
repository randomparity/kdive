# Make `recover` reach the residual slots

Issue #2533, child 2 of 2 of epic #2488. Decision record:
[ADR-0667](../../adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md).
Governing constraint: [ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md).

## Problem

#2532 shipped `recover` for a dead slot whose retained `state.json` is readable and whose binding
still matches the `worker_incarnations` row. Five residual cases still wedge, each stopped by a
named line in the shipped code:

1. **`state.json` absent.** `SlotStore.load` returns `None` on `FileNotFoundError`. `_recover_slot`
   then clears only the failed unit identity; the fence is never reached and the row stays
   `active` forever.
2. **`state.json` truncated or unparseable.** `SlotStore.load` raises
   `StateConflict("slot N state is malformed")`, which aborts the whole sweep.
3. **The retained binding no longer matches the row.** `PostgresAuthority.terminate` passes
   `state.authority_binding()`, `public.terminate_worker_incarnation` returns false, and
   `EvidenceRejected` is raised. `cleanup_terminated`'s `retained != state` guard fires for the
   same drift.
4. **`EvidenceRejected` from the authority.** `_terminate` re-raises it unchanged, the terminal
   write never commits, and `_post_evidence_cleanup` is never reached. Facts and fence both
   survive.
5. **Unreadable invocation identity.** `_terminal_observation` has no rule for it. ADR-0657
   forbids recovering this case.

## Scope

Four modules plus one migration. `Operation`, `LifecycleRequest`, and `LifecycleResponse` are
untouched, so `lifecycle_protocol_identity()` is unchanged and no fleet reprovision is needed.

**`src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql`** (new). One
`SECURITY DEFINER` read function returning the `active` local rows whose `incarnation` carries the
exact slot-derived prefix, with their stored `authority_binding`. Witness role only. No table
change, no fence-protocol change; `CURRENT_WORKER_FENCE_PROTOCOL` stays 4.

**`src/kdive/worker_lifecycle/authority_store.py`.** One async wrapper over that function,
reusing the existing `_validated_binding` and `require_top_level_transaction` discipline.

**`src/kdive/processes/lifecycle/systemd/systemd_worker_state.py`.** Two additions beside `load`:
`inspect()`, a raw validation-free read distinguishing an absent slot directory, an absent
`state.json`, an unparseable one, and a valid one; and `discard_unrecoverable()`, an
unconditional fact-clearing form that keeps the root requirement and the slot-permission
validation but needs no parseable `SlotState`.

**`src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`.** `recover` gains a
fence-release path keyed on the row rather than on `state.authority_binding()`. Death is proven
against the row's stored `boot_id`/`invocation_id` by the rules `_terminal_observation` already
applies, lifted to take an identity pair. Case 5 is encoded as an explicit per-slot refusal.

**Ownership.** `_terminal_observation` currently owns both "which identity is authoritative" and
"what does this observation prove about it". The second half moves to a helper taking an identity
pair; `_terminal_observation` keeps the first half and delegates. Current callers are unchanged
because a `SlotState` still supplies the pair. No compatibility path is retained and no caller
migrates: the existing signature stays.

### Failure model

**Actors and deployments.** A local operator invoking `kdive-worker-lifecycle recover` as root on
a provisioned systemd host; the lifecycle server process holding the `kdive_lifecycle_witness`
role. No anonymous or tenant traffic reaches this seam. Designed for the single-host systemd
deployment ADR-0574 defines; x86_64 and ppc64le.

**Invariants and assets at stake.**
- A fence is never released while its worker is running.
- No `TerminationOutcome` is synthesized, and no invocation's exit facts are attributed to another.
- On-disk slot facts are removed only after the fence for that identity is released, or where no
  fence was ever held.
- The `worker_incarnations` table shape and fence protocol are unchanged.
- The lifecycle protocol identity is unchanged.

**Accepted failure classes.**
- A slot whose row is `active` while its unit was never started on this boot is reported `killed`
  — accepted: ADR-0657 already fixes `killed` as the outcome for any unobservable termination,
  and this design adds no new outcome value.
- A row whose `authority_binding` was edited out of band to a readable but wrong identity is
  proven dead against that wrong identity — accepted: bounded, because the binding is writable
  only by the witness role and by a superuser, and a superuser editing the fence is the manual
  procedure #2481 exists to retire.
- `recover` does not repair a slot whose *directory* is unreadable for reasons other than absence
  (a permission or I/O fault) — accepted: held by the existing slot-permission validation, which
  fails closed.

**Covered elsewhere.** Documenting `operator_recovery` and the operator procedure — #2489. The
`Operation` value and the fleet reprovision — #2532, closed. Relaxing ADR-0657's case-5
prohibition — ADR-0657; not authorized here.

### Threat model

**Boundary inventory.** No boundary is added. One is widened: the witness role gains a read of
`worker_incarnations` rows it does not hold a credential for, keyed on a caller-supplied unit
string. Nothing crosses from an untrusted source — the unit name is derived from the fixed slot
index, never from the request.

**Actor model.** The untrusted party at this seam is a local unprivileged process attempting to
release another worker's fence. It cannot: the operation requires root for every on-disk write and
the witness role for every database write, and neither is reachable without already holding the
privileges the fence protects. Trust is placed in the `kdive_lifecycle_witness` role and in
systemd's `InvocationID`, exactly as ADR-0574 and ADR-0657 already place it.

**Control per boundary.** The read function is `SECURITY DEFINER` with `SET search_path = ''`,
checks `pg_has_role(session_user, 'kdive_lifecycle_witness', 'member')` as the sibling fence
functions do, is granted to that role alone and revoked from `PUBLIC` and the other runtime roles,
bounds its result set, and takes a unit name it validates against the fixed
`kdive-live-worker@N.service` shape before building the prefix. It matches with `starts_with`
plus an explicit length and hex-generation check rather than `LIKE`, so no pattern
metacharacter exists to escape and a widened match is unreachable by construction rather than by
correct escaping. It returns no `credential_hash` and no `credential_envelope`. On failure it
raises rather than returning rows.

**Explicitly out of scope.** Authenticating *which* operator ran `recover` — not reachable in this
deployment, where root on the host already implies full control of the slot files. Protecting the
fence from a database superuser — covered by the same reasoning, and by the accepted class above.

## Success

1. Cases 1 through 4 are each named in a test and proven: `recover` clears the on-disk slot facts
   and releases the fence in one call for a slot proven dead.
2. Case 5 is refused, not cleared, with a per-slot disposition distinct from every other refusal
   this seam emits, and the code encoding it cites ADR-0657:62-66.
3. A live unit is refused in each of the five cases: `recover` releases no fence for a slot whose
   unit cgroup is populated, and none for a slot it cannot prove dead.
4. No synthesized `TerminationOutcome` and no cross-invocation attribution in any of the five
   cases.
5. `lifecycle_protocol_identity()` is byte-identical to its value on the base commit, and
   `tests/scripts/test_live_stack_scripts.py` needs no identity update.
6. `just ci` is green.

## Validation

The per-task **Verification** inventories in
`docs/workflow/plans/2026-09-16-recover-residual-slots.md` carry one entry per material changed
contract, with the observable contract, test case, expected red failure, and exact focused green
command. They are not restated here: in the full-spec lane the plan owns that inventory, and two
copies drift.

Coverage of the success criteria above: criterion 1 by Task 4's four residual cases; criterion 2
by Task 4's unreadable-identity case; criterion 3 by Task 4's parametrized live-unit case;
criterion 4 by Task 3's equivalence case, which pins that every outcome still comes from
`_identity_outcome` over a real observation; criterion 5 by Task 4's protocol-identity case
against a literal captured from the base commit; criterion 6 by the plan's closing verification.

One contract has no task-specific executable observation: **migration ordering**.
`Mode: task-test-not-applicable` — the changed surface is the migration file's ordinal position,
which has no observation of its own; the repository's `migration-order-check` recipe is the
consumer that validates it, and it runs in `just ci`.

Beyond the focused tests: `just lint`, `just type`, `just test-changed`, then the full `just ci`
including `lock-check`, `lint-workflows`, and `container-arch-check`, which no workflow runs
(#2582).

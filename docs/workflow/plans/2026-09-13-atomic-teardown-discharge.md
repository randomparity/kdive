# Atomically finalize ordinary System teardown

**Goal.** Prevent ordinary teardown from recording `torn_down` before it has discharged mutation
obligations, without reopening its provision race.

**Architecture.** A new non-terminal `tearing_down` state is the committed provider-call fence.
The post-provider locked transaction discharges obligations and publishes `torn_down` together.

**Tech stack.** Python 3.14, PostgreSQL migrations, `pytest`, `uv`. Decision:
`docs/adr/0650-atomic-ordinary-teardown-terminal-commit.md`.

Expected implementation size: 280–420 changed lines (M) — state/migration consumers, generated
references, and focused integration and adversarial proof.

## Global Constraints

- Python uses Ruff's 100-column limit and strict whole-tree `ty` checks.
- Existing schema files are immutable; add the next migration only.
- `torn_down` must not commit before worker-fenced mutation discharge succeeds.
- Preserve external-boot and authority-owned teardown behavior.
- Refresh generated artifacts before checks; run `just ci > /tmp/kdive-2370-ci.log 2>&1 < /dev/null`.

## File map

| Path | Responsibility |
| --- | --- |
| `src/kdive/domain/capacity/state.py` | State value, legal edges, rootfs classification. |
| `src/kdive/db/schema/0153_system_tearing_down_state.sql` | Add the state to the database constraint. |
| `src/kdive/jobs/handlers/systems.py` | Fence before provider call, compensate a slow provision, and atomically finalize. |
| direct state-set consumers | Correct capacity, reconciliation, console, and public-state classification. |
| focused tests and generated references | Prove behavior and publish the returned state. |

## Task 1 — Add the lifecycle fence

**Files.** Modify `src/kdive/domain/capacity/state.py`, direct state-set consumers, and their
state tests. Create `src/kdive/db/schema/0153_system_tearing_down_state.sql`.

**Interfaces.** Add `SystemState.TEARING_DOWN`; each current ordinary teardown source state gains
`-> TEARING_DOWN`; `TEARING_DOWN -> TORN_DOWN` is its only legal successor. The migration changes
only the current `systems_state_check` constraint.

**Verification.** Mode: focused-test. Extend `tests/domain/test_state.py`,
`tests/domain/test_system_state_sets.py`, and
`tests/domain/test_rootfs_reclaim_state_classification.py`. Expected red: the enum/transition and
exhaustiveness assertions fail before the state and classifications exist. Green:
`just test-verbose tests/domain/test_state.py tests/domain/test_system_state_sets.py tests/domain/test_rootfs_reclaim_state_classification.py` exits 0.

**Steps.** Write the failing expectations; run the command and observe failure; add the enum,
transition table, migration, and classification entries; re-run to green. The rollback is removing
the additive migration only before it ships.

## Task 2 — Couple discharge and terminal publication

**Files.** Modify `src/kdive/jobs/handlers/systems.py`,
`src/kdive/jobs/handlers/system_reclaim.py`, and
`tests/jobs/handlers/test_systems_bootstrap_key.py`.

**Interfaces.** A locked post-provider helper in `systems.py` consumes `(conn, job, system,
system_id)` and invokes `worker_discharge_system_mutation_obligations`,
`SYSTEMS.update_state(..., TORN_DOWN)`, and `audit_transition` in one transaction. The provision
result compensator treats `TEARING_DOWN` like `TORN_DOWN` for domain reap only; it remains
non-terminal everywhere else. The provider call is outside the transaction.

**Verification.** Mode: focused-test. Add a test that injects a discharge exception and asserts
`tearing_down` plus an open obligation, and update the success arm to assert terminal state and a
closed obligation. Expected red: current code either reports `torn_down` after the injected failure
or lacks `tearing_down`. Green:
`just test-verbose tests/jobs/handlers/test_systems_bootstrap_key.py` exits 0.

**Steps.** Add the failing assertions and observe red; commit the initial state fence before the
provider call; implement the final atomic transaction; run the focused suite. Provider failure must
leave `tearing_down`; do not discharge early.

## Task 3 — Preserve the race and regenerate public artifacts

**Files.** Modify `tests/adversarial/test_provider_state_races.py` only as needed for the new
intermediate assertion; regenerate committed CLI/MCP reference artifacts with repository recipes.

**Interfaces.** The existing blocked-provider race remains: provisioning after teardown starts
creates no live domain, and teardown completes at `torn_down` after release.

**Verification.** Mode: focused-test. Run
`just test-verbose tests/adversarial/test_provider_state_races.py`; expected red before the fence is
implemented is a provisioned domain or wrong final state, and green exits 0. Then run the relevant
generator recipe and `just ci > /tmp/kdive-2370-ci.log 2>&1 < /dev/null`; expected result is exit 0.

**Steps.** Run the race test with the provider gate; regenerate after code and state-schema changes;
inspect generated diffs; run the full gate. Revertable source changes are the rollback; the migration
is additive and must never be edited after deployment.

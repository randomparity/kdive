# Re-land residual systemd worker-slot recovery — implementation plan

**Goal:** Make the existing `recover` operation retire the four recoverable residual slot shapes,
refuse unreadable invocation identity distinctly, and preserve every existing protocol and fence
contract.

**Architecture:** Extend the existing `SystemdWorkerLifecycle` seam. `SlotStore` exposes raw residue
and fixed-file cleanup; `PostgresAuthority` wraps retained migration 0155's read function and the
unchanged exact termination function; the coordinator classifies a slot's complete active-row set
before any release. The ordinary evidence path remains first and falls back only for the two
verified residual exceptions.

**Tech stack:** Python 3.14, Pydantic models, psycopg 3 async connections, PostgreSQL functions,
pytest, `uv`, `just`, systemd.

Expected implementation size: 900–1250 changed lines (L) — derived from three source modules,
three focused test modules, the prior reviewed 1,176-line source/test diff, and bounded docs.

## Global constraints

- Base branch: `main`; branch: `feat/reland-residual-slots-2533`; sibling worktree only.
- Supported native targets: x86_64 and ppc64le. Python floor: 3.14. Ruff line length: 100.
- Retained migration `0155_recoverable_worker_incarnation_read.sql` is byte-immutable and must not
  change. `Operation`, request/response models, protocol identity tests, table schema,
  `terminate_worker_incarnation`, and `CURRENT_WORKER_FENCE_PROTOCOL` stay unchanged.
- ADR-0657 forbids fabricated outcomes, cross-invocation result attribution, and recovery when the
  invocation identity is unreadable. ADR-0667 requires slot-derived prefix lookup, stored-binding
  echo, host filtering, and full-row classification before release.
- The exact approved exclusions and their owners are frozen in `WORK:SCOPE` token
  `q2533-d0a8dc5d`. A changed exclusion or protected contract returns to the scope checkpoint.
- Iterate with `just test-changed`, focused `just test-verbose <path>`, `just lint`, and `just type`.
  Before push run `just ci > <private-log> 2>&1 < /dev/null` with its bare exit status.
- For commits touching Markdown, stage exact paths, run `prek run`, and re-add only those paths.
- Public issue, PR, and commit text must contain no hostnames, IPs, usernames, internal domains,
  credentials, or private paths.

## Task 1: Inspect and clear residual slot facts

**Files:** modify `src/kdive/processes/lifecycle/systemd/systemd_worker_state.py` and
`tests/processes/lifecycle/systemd/test_systemd_worker_state.py`.

**Interfaces**

- Produce `SlotResidue(StrEnum)` with `EMPTY`, `STATE_ABSENT`, `STATE_UNREADABLE`, and
  `STATE_VALID`.
- Produce frozen `SlotInspection(residue: SlotResidue, state: SlotState | None)`.
- Produce `SlotStore.inspect() -> SlotInspection` and
  `SlotStore.discard_unrecoverable() -> bool`.
- Task 3 consumes all four interfaces through the `SlotStorage` protocol.

**Verification**

- Contract: inspection distinguishes missing directory, missing document, malformed document, and
  valid state. Mode: focused-test. Add named tests in `test_systemd_worker_state.py`; before code,
  they fail on missing imports/methods. Green command:
  `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_state.py`.
- Contract: unconditional cleanup removes only `worker.env`, credential, `release`, and
  `state.json`, reports whether it removed anything, requires root, rejects unsafe directory
  metadata, and fsyncs the slot directory. Mode: focused-test. Add named cleanup, idempotence,
  root, and metadata tests; before code they fail on missing method. Use the same green command.

**Steps**

1. Add the focused tests and run the green command to observe the expected missing-interface red.
2. Add `SlotResidue` and `SlotInspection` beside `SlotState`.
3. Implement `inspect` by opening the validated fixed slot directory, reading only `state.json`,
   and converting parse failure to `STATE_UNREADABLE` without changing `load`.
4. Implement `discard_unrecoverable` using `_require_root`, `_slot_descriptor(create=False)`,
   fixed literal filenames, `dir_fd` unlink, and one directory fsync after attempted removals.
5. Run the focused test command, `just lint`, and `just type`; commit the task.

Acceptance: existing state transitions remain unchanged; only recovery can call the new cleanup,
and malformed state remains an error through ordinary `load`.

Rollback: revert this task commit; it changes no persisted format.

## Task 2: Read recoverable fence rows through retained migration 0155

**Files:** modify `src/kdive/worker_lifecycle/authority_store.py` and
`tests/worker_lifecycle/test_authority_store.py`; read but do not modify migration 0155.

**Interfaces**

- Produce
  `recoverable_worker_incarnations(conn: AsyncConnection, unit: str) -> tuple[LocalWorkerIncarnation, ...]`.
- Task 3 consumes it through `PostgresAuthority.recoverable`.
- Reuse existing `_validated_binding`, `LocalWorkerIncarnation`,
  `require_top_level_transaction`, and `terminate_worker_incarnation` signatures confirmed in
  `authority_store.py` on the base commit.

**Verification**

- Contract: the wrapper calls `public.recoverable_worker_incarnations(%s)` in a top-level
  transaction, reconstructs validated local records in SQL order, and rejects more than 16 rows
  because migration 0155 uses row 17 as an overflow sentinel. Mode: focused-test. Add query,
  malformed-binding, transaction, empty, and overflow tests; before code they fail on the missing
  function. Green command: `just test-verbose tests/worker_lifecycle/test_authority_store.py`.
- Contract: retained migration 0155 remains byte-identical. Mode: focused-test. Capture
  `git hash-object src/kdive/db/schema/0155_recoverable_worker_incarnation_read.sql` before changes
  and require the same value before ship; any mismatch is a hard failure.

**Steps**

1. Record migration 0155's blob hash.
2. Add focused tests and observe the expected missing-import red.
3. Add `_MAX_RECOVERABLE_ROWS = 16` and the async wrapper. Validate each returned binding with
   `_validated_binding("local", row[1])`; construct `LocalWorkerIncarnation` records without adding
   a new model or query.
4. Run the focused test command, `just lint`, `just type`, and the migration hash comparison;
   commit the task.

Acceptance: no SQL, table, role grant, write function, or fence protocol changes.

Rollback: revert this task commit; retained migration 0155 remains installed but inert.

## Task 3: Classify complete row sets and recover atomically per slot

**Files:** modify `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` and
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

**Interfaces**

- Extend `IncarnationAuthority` with
  `recoverable(unit: str) -> tuple[LocalWorkerIncarnation, ...]` and
  `release(record: LocalWorkerIncarnation, outcome: TerminationOutcome) -> None`.
- Extend `SlotStorage` with Task 1's `inspect` and `discard_unrecoverable`.
- `PostgresAuthority.recoverable` calls Task 2's wrapper;
  `PostgresAuthority.release` passes the row's stored binding unchanged to existing
  `terminate_worker_incarnation`.
- Factor `_identity_outcome(*, unit, boot_id, invocation_id, observation)` from
  `_terminal_observation`; ordinary callers retain the existing exception behavior.
- Add distinct internal refusal codes `recovery_refused_unreadable_identity` and
  `recovery_refused_incoherent_row`; no response schema changes.

**Verification**

- Contract: cases 1-4 each release the complete local matching active-row set before clearing
  files, and ordinary valid-state termination stays first. Mode: focused-test. Add one named test
  per issue case plus multiple-row, no-row residue, and evidenced-first/fallback tests. Before code,
  missing protocol methods and unchanged residual behavior fail. Green command:
  `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
- Contract: case 5 refuses with its distinct code and ADR-0657 comment, preserving rows/files.
  Mode: focused-test. Add same-boot `BootObservation` tests for valid and raw residue; before code
  the sweep raises `SystemdUnavailable`. Use the same green command.
- Contract: populated cgroup and unknown membership fail closed for each residual input, unmanaged
  workers refuse the whole sweep, foreign host/unit rows refuse before any release, and one
  unclassifiable row prevents partial release. Mode: focused-test. Add parameterized liveness and
  classification-order tests; before code existing recovery lacks the row path. Same green command.
- Contract: no fabricated or cross-invocation outcome. Mode: focused-test. Assert release outcomes
  from current observation versus stored identity, including successor identity yielding `killed`
  without consuming successor result fields. Same green command.

**Steps**

1. Add protocol doubles and focused tests; run the command and retain the expected behavioral red.
2. Add refusal constants/messages, `_Recovery`, and the two protocol extensions.
3. Add `PostgresAuthority.recoverable/release` with unchanged exact terminate semantics.
4. Factor identity comparison into `_identity_outcome`; keep `_terminal_observation` behavior
   byte-for-byte equivalent for existing callers.
5. Replace `load`-only recovery with inspection plus an evidenced-first retirement. On only
   `EvidenceRejected` or `StateConflict`, query row-derived recovery.
6. Validate each row's exact unit and local hostname; derive outcomes for the complete row set;
   return the identity or incoherent refusal before writes. Then release rows sequentially. A
   release failure stops file cleanup, leaving remaining evidence visible.
7. Clear files only after successful complete release/no-row classification; reset failed identity
   only for a retained `UnitObservation`. Preserve completed/refused slot results on later failures.
8. Add the unmanaged-worker guard already used by `start` so an out-of-unit worker cannot evade the
   per-slot cgroup check.
9. Run focused tests, `just lint`, `just type`, and `just test-changed`; commit the task.

Acceptance: all issue cases and liveness guards pass; ordinary start/status/stop behavior remains
covered and the protocol model files are unchanged.

Rollback: revert this task commit before Task 4 docs; no migration rollback exists or is needed.

## Task 4: Align operator and decision records

**Files:** modify only the residual-gap subsection of
`docs/operating/runbooks/live-stack.md`; append one implementation-status amendment under
`## Consequences` in
`docs/adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md`.

**Interfaces**

- The runbook tells operators cases 1-4 are recovered and case 5 remains refused with the exact
  greppable code.
- The ADR amendment records that #2533 is implemented again and supersedes only the prior
  implementation-status amendment, not ADR-0667's decision.

**Verification**

- Contract: docs are truthful and links resolve. Mode: task-test-not-applicable. There is no
  executable consumer of these prose claims; verify behavior in Task 3 and run document guards:
  `just docs-links`, `just docs-paths`, `just served-doc-links`, and `just adr-status-check`.
- Contract: merged ADR history is append-only. Mode: focused-test. Run `git fetch origin main` then
  `just records`; expected result is exit 0 with no E-REWRITE/E-GONE finding.

**Steps**

1. Update only the now-false residual-gap prose; do not add the #2489 operator procedure.
2. Append the dated implementation-restored amendment after the 2026-09-17 amendment without
   rewriting accepted text or changing Status.
3. Stage the exact spec, plan, runbook, and ADR paths; run `prek run`; re-add only those exact paths.
4. Run the document guards and `just records`; commit the task.

Acceptance: operator prose no longer recommends a manual database edit, case 5 remains a decision,
and the ADR history truthfully describes merge, revert, and reimplementation.

Rollback: use another append-only amendment if this implementation is later reverted; never delete
or rewrite the existing amendments.

## Task 5: Verify the assembled branch and provisioned-host contract

**Files:** no intended code changes. Verification fixes stay within the frozen surface and get
their own commit; any protected-contract or exclusion change returns to scope checkpoint.

**Interfaces**

- Consume Tasks 1-4 as one assembled candidate.
- Produce exact local guardrail evidence and a sanitized provisioned-host proof record for the PR.

**Verification**

- Contract: protocol identity unchanged. Mode: focused-test. Compare
  `lifecycle_protocol_identity()` on branch and base in isolated checkouts and run the existing
  contract/script tests. Expected equality and no diff to protocol identity fixtures.
- Contract: installed host runs the exact branch build and migration 0155. Mode: focused-test.
  Before reinstall, importing `recoverable_worker_incarnations` from the installed witness
  environment must fail or resolve to the prior build; after reinstall it succeeds and the
  database function exists. Record the installed package/commit identity without publishing host
  identifiers.
- Contract: real residual behavior. Mode: focused-test. On a provisioned systemd host, induce in
  separate cleaned arms: absent state, malformed state, rewritten stored binding, ordinary
  evidence rejection, same-boot unreadable identity, and live populated unit. Cases 1-4 end with no
  slot files and no active matching rows; case 5 and live unit preserve both. Restore the host after
  each arm and verify no active test row/unit/file remains before the next.

**Steps**

1. Run `just lint`, `just type`, `just test-changed`, then focused tests for all three test modules.
2. Stage all intended paths, run `prek run`, re-add exactly those paths if hooks rewrite, and commit
   any formatting-only fixes separately.
3. Run `just ci > <private-log> 2>&1 < /dev/null` as the last command in its tool call; record bare
   exit status, summary, commit, architecture, and observed duration.
4. Attempt only configured safe provisioned-host access. Confirm OS/architecture/tool versions,
   install the exact branch build, validate the positive accessor signal, then run the six bounded
   arms with cleanup checks. Redact host, user, IP, serial, and internal names from shared output.
5. Re-run changed focused checks after any in-scope host-proof fix, then repeat the affected arm.
   If exact-build provisioned proof remains unavailable, park the issue rather than weakening or
   fabricating criterion 7.
6. Confirm migration 0155's hash, protocol identity parity, clean worktree, and no changed file
   outside the frozen surface. Hand the branch to iterative adversarial and security review.

Acceptance: all local and live criteria pass against one final commit, or the issue is durably
parked with the exact unavailable external prerequisite.

Cleanup: restore host services, units, slots, database rows, checkout, and backends to their
pre-proof state; record any unavoidable residue explicitly and privately.

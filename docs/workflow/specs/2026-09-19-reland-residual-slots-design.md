# Re-land residual systemd worker-slot recovery

Issue #2533, child 2 of epic #2488. The governing decisions are
[ADR-0667](../../adr/0667-recovery-names-the-fence-row-by-the-slot-derived-incarnation.md)
and
[ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md).

## Problem

`recover` can retire a dead fixed worker slot only while `state.json` parses and its authority
binding still equals the active `worker_incarnations` row. An absent or malformed state document,
a drifted stored binding, or rejection of the ordinary evidence path therefore leaves the database
fence and root-owned slot files in place. A fifth shape, systemd reporting no invocation on the
row's retained boot, must stay refused because ADR-0657 says same-boot absence is not termination
evidence.

PR #2592 demonstrated a working implementation but was reverted by PR #2597 for an unauthorized
merge, not a technical defect. This design revalidates the behavior on current `main`; it does not
merge or modify the retained branch. Migration 0155 and ADR-0667 survived the revert and are
immutable inputs.

## Architecture

The coherent owner remains `SystemdWorkerLifecycle.recover`. `SlotStore` reports raw residue and
can discard files after authorization; `PostgresAuthority` exposes ADR-0667's read-only row lookup
and uses the existing exact terminate function to release a row with its stored binding. No caller
moves and no compatibility facade is added.

For each fixed slot, recovery first observes systemd and refuses a populated or unobservable
cgroup. It then inspects the slot without requiring valid JSON and asks the witness authority for
active rows with migration 0155's exact slot-derived incarnation prefix. The coordinator validates
that every row describes this unit and host, derives each row's outcome from its stored boot and
invocation identity against the current systemd observation, and classifies the complete row set
before releasing any row. Only after every row releases does it clear root-owned slot files and
reset a retained failed unit identity.

A valid retained state first uses the ordinary evidence-preserving path. `EvidenceRejected` or
`StateConflict` switches to row-derived recovery; other failures still fail the request. Prepared
state holds no fence and remains a file-only discard. Already terminated state remains ordinary
cleanup. This preserves the existing stop/reconciliation path rather than duplicating it.

## Components and contracts

- `SlotInspection` distinguishes an absent slot directory, absent `state.json`, malformed
  `state.json`, and valid state without weakening `SlotStore.load`.
- `SlotStore.discard_unrecoverable() -> bool` requires root, validates directory metadata, unlinks
  only the four fixed slot files, fsyncs the directory, and reports whether it removed a fact.
- `recoverable_worker_incarnations(conn, unit) -> tuple[LocalWorkerIncarnation, ...]` wraps retained
  migration 0155, validates every returned binding through the existing local-binding validator,
  and refuses an implausibly large result rather than accepting the SQL sentinel row.
- `IncarnationAuthority.recoverable(unit)` returns the complete bounded active row set;
  `release(record, outcome)` calls the existing binding-checked termination path unchanged.
- Identity comparison is factored from `_terminal_observation` so both retained state and database
  rows use one mapping. A same-boot `BootObservation` returns the distinct
  `recovery_refused_unreadable_identity` disposition only in recovery; ordinary lifecycle callers
  continue raising `SystemdUnavailable`.
- The response remains the existing `LifecycleResponse` shape. Any per-slot refusal makes the
  request a conflict while retaining results for slots already completed.

The lifecycle protocol models, table, terminate function, and fence protocol do not change.
Migration 0155 is neither edited nor re-added. The runbook changes only its now-false description
of the residual gap. ADR-0667 receives an append-only implementation-status amendment after the
behavior and proof are complete.

## Failure model

**Actors and deployments**

- A local operator invokes the root-only lifecycle socket on a provisioned systemd host.
- The host lifecycle service uses the `kdive_lifecycle_witness` database role.
- The named deployments are the fixed-slot x86_64 and ppc64le systemd hosts; tenant and anonymous
  traffic cannot invoke this seam.

**Invariants and assets at stake**

- A slot with a populated cgroup or an unclassifiable active row retains every fence and file.
- A recovery pass releases either the complete classified row set for one slot or none of it.
- Outcomes come only from current systemd observations compared with stored identities.
- On-disk facts are cleared only after the corresponding active rows are released or no active row
  exists; failure between database release and file deletion leaves repairable residue, not an
  untracked live worker.
- A host cannot release a row whose stored host or unit does not match its fixed slot.

**Accepted failure classes**

- A crash after complete fence release but before file deletion can leave residual files; retry is
  safe because the row query then returns no active rows.
- A row edited by a database superuser to a readable but false identity is judged against the
  stored identity; protecting state from a database superuser is outside the named deployment.
- Directory permission or I/O faults refuse cleanup through existing metadata validation.
- Same-boot absence of invocation identity remains refused until a reboot provides different-boot
  evidence, as required by ADR-0574 and ADR-0657.

**Covered elsewhere**

- Operation/schema identity and the original fleet reprovision: #2532.
- Ordinary successor-invocation reconciliation: #2485.
- Gate restart/tampering distinction: #2486.
- Operator procedure beyond correcting false residual-gap text: #2489.
- The sixth systemd observation shape: #2596.

## Threat model

**Boundary inventory.** No external entry point is added. The existing privileged recovery path is
widened to read active local fence rows without their credential and release them using the stored
binding. The unit argument is derived from the fixed slot index, not request text.

**Actor model.** A local unprivileged process is untrusted. Root on the host and the lifecycle
witness database role remain trusted, as in ADR-0574. A database superuser is outside this model.

**Controls.** Migration 0155 already validates the fixed unit grammar, constrains exact prefix,
generation, authority kind, active state, order, count, role membership, and search path; it returns
no credential material. Python revalidates returned bindings, enforces a stricter maximum count,
filters exact unit and local hostname, refuses populated or unknown cgroups, classifies the full row
set before writes, and calls the unchanged exact-binding termination function.

**Explicitly out of scope.** Attribution of the human operator and protection against a database
superuser are not reachable controls for this local root-operated deployment. Relaxing unreadable
identity refusal requires an ADR amendment and is not part of this change.

## Success

1. Tests named for cases 1-4 prove a single `recover` call clears the four fixed slot files and
   releases the complete matching active fence-row set for a proven-dead slot.
2. Tests for case 5 prove a distinct `recovery_refused_unreadable_identity` result, unchanged files,
   and active fences, with the implementing comment citing ADR-0657.
3. Tests over the five named residual shapes prove a populated cgroup releases no row and clears no
   file; unknown membership also fails closed.
4. Tests prove every published outcome is derived from the current observation and stored identity,
   never synthesized from another invocation's result fields.
5. The lifecycle protocol identity equals current `main`, retained migration 0155 is byte-identical,
   and no protocol-identity fixture changes.
6. Focused tests, `just lint`, `just type`, `just test-changed`, and `just ci` pass.
7. On a provisioned systemd host running the installed exact branch build, induced absent state,
   malformed state, drifted binding, ordinary evidence rejection, unreadable identity, and live-unit
   arms exhibit the same release/refusal behavior as the focused suite.

## Validation

The implementation plan inventories each material contract and its focused red/green proof. The
live proof additionally verifies installation by importing the new read accessor from the installed
witness environment and confirming migration 0155's function exists; protocol identity alone cannot
distinguish the branch because preserving it is a success criterion. Each destructive arm includes
precondition capture and post-arm cleanup verification before the next arm.

Host architecture is x86_64; declared project targets are x86_64 and ppc64le, so the host covers one
declared native target while the ordinary CI/container guards retain cross-target coverage.

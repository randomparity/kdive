# 0657 — A successor invocation is terminal evidence for the retained one

## Status

Accepted (2026-09-15)

## Context

[ADR-0574](0574-systemd-supervises-host-worker-incarnations.md) binds each retained worker slot to
an exact unit, generation, boot ID, and invocation ID, and states two rules this record qualifies:
the gate releases a worker only when the root-owned marker equals both the retained generation and
systemd's `INVOCATION_ID` for the gate's own process, so the marker proves *this* invocation was
registered; and absence within the same host boot is never termination evidence. Both claims are
cited here by their wording rather than by line, because this change appends to ADR-0574 and would
shift any number given.

`kdive-live-worker@N.service` is restarted outside the lifecycle contract — a `needrestart` sweep,
unattended upgrades, a manual `systemctl restart`. The unit comes back carrying a new
`INVOCATION_ID` while retained slot state still names the previous one. `_terminal_observation`
(`src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`) then raises
`LifecycleConflict("systemd invocation does not match retained state")`. Every lifecycle caller
reaches that function, so `stop` wedges as thoroughly as `start` and the slot cannot be reconciled
through the shipped contract at all; recovery took hand-edited root-owned slot files and an
`UPDATE` against `worker_incarnations` (#2481, #2484).

## Decision

**A successor invocation is terminal evidence for the retained one.** When the retained unit
reports, on the retained boot, an invocation identity other than the retained one, the witness
records the retained incarnation as `killed` rather than refusing the slot. A systemd unit carries
at most one invocation at a time and is assigned a new `INVOCATION_ID` only when it changes from an
inactive state into an activating or active one, so a different identity for the same unit on the
same boot is the *presence of a successor*, which entails that the retained cycle already ended.
That is why the rule does not contradict ADR-0574's same-boot rule, which governs *absence*: an observation
carrying no invocation identity at all on the retained boot still raises `SystemdUnavailable`, and
a differing boot ID still yields `killed`. No second discriminator — a "newer than the retained
one" check — is added, for the reason recorded in the first rejected alternative below.

**The outcome is `killed`, never a mapped result.** The successor's `Result`, `ExecMainStatus`, and
cgroup membership describe the successor, so they are never mapped onto the retained incarnation.
`killed` is what the differing-boot-ID rule already publishes, for the same reason: the retained
invocation ended and its own exit facts are no longer observable.

**The rule releases only the retained binding.** It publishes evidence for the exact retained unit,
generation, boot, and invocation, which PostgreSQL re-checks before accepting it
(`terminate_worker_incarnation`); a rejected binding stays `EvidenceRejected` and retains every host
object. It can never release the successor, release a slot on a boot it does not name, release a
slot whose invocation identity is unreadable, or publish `killed` for a live worker — the successor
cannot be live under the gate decision below, and the retained invocation cannot be live once a
successor exists.

**The gate's marker binding stays strict (#2486).** The marker is not re-derived or re-published
for the current `INVOCATION_ID`. Re-deriving it would leave the marker proving only that *some*
invocation of that generation was registered, and that binding is the one thing stopping a replayed
marker from releasing an unregistered generation. Epic #2484's R2 requires the strict branch to
carry a distinguishable out-of-band-restart disposition; #2486 owns what the gate emits. The
consequence this record relies on is narrower and is a property of the strict binding itself: a
successor invocation's gate exits before it can `exec` the worker, so a successor never holds a
running worker.

**A `recover` operation clears facts, never evidence (#2488).** ADR-0574 calls force removal
"an operator recovery that may strand fences and cannot create termination evidence", and that
holds. Such an operation may clear the on-disk slot facts and release the `worker_incarnations`
fence for a slot proven dead; it may not fabricate a `TerminationOutcome`, attribute one
invocation's exit facts to another, or run for a slot whose invocation identity is unreadable.

## Consequences

`stop` now publishes terminal evidence for the retained incarnation of a restarted slot, clears its
fence, and removes its slot files, so the part that previously took an `UPDATE` against
`worker_incarnations` is reached through the contract. Such a slot is reported `killed` with no
distinction from other unobservable terminations; the restart is visible in the journal and in the
gate disposition #2486 emits, not in the outcome value.

**The unit is left `failed`, and `start` still refuses it.** Under the strict gate binding the
successor invocation exits non-zero, which leaves `ActiveState=failed` with its `InvocationID`
retained. `stop_retained` runs `systemctl stop`, which is a no-op on a failed unit — reproduced on
systemd 259: `ActiveState` and `InvocationID` are unchanged and only `systemctl reset-failed`
clears them — and nothing in the coordinator calls `reset-failed`. `require_inactive`
(`src/kdive/processes/lifecycle/systemd/systemd_worker_runtime.py`) demands an empty
`InvocationID`, so the next `start` returns `conflict / operator_recovery` until an operator runs
`systemctl reset-failed` or #2488's recovery operation clears it. This residual is not created
here: any worker exiting non-zero already leaves the same failed unit behind the same
`stop_retained` no-op. Epic #2484 anticipates it — under the strict branch its first success
criterion is satisfiable only once #2488 merges.

A `start` that ends that way reports the conflict and an empty slot list, so its response does not
say that the retained incarnation was retired and its fence released before activation refused —
the operator learns that from `status` or from the database, not from the failing call. Reporting
it belongs to the response-taxonomy work in #2487 and the recovery operation in #2488; this record
states the consequence rather than threading retired states through a failure path outside the
rule this decision governs.

An operator who restarts a unit deliberately while a worker is doing useful work now has that
worker's incarnation retired on the next lifecycle request instead of blocking it. That is the
intended trade: the worker is already gone, because the restart took its cgroup down.

This record amends ADR-0574 rather than superseding it. ADR-0574's lifecycle, credential, witness,
gate-binding, and cleanup decisions remain in force, and the amendment is recorded in the section
it qualifies.

## Considered & rejected

- **Require the current invocation to be newer than the retained one.** verified: `systemd.exec(5)`
  `$INVOCATION_ID` (systemd 259) states a new ID is assigned only when the unit changes from an
  inactive state into an activating or active state, so the reported identity is the unit's current
  cycle and cannot be older than a retained one for the same unit on the same boot. `UnitObservation`
  (`src/kdive/processes/lifecycle/systemd/systemd_worker_runtime.py`) carries no timestamp, so the
  check would need a new observation field to express a property already entailed.
- **Keep the `LifecycleConflict` and recover only through a new operation.** judgment: it leaves the
  shipped `start`/`status`/`stop` contract unable to retire a slot it created, and leaves the
  database fence reachable only by an operator `UPDATE`.
- **Map the observed `Result`/`ExecMainStatus` onto the retained incarnation.** verified: those
  fields are read from the unit's current properties, which after a restart belong to the successor.
- **Treat same-boot absence as terminal too, for symmetry.** judgment: absence is consistent with a
  live invocation systemd cannot currently report, which is the case ADR-0574's same-boot rule refuses to guess
  at; the successor's presence is what carries the proof here.
- **Re-derive the gate marker for the current `INVOCATION_ID`.** verified: the marker comparison in
  `_wait_for_release` (`deploy/systemd/bin/kdive-live-worker-gate`) is the only check binding a
  release to the invocation that was registered; accepting any invocation for a retained generation
  would let a marker left by a registered invocation release a later, unregistered one.
- **Supersede ADR-0574 with a banner rather than amending it.** verified: `docs/adr/README.md`
  directs a partial supersession to an appended amendment, and this record replaces one claim while
  ADR-0574's lifecycle, credential, witness, gate-binding, and cleanup decisions stay in force, so
  the `> **Superseded by …**` form that `.github/scripts/profiles/adr.sh:47-49` recognizes would
  misstate the relationship.
- **Do nothing.** verified: `_terminal_observation` raises on every caller path, so on the
  provisioned host reported in #2481 no `start`, `status`, or `stop` could retire the slot.

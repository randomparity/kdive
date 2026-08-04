# 0541 — Teardown of an adopted host restores the baseline kernel or cordons the host

## Status

Proposed

## Context

Teardown on every provider KDIVE has destroys a machine. `local_libvirt` and `remote_libvirt`
undefine a domain; `fault_inject` drops a synthetic record. Success is trivially verifiable —
the domain is gone — and a failure leaves an orphan the reconciler
([ADR-0021](0021-reconciler-loop-drift-repair.md)) reaps. Nothing that survives a failed
teardown can be handed to the next tenant, because nothing survives.

An adopted host ([ADR-0540](0540-adopt-only-provisioning.md)) inverts that. Release does not
destroy anything; it *restores*. KDIVE installed a kernel into the operator's running OS,
pointed the bootloader at it, and then deliberately crashed it. Returning the host means
undoing that. And a restoration, unlike a destruction, can partially fail while leaving a
machine that looks perfectly healthy: SSH answers, the service processor answers, discovery
finds it, and placement schedules it. The next allocation then starts on someone else's debug
kernel, with someone else's `crashkernel=` reservation and someone else's `kgdboc` on the
command line — and the resulting behavior is attributed to whatever that tenant is debugging.

That is the failure this record exists to make impossible. It is not hypothetical: a
force-crash is the provider's central operation, and a host is at its most likely to be
unreachable exactly when teardown runs.

The available mechanisms are all already in the tree.

- `Resource.cordoned` (`src/kdive/domain/catalog/resources.py:34`,
  [ADR-0062](0062-platform-operations.md) §3) is a schedulability axis orthogonal to the health
  `status` enum. Placement skips a cordoned host
  (`src/kdive/services/allocation/admission/placement.py:86`, `:104`), and
  `resources.set_scheduling` is the operator's path back. **No existing producer persists a
  reason.** There are six write sites — `inventory/reconcile/prune.py:52` and `:85`,
  `reconciler/cleanup/runtime_resources.py:148`, `mcp/tools/ops/resources/deregister.py:283`
  and `:347`, and `mcp/tools/ops/resources/host_ops.py:145` (reached by both
  `resources.set_scheduling` and `resources.drain`) — and every one writes the boolean alone.
  `resources.drain` takes an audited `reason`, but it goes to the audit log, not the row. So a
  cordoned host today carries no machine-readable cause, which is what this decision changes for
  its own writes and what step 0 below must read before it writes.
- `ErrorCategory.RESTORE_INCOMPLETE` ([ADR-0513](0513-restore-incomplete-failure-category.md))
  already names a restore that can never complete, whose subject's state is indeterminate, and
  whose operator response differs from `infrastructure_failure` — which is a retryable,
  unclassified fault in the layer below. It was added for a snapshot revert whose worker died
  mid-flight. The shape matches.
- Reconcile merges into the `capabilities` jsonb rather than replacing it
  (`src/kdive/inventory/reconcile/resources.py:292`, `:493`), so a key written by another
  writer survives a reconcile pass.

## Decision

**The host is cordoned for the whole restore, and the cordon clears only on verified success.**
In order:

0. **Cordon the Resource if it is not already cordoned**, recording in the reason both that a
   restore is in progress **and whether this teardown is the writer that set the boolean**.
   Placement excludes a cordoned Resource on both paths
   (`src/kdive/services/allocation/admission/placement.py:86` and the `AND NOT cordoned`
   predicate at `:104`), so from here until step 4 no tenant can be granted this machine.
1. Re-point the host's bootloader default at the declared `baseline_kernel`, arch-keyed
   (`grubby` on x86, grub2-PReP or petitboot on a PowerVM LPAR).
2. Power-cycle through the out-of-band driver
   ([ADR-0539](0539-out-of-band-control-port.md)), not over SSH. The host may be wedged, and
   the in-band path is the one a kernel debugger destroys.
3. Re-run the adopt preconditions — the same module `provision` and `doctor` call
   (ADR-0540) — against the rebooted host, including that the running kernel is now the
   declared baseline.
4. **Clear the cordon.** This is the success signal, and it is the only thing that returns the
   host to the schedulable pool.

Step 3 is the load-bearing verification. A power-cycle that returns is not evidence that the
host is back in the state adopt requires; only the adopt predicate is evidence of that, and
running the same predicate is what makes "returned" mean the same thing as "adoptable". This is
the precondition module's third caller, and the reason it is one module.

**Step 0 is what makes the invariant continuous rather than eventual**, and it is there because
the allocation is already gone by the time the restore starts. `_release_locked`
(`src/kdive/services/allocation/release.py:229`) drives `active → releasing → released` in one
transaction, consulting nothing about the System or its teardown, and `_count_occupying`
(`src/kdive/services/allocation/admission/core.py:586`) counts only GRANTED/ACTIVE/RELEASING —
so ADR-0540's `concurrent_allocation_cap = 1` slot frees the moment `allocations.release`
commits. `teardown_handler` then commits `SystemState.TORN_DOWN` under the System advisory lock
(`src/kdive/jobs/handlers/systems.py:760-778`) **before** calling the provider at `:783`. Both
the allocation and the System are therefore terminal, and the Resource pristine to placement,
for the several minutes the bootloader write, the firmware power-cycle, and the full
precondition re-check take. Without step 0 the next tenant can be granted the machine inside
that window — early, they are denied at adopt against the previous tenant's still-running debug
kernel, which is precisely the misattribution the first rejected alternative below says this
design avoids; late, they adopt successfully and then meet the previous tenant's power-cycle.

**Nothing else in the release path changes.** `services/allocation/release.py` needs no edit,
and an implementer should not add one: the cordon is the whole mechanism, and holding an
allocation in `releasing` until a job completes would make every provider wait on something
only this one needs.

**A restore that fails keeps the cordon and replaces the reason.** If the bootloader write
fails, if the power-cycle does not return, if the host comes back on the wrong kernel, or if
any adopt precondition fails, step 4 does not run and the in-progress reason is replaced with
the specific defect. The failure surfaces as `RESTORE_INCOMPLETE` — an existing category whose
meaning is exactly this: a restore that did not complete, leaving indeterminate state, needing
an operator rather than a retry. No new `ErrorCategory` is invented. A worker that dies
mid-restore therefore leaves an **already-cordoned** host, which is the fail-closed outcome the
reconciler arm below would otherwise have to produce.

**Clearing requires that this teardown set the cordon, and re-checks at the moment it clears.**
Step 4 clears only when the reason is *still* this teardown's own — re-read immediately before
the write, not carried from step 0 — **and** step 0 recorded that it flipped the boolean. Both
halves are needed because the guard has two directions and step 0 alone covers one. Step 0's
read-before-write catches an operator cordon taken *before* teardown ran. The re-read at step 4
catches one taken *during* it, which is the longer exposure: the window spans a bootloader
write, a firmware power-cycle, and a full precondition re-check on a host that has just been
force-crashed, which is exactly when an operator is most likely to pull it from rotation. For
the re-read to see anything, `resources.set_scheduling` must stamp an operator-origin reason on
the cordon path — today `_apply_cordon`
(`src/kdive/mcp/tools/ops/resources/host_ops.py:141-149`) writes the boolean unconditionally and
records nothing — so that write lands in the same already-declared touch-point as the surfacing
work. The weaker rule — clear when the reason matches — protects nothing, because step 0 would
have written that reason itself moments earlier: an operator who cordoned the host for
maintenance while a Run was live would have their cordon silently lifted by the ordinary release
that follows. No existing producer persists a reason at all (`_apply_cordon`,
`src/kdive/mcp/tools/ops/resources/host_ops.py:141-149`, writes only the boolean, and it is what
both `resources.set_scheduling` and `resources.drain` call), so a pre-existing cordon is
reason-less and indistinguishable from no cordon unless step 0 reads before it writes. That
read-before-write is the load-bearing half; the reason key alone is not.

**The reason is persisted on the Resource, under a namespaced `capabilities` key.** A cordon
whose cause requires cross-referencing an audit log is a cordon an operator will clear without
understanding. The key is written by teardown, surfaced on `resources.describe`, and cleared
when an operator uncordons through `resources.set_scheduling`. Reconcile merges rather than
replaces, so the key survives a reconcile pass. This is a jsonb key rather than a column
because R9 budgets one migration for the whole epic and a cordon reason does not need the
second one — the same reasoning by which a remote-libvirt resource's connect URI and cert ref
are capability keys rather than columns.

**Cordon, not delete.** The host is still declared in `systems.toml`, so deleting the row
would fight `reconcile_resources`, which would recreate it schedulable on the next pass — the
one outcome this decision exists to prevent. Cordoning leaves the declaration intact and the
row unschedulable until a human looks.

**Teardown is idempotent and re-runnable.** Each step is keyed so a re-run resumes rather than
repeats, following the existing per-System idempotency rule.

**The drift signal is the Resource, not the System — because the System has already finished.**
There is no teardown-in-progress `SystemState`: the members are `provisioning`, `ready`,
`reprovisioning`, `restoring`, `paused`, `crashing`, `crashed`, `torn_down`, `failed`
(`src/kdive/domain/capacity/state.py:81-89`), and `teardown_handler` commits `TORN_DOWN` at
`src/kdive/jobs/handlers/systems.py:770` before it calls the provider at `:783` — the same
ordering step 0 above depends on. A worker that dies mid-restore therefore leaves a System in
`torn_down`, which is terminal: `_TRANSITIONS` maps `SystemState.TORN_DOWN` to `frozenset()`
(`state.py:249`), so there is no legal edge out of it and no state for the reconciler to key on.

**The reconciler's BYO arm therefore keys on the cordon reason.** The durable marker of a
restore in flight is the one step 0 writes: a Resource cordoned with a restore-in-progress
reason. The arm added to ADR-0021's loop fails closed over exactly that — a Resource carrying
that reason with no live worker attributable to it has its reason replaced with
`RESTORE_INCOMPLETE` and **stays cordoned**. The System is left in `torn_down` and is not driven
to `failed`; that transition does not exist, and adding it would be a `state.py` widen in gated
core that this milestone has not declared and does not need. It does not retry the restore
either: a restore is a write to a machine whose state is unknown, and an unattended retry
against an unknown state is how a host in a bad state becomes a host in a worse one.

**The power-cycle and re-verify windows carry the full limit contract** — unit, reference
clock, scope, consequence, and recovery action — per the AGENTS.md rule. Firmware POST on real
metal is a materially different wait from a VM boot, and an agent handed a bare number will
treat it as a wall to route around.

## Consequences

The invariant this buys is stated in one sentence and is the reason for every part above: a
crashed host never silently becomes the next allocation's starting point. Every path from a
Run that crashed the machine either ends in a host verified back on its baseline, or in a host
no scheduler will pick.

A lab will accumulate cordoned hosts — the ones whose reason records a failure rather than a
restore in progress. That is the design working: each one is a machine that genuinely needs a
human, and the alternative is the same machines silently in rotation. The
cost is an operator workflow: read the reason on `resources.describe`, fix the host, uncordon.
`doctor` (#1824) is the tool that tells them whether the fix took, and it runs the same
predicate teardown ran.

Refusing to auto-retry the restore means a transient failure — a service processor that was
briefly unreachable — costs an operator round trip that a retry would have absorbed. Accepted:
a retry loop cannot distinguish "briefly unreachable" from "wedged in a state a second write
will worsen", and the population where it matters is small compared with the population where
a wrong retry is expensive.

Persisting the reason in `capabilities` puts an operational outcome in a structure otherwise
holding host facts. The tension is real and the alternative was worse — an audit-log-only
reason is one an operator will not read before clearing the cordon. The clearing obligation is
explicit: uncordoning removes the key, so a stale reason cannot linger on a schedulable host.

**The cordon is not the provider's write, and cannot be.** `Provisioner.teardown(domain_name)`
(`src/kdive/providers/ports/lifecycle.py:130-138`) takes a domain name, receives no database
connection, and documents only `INFRASTRUCTURE_FAILURE` / `TRANSPORT_FAILURE`. Cordoning a
Resource, persisting the reason, and raising `RESTORE_INCOMPLETE` therefore happen in the
caller, `teardown_handler` (`src/kdive/jobs/handlers/systems.py:751`; the provider call is at
`:783`) — which is inside the portability gate's core prefixes and is not among that package's
allowlisted files. `teardown` is a member of `Provisioner`, not a port of its own — the
Protocol classes in that module are `Provisioner`, `Installer`, `Booter`, `Connector`,
`Controller`, and `Snapshotter`. This decision consequently costs the milestone a declared
core touch-point
rather than being pure provider work, and it is recorded in the milestone design doc's gate
table for that reason. Widening the port to take a connection would spread the same coupling to
every provider that has no use for it; keeping the write in the one handler that already owns
the transaction is the smaller change, at the price of a named, reviewed allowlist entry.

Reusing `RESTORE_INCOMPLETE` means one category now covers two subjects — a snapshot revert and
a host restore. Both are "a restore did not finish, the state is indeterminate, a retry is not
the answer", which is what the category names. Its ADR-0513 prose describes the snapshot case
concretely and is amended rather than rewritten when this lands.

Teardown becomes the slowest operation on the provider: a bootloader write, a firmware
power-cycle, and a full precondition re-check. That is time a tenant is not using the host, and
the step-0 cordon is what makes it also time the next tenant cannot have it — the allocation's
capacity slot freed minutes earlier. It is also the only point at which the host's state is
checked against what the operator declared, so shortening it means shipping a host whose state
nobody verified.

The visible cost is that a BYO host reads `cordoned` during every ordinary release, not only
after a failure. An operator watching `resources.list` sees hosts move in and out of the state,
and the reason key is what distinguishes a routine restore from a defect — which is another
reason it is persisted on the row rather than only in an audit log.

## Considered & rejected

- **Free the host optimistically and let `doctor` or the next adopt catch a bad state.**
  Adopt does re-check preconditions, so a badly-restored host would fail the *next* tenant's
  provision rather than corrupting their run. But it fails them at allocation time, after they
  waited for a host, and it attributes the failure to their request rather than to the previous
  release. Verifying at release attributes it correctly and keeps the host out of the pool.
- **Best-effort restore with a warning on the Resource, still schedulable.** A warning nobody
  is required to read is not a control. Placement would keep selecting the host, and the
  warning would be visible only to someone already investigating.
- **Delete the Resource row on teardown failure.** It removes the host from placement
  immediately, and `reconcile_resources` recreates it from the still-present `systems.toml`
  declaration on the next pass — schedulable, with the failure forgotten. Deleting a declared
  row fights the reconciler by construction.
- **Add a `cordon_reason` column.** Cleaner typing than a jsonb key, and it costs the epic's
  second migration for a string that has one writer and one reader. ADR-0517 makes migration
  numbers strictly ascending across merges, so a second migration is also a second
  serialization point in the epic's merge order.
- **Invent a `teardown_failure` `ErrorCategory`.** The taxonomy rule is to pick the most
  specific existing value and never invent strings. `RESTORE_INCOMPLETE` already means a
  restore that did not complete over indeterminate state; a second string for the same meaning
  would split the operator's mental model across two names.
- **Have the reconciler retry the restore before cordoning.** Attractive for transient service
  processor failures and wrong for the case that matters: the reconciler cannot see why the
  first attempt failed, so it would write to a machine in an unknown state without a human
  having looked at it. Cordoning preserves the evidence.
- **Restore by re-imaging the host from an operator-supplied image.** It would guarantee a
  known state rather than verifying one. It is also exactly the OS-install capability the epic
  declares a non-goal, and it would destroy whatever else the operator keeps on the machine.
- **Skip the re-verify step and trust a returning power-cycle.** A host that answers SSH after
  a reboot has proven that it booted, not that it booted the baseline kernel. The two differ
  precisely when the bootloader write silently failed, which is the failure the step exists to
  catch.

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
  `resources.set_scheduling` is the operator's path back. It carries no persisted reason: today
  its only producers are the inventory prune path
  (`src/kdive/inventory/reconcile/prune.py:52`, for a declared-removed host still in use) and
  `resources.drain`, whose audited `reason` goes to the audit log rather than to the row.
- `ErrorCategory.RESTORE_INCOMPLETE` ([ADR-0513](0513-restore-incomplete-failure-category.md))
  already names a restore that can never complete, whose subject's state is indeterminate, and
  whose operator response differs from `infrastructure_failure` — which is a retryable,
  unclassified fault in the layer below. It was added for a snapshot revert whose worker died
  mid-flight. The shape matches.
- Reconcile merges into the `capabilities` jsonb rather than replacing it
  (`src/kdive/inventory/reconcile/resources.py:292`, `:493`), so a key written by another
  writer survives a reconcile pass.

## Decision

**Teardown restores, verifies, and only then frees.** In order:

1. Re-point the host's bootloader default at the declared `baseline_kernel`, arch-keyed
   (`grubby` on x86, grub2-PReP or petitboot on a PowerVM LPAR).
2. Power-cycle through the out-of-band driver
   ([ADR-0539](0539-out-of-band-control-port.md)), not over SSH. The host may be wedged, and
   the in-band path is the one a kernel debugger destroys.
3. Re-run the adopt preconditions — the same module `provision` and `doctor` call
   (ADR-0540) — against the rebooted host, including that the running kernel is now the
   declared baseline.
4. Free the Resource.

Step 3 is the load-bearing one. A power-cycle that returns is not evidence that the host is
back in the state adopt requires; only the adopt predicate is evidence of that, and running
the same predicate is what makes "returned" mean the same thing as "adoptable". This is the
precondition module's third caller, and the reason it is one module.

**Any failure cordons the host with a reason; it never frees it.** If the bootloader write
fails, if the power-cycle does not return, if the host comes back on the wrong kernel, or if
any adopt precondition fails, the Resource is cordoned and the reason is recorded. The failure
surfaces as `RESTORE_INCOMPLETE` — an existing category whose meaning is exactly this: a
restore that did not complete, leaving indeterminate state, needing an operator rather than a
retry. No new `ErrorCategory` is invented.

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
repeats, following the existing per-System idempotency rule. A worker that dies mid-teardown
leaves a System in a teardown state with no live job, which is a drift the reconciler already
has a shape for.

**The reconciler drives a mid-teardown host to cordoned, never to available.** The BYO drift
arm added to ADR-0021's loop fails closed: a host whose teardown cannot be attributed to a
live worker is cordoned with `RESTORE_INCOMPLETE` and the System is failed. It does not retry
the restore. A restore is a write to a machine whose state is unknown, and an unattended retry
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

A lab will accumulate cordoned hosts. That is the design working — each one is a machine that
genuinely needs a human, and the alternative is the same machines silently in rotation. The
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

**The cordon is not the provider's write, and cannot be.** `Teardown.teardown(domain_name)`
(`src/kdive/providers/ports/lifecycle.py:130-138`) takes a domain name, receives no database
connection, and documents only `INFRASTRUCTURE_FAILURE` / `TRANSPORT_FAILURE`. Cordoning a
Resource, persisting the reason, and raising `RESTORE_INCOMPLETE` therefore happen in the
caller, `teardown_handler` (`src/kdive/jobs/handlers/systems.py:751`; the provider call is at
`:783`) — which is inside the portability gate's core prefixes and is not among that package's
allowlisted files. This decision consequently costs the milestone a declared core touch-point
rather than being pure provider work, and it is recorded in the milestone design doc's gate
table for that reason. Widening the port to take a connection would spread the same coupling to
every provider that has no use for it; keeping the write in the one handler that already owns
the transaction is the smaller change, at the price of a named, reviewed allowlist entry.

Reusing `RESTORE_INCOMPLETE` means one category now covers two subjects — a snapshot revert and
a host restore. Both are "a restore did not finish, the state is indeterminate, a retry is not
the answer", which is what the category names. Its ADR-0513 prose describes the snapshot case
concretely and is amended rather than rewritten when this lands.

Teardown becomes the slowest operation on the provider: a bootloader write, a firmware
power-cycle, and a full precondition re-check. That is time a tenant is not using the host and
the next one cannot have it. It is also the only point at which the host's state is checked
against what the operator declared, so shortening it means shipping a host whose state nobody
verified.

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

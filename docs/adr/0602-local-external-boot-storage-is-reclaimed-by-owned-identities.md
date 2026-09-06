# 0602: Local external-boot storage is reclaimed by owned identities

## Status

Accepted (2026-09-06)

## Context

Local external boot stores boot payloads and target projections below an owner-only per-worker
root, and stores recovery evidence beside them. ADR-0591 deliberately retained a per-Run artifact
directory while routing its cross-activation deletion hazard to #2212. Cleanup currently removes
only the three payload files and the recovery archive. Digest, Run, and System directories accumulate, interrupted
preparation can leave a partial recovery directory, and host-only cleanup is incorrectly blocked
after power has been restored. The same root is provisioned without a capacity admission contract.

Deletion crosses a trust boundary: filesystem entries can outlive a process and may have been
replaced. Recursive path deletion would allow an ambiguous or substituted entry to widen the
deletion set.

## Decision

This record supersedes ADR-0591's per-Run artifact-layout decision. Before the dormant local port is
bound, artifact references move to `local-artifact-v2` and insert the canonical activation id:
`<system>/<run>/<activation>/<digest>/<filename>`. The session artifact descriptor is the
activation directory. Payload names and every projection digest directory are therefore exclusive
to one activation even when two activations share a Run and projection digest.

Terminal cleanup receives the authenticated recovery metadata, not only its binding. A local
reclaimer derives the one projection digest and activation names from that metadata, opens every
directory descriptor-relative with no-follow and owner-only checks, and removes only an explicit
bounded set of files. It removes the exact digest directory, then prunes the activation, Run, and System
parents only when empty. Absence is retry success; an unexpected entry, shape, mode, owner, or
record is reported and left untouched. No recursive deletion is used.

The local authority adapter has a separate teardown path for an activation whose preparation has
not reached complete metadata. It authenticates the request against the canonical durable
preparation receipt or pre-stop intent, then asks the provider-local operation to abort preparation.
A receipt-only partial is removed directly because no guest operation has begun. A pre-stop partial
first verifies that the domain still has its recorded source definition and has not reached a
target/mutated state; it restores recorded prior power, then removes only its known files and
directory. After it removes the partial, or when a retry proves it already absent, the adapter
records a bounded pending-absence handoff containing the exact admitted request. The same adapter
accepts that handoff only for a byte-for-byte equal request and returns the stable terminal
`absent` observation without entering recovery-point-dependent categorization.

Before removing the last partial ownership record, cleanup publishes and fsyncs a bounded,
canonical abort receipt outside the partial directory. It binds the activation, plan, and
authority, survives interruption between the final unlink and directory removal, and is removed
last. A fresh process validates that receipt before continuing exact-owner cleanup; malformed or
foreign receipts fail closed. Both cached handoffs and recovery observations recheck all durable
absence evidence, including this receipt and activation artifacts, before reporting terminal absence.

The shared authority adapter contract has a separate typed recovery-observation call. The service
invokes it only while recovering an exact authenticated `mutation-started` or `provider-returned`
TEARDOWN journal record, passing a closed context containing that phase and the record's identity.
Ordinary `observe` remains read-only. When adapter restart or oldest-entry eviction loses the
in-memory handoff, local recovery observation performs only a non-mutating, exact-request check
that complete recovery metadata, a cleanup tombstone, the canonical partial, and activation-owned
cleanup artifacts are all absent; it never repeats deletion. Complete metadata and tombstones take
precedence over partial absence. Present owned residue falls back to the authenticated
record-specific cleanup path, while malformed, foreign, or unreadable state fails closed. Absence
without either the equal in-memory handoff or service-supplied recovery
context does not authorize a terminal result. A normal prepare retry instead resumes the same matching partial. Any malformed,
foreign, symlinked, wide-mode, non-directory, or ambiguous partial is retained and reported.

Host-artifact cleanup is separated from guest mutation. Opening or changing the overlay, module
tree, or guest remains behind `require_inactive`; removal of already-materialized host files does
not inspect guest power.

Provisioning passes the resolved per-slot recovery root and one positive per-activation capacity
ceiling through the fixed-worker allowlist. Runtime computes a reservation before materialization
from the plan's bounded kernel/initrd/module declarations plus fixed projection, metadata, recovery
archive, and in-flight partial overhead, and refuses a plan whose reservation exceeds that ceiling.
Ansible derives minimum free bytes from the same provisioned ceiling multiplied by admitted
concurrency; runtime has no independent default to drift from it. The reference state is
free bytes observed after the per-slot roots exist and before worker release. Insufficient space
fails provisioning, so external boot is not advertised; the recovery action is to increase the
filesystem or lower admitted concurrency and rerun provisioning.

The shipped ceiling is 32 GiB per activation. It admits the simultaneous maximum represented by
the current plan and archive bounds, including atomic temporary copies; provisioning multiplies
that ceiling by admitted concurrency rather than assuming typical payload sizes.

## Consequences

Cleanup is idempotent across interruption at each unlink and rmdir, while sibling Runs and
activations remain outside its derived names. A non-empty parent remains available for its
siblings. Ambiguity consumes operator attention and disk rather than risking unrelated data.

The artifact reference version changes before a production writer exists. Existing test fixtures
using v1 are replaced; no compatibility reader or data migration is needed because the local port
remains unbound until #2246.

The cleanup callback and session method now carry recovery metadata. Deployment defaults must
track the source bounds deliberately; raising a payload or archive bound changes runtime's computed
reservation but not the operator-selected ceiling. The capacity gate reserves for a worst-case admitted envelope and may require
operators to allocate substantially more storage than typical activations consume.

## Considered & rejected

- **Recursively delete the activation's Run directory.** judgment: it makes sibling or foreign
  entries part of the deletion set and cannot prove exact ownership at each descent.
- **Delete every partial matching the activation-shaped filename.** judgment: a filename is not
  durable ownership evidence; retaining an ambiguous entry is the safe failure mode.
- **Retain the per-Run artifact layout with reference counting.** judgment: it adds a second
  durable ownership ledger for objects that are not intentionally shared; activation-exclusive
  placement makes deletion authority structural and removes the counter and its crash recovery.
- **Keep the inactive-domain gate around all cleanup.** verified: issue #2245 traces
  `cleanup_payloads` to host-only unlink operations and shows that restored-running cleanup cannot
  reach tombstone finalization under that gate.
- **Treat capacity as operator guidance only.** judgment: documentation does not prevent a clean
  host from advertising a port whose first bounded worst-case write can fail with `ENOSPC`.
- **Duplicate fixed byte totals in Python and YAML.** judgment: two literals can pass their local
  tests while disagreeing; a single provisioned ceiling consumed by both runtime and Ansible makes
  divergence observable at startup.

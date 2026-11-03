# Remote System teardown collects every activation of the System (#3486)

Decision record: [ADR-0787](../../adr/0787-abortable-finalize-proof-checkpoint.md), amended
2026-11-02 by this change. Follows #3472 (PR #3485). Bounded scans stay with #3475.

## Problem

`RemoteModuleVolumePreparationStore.collect_torn_down_activation` (in
`src/kdive/providers/remote_libvirt/external_boot_authority.py`) removes the files of the one
`(binding, plan_identity)` the per-System `.teardown` record names. A System can have several
activations (a Run is re-activated, a release is adopted, a preparation never finalizes). Their
`.lifecycle-request`, `.request`, `.preparation`, `.terminal`, recovery and marker files stay
for the life of the host, and #3475's bounded scans count them. Stores torn down before #3472
keep everything.

The `.request`, `.preparation`, `.terminal` and `.recovery` files of an activation are the only
record of the pool volumes it created. Teardown proves absent only the named activation's
volumes (`_protected_teardown_volumes` in
`src/kdive/providers/remote_libvirt/external_boot_operations.py`). Deleting a sibling's records
while its volumes remain would leave volumes nothing names.

## Decision

On a `complete` teardown record, collection covers every activation of the System, found by
`system_id` in file content. The named activation is collected as today. Every other activation
(a **sibling**) is collected only when it is finalized (its `cleanup-finalized` marker exists) or
when every pool volume its records name is read absent from the pool. Otherwise its files are
kept and a warning names it. The finalize/teardown predicates become System-wide.

## Design

### System-wide refusal

`torn_down(binding, plan_identity)` becomes `system_torn_down(system_id) -> bool`: the System's
`.teardown` record exists and is in phase `complete`. `cleanup_finalized(binding, plan)` is the
marker **or** `system_torn_down(binding.system_id)`; `refuse_finalized`, `stage`,
`stage_lifecycle`, `reopen_terminal`, `reopen_restored` and the coordinator's
`execute_preparation` inherit it. `finalize_cleanup_quarantine` writes nothing once
`system_torn_down`. `RemoteExternalBootCoordinator.system_teardown_state` returns the empty state
once `system_torn_down`.

A `system_id` is never re-activated after teardown: `systems.id` is
`uuid PRIMARY KEY DEFAULT gen_random_uuid()` (`src/kdive/db/schema/0001_init.sql`), and
`SystemState.TORN_DOWN` and `ExternalBootActivationState.TORN_DOWN` have no outgoing edges
(`src/kdive/domain/capacity/state.py`). So refusing every activation of a torn-down System
refuses nothing legitimate.

Two readers change meaning, both safely:

- `RemoteExternalBootAuthorityAdapter._observe` reads `absent` for a CLEANUP of any activation of
  a torn-down System whose recovery record is gone. A CLEANUP commit names its recovery objects
  (`_matched_point`), so its record existed at commit; after teardown only collection removes it,
  and collection keeps an unproven sibling's record. A kept sibling still observes its live state.
- `system_teardown_state` is only called with the record's own binding: `begin_system_teardown`
  and `reopen_system_teardown` refuse a different binding for the same System. The widening there
  changes no reachable result.

### Discovery

New store method `system_activations(system_id) -> list[tuple[ExternalBootActivationBinding,
str]]` lists every distinct `(binding, plan_identity)` named by the content of a 64-hex-named
file with one of these suffixes (the **anchors**):

| suffix | binding and plan read from |
|---|---|
| `.lifecycle-request` | `_PersistedRemoteModuleLifecycleV1.request.authority` |
| `.request` | `_PersistedRemoteModulePreparationV1.request.authority` |
| `.preparation` | `ExternalBootPreparationObservation.binding`, `.plan_identity` |
| `.terminal` | `RemoteModuleTerminalRecord.request.authority` |
| `.recovery` | `RemoteExternalBootRecoveryRecord.binding`, `.plan_identity` |
| `.cleanup-quarantine` | `RemoteCleanupQuarantineV1.binding`, `.plan_identity` |
| `.cleanup-finalized` | the marker's receipt (`_parse_finalized_marker`) |

Only entries whose `binding.system_id` equals the argument are returned, sorted for a
deterministic order. A file that does not parse is left in place with the existing
`_owns` warning: it cannot be proven this System's. The scan is unbounded like `_scan_owned`
(#3419 accepted limitation; #3475 owns bounding).

### Claims

New store method `activation_claims(binding, plan_identity) -> RemoteFinalizedTeardownRefsV1 |
None`. `None` means finalized (the marker exists): no proof is needed. Otherwise it returns every
pool volume the activation's records name, in the existing refs shape:

- `_teardown_refs(binding, plan_identity)`: the terminal record's module source and scratch
  volumes, the one operation nonce of its `.request` files (strict scan; conflicting operations
  refuse), and the materialization record's kernel and initrd;
- plus the recovery record's `module_recovery` pool/source/scratch volumes and its
  materialization's kernel and initrd (`teardown_recovery`);
- plus the kernel and initrd of every owned `.preparation` receipt that carries a
  materialization (strict scan).

Module volumes and boot artifacts are deduplicated and sorted. Any parse or binding failure
raises; the coordinator treats a raise as "not proven" and keeps the activation.

### Proof

New operations method `activation_volumes_absent(binding, refs) -> bool` on
`ConcreteRemoteExternalBootOperations`, added to the `RemoteSystemTeardownOperations` protocol.
Under one connection it reads absent, with `_teardown_volume_absent`: each `refs.module_volumes`
pair; the source and scratch names `_module_volume_names(refs.operation_nonce, binding.system_id,
binding.run_id)` render when a nonce is present; and each boot artifact ref in the configured
pool. It deletes nothing and returns false when any volume is present. A libvirt error
propagates.

### Collection

`RemoteExternalBootCoordinator.collect_torn_down_activation(record)` keeps its name, its
`complete` guard and its log-not-raise contract, and now:

1. For each sibling from `system_activations(record.intent.binding.system_id)` other than the
   named pair: `refs = activation_claims(...)`; proven when `refs is None` or
   `activation_volumes_absent(binding, refs)`. A raise or a present volume marks it kept and logs
   a warning naming the System, Run and activation ids.
2. The plan identities of kept siblings are the **kept plans**.
3. It calls the store's `collect_torn_down_activation(binding, plan, keep_plan=...)` for the
   named pair, then `collect_proven_activation(binding, plan, claims, keep_plan=...)` for each
   proven sibling, each in its own `try`, logging a failure and continuing. `keep_plan` is true
   when the plan is a kept plan.

Both store methods run under `_exclusive()`. `collect_proven_activation` first recomputes
`activation_claims` and returns `False` without unlinking unless the current claims are within
the proven ones: no module volume, boot artifact or nonce the proof did not cover, and a
finalized proof still finalized. The proof was read outside the lock, so this fails closed if the
records grew; records only shrink while collection runs, so a subset is still proven. Both then
unlink, suppressing `FileNotFoundError`, then fsync, in this order:

1. stem dependents of owned `.lifecycle-request` and `.request` files: `.lifecycle-completion`,
   `.result`, `.completion`;
2. the terminal-key files `.restored`, `.prior-power-owner`, `.prior-power`;
3. the plan's `.materialization` then `.materialization-index`, unless `keep_plan` (a kept
   sibling of the same Run shares the plan's materialization);
4. per recovery identity named by the recovery index or the marker: the `.object-index` files
   naming it;
5. the anchors `.lifecycle-request`, `.request`, `.preparation`, `.terminal`;
6. per recovery identity, unless its receipt is `managed`: `.recovery`, `.cleanup-quarantine`;
7. the `.recovery-index`, unless a `managed` receipt was found;
8. the `cleanup-finalized` marker.

Every file that is not an anchor goes before the anchors, so a crash at any unlink leaves the
activation discoverable by a later collection. Finalize keeps using `_activation_files`, whose
order changes to steps 1, 2, 3, 5; finalize writes its marker first, so its order does not matter.

The proof is taken outside the lock because it is libvirt I/O. Volumes of a torn-down System do
not reappear: every stage of it is refused System-wide before collection starts.

### Pre-#3472 stores

A manual remedy, not a sweep, in `docs/operating/runbooks/remote-libvirt-host-setup.md`: stop
`kdive-external-boot-authority.service` with no System of that host active, confirm no
`kdive-module-*` or `kdive-boot-v1-*` volume of a torn-down System remains in the pool, then
remove the store files other than each `.teardown` record and each `managed` receipt with its
`.recovery` and the `.recovery-index` naming it.

## Failure model

1. **Actors and deployments**
   - the provider-authority service on a remote-libvirt host, running System teardown execute and
     observe calls through the System lane (ADR-0584);
   - an operator adopting a cleanup receipt (`ops.resolve_recovery_orphan`) concurrently;
   - an operator following the runbook remedy on a drained host.
2. **Invariants and assets at stake**
   - a pool volume is never left with no store record naming it because collection deleted the
     record;
   - an adopted receipt, its `.recovery` and its `.recovery-index` survive teardown;
   - a late stage of any activation of a torn-down System is refused;
   - a crash at any unlink leaves every remaining file reachable by a later collection.
3. **Accepted failure classes**
   - a sibling whose volumes stay present, or whose records do not parse, keeps its files for the
     life of the host: bounded to that sibling, logged, and the operator remedy names it;
   - a collection failure is logged and retried only by the service's next teardown observation,
     as #3472 accepted;
   - a crash mid-collection may shrink a sibling's claims on retry: the first pass proved the
     superset absent before any unlink;
   - a second authority instance writing the same store directory breaks the System-lane
     assumption, as `refuse_finalized` already documents.
4. **Covered elsewhere**
   - bounded scans: #3475;
   - a `managed` re-check in remote finalize, an orphan `.recovery` without its index, markers and
     `.teardown` records accumulating, the local-libvirt store, a GC scheduler, and removing
     sibling volumes: operator exclusions in the #3486 scope record.

## Testing

All in `tests/providers/remote_libvirt/test_external_boot_authority.py` unless named:

- a System with a named activation plus siblings that are finalized, never-finalized with volumes
  absent, never-finalized with a volume present, and adopted ends with exactly the `.teardown`
  record, the kept sibling's files, and the adopted trio;
- a late `stage` of a collected sibling raises `RemoteCleanupFinalizedError`;
- a crash at every unlink, then an observation, ends in the same file set;
- a sibling whose claims grew between proof and lock is not collected;
- a kept sibling sharing the named plan keeps the plan's materialization pair;
- `activation_volumes_absent` reads module, nonce and boot names and returns false when one is
  present.

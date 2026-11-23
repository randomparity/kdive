# Remote authority storage ownership

## Problem

The remote external-boot authority module combines exchanged values, durable
filesystem evidence, coordinator decisions and provider execution. After common
contracts move, its descriptor-confined store still obscures those separate owners.

## Scope

Move `RemoteModuleVolumePreparationStore` from `remote_libvirt/external_boot_authority.py`
to `remote_libvirt/external_boot_store.py`, naming it `RemoteExternalBootStore`
because it already owns both kernel and module evidence. Move its private persisted
preparation/lifecycle/completion/terminal models, cleanup/quarantine/finalized records,
System teardown fields/anchor/intent/record, recovery/materialization records,
admitted-operation views, power/phase aliases, bounds and filesystem helper closure.
Shared wire values remain in the common authority boundary. Move quarantine receipt
observation conversion with its record and expose that helper for the adapter.
Keep coordinator, operation protocols, executor host, provider adapter and live
observation/state views in the current orchestration module. Neither the new store
nor its private models may import that orchestration module in reverse.
Migrate direct source/test consumers and the schema inventory to the owning module;
remove old definitions and internal class references without compatibility wrappers.
This is an ownership change under the existing architecture, with no new durable
format, locking, descriptor, cleanup-order, queue or provider algorithm.

### Failure model

- Prevent: store-to-orchestration dependency and misplaced private durable records.
- Detect: missed caller/schema imports, changed bytes or definition bodies, and
  regressions in descriptor confinement, restart/replay and cleanup refusal.
- Recover: revert the local ownership commit; on-disk records need no migration.
- Accepted: coordinator/executor workflow complexity is outside this storage unit;
  shared exchanged models and their validation remain common-owned.

## Success

1. The store and named durable closure have one owner independent of orchestration.
2. Provider assembly and tests import the new owner directly; obsolete paths vanish.
3. Stored schemas/bytes, locking, refusal and cleanup behavior remain unchanged.

## Validation

- `Mode: focused-test`: a structural import-boundary test rejects a reverse import
  from the store to orchestration; demonstrate red, green and a controlled fault.
- `Mode: task-test-not-applicable`: relocating preserved definitions and renaming
  the internal class adds no runtime algorithm. Compare normalized moved/retained
  ASTs, JSON schemas and canonical record bytes, keeping existing schema pins.
- `Mode: focused-test`: existing remote authority, module host/transport, cleanup,
  journal and preparation tests exercise restart, confinement, refusal and assembly.
  Run those paths with `just test-verbose`, then `just lint`, whole-tree `just type`,
  documentation checks and configured hooks before committing locally.

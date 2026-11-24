# Absent registration for inventory-owned providers

## Problem

Remote-libvirt, fault-inject and BYO-host assemble discovery metadata whose `creates=False`
registrar returns before its target or database operation. Remote's unreachable target throws;
fault-inject constructs a synthetic source; BYO supplies a refusing operation. Inventory already
owns their resource rows under [ADR-0112](../../adr/0112-systems-inventory-config.md).
`ProviderRuntime.discovery_registrar` already supports absence, so these adapters add no work.

## Scope

Make `_RuntimeDescriptor.discovery_registration_factory` optional with default `None`. Build its
runtime once, return it when the factory `is None`, otherwise attach the existing real registration.
Keep the local-libvirt factory and registration path unchanged. Omit the other three descriptor
factories. Keep exported `build_local_runtime`, `build_remote_runtime` and
`build_fault_inject_runtime` names and parameter/default/resolved-hint contracts; remote/fault
helpers return their provider runtime without a no-op wrapper. Removing those helpers is a
separate queued task.

Remove the three unused inventory-only registration factories, remote/fault targets and their
unused metadata constants/imports. Migrate every direct test caller. Keep concrete discovery
classes and all other provider build functions/ports unchanged. Keep generic
`ProviderDiscoveryRegistration`, its `creates=False` guard and local registration helpers intact.
No registry, migration, new module or compatibility shim is needed.

### Failure model

- Actors: provider assembly constructs runtimes; reconcilers call discovery registration;
  inventory reconciliation creates/adopts declared resource rows. Assets: one creator per kind,
  stable resource identities, local discovery, shared registry/store/sender identity and capability
  wiring, BYO refusal and provider-enable behavior.
- Preserve no target/DB discovery operation for inventory-owned runtimes. Preserve real local
  registration and generic explicit `creates=False` metadata safety. Core resolver isolation,
  inventory/adoption algorithms, schema, public MCP/wire/error contracts and all other ports stay
  unchanged. No local/remote libvirt daemon, guest or operator service is exercised by tests.
- Accepted: the internal descriptor constructor gains an optional field; obsolete internal metadata
  factories disappear after in-tree tests migrate. No compatibility alias is required. Local
  registration retains its existing synchronous discovery behavior and availability limits.
- Other owners: provider-runtime builder consolidation, artifact staging and journal projection
  remain their existing queued work. No new deferral or broader resource-ownership promise.

## Success

1. Inventory-owned production and direct runtimes have no discovery registrar and register without
   looking up a target or invoking database discovery registration.
2. Local discovery still registers and refreshes one stable row. The generic false guard still
   returns before target/database access; no unrelated assertion or capability contract is lost.
3. Provider enables, factory dependency identities, BYO refusal and existing inventory/adoption
   behavior remain intact. All three exported assembly builders remain available.

## Validation

Add focused None/default/production-path tests before source edits, and update tests protecting
obsolete no-op wiring without weakening unrelated assertions. Execute an actual local registration
through the resolver with a fake host-discovery boundary and real disposable Postgres, repeating it
for idempotency; run existing real-PG inventory/adoption cases. A fake host prevents VM/libvirt I/O.
Use controlled reintroduced no-op, missing local attachment and missing generic false guard faults;
restore exact bytes. Preserve all unchanged producer/assembly functions, core/local/inventory code,
MCP/schema contracts and SQL by AST/byte checks. Verify actual built/installed runtime metadata,
module bytes/origins and focused cases. Run lint, whole-tree types, documentation checks and commit hooks and bounded fresh
code/security reviews before local commit and exact finding attribution.

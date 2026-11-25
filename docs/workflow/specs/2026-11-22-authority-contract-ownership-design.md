# Authority contract ownership

## Problem

Common authority service and transport import remote wire models from the
concrete coordinator. The coordinator imports the service exception in reverse.
The shared protocol boundary should own exchanged values without loading concrete
provider storage, libvirt executors or service orchestration.

## Scope

Move `AuthorityServiceError` and its closed `JournalConflictReason` vocabulary
unchanged from service to the existing authority protocol. Migrate direct callers.
Move preparation/begin/terminal/lifecycle/teardown wire request and response
models from the remote coordinator to `external_boot_authority/module_protocol.py`.
Keep durable completion, persisted request and terminal-record models remote-owned.
Move the pure module documents to `external_boot_authority/module_documents.py`;
move the pure volume-name grammar to `shared/module_volume_names.py`. Move the two
prepared-volume dataclasses beside the wire values, retaining conversion methods.
These moves close validation dependencies without importing provider I/O.
Carry canonical encoding, attempt-reference and bound/constants dependencies with
the same semantics; the generic authority encoder has different oversize errors.
Migrate direct imports in service, transport, provider assembly/execution, tests,
and provisioning scripts. Remove obsolete modules; retain no compatibility shims.
The coordinator and durable store remain remote-owned, for the next separate task.
No new wire fields, validation policies, byte encoders, scheduling or filesystem
algorithms are introduced. This implements the existing typed-port architecture.

### Failure model

- Prevent: common-to-concrete imports and the deferred reverse exception import.
- Detect: missed imports, changed validation/schema/canonical bytes, provisioning
  import drift, and accidental movement of private durable records.
- Recover: revert the local ownership commit; no stored data migration is needed.
- Accepted: concrete provider execution and durable-store concentration remain
  until their separately planned task; this change promises only contract ownership.

## Success

1. Service/transport import shared contract values without a remote implementation.
2. The remote implementation imports shared exceptions and exchanged values directly.
3. Existing public schemas, canonical bytes and refusal behavior remain identical.

## Validation

- `Mode: focused-test`: authority protocol, transport, service and remote module
  document/preparation/volume suites exercise the exchanged contracts and validation.
  Add an AST import-boundary check against concrete provider imports in the shared
  contract modules; prove it detects a controlled reverse import.
- `Mode: task-test-not-applicable`: relocation itself changes no runtime algorithm;
  compare normalized definition ASTs and pre/post model JSON schemas and canonical
  fixture bytes instead of writing tests that merely mirror moved declarations.
- Inspect the complete direct-import inventory, including dynamic imports and
  Ansible/module-install consumers. Run applicable provisioning tests, `just lint`,
  whole-tree `just type`, documentation checks and configured commit hooks.

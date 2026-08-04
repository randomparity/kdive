# Object-store assembly injection design

## Problem

Long-running processes assemble an object store as part of their composition root, but several
active runtime paths discard that dependency and reconstruct a client from environment variables
below assembly. Local-libvirt provisioning does this when fetching uploaded rootfs artifacts,
shared module-debuginfo resolution does it during debug setup, and report generation does it from
the MCP handler. The reconciler also constructs a bare client instead of using the process assembly
container.

Those bypasses hide ownership, allow one process to create clients with different configuration or
validation timing, and make exact dependency identity impossible to verify. They also conflict with
the existing pattern used by install, retrieve, introspection, worker handlers, and most MCP
registrars, where one process-owned store is passed through composition.

## Decision

Keep `ObjectStoreAssembly` as the process-level ownership boundary and pass its existing store
through every active service path covered by this change.

- Local provider composition passes the store to `LocalLibvirtProvisioning.from_env` and to the
  shared module-debuginfo resolver. The rootfs upload-fetch closure accepts the narrow
  `UploadObjectStore` protocol and keeps opening its database connection per invocation.
- Report registration receives `ObjectStoreAssembly` and supplies a required `StoreFactory` whose
  result is the assembly's store. Report handlers retain the factory seam so artifact-store
  failures continue to be categorized at request time.
- The reconciler runtime body constructs one `ObjectStoreAssembly` and shares `.store` with
  provider composition and reconciliation configuration.
- Reconciler readiness retains its fresh `object_store_from_env` factory so each probe checks
  current object-store connectivity independently.
- Teardown-only local-libvirt assembly may retain the existing unconfigured-store sentinel because
  it cannot execute artifact-fetching behavior.

No global cache or service locator is introduced. Environment-backed construction remains valid at
explicit process/store assembly roots, standalone CLI entry points, readiness probes whose contract
requires a fresh connectivity check, and request-time fallback paths explicitly retained by their
own contracts.

## Alternatives

### Inject concrete stores into report handlers

Passing a concrete store directly would be a smaller signature, but it would move construction and
failure timing away from the report operation and remove the existing test seam for degraded
artifact generation. A required factory preserves that behavior while making ownership explicit.

### Normalize every direct environment construction

Changing standalone commands, readiness probes, and intentionally lazy request handlers would make
the textual pattern uniform at the cost of changing established lifecycle and failure contracts.
Those paths are not assembly bypasses and remain outside this change.

### Cache a process-global store

A cached global would reduce construction without exposing dependencies in signatures. It would
retain hidden ownership, contaminate tests and environment changes, and make startup I/O timing
implicit, so it is rejected.

## Testing

Tests first establish the missing wiring by asserting exact object identity at each seam:

- local runtime composition forwards its store to provisioning and module-debuginfo resolution;
- provisioning forwards the same narrow store to the rootfs upload-fetch closure;
- the closure calls rootfs fetching with the injected store while retaining a per-call database
  connection;
- report registration supplies a factory returning the app assembly's store;
- report handlers require and use the injected factory while preserving artifact-outage behavior;
- reconciler provider composition and reconciliation configuration receive the same assembled
  store, while readiness creates fresh clients independently.

Focused tests cover each seam during red-green-refactor. The integrated change then runs the
repository lint, whole-tree type, and CI recipes.

## Scope

This change is dependency-wiring cleanup. It does not change object-store configuration, artifact
keys, authorization, report response envelopes, provider capability selection, readiness semantics,
or standalone command behavior. It adds no dependency and no compatibility shim.

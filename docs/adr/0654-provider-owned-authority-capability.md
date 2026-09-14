# 0654 — Provider-owned authority capability

## Status

Proposed (2026-09-14)

## Context

Issue #2473 identifies authority route selection in shared worker factories and
remote-libvirt implementations imported by shared module services. The existing
`ProviderRuntime.authority` port already supplies a Resource-bound authority sender,
but does not cover the worker operation client and module preparation/lifecycle uses.

## Decision

Extend the existing runtime authority surface so local and remote provider composition
own their fixed routes and optional module capability. Shared worker code validates
operation markers and borrows its active incarnation credential only while encoding
closed requests. Concrete transports and remote wire codecs belong to provider adapters.

Shared module services retain durable receipts, evidence, System verification and
recovery sequencing. Provider implementations own volume, appliance, attachment and
libvirt marker operations behind neutral ports and values. Remove the unused service
module-operation runtime and phase facade rather than creating adapters for test-only
orchestration. Retain the live authority-host configuration and recovery evidence tests.
Migrate shared routing-helper callers to fixed runtime metadata; server composition
does not require a worker sender or credential.

Preserve ADR-0606's lazy per-call TLS and Resource binding and ADR-0612's operation
identity and deadline checks. This refactor changes no wire format, persisted schema,
authorization policy, credential lifetime, or lifecycle/recovery semantics.

## Consequences

Provider authority routing has one composition owner. Services can exercise evidence
ordering through typed provider boundaries without importing remote-libvirt internals.
The migration spans both sender assembly and module services and must land together
with local/remote route and recovery regression coverage.

## Considered & rejected

- Add a separate authority registry. judgment: duplicates the established Resource-to-runtime
  selection instead of completing it.
- Move complete module services under remote-libvirt. judgment: relocates database evidence
  ownership along with provider I/O and makes the provider boundary less useful.
- Change only worker client routing. verified: at commit
  `847e5fc9436013eb674fa536b496208514170710`, the imports in
  `src/kdive/services/remote_module_operation.py` include appliance, volume and reaping
  implementations; client-only changes cannot satisfy issue #2473's service criterion.

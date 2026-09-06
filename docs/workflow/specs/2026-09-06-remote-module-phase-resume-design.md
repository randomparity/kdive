# Remote module phase resume design

Issue: #2173
Status: Accepted

## Scope

Remote-libvirt module capture/install, restore, reap, and inventory converge from durable
provider and PostgreSQL evidence after a worker process disappears. This design does not bind
the full external-boot operation, add transport surfaces, or advertise a provider capability.

## Runtime contract

The provider protocol is asynchronous. Every phase call receives the server-committed
`ModuleAttemptPreparationRequestV1`, the Resource-bound authority sender where mutation is
required, a completion-owned `RemoteModulePreparationExecutor`, and one caller-established
absolute monotonic deadline. The same deadline reaches preparation, appliance execution,
scratch-result reads, teardown, marker writes, and volume deletion. A call never starts a fresh
event loop, detaches provider work, or returns from cancellation while a provider binding call
remains live.

Worker-owned terminal and reap evidence crosses the database trust boundary only through the
ADR-0609 `SECURITY DEFINER` operation. The runtime supplies the active incarnation credential,
job identifier and attempt, and the server-committed closed receipt. PostgreSQL binds that tuple to
the exact running job payload under the System advisory lock. Workers retain read-only table
grants; missing context, stale credentials, cancelled jobs, and mismatched System, Run, or nonce
fail closed. Mutation-obligation opening and discharge remain server-owned.

The concrete deadline adapter executes inside the bounded completion-owned offload. It checks
the inherited deadline before starting and after the binding call completes. Expiry never
abandons a live call or creates an unbounded helper thread.

## Durable phase rules

The only resumable composites are:

| Operation | Durable phase | Next action |
| --- | --- | --- |
| `capture_install` | `captured`, `staging-intent`, `replacement-ready` | install |
| `capture_install` | `installed` | finish install |
| `restore` | `installed`, `restore-ready` | restore |
| `restore` | `restored` | finish restore |

All other composites are conflicts. Result bytes must decode as the bounded canonical document
and match the exact operation identity. An absent attempt, malformed result, complete result for
another attempt, and valid current result are separate observations.

Capture retains both deterministic source and scratch volumes through the handoff. They remain
covered by the open mutation obligation and charged to their persisted capacities; capture does
not discharge the obligation or delete source early. This avoids inventing recovery geometry if
the worker dies after returning installed evidence.

Restore validates the immutable capture operation and installed result identities before using
current scratch state. After a durable `restored` result and complete appliance teardown, it
stores exact terminal evidence and opens the reap obligation before publishing the whole-name
`reaping.journal` marker. Only then may source and scratch be deleted. The `reaped.journal`
marker and reap-obligation discharge follow successful deletion. A restart at `reaping` resumes
teardown and exact-name deletion; a restart at `reaped` performs no mutation.

## Ownership and inventory

Volume ownership comes only from the complete bounded volume name defined by ADR-0588. Metadata
namespaces are not deletion authority. Inventory returns only parsed owned keys. Keys retained by
an open durable obligation are `retained`; other valid owned keys are `drainable`. An unreadable
runtime makes inventory incomplete and rollback unsafe. Foreign, malformed, sibling-attempt, and
unknown entries are never deletion authorization.

## Recovery geometry

References use `RemoteModuleRecoveryRefV2`. They preserve the observed source capacity, opaque
pool/root/source/scratch keys, exact capture-operation and installed-result identities, appliance
and authority identities, and installed counts. Reopen derives expected volume identities from
that persisted geometry; it does not require original upload entries or an image writer and does
not substitute a default capacity.

## Verification

Unit phase cases cover every table row, invalid composites, exact retry, incomplete teardown,
foreign authority, and unreadable inventory. Runtime tests distinguish absent, malformed,
foreign-complete, and valid current scratch evidence. The integration matrix uses PostgreSQL
obligation rows, shared fake libvirt storage/appliance state, and a newly constructed concrete
runtime after each injected provider boundary interruption. Assertions are based on durable
rows, bytes, volume names, marker state, and mutation counts—not process-local completed-step
sets.

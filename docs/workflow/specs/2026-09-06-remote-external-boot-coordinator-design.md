# Remote external-boot coordinator contract

Issue: #2200
Status: Implemented coordinator contract; host integration remains in #2216

This slice defines the closed durable recovery record, preparation receipts, and six-operation
boundary consumed by the remote coordinator. Construction, host service wiring, job handling,
and provider advertisement remain owned by #2216.

The record binds one exact System, Run, and activation to the plan and materialization identities,
the validated source/target domain definition pair, the V2 module recovery geometry, both provider
state identities, prior power, and the complete sorted set of provider-owned recovery objects. It
contains no URI, credential, request-selected path, XML fragment, or command beyond the already
validated closed definition value. Rehydration recomputes ownership and identity relationships and
rejects unknown fields or records larger than 1 MiB.

`RemoteExternalBootOperations` exposes exactly materialize, prepare, activate, observe, recover,
and cleanup. Every method receives one caller-established absolute monotonic deadline. The
coordinator sequences these methods without a general filesystem, libvirt, command,
or transport escape hatch. Materialize additionally requires the exact activation binding and
passes it unchanged to the provider operation.

The coordinator implements ADR-0595's existing `ExternalBootPreparationPorts` receipt seam.
Observe reopens exact canonical evidence; execute persists the phase-appropriate materialized or
prepared receipt before returning to core. Restart after that return reuses the receipt without
another provider mutation. Takeover adoption requires the authenticated predecessor receipt
identity and equal phase, activation binding, and plan; it publishes a new authority-bound receipt
without rewriting the predecessor. A current lifecycle grant need not equal the original PREPARE
grant: current mutation authorization remains the authority service's responsibility.

The private store validates owner, mode, regular-file shape, canonical bytes, and bounded digest
indexes before opening a referenced record. Nonblocking opens reject FIFOs without waiting.
Atomic publication removes its exact temporary file on write, sync, or link failure and rejects
zero-progress writes. Wrong-phase or malformed receipts fail before publication.

Focused tests pin the six names and deadline parameters, canonical round trips, exact ownership,
source/target state relationships, V2-only module geometry, sorted unique recovery objects, and
the serialized bound.

Remote module volume creation is a closed nested PREPARE operation. Its request contains the exact
already-authorized preparation binding and `RemoteModuleOperationV1`; validation binds System, Run,
plan, and phase before provider contact. The provider host supplies storage, appliance, attachment,
and writer configuration and returns only the two bounded `PreparedVolume` descriptions. It never
receives a worker database pool, obligation receipt, reusable assertion, caller-selected route, or
generic execution payload. The worker-side verifier retains its transaction and System advisory
lock while awaiting this request. Provider-host blocking work uses the completion-owned remote
module executor, so cancellation is not reported until the underlying mutation has resolved.
The same completion-owned host call runs or adopts the fixed appliance, reopens its newline-framed
scratch result, proves teardown, and durably records a typed terminal result and V2 recovery
geometry before returning. A restarted host replays that exact result without a second provider
mutation. Materialization also retains the exact plan alongside its receipt so later PREPARE can
derive the target definition without caller reconstruction.

Remote PREPARE is split into two closed authenticated authority operations. `begin` admits the
exact current PREPARE request, anchors its `mutation-started` journal record, and then opens the
0146 attempt obligation using that record's exact acknowledgement, attempt, identity, and digest.
It returns the resulting receipt but performs no provider mutation. The worker uses the receipt
nonce to form the fixed remote-module request and retains the ADR-0605 read-only verifier
transaction and System lock while it awaits `execute`. `execute` accepts only the immutable
begin-bound request, runs the configured provider-host operation, and finalizes that same PREPARE
phase from its durable terminal response. Begin replay is exact-request idempotent; stale,
malformed, receipt-mismatched, or nonce-mismatched calls do not start a mutation. A lost execute
response or worker cancellation does not release verification before the host operation's durable
completion, and replay returns that completion rather than repeating it.

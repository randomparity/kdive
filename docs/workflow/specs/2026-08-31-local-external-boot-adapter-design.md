# Local-libvirt external-boot adapter design

## Scope

Issue #2144 wires the real local-libvirt six-port adapter to the recovery artifacts from
[ADR-0586](../../adr/0586-local-external-boot-recovery-uses-an-owned-host-directory.md) and the
operation session from
[ADR-0587](../../adr/0587-local-external-boot-uses-an-operation-session.md). Production capability
advertisement, provider-host authority translation, schemas, migrations, dependencies, and public
MCP contracts remain excluded. The implementation targets Python 3.14 on x86_64 and ppc64le.

## Design

`LocalLibvirtExternalBoot` replaces the fine-grained `LocalExternalBootIO` calls with
`LocalExternalBootIO.open(authority, expected: ExpectedOperationOwnership) ->
AbstractContextManager[LocalExternalBootOperation]`. Each public six-port method enters that
context once and performs reopen, phase writes, observations, and mutations through the returned
operation object; validation failure and work failure still exit the context and attempt session
close. No hidden session is retained between calls.

`RealLocalExternalBootIO` stops accepting `LocalExternalBootHost`. It accepts the narrow
provider-local materializer, recovery/artifact roots, `RealGuestRecoveryWriter`, an injected
`resolve_operation_lease(authority) -> LocalExternalBootOperationLease`, and the existing
`LocalExternalBootSessionFactory`. The resolver is the future #2140 composition seam; this adapter
does not decode or authenticate the opaque authority. It passes the resolved live lease to
`LocalExternalBootSessionFactory.open`, whose `pin_lease` atomically supplies the retained pin and
complete binding. The caller never invents or supplies a binding that its port does not own. Every
public six-port call opens exactly one session, performs every observation
and mutation for that call through it, and closes it after durable evidence has been written. No
session, guest wrapper, descriptor, libvirt object, or host path escapes the call.

The injected resolver is the only authority-to-lease translation point. It must reject an authority
that does not resolve to a live lane-owned `LocalExternalBootOperationLease`. The factory pins that
lease through `LocalExternalBootSessionFactory.open(lease, expected)`, where immutable
`ExpectedOperationOwnership(system_id, run_id, activation_id=None)` is compared with the atomic
`PinnedOperationOwnership` before any libvirt or filesystem resource opens. The factory never
rereads the caller lease. Materialization supplies System and Run from `ExternalBootPlan` and
sets `activation_id=None`, meaning compare only System and Run; preparation and every
recovery-point operation additionally require
the exact activation ID. Same-System/cross-Run and same-System-and-Run/cross-activation leases reject
before resource open. Later
operations supply ownership from the materialization or recovery point. The adapter compares the session's binding and System
inspection with the caller's plan, materialization, or recovery point before mutation. It does not
decode the provider-host authority protocol or advertise support; #2140 owns those tasks.

### Materialization and preparation

Materialization resolves the plan's immutable kernel bundle and optional initrd beneath the
session-owned artifact root, converts the release-qualified modules to the canonical Task 2
archive, and publishes `TargetProjectionV1`. Existing artifact references are reusable only after
exact owner, plan, digest, byte-count, architecture, release, and optional-initrd validation.

Preparation first reopens complete metadata or pre-stop intent by exact activation binding. For a
new preparation it inspects the closed domain through the session, verifies the domain is still in
the recorded source state, and publishes the complete pre-stop intent before stopping the guest.
It then requires inactivity, constructs a read-only `LibguestfsAuthenticatedGuestTree` for the live
release, and invokes `RealGuestRecoveryWriter.capture` with an owner-bound archive sink. The target
projection and materialized archive are reopened from descriptor-bound sources before rendering
the target XML. Complete recovery metadata is published only after the captured module identity,
source XML, target XML, prior power, and every owner/digest field agree with the intent.

The guest-tree traversal is iterative and bounded before materialization. `InactiveGuest` adds
`open_tree(path: str, *, limit: int) -> TreeCursor`, where `TreeCursor` is an
`AbstractContextManager[Iterator[InactiveGuestDirectoryEntry]]` with mandatory `close()`. The session
implementation uses libguestfs `find0`, its streaming and cancellable `FileOut` operation, on a
worker-owned FIFO in a private temporary directory. A producer thread calls
`find0(path, fifo_path)` once for the entire recursive tree while the cursor incrementally parses
NUL-delimited relative names. The adapter does not reopen discovered directories; every guest entry
is emitted and visited exactly once. The cursor
yields at most `limit + 1` entries; it must not first call a list-returning `find`, `readdir`, `ls`,
or glob API. Seeing entry `limit + 1` is the explicit
over-limit signal and stops traversal before visiting or retaining it. The adapter consumes every
cursor in `with`; success, early termination, limit rejection, and backend failure therefore close
the FIFO read side and call the guest handle's thread-safe `user_cancel()` before joining the
producer and removing the FIFO/private directory. Producer `EINTR` is accepted only after deliberate
consumer cancellation; every other error is reported. Libguestfs promises that `user_cancel()`
stops the current transfer shortly. The cursor retains no paths until it has seen end-of-stream or
entry `limit + 1`. On a bounded stream it validates and byte-sorts at most `limit` relative paths
before yielding them, preserving canonical recovery identity across backend enumeration orders.
The adapter does not claim a stronger wall-clock bound: if the
backend violates that contract, cleanup waits with the operation pin held rather than releasing a
lane while the guest handle is live. The adapter increments entry and regular-byte counters before
retaining an item and rejects duplicate, non-canonical, hard-linked, cross-release, or unsupported
topology. Tests prove no list-returning enumeration is called, prove every entry in a multilevel
tree is emitted once, prove entry `MAX_ENTRIES + 1` is neither visited nor retained, and prove cursor
close and `user_cancel()` on caller-abandoned iteration, limit rejection, and backend error. Two
producers emitting the same tree in different orders must yield the same byte-sorted paths and
recovery identity.
Tests call the concrete adapter with an instrumented `find0` FileOut producer, not only a fake
iterator, and prove that closing at the limit invokes `user_cancel()`, joins a cooperating blocked
producer, and creates no unbounded regular host file.

### Activation, observation, and recovery

Activation reopens exact metadata before opening a mutable staging-tree capability. The existing
publication state tables remain the sole action selector. The adapter records deterministic
staging intent before Task 2 writes, installs the desired tree into staging, observes the exact
live/staging/old layout, and calls `advance_module_publication` or
`advance_absence_publication`. Each table action uses only session guest primitives; phase writes
remain in `RecoveryMetadataStore` and are fsynced before the next mutation. Unlisted, unreadable,
substituted, duplicated, or over-limit layouts fail with the domain inactive and no further
mutation. Target XML is defined only after terminal module-publication evidence. A retry while
durable phase is `module-restored` classifies the closed domain before acting:

| Recorded phase | Observed exact XML | Observed power | Sole next action |
|---|---|---|---|
| `module-restored` | source | inactive | define the recorded target XML |
| `module-restored` | target | inactive, prior was inactive | record `target-defined` |
| `module-restored` | target | inactive, prior was running | start the domain |
| `module-restored` | target | running, prior was running | run readiness, then record `target-defined` |
| `module-restored` | any other XML/power combination | any | conflict without mutation |

Defining exact target XML and starting after an observed inactive state are retryable effects. A
failure or worker loss after either effect is classified by the next exact XML/power observation;
the adapter never assumes the call failed or repeats it blindly. Readiness is an observation and
may be repeated. Success is exactly `ReadinessResult(answered=True, ok=True, probe_error=None)`.
Every unanswered, negative, or probe-error result records no later phase, so an identical retry observes
the already-running target, repeats readiness, and only then records `target-defined`. Tests fault
immediately before and after define, start, readiness, and phase fsync and cover unanswered,
negative, and probe-error results.

Observation requires `target-defined`, checks the domain through the same session, and returns the
session's running-kernel observation only when it matches the materialization's expected
architecture, release, and GNU build id.

Recovery performs the inverse sequence. It stops and fences the guest before disk mutation,
reconstructs the exact recorded module capture into a private staging tree through an
owner-bound `RecoveryArchiveSource`, drives the same restart tables to the recorded present or
absent state, defines the exact recorded source XML, restores prior power, and requires readiness
when prior power was running. Host-side restart classification is:

| Recorded phase | Observed exact XML | Observed power | Sole next action |
|---|---|---|---|
| `module-restored` | target | inactive | define the recorded source XML |
| `module-restored` | source | inactive | record `source-restored` |
| `source-restored` | source | inactive, prior was inactive | record `recovered` |
| `source-restored` | source | inactive, prior was running | start the domain |
| `source-restored` | source | running, prior was running | run readiness, then record `recovered` |
| either phase | any other XML/power combination | any | conflict without mutation |

The module restart tables run before the first row and must reach the exact recorded source module
state. The source-XML row covers recovery requested after activation durably recorded
`module-restored` but crashed before defining target XML: once exact source modules are restored,
the sole action is recording `source-restored`, followed by the prior-power rows. A stop error is
classified by observing active/inactive state; only inactive permits guest
mutation. Definition, power, and readiness have the same fail-before/fail-after fault coverage as
activation. `recovered` is written only after all required actions succeed.

### Cleanup and restart behavior

Cleanup opens and validates one session before consulting `cleanup_complete`; an exact tombstone
does not bypass authority, binding, System, or Run validation. It then requires the recovered phase
and exact point identity, removes only
payloads owned by the current activation, verifies their absence, and publishes the existing
cleanup tombstone before closing the session. Finalization is a separate narrow operation because
it deletes only provider-owned recovery-store state and must remain possible after guest resources
are unavailable. #2140 authenticates the current authority operation and constructs
`FinalizeCleanupProof`; `LocalLibvirtExternalBoot.finalize_cleanup_tombstone(recovery, proof,
authority)` treats authority as opaque, checks the closed point digest and complete binding, and
passes only the exact recovery token, point, and proof to descriptor-relative
`RecoveryMetadataStore.finalize_tombstone`. A present exact tombstone is deleted, its absence is
verified, and the parent is fsynced. An absent tombstone succeeds only for the same exact
`mutation-started` proof, covering a lost response after deletion; stale, cross-binding,
cross-point, malformed, or non-`mutation-started` proof fails before deletion. Session close failure
cannot suppress a successfully published tombstone, and finalization never runs inside a closing
guest session.

A restart reopens canonical metadata and the three deterministic
guest names, derives the sole allowed action from durable phase plus observed identities, and
never infers success from absence alone.

## Failure behavior

Owner, binding, plan, materialization, source/target state, artifact, archive, XML, domain,
overlay, module-tree, phase, power, readiness, or running-kernel mismatch fails before the next
mutation. An ambiguous move is immediately re-observed under the inactive fence and only its exact
before or after layout is accepted. Work errors remain primary while session and capability close
errors are attached; every owned resource is still closed. A failed readiness check leaves durable
evidence at the last completed mutation so an identical retry resumes rather than repeats an
unproven step.

## Threat model

The authenticated worker and configured local roots are trusted. Tenant-influenced kernel bundles,
initrds, release strings, stored opaque references, libvirt XML, guest filesystem entries, stale
retries, and substituted host files are untrusted. This design adds no external entry point, but it
connects those existing inputs to guest disk, domain XML, and power mutations.

Controls are the operation-session lease pin, exact binding comparisons, no-follow
descriptor-relative artifact and recovery access, closed Pydantic values, defused XML parsing,
canonical UTF-8/NFC names, entry and byte bounds, hard-link rejection, inactive fencing before
every guest mutation, complete layout observation, durable phase fsyncs, and exact readiness and
running-kernel checks. Failure responses expose identities and categories, never host paths or
guest contents. A compromised worker host, libvirtd, libguestfs appliance, or operator-owned root
is out of scope; production authority integration and advertisement remain #2140.

## Verification

Focused integration tests replace the semantic host fake with session, guest-tree, artifact, and
recovery fakes. They prove all six operations, one authenticated session per operation, exact close
on success and failure, reject-before-mutation owner substitution, present and absent module trees,
fail-before-effect and fail-after-effect moves, restart at every durable phase, exact XML/power/
readiness restoration, recovery after the activation module-phase fsync but before target XML
definition, cleanup tombstones, present-tombstone finalization, lost-response replay for
an absent tombstone with the same proof, stale/cross-binding/cross-point proof rejection, and
unchanged production support advertisement. Finalization is asserted to open no libvirt or guest
session. Cleanup replay tests present a correct tombstone with stale and foreign authority and
require rejection before the completion fast path. A
controlled fault in a new restart test must make the test fail before the fault is reverted.

Run `just test-verbose tests/providers/local_libvirt/test_external_boot.py`,
`just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_recovery.py`,
`just test-verbose tests/providers/local_libvirt/lifecycle/boot/test_session.py`, `just lint`,
`just type`, `prek run`, and the pre-push `just ci` gate. Live VM tiers remain excluded because
they require an operator-provided host and image.

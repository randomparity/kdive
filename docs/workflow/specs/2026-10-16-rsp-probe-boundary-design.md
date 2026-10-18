# Preserve guest state at gdbstub session reservation

## Problem and authority

Issue #3245 records a guest paused by the standalone RSP reachability check.
A QEMU 10.2.2 no-disk diagnostic at `82ce88943` observed running -> paused
on TCP connect, before `?`; disconnect left it paused. Startup `-S` stayed halted.
This is diagnostic evidence, not candidate Linux/provider qualification.
The operator approved removal and deferred actual attachment on 2026-10-16
(ten-design approval SHA256 `d94de308ce0f426d1f3db3158c6f60cb3b207c1b3bfbb31c526f737debe09418`,
section 3245); the frozen issue WORK:SCOPE owns criteria and exclusions.

## Design

Local and remote Connect resolve and validate gdbstub endpoints and return the
existing TransportHandleData encoding without connecting to the stub. Session
reservation, access checks and per-System serialization remain with existing
session handlers. Remove the obsolete shared RSP probe and its injected seams.
No replacement TCP check, detach, continue, or before/after state restoration.
The real owner of protocol validation is existing DebugEngineRuntime.attach_or_reuse:
provider attach seam -> shared gdb_attach_seam -> GdbMiEngine.attach ->
`-target-select remote`. This existing attachment can halt the guest and reports
categorized failures; it is lazy on the first debugger operation, not start_session.
The wrapper must say a live session reservation does not guarantee reachability.
See [ADR-0798](../../adr/0798-rsp-probe-attachment-boundary.md).

## Success

- C1: local/remote gdbstub open/close performs no debugger socket IO, preserving
  initially running or halted model state even when a hypothetical dial would pause it.
- C2: both existing endpoint/access policies and handles remain; invalid hosts and
  resolver faults retain their existing errors. Local SSH and remote drgn-live stay unchanged.
- C3: the first actual GDB attachment still validates the target with categorized
  failure and cleanup; Connect does not claim current reachability.
- C4: remove the obsolete implementation and adapt its direct tests/callers;
  clarify only ADR-0032's false standalone probe claim, without editing accepted text.

## Failure model

- Actors and deployments: authenticated agents reserving sessions on local-libvirt
  and configured remote-libvirt; operator-controlled libvirt/QEMU and gdb endpoints.
- Invariants and assets: guest execution state during reservation; host access policy;
  honest session contract; existing attachment failure taxonomy and cleanup.
- Accepted failure classes: stale/unreachable/non-RSP endpoint after successful
  reservation is detected by actual GDB attachment; endpoint resolution is not liveness.
- Covered elsewhere: between-call execution semantics #3244; missing interrupt stop #3230;
  boot/capture probes and actual attachment execution semantics retain existing ownership.

## Threat model

- Boundary inventory: no added/widened trust boundary. Existing endpoint metadata and
  operator configuration cross into Connect; actual GDB connects later through existing seams.
- Actor model: tenants cannot choose arbitrary local hosts; remote operators own ACL addresses.
- Controls: retain loopback literal gate locally, allow_acl_remote remotely, live XML/config
  resolution and existing session authorization/reservation. Do not add DNS or socket fallback.
- Out of scope: malicious privileged operator configuration and debugger lifecycle redesign;
  existing provider deployment trust and excluded issue owners hold those contracts.

## Global Constraints

Python 3.14; existing dependencies only; preserve x86_64 and ppc64le targets.
No migration, ADR index edit, engine/controller redesign, or new live-operation authority.

## Validation

Focused Connect tests inject a socket boundary that pauses a running model on dial;
reservation must leave both initial states unchanged and perform zero socket operations.
Run red on unchanged production code, green after removal, then controlled dial faults
must make both local and remote tests fail. Retain endpoint/SSH tests and actual GDB
attach failure/cleanup tests. No prose snapshot tests; wrapper/ADR/spec truth is reviewed.
The existing panic-stub live assertion migrates to native `gdb --nx --batch`,
`target remote`, `info registers`, and explicit `disconnect`, with a 10-second subprocess
timeout and 2-second remote timeout. Assert native PC register output, not exit status
alone. Native GDB is an existing declared prerequisite; no symbols fixture is added.
The existing transient-domain context retains cleanup on failure or timeout.
Regenerate wrapper-derived tool documentation and resource snapshots when changed.
Execution of the changed live test and a candidate-bound running/halted Linux/provider
proof require separate operator resource admission.
No firmware diagnostic or mock test substitutes for that qualification. Do not emit
build-complete or MERGE-READY while required real proof is unrun.

## Session-runtime ownership amendment

The operator-approved #3245 amendment adds creation-time ownership to the shared session
helper and its private receipt consumer ([ADR-0801](../../adr/0801-session-runtime-directory-ownership.md)).
Create the short directory exclusively with mode0700 and capture no-follow UID/device/inode
before redirecting XDG. Collisions and uncertain capture leave the environment unchanged.
Restore XDG first; delete only the still-owned empty directory, retain uncertain residue,
and report cleanup failures with primary exception context. Repeated restoration must not
delete a new occupant. The consumer persists the creation receipt and restores ownership
if writing that receipt fails. Preserve non-session None, short socket paths and existing
caller-owned domain/disk behavior. This adds no atomic arbitrary same-UID rename guarantee.
Focused tests cover collision, capture failure, replacement, nonempty residue, repetition,
both boot callers and combined primary/cleanup errors. Actual provider proof remains unrun
until separately admitted; no existing baseline-preservation obligation is relaxed.

## Native completion and revised causal qualification

The adopted helper contract in [ADR-0802](../../adr/0802-session-native-completion-and-retained-runtime.md)
adds optional create/destroy submission and return observations. Positive native completion
remains separate from client reap. Native or lifecycle-receipt uncertainty retains the
child runtime; the private parent retains its backing resources until both the native
chain and actual/logical successful child completion with verified descendants are proven.
A failed destruction-submitting observer must not prevent otherwise possible teardown.
Primary and cleanup errors remain visible, including default callers with no observer.
Other callers retain their existing storage ownership and cleanup policies.

The operator revised the local Linux qualification criterion to complete observation of
the actual reservation invocation and its threads, descendants, inherited descriptors and
alternate routes, demonstrating no attempted debugger-endpoint connection. The proposed
proof uses a bounded reservation child and a finite native syscall-trace consumer. It
retains real running/paused state samples, running progress and paused counter stability.
Those samples expressly do not exclude a transient STOP/RESUME pair between observations.
No second monitor transport, state-mutating sentinel or automatic resume is introduced.

This causal evidence trusts admitted Python, kernel, tracer and libvirt implementations
and the exact local libvirt resolver API/socket boundary. It does not trace an already
running daemon or assert universal daemon noninterference. Unknown trace forms, missing
lineage or descriptor provenance, observer loss and late completion fail qualification.
Saved monotonic acceptance deadlines are not extended for tracing overhead; they do not
cancel blocking calls or promise physical publication by the deadline. Product local and
remote reservation contracts remain unchanged; this x86_64 proof grammar does not qualify
remote deployments or replace the separate native POWER owner.

Offline contract tests establish only the implemented rejection and ownership behavior.
Installed tracer readiness and all real running, paused, actual-GDB and panic proof arms
remain unrun. Separate operator admission is still required before any native operation;
offline green is not Linux/provider qualification or merge readiness.

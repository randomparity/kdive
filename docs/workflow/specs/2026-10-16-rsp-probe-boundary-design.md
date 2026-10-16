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

# ADR 0083 — Remote connect/debug plane: shared gdb-MI/drgn infra + ACL'd direct-TCP gdbstub (M2)

- **Status:** Proposed
- **Date:** 2026-06-09
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0079](0079-remote-live-debug-transport.md) (the
  remote live-debug transport design this implements), [ADR-0076](0076-remote-libvirt-provider-package.md)
  (the independent remote-libvirt package + portability gate), [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the guest-agent in-target seam drgn-live reuses), [ADR-0080](0080-remote-provisioning-disk-image-profile.md)
  (the domain-XML gdbstub port registry the connector reads), [ADR-0032](0032-connect-plane-gdbstub-debugsession.md)
  (the gdbstub Connect plane + DebugSession lifecycle), [ADR-0034](0034-debug-plane-gdbmi-tier.md)
  (the gdb-MI tier), [ADR-0033](0033-drgn-introspection-from-vmcore.md) (vmcore drgn),
  [ADR-0039](0039-ssh-transport-live-introspection.md) (the local SSH live-drgn path the remote
  guest-agent path replaces).
- **Spec:** [`../specs/m2-remote-libvirt.md`](../specs/m2-remote-libvirt.md) §Decomposition issue 6
- **Issue:** #205

## Context

ADR-0079 settled *what must cross the network* for remote live debug. This ADR settles *where
the implementing code lives* and *how the remote provider reuses the worker-side debug
mechanics* without coupling to local-libvirt or breaching the ADR-0076 portability gate.

Three concrete obstacles drove the decisions below:

1. **The real gdb-MI engine and the drgn report helpers live inside `local_libvirt/`.** The
   gdb-MI engine (`debug_gdbmi.py`: MI parsing, command timeouts, transcript redaction) and the
   drgn report assembly (`introspect_drgn.py`: the three fixed helpers, redaction, byte-cap) are
   worker-side debug mechanics with nothing libvirt-local about them — yet they sit under
   `providers/local_libvirt/debug/`. Remote needs the *real* engine (its acceptance is real
   gdb-MI over direct TCP, not a mock like fault-inject's), so reuse means either coupling
   remote→local (against ADR-0076's independence goal) or duplicating ~500 lines.

2. **The local connector enforces loopback as an SSRF control; the remote gdbstub is
   deliberately a remote host.** `LocalLibvirtConnect` and `GdbMiEngine.attach` both reject any
   non-loopback RSP host (the ported v1 "F2" control, ADR-0032 §5). On a remote host the gdbstub
   port is on the *remote* host, reached over the operator-ACL'd worker↔host segment (ADR-0079).
   The loopback gate is correct for co-located QEMU and wrong for remote — the reachability
   control moves from "must be loopback" to "the operator ACL restricts the port to the worker
   pool source" (ADR-0079, the network ACL *is* the auth).

3. **The live-drgn MCP path is hard-wired to the local SSH model in core.**
   `debug.start_session(transport="ssh")` resolves an `ssh_credential_ref` from the System
   profile, and `introspect.run` gates on `session.transport == "ssh"`. Both files are under
   `mcp/` — inside the ADR-0076 portability-gate core surface. A remote disk-image System has no
   ssh credential and reaches drgn through the qemu-guest-agent, not ssh, so routing remote
   in-guest drgn through these tools requires generalizing core (a gate-blocked change).

## Decision

### 1. Extract a provider-neutral debug-infra package

Move the worker-side debug mechanics out of `local_libvirt/debug/` and the RSP codec out of
`local_libvirt/lifecycle/connect.py` into a new provider-neutral package
`src/kdive/providers/debug_common/`:

- `gdbmi.py` — the gdb-MI engine, MI records, execution control, controllers.
- `introspect.py` — the three fixed drgn helpers, `assemble_report` (redact-then-byte-cap), and
  the narrow `_Program`/`_Task`/`_Module` protocols.
- `rsp.py` — `rsp_frame` / `valid_rsp_frame` / `rsp_reachable`.

`local_libvirt` re-imports these from `debug_common`; the move is behavior-preserving (the
local provider's own `Connect`, `VmcoreIntrospect`, and `LiveIntrospect` wiring classes stay in
`local_libvirt/`). `debug_common` lives under `providers/` (not a portability-gate core prefix),
so the extraction touches no gated surface. This is what ADR-0076's hypothesis predicts: a new
provider is provider-specific wiring over shared seams, not a copy of them.

### 2. The RSP host reachability is a policy parameter, not a hard-coded loopback gate

The engine's `attach` and the connectors take a **host-reachability policy** — a callable that
validates the resolved RSP host or raises `CONFIGURATION_ERROR`. Local wires the loopback-only
policy (unchanged SSRF control). Remote wires an ACL-remote policy: the host must be a valid IP
literal (no DNS, no SSRF amplification) but need not be loopback — the operator ACL restricting
the unauthenticated gdbstub to the worker-pool source is the security boundary (ADR-0079), not a
loopback assertion. The policy is the *only* behavioral difference between the local and remote
gdb-MI attach.

### 3. Remote gdbstub connector reads the port from the domain XML

`RemoteLibvirtConnect.open_transport(system, "gdbstub")` resolves the gdbstub `host:port` from
the running domain's definition over the qemu+tls connection — the port recorded by provisioning
(ADR-0080), the host being the remote host's reachable address — applies the ACL-remote policy,
probes RSP reachability with the shared probe, and returns the same `TransportHandleData`
encoded handle the gdb-MI tier already consumes. The slow seams (XML resolve, socket probe) are
injected and `live_vm`-gated; orchestration and the full error contract are unit-tested with
fakes. `close_transport` validates the handle and no-ops (connectionless RSP). The remote
`attach_seam` spawns the worker's gdb against the remote `host:port` with the ACL-remote policy.

### 4. In-guest drgn-live runs through the guest-agent seam, not ssh

`RemoteLiveIntrospect.introspect_live(transport_handle, helper)` validates `helper` against the
fixed in-tree set **worker-side** (never an in-guest shell), composes the constrained drgn
invocation, and runs it inside the guest through the ADR-0078 guest-agent exec seam (the same
seam install uses), reusing the shared `assemble_report` for the single redaction + byte-cap
boundary. The base image carries drgn + matching vmlinux (a provisioning-profile obligation,
ADR-0079). The port is implemented and unit-tested in #205 and wired into the remote runtime's
`live_introspector`; the **end-to-end MCP routing is deferred** (see Consequences).

### 5. Worker-side vmcore postmortem reuses the offline drgn path

`RemoteVmcoreIntrospect.from_vmcore` fetches the vmcore + vmlinux from the object store on the
worker, verifies the core's build-id against the Run's recorded build-id, and runs the shared
drgn helpers locally — no live reachability (ADR-0079). Its tool (`introspect.from_vmcore`) is
keyed on `run_id` with no ssh coupling, so it wires end-to-end in #205.

## Consequences

- **gdb-MI direct-TCP and worker-side vmcore postmortem land end-to-end in #205**, exercising
  the real gdb-MI tier and the real offline drgn path on the remote provider (acceptance
  criterion 1 and the vmcore half of criterion 2). Both are entirely within `providers/` and
  `tests/providers/`, so the portability gate stays green with no allowlist change.
- **In-guest drgn-live is delivered at the port + composition level in #205**, unit-tested
  through the guest-agent seam; the in-guest-drgn half of acceptance criterion 2 is met at the
  port boundary and verified live in the operator e2e (issue 8).
- **Two pieces are deferred to a follow-up** because they are core coupling the ADR-0076 gate
  deliberately blocks, not provider work:
  - **drgn-live MCP routing** — generalizing `start_session`/`introspect.run` off the
    ssh-transport + ssh-credential assumption so remote guest-agent drgn is reachable through the
    tools. Needs a core change (`mcp/`) + an allowlist extension + its own ADR.
  - **The dead-worker gdbstub reconciler reset** (ADR-0079's single-client-contention
    consequence, `→ transport_conflict`). The reconciler is core (`reconciler/`); the reset is a
    deliberate, separately-reviewed core change. The spec's issue-6 decomposition row and #205's
    acceptance criteria already omit it.

  The follow-up carries the gate-allowlist extension and the ADR amendment for both.
- **The extraction makes local and remote share one tested gdb-MI engine and one drgn report
  assembler.** A future provider (cloud, bare-metal) reuses `debug_common` with its own host
  policy and transport, keeping the ADR-0063 falsifiability claim measurable.
- **No new error strings.** Unreachable gdbstub / guest agent → `transport_failure`; an
  unattachable endpoint → `debug_attach_failure`; a build-id provenance mismatch →
  `configuration_error`; off-gate drgn → `missing_dependency` (all existing categories, ADR-0079).

## Alternatives considered

- **Remote imports local-libvirt's engine directly.** Smallest diff, but couples remote→local
  against ADR-0076's independence goal; a reviewer enforcing the gate's spirit would flag it.
  Rejected.
- **Duplicate the engine + helpers into `remote_libvirt/`.** Satisfies independence literally
  but copies ~500 lines of tested mechanics — two copies to drift. Rejected.
- **Add a non-loopback flag to the local engine in place.** Keeps the engine where it is but
  weakens the local SSRF control's locality and still couples remote→local. Rejected in favor of
  the host-policy parameter on the extracted engine.
- **Extend the portability-gate allowlist now to deliver drgn-live MCP routing + the reconciler
  reset in #205.** Crosses the core boundary the gate protects for two changes that are not
  provider work; declined for #205 and tracked as the deliberate, separately-reviewed follow-up.

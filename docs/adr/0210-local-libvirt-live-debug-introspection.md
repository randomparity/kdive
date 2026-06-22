# ADR 0210 — Local-libvirt production live-debug transport resolution and drgn introspection

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** M2.8 B1, B2, B3
- **Builds on:** [ADR-0032](0032-connect-plane-gdbstub-debugsession.md) (the lock-free bounded
  transport-probe connect contract), [ADR-0063](0063-typed-provider-runtime.md) (the
  `Connector`/`VmcoreIntrospector`/`LiveIntrospector` ports these satisfy),
  [ADR-0208](0208-provider-capability-descriptor.md) (the descriptor each plane flips on as it
  lands), [ADR-0209](0209-capability-aware-mcp-admission.md) (the fail-fast that gates these
  planes until they are wired).
- **Spec:** authored per-issue during `work-issue` (B1/B2/B3), alongside this ADR.

## Context

`local_libvirt`'s connect and introspection seams are `live_vm`-test-injected stubs. Production
`from_env()` wires placeholder functions that raise `MISSING_DEPENDENCY` "only under the live_vm
gate", and the real implementations exist only as fixtures the test harness supplies:

- `LocalLibvirtConnect.from_env()` wires `_real_resolve_endpoint` and `_real_resolve_ssh_endpoint`,
  **both of which raise `MISSING_DEPENDENCY` unconditionally**. So `debug.start_session` cannot
  open a gdbstub or drgn-live transport in production, which strands every session-bound `debug.*`
  op (`set_breakpoint`, `continue`, `read_memory`, `read_registers`, `interrupt`,
  `list_breakpoints`).
- `LocalLibvirtVmcoreIntrospect.from_env()` and `LocalLibvirtLiveIntrospect.from_env()` leave
  their drgn seams `None`, so `introspect.from_vmcore` and `introspect.run` raise
  `MISSING_DEPENDENCY` up front.

The connect plane's `_open_gdbstub` / `_open_ssh` orchestration, the loopback-only enforcement,
and the RSP/SSH reachability probes are already real and unit-tested (ADR-0032); the *only*
missing piece is the production resolver that turns a System into its `(host, port)` endpoint.
Under local-libvirt's direct-kernel boot, the gdbstub host:port and the loopback-forwarded SSH
endpoint are **determined by the domain the provider itself defined** — they are recoverable from
the running libvirt domain XML / the per-System config the provisioner recorded, not from any
external dependency. The development host carries the full toolchain (KVM, libvirt
`qemu:///session`, gdb, drgn) so these seams are provable live here.

## Decision

Implement the production resolvers and drgn seams for local-libvirt's connect and introspection
planes, satisfying the existing ports unchanged. Each plane flips its ADR-0208 descriptor field
and promotes its ADR-0175 maturity to `implemented` **in the same PR that wires it**, so the
surface is never wired-but-marked-partial or marked-implemented-but-stubbed.

### 1. B1 — gdbstub + drgn-live endpoint resolution from the running domain

Replace `_real_resolve_endpoint` and `_real_resolve_ssh_endpoint` with real resolvers that read
the System's running libvirt domain (the domain XML the provisioner defined, looked up by the
System's domain name) to recover:

- the **gdbstub** endpoint — the loopback host and the port the domain exposes its QEMU gdbstub
  on (the port the provisioning profile allocated and the domain XML records), and
- the **drgn-live SSH** endpoint — the loopback-forwarded host:port for the in-guest SSH the
  drgn-live transport uses.

Both feed the *existing* `_open_gdbstub` / `_open_ssh` paths, which already enforce
loopback-only-before-IO and probe RSP/SSH reachability (ADR-0032). `debug.start_session` opens a
real transport; the session-bound `debug.*` ops run against it through the unchanged
`GdbMiEngine`. The resolver reads provider-owned state (the domain it defined), so it introduces
no new external dependency — `MISSING_DEPENDENCY` is reserved for an absent host tool, not the
resolution itself. `supported_debug_transports` gains `GDBSTUB` and `DRGN_LIVE`; `debug.*`
maturity promotes.

### 2. B2 — offline drgn introspection (`introspect.from_vmcore`)

Wire `LocalLibvirtVmcoreIntrospect.from_env()` to construct the real drgn seams (open the program
from the captured core fetched from the object store, run the fixed in-tree helpers
tasks/modules/sysinfo) instead of leaving them `None`. The redaction and store-fetch seams are
already real; only the drgn `_open_program` / `_run_helper` seams change from stub to live.
`supported_introspection` gains `OFFLINE_VMCORE`; the tool's maturity promotes. A genuinely
absent `drgn` import remains a legitimate `MISSING_DEPENDENCY` surfaced up front (the seam tries
the import on a provider that *does* support the plane).

### 3. B3 — live drgn introspection (`introspect.run`)

Wire `LocalLibvirtLiveIntrospect.from_env()` to construct the real live drgn seam: attach drgn to
the live kernel over the **B1 transport** and run one selected helper. B3 therefore depends on
B1 (it needs the resolved live transport). `supported_introspection` gains `LIVE`; the tool's
maturity promotes.

### 4. Seam split preserves unit-testability; live proof is per-issue

The orchestration (endpoint resolution ordering, loopback enforcement, helper selection, the
session/transport contract) stays pure and is unit-tested with fakes that record what they were
handed, exactly as today. Only the libvirt-XML read and the drgn attach/open join the existing
`# pragma: no cover - live_vm` real seams. **Each B issue closes only after a live drive on the
development KVM host** proves the real attach/introspect — `debug.start_session` → breakpoint →
`read_registers` for B1; `introspect.from_vmcore` against a real captured core for B2;
`introspect.run` against a live kernel for B3. The `live_vm`-marked tests run locally; CI gates
the fake-seam contract.

## Consequences

- `debug.start_session` and the session-bound `debug.*` surface work on local-libvirt in
  production; `introspect.from_vmcore` and `introspect.run` work against captured cores and live
  kernels. The interactive-debug wall is gone.
- No port, schema, or migration change — the seams satisfy the existing
  `Connector`/`VmcoreIntrospector`/`LiveIntrospector` ports; the change is `from_env()` wiring +
  the production resolvers + the descriptor/maturity flips.
- `MISSING_DEPENDENCY` narrows to its true meaning on these planes: an absent host tool (`drgn`
  not importable), surfaced up front — never an unwired seam.
- Until B1/B2/B3 land, ADR-0209's fail-fast rejects these planes on local with a clear
  `configuration_error`; the planes light up one PR at a time as their descriptor fields flip.

## Considered & rejected

- **Keep injecting the resolvers only under the live_vm gate (status quo).** Rejected: that is the
  defect — production has no working resolver, so the advertised tools fail. The resolution is
  recoverable from provider-owned domain state; there is no reason it cannot run in production.
- **Resolve the gdbstub port from a stored System config field instead of the live domain XML.**
  Considered; the domain XML the provider defined is the authoritative running-state source and
  already encodes the port. A stored field is acceptable if the provisioner records it
  canonically, but the live domain read avoids a second source that could drift from the actual
  domain; the per-issue spec pins which, with the live domain XML preferred.
- **Fold B2 and B3 into one PR.** Rejected: B3 depends on B1's transport while B2 does not, so B2
  can land in parallel with B1; splitting keeps each plane's live-proof scoped and lets B2 ship
  without waiting on the transport work.
- **Promote maturity to `implemented` on merge, before the live drive.** Rejected: maturity
  asserts the plane *works*; per ADR-0208/§invariant-5 the promotion waits for the live proof on
  hardware, so a green CI run (fakes only) never flips a plane to `implemented`.

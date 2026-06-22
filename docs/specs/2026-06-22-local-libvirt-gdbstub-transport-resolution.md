# Spec — Local-libvirt gdbstub live-debug transport resolution (M2.8 B1, #675)

- **Status:** Draft
- **Date:** 2026-06-22
- **Issue:** [#675](https://github.com/randomparity/kdive/issues/675) (M2.8 Epic B, B1)
- **ADR:** [ADR-0210](../adr/0210-local-libvirt-live-debug-introspection.md) §1 (this spec is its
  concrete realization for gdbstub). Builds on [ADR-0032](../adr/0032-connect-plane-gdbstub-debugsession.md)
  (bounded transport-probe connect contract), [ADR-0208](../adr/0208-provider-capability-descriptor.md)
  (capability descriptor), [ADR-0209](../adr/0209-capability-aware-mcp-admission.md) (fail-fast
  admission), [ADR-0079](../adr/0079-remote-live-debug-transport.md) /
  [ADR-0080](../adr/0080-remote-provisioning-disk-image-profile.md) (the remote
  gdbstub-in-domain-XML pattern this mirrors).
- **Design:** [M2.8 local-libvirt service parity](../design/m2.8-local-libvirt-service-parity.md).

## Context

`LocalLibvirtConnect.from_env()` wires `_real_resolve_endpoint` / `_real_resolve_ssh_endpoint`,
which raise `MISSING_DEPENDENCY` **unconditionally** (not only under the `live_vm` gate). So
`debug.start_session` cannot open a transport in production: capability-aware admission
(ADR-0209) currently rejects every `debug.*` request on local because
`supported_debug_transports` is empty (`frozenset()`).

The connect-plane orchestration is already real and unit-tested (ADR-0032): `_open_gdbstub`
resolves an endpoint, enforces loopback-only **before any IO**, probes RSP reachability over an
injected seam, and returns an encoded `TransportHandle`. The missing piece is the production
resolver that turns a System into its `(host, port)` gdbstub endpoint.

### The gap this spec closes (verified against `main`)

ADR-0210 §1 says the resolver recovers "the port the provisioning profile allocated and the
domain XML records." **That recorded state does not exist on `main`:**

1. `LibvirtDebugOptions.gdbstub` (`profiles/provisioning.py`) is documented as "adds the QEMU
   `-gdb` argument" but `local_libvirt/lifecycle/xml.py` (`render_domain_xml`) renders **no**
   gdbstub — the flag is a phantom contract.
2. No per-System gdbstub port is allocated or recorded anywhere in the local provider.

So before a resolver can read a port back, provisioning must **write** it. Remote-libvirt
already solves the symmetric problem: it renders `<qemu:commandline><qemu:arg value="-gdb"/>
<qemu:arg value="tcp:<addr>:<port>"/></qemu:commandline>` into the domain XML and reads it back
with `recorded_gdb_port()` (`remote_libvirt/lifecycle/xml.py`). This spec brings local to that
shape, scoped to loopback.

### drgn-live SSH is explicitly out of scope (deferred)

The `drgn-live` transport realizes over a loopback-forwarded **guest SSH port** (ADR-0039). That
requires a session-networking capability that does not exist on `main`: host port-forward
(hostfwd/passt) rendering, a guest image running `sshd`, and credential plumbing. That is a
separate feature, filed as a follow-up issue (recorded in the PR body). This spec wires
**gdbstub only** and leaves `drgn-live` **out** of `supported_debug_transports`, so ADR-0209
admission fail-fasts a `drgn-live` request with `capability_unsupported` **before** it reaches
`_real_resolve_ssh_endpoint`. That stub is rewritten to return an honest
`CONFIGURATION_ERROR` ("drgn-live is not supported on local-libvirt") rather than
`MISSING_DEPENDENCY` (which now wrongly implies an absent host package).

## Decision

### 1. Provision: allocate a loopback gdbstub port and render it into the domain XML

In `local_libvirt/lifecycle/xml.py::render_domain_xml`, when (and only when)
`profile.provider.local_libvirt.debug.gdbstub` is `True`, render the QEMU gdbstub passthrough on
**loopback only**:

```xml
<qemu:commandline>
  <qemu:arg value="-gdb"/>
  <qemu:arg value="tcp:127.0.0.1:<port>"/>
</qemu:commandline>
```

using the existing `QEMU_NS` / `register_qemu_namespace()` helpers (the same ones remote uses).
When the flag is `False`, render no gdbstub element (unchanged behavior). The `127.0.0.1`
literal is hard-coded: local-libvirt is single-host and the loopback bind is the security
boundary (mirrors `_is_loopback_literal` enforcement at connect time).

`render_domain_xml` gains a keyword-only `gdb_port: int | None = None` parameter. The
caller (`LocalLibvirtProvisioning.provision`) allocates the port and passes it. Passing
`gdb_port=None` with `debug.gdbstub=True` is a programming error (the provisioner always
allocates when the flag is set) and raises `CONFIGURATION_ERROR`; `gdb_port` is ignored when the
flag is `False`.

### 2. Port allocation: ephemeral bind-probe, with recorded-port reuse for idempotency

`provision()` is **idempotent** (a retry redefines the domain). Allocation must therefore be
idempotent too, or a retry would record a *different* port than the QEMU already running from the
first define still listens on. Strategy (in `provision`, before `render_domain_xml`):

1. If the System's domain is already defined **and** records a gdbstub port, **reuse** that port
   (read it back via `recorded_gdb_port()` over `XMLDesc()` of the existing domain). This keeps a
   retry stable and matches the live QEMU's actual `-gdb` port.
2. Otherwise allocate a fresh loopback port by **bind-probe**: bind a socket to
   `("127.0.0.1", 0)`, read the OS-assigned port via `getsockname()`, close the socket, render
   that port. The TOCTOU window (port could be taken between close and QEMU bind) is accepted:
   loopback, single-host, single-attach, and a bind collision surfaces as a
   `PROVISIONING_FAILURE` on domain start (libvirt/QEMU reports it), which the existing
   transactional `_define_and_start` already converts to a clean failure + undefine.

Reuse is gated on the **gdbstub flag still being set**: a reprovision that flips the flag off
drops the gdbstub element (the `reprovision` path tears the domain down first, so the
already-defined branch does not apply there).

The bind-probe and the existing-domain `XMLDesc` read are injected seams so allocation is
unit-tested with fakes (a fake "free port" source and a fake existing-domain XML), with the real
socket/libvirt calls `# pragma: no cover - live_vm`. Concretely, `provision()` gains a
`_gdb_port_for(system_id, *, connect)` step: it opens a connection, looks the domain up by
`domain_name_for(system_id)`, and on `VIR_ERR_NO_DOMAIN` (no prior define) or an absent recorded
port falls through to the bind-probe; any other libvirt error is `INFRASTRUCTURE_FAILURE`. The
"free port" source is a `Callable[[], int]` injected into `LocalLibvirtProvisioning.__init__`
(default = the real bind-probe), so the allocation branch is fake-driven in tests. This step runs
**only** when `debug.gdbstub` is set, so a non-gdbstub provision opens no extra connection
(unchanged path).

### 2a. Install preserves the gdbstub element across the direct-kernel re-define

Local boot is two-phase: `provision()` defines the domain, then `install()`
(`local_libvirt/lifecycle/install.py::_render_os_section`) re-defines it by reading the live
`XMLDesc()`, editing **only** the `<os>` subtree, and `defineXML`-ing the result — it preserves
all other elements, including `<qemu:commandline>`. **Hazard:** `install.py` parses with
`defusedxml.fromstring` and re-serializes with `ET.tostring` **without** registering the `qemu:`
namespace prefix, so the round-trip would emit `<ns0:commandline>` (an auto-assigned prefix)
instead of `<qemu:commandline>`. libvirt's qemu-passthrough schema requires the `qemu:` prefix
and the `xmlns:qemu` declaration on the `<domain>`, so a re-prefixed element is rejected or
silently dropped — the gdbstub vanishes after install, breaking the live round-trip even though
provision recorded it correctly. **Fix:** `install._render_os_section` must call
`register_qemu_namespace()` (and `register_kdive_namespace()`, which it already implicitly relies
on for the metadata element it also round-trips) before `ET.tostring`. A unit test renders a
provision XML with the gdbstub element, runs it through the install os-edit, and asserts the
re-serialized XML still contains a `qemu:`-prefixed `<commandline>` with the same `-gdb tcp:` arg
(it would catch a regression where the prefix is dropped). This is the one edit in `install.py`;
it is a correctness fix for the element this PR introduces, not new install behavior.

### 3. Resolve: read the recorded port back from the live domain XML

Implement `_real_resolve_endpoint(system)` in `local_libvirt/lifecycle/connect.py`:

1. Connect to libvirt (`KDIVE_LIBVIRT_URI`), look up the domain by `lookupByName(str(system))`.
   The `SystemHandle` passed to `open_transport` **is the domain name**: the caller computes
   `handle_name = system.domain_name or str(system.id)` (`sessions_lifecycle.py`), and a
   provisioned local System's `domain_name` is `domain_name_for(system.id)` = `kdive-<uuid>`.
   So `str(system)` is directly `lookupByName`-able, exactly as remote's gdbstub-port enumeration
   uses `domain.name()`. (If a System with no recorded `domain_name` somehow reaches here,
   `str(system)` is the bare UUID, which `lookupByName` will not find → the not-found branch
   below, a clean `CONFIGURATION_ERROR`.)
2. Read `domain.XMLDesc()` and parse the gdbstub port with the shared `recorded_gdb_port()`
   reader (promoted to a shared helper, see §5).
3. Return `("127.0.0.1", port)`.

Error contract:
- Domain not found / not running → `CONFIGURATION_ERROR` ("System has no running libvirt
  domain") — an operator-actionable state, not a missing host tool.
- Domain exists but records **no** gdbstub port (System provisioned without the flag) →
  `CONFIGURATION_ERROR` ("System was not provisioned with a gdbstub; reprovision with
  `debug.gdbstub = true`"). **Not** `MISSING_DEPENDENCY`.
- libvirt connection / XML-read fault → `INFRASTRUCTURE_FAILURE`.
- Malformed XML → `INFRASTRUCTURE_FAILURE` (mirrors remote `recorded_gdb_port_strict`).

The libvirt connect + `XMLDesc` read is the only `# pragma: no cover - live_vm` seam; the
host:port composition, the not-found / no-port / loopback branches, and the error mapping are
pure and unit-tested with a fake libvirt connection. `_real_resolve_endpoint` no longer raises
`MISSING_DEPENDENCY` — the resolution runs in production against provider-owned domain state.

### 4. `_real_resolve_ssh_endpoint`: honest unsupported, deferred

Rewrite the stub to raise `CONFIGURATION_ERROR` ("drgn-live is not supported on local-libvirt:
no session SSH transport (deferred, see <follow-up issue>)"). It is unreachable in production
(admission rejects `drgn-live` first) but the honest category matters if it is ever reached
directly.

### 5. Descriptor + maturity

- `local_libvirt/composition.py`: `supported_debug_transports=frozenset({"gdbstub"})` (gdbstub
  only; **not** `"drgn-live"`). This is the single descriptor edit; the file is a cross-agent
  conflict zone, so the change is minimal/additive.
- `debug.*` **tool maturity stays `partial`.** Per ADR-0208 invariant 5, maturity asserts the
  plane *works on hardware*; CI proves only the fake-seam contract (no KVM). The `providers`
  pointer wording updates from "local-libvirt: planned (M2.8 B1)" to "local-libvirt: wired,
  pending live KVM proof (M2.8 B6 #680)" — honest: the code path now exists but is not yet
  hardware-proven. Promotion to `implemented` is the orchestrator's post-merge live-drive job.

### 6. Shared `recorded_gdb_port` reader

`recorded_gdb_port()` / the `-gdb tcp:host:port` parse currently lives in
`remote_libvirt/lifecycle/xml.py`. Both providers now need it. Promote the pure parse helper to
`providers/shared/libvirt_xml.py` (where `QEMU_NS` already lives) and have both providers import
it, rather than duplicating the `<qemu:arg>` walk. Remote keeps its `recorded_gdb_port_strict`
wrapper (it adds remote-specific operation/domain detail); the shared helper is the bare
`(xml) -> int | None`. This is "write the same code twice → extract", not premature abstraction.

## Consequences

- `debug.start_session(run, "gdbstub")` opens a real loopback gdbstub transport on local-libvirt
  in production; the session-bound `debug.*` ops run against it through the unchanged
  `GdbMiEngine`.
- `debug.start_session(run, "drgn-live")` fails fast with `capability_unsupported` (honest:
  local does not advertise drgn-live).
- A System provisioned **without** `debug.gdbstub = true` gets an actionable
  `CONFIGURATION_ERROR` on attach, not an opaque `MISSING_DEPENDENCY`.
- No schema/migration change; the seams satisfy the existing `Connector` port unchanged.
- The phantom `LibvirtDebugOptions.gdbstub` flag becomes a real contract.
- **No change to `vmcore.fetch` admission.** `ProfilePolicy.capture_method` already returns
  `GDBSTUB` for a gdbstub-flagged System (`profile_policy.py`), and `GDBSTUB` is not a
  core-producing method (not in `vmcore.py::_VMCORE_METHODS`) nor in local's
  `supported_capture_methods` (`{KDUMP}`). So a gdbstub-only System still resolves to *no implicit
  core method* and `vmcore.fetch` fail-fasts with the existing actionable `CONFIGURATION_ERROR`.
  This PR neither fixes nor regresses that pre-existing, correct behavior; flagged here so the
  `gdbstub`-implies-`GDBSTUB`-capture-method interaction is not mistaken for a new gap.

## Acceptance

**CI (fakes):**
- `render_domain_xml` renders the `<qemu:commandline>` gdbstub arg **iff** `debug.gdbstub` is set,
  on `127.0.0.1`, with the allocated port.
- Port allocation reuses a recorded port (idempotent retry) and bind-probes a fresh one
  otherwise.
- The install os-edit preserves the gdbstub element: a provision XML carrying
  `<qemu:commandline>` survives `install._render_os_section` with the `qemu:` prefix and `-gdb
  tcp:` arg intact (regression test for the namespace-prefix hazard in §2a).
- `_real_resolve_endpoint` returns `("127.0.0.1", port)` from a fake domain XML; maps not-found,
  no-recorded-port, and malformed-XML to the categories in §3.
- `supported_debug_transports == frozenset({"gdbstub"})` in `build_runtime`; admission now admits
  `debug.start_session(..., "gdbstub")` on local and still rejects `drgn-live`.
- `debug.*` tool maturity remains `partial`.

**Live (KVM host), orchestrator post-merge:** `debug.start_session` opens a real loopback
transport; `set_breakpoint` → `continue` → `read_registers` round-trip against a live guest.
Maturity promotes only after this proof (B6 #680).

## Considered & rejected

- **Deterministic per-System port by convention (UUID-derived), no XML read.** Rejected: diverges
  from ADR-0210's "domain XML records" mechanism, risks silent collisions across Systems with no
  feedback, and would not match what QEMU actually bound. The live domain XML is the authoritative
  running-state source — read it.
- **Configured port range (remote's `gdbstub_range`).** Rejected as over-built for single-host
  loopback: a bind-probe needs no operator config and no reservation policy. Remote needs a range
  because its ports are operator-ACL'd LAN-visible; local's are loopback-private.
- **Wire drgn-live SSH in this PR.** Rejected: needs session-networking (hostfwd/passt + guest
  sshd + credential injection) that does not exist on `main`. Deferred to a filed follow-up; B1
  ships gdbstub independently (the issue lists both but gdbstub is self-contained).
- **Promote `debug.*` maturity to `implemented` on merge.** Rejected per ADR-0208 invariant 5 /
  ADR-0210 "Considered & rejected": maturity asserts the plane works on hardware; a fakes-only CI
  run never flips it. Held to the post-merge live drive.
- **Keep `_real_resolve_endpoint` raising `MISSING_DEPENDENCY`.** Rejected: that is the defect.
  The resolution is recoverable from provider-owned domain state and runs in production.

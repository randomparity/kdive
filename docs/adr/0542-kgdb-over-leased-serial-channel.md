# 0542 — KGDB over the leased out-of-band serial channel

## Status

Proposed

## Context

Live debugging on both existing libvirt providers is the QEMU gdbstub: a TCP endpoint the
hypervisor exposes, which stops one guest while the host it runs on keeps running.
[ADR-0079](0079-remote-live-debug-transport.md) and
[ADR-0083](0083-remote-connect-debug-plane.md) built the remote realization of that, and the
worker-side gdb-MI engine (`src/kdive/providers/shared/debug_common/gdbmi/core/engine.py`)
plus arch-aware binary selection
(`src/kdive/providers/shared/debug_common/gdbmi/policy/arch.py:59`) came out of it as
provider-neutral machinery.

An adopted host ([ADR-0538](0538-byo-host-provider-package.md)) has no hypervisor, so it has
no gdbstub. The kernel's own equivalent is KGDB, driven over a serial line by the `kgdboc`
driver. That is the only live-debug path on metal, and epic #1814 makes it a success
criterion on both architectures.

Three properties of KGDB differ from a hypervisor gdbstub, and each has a consequence
somewhere in KDIVE.

**It shares the serial line with the console, by design.** `kgdboc` binds to a tty that the
kernel is already using for `printk`. The wire therefore carries console text and GDB remote
protocol packets interleaved at trap boundaries. This is not an accident of KDIVE's design; it
is how the kernel's serial debugger works, and the kernel tree ships `agent-proxy` precisely
to split one port into a console stream and a gdb stream for people who want both. On an
adopted host that line is the out-of-band console — Serial-over-LAN on x86, the HMC virtual
terminal on POWER — which [ADR-0539](0539-out-of-band-control-port.md) already makes a leased
resource because log capture, SysRq injection, and crash watch all contend for it.

**It stops the machine, not a guest.** A breakpoint on metal halts every CPU. SSH freezes, any
in-band readiness probe times out, and kdump cannot run because nothing is running. On the
libvirt providers a stopped guest leaves a live host to observe it from; here there is no such
vantage.

**Its security boundary is not a port ACL.** ADR-0079's gdbstub is unauthenticated and
unencrypted, so the control is binding and ACL-ing the port to the worker pool's source — the
ACL *is* the auth. A KGDB session reaches the target through the service processor, so the
control is the OOB credential and the network path to that endpoint. Different mechanism,
different failure mode, and worth stating rather than inheriting by analogy.

Two closed literals govern how a transport is expressed, and the file's own comment says to keep
them separate:

- `DebugTransportKind = Literal["gdbstub", "drgn-live"]`
  (`src/kdive/providers/ports/lifecycle.py:27`) — the agent-facing value accepted by
  `debug.start_session`.
- `TransportHandleKind = Literal["gdbstub", "ssh", "drgn-live"]` (`:21`) — the realization,
  serialized as `<kind>://host:port` (`:41-50`), always a loopback endpoint.

Widening the agent-facing literal touches the debug-session registrar under `mcp/tools/`, which
is inside the portability gate's core prefixes (`scripts/m2_portability_gate.py:44-53`). There
is precedent for treating that as a deliberate, reviewed core touch: ADR-0085's drgn-live
generalization did exactly this. The precedent's *allowlist entries* do not transfer, though —
they name `src/kdive/mcp/tools/debug/sessions.py` and `introspect.py`, and both are now
packages, so those strings match nothing under the gate's exact-path rule
(`violations()`, `:229-231`). This decision therefore owes new entries naming the real modules,
which the milestone design document records.

## Decision

**A third `DebugTransportKind`, `kgdb`.** An agent choosing a transport has to know which one
it is getting, because the operational consequences differ: `gdbstub` stops a guest under a
live host, and `kgdb` stops the machine. Folding KGDB into `gdbstub` would give an agent one
name for two behaviors, and the one it would assume is the one it has seen.

**`TransportHandleKind` is unchanged; the handle is a `gdbstub://` loopback endpoint.** The
connector leases the OOB console, bridges it to a worker-local loopback TCP port, and returns
the ordinary `<kind>://host:port` handle. What sits behind that port speaks the GDB remote
protocol, which is what the handle kind names, so the existing encode/decode, the existing
engine, and `select_gdb_binary()` all apply with no change. Splitting the two literals is what
makes this possible, and this is the case they were split for.

**KGDB takes the console lease exclusively, for the session's lifetime.** No splitter. While
the lease is held:

- Console collection is suspended, and the console read seam reports that the console is not
  being pumped rather than returning empty bytes
  ([ADR-0429](0429-remote-console-read-seam.md)) — the property that keeps an empty read from
  reading as "the kernel printed nothing".
- `control.watch_for_crash` and `control.diagnostic_sysrq` refuse with `TRANSPORT_CONFLICT`,
  naming the holding debug session and the action that releases it. SysRq needs to *write* to
  the same channel, so the conflict is symmetric rather than a reader-writer special case.
- In-band readiness and health probes are suspended for the session's duration, because a
  stopped machine cannot answer them and a timeout would otherwise be read as a dead host.

**`supports_crash_watch` is advertised `True`.** The capability is real: crash watch works on
every BYO host whenever no debug session holds the lease, which is the common case. Advertising
`False` because a conflict is possible would be a static lie about a dynamic condition, and
`ProviderSupport` flags (`src/kdive/providers/core/runtime.py:66-89`) are read at admission to
tell an agent what a provider *can* do. The dynamic condition is reported where it occurs, as a
refusal that names its holder, its expiry, and its remedy — and, per
[ADR-0539](0539-out-of-band-control-port.md), a remedy that stays performable when the holding
worker has died, because the reconciler reclaims a stranded lease.

**`kgdboc` is composed into the target cmdline at install time**, through the existing cmdline
composition path ([ADR-0061](0061-boot-cmdline-composition.md)), naming the same console device
the host's `[[byo_host]]` declaration gives the OOB driver. A KGDB session against a kernel
booted without `kgdboc` fails with `MISSING_DEPENDENCY` naming the absent cmdline token, not
with a transport timeout.

**The session's limits carry the full contract** — unit, reference clock, scope, consequence,
and recovery action. A held lease is a limit on other tools, and its holder, its expiry, and
the release action are all part of what an agent must be told for the refusals above to be
actionable rather than mysterious.

## Consequences

The core touch is one enum value plus two modules of the debug-session package under
`mcp/tools/` — `sessions/lifecycle.py`, which carries the per-transport branching, and
`sessions/registrar.py`, which carries the agent-facing `Field` text — declared in the R9
allowlist up front. Keeping `TransportHandleKind` unchanged is what holds it to that:
the alternative — a fourth handle kind — would have widened the decode path, the handle
round-trip tests, and every consumer that switches on realization.

Suspending console collection for a debug session means a per-Run console artifact can have a
gap. That is honest and it is reported: the read seam distinguishes "not pumped" from "empty",
so the gap is legible as a lease rather than as silence. It is also the smaller loss than it
sounds — while gdb has the machine stopped, the kernel is not printing.

A conflict between crash watch and an attached debugger is a refusal an agent will meet during
ordinary work. It names the holding session, so the remedy is one call away. The alternative
shapes are worse in the same place: a splitter would let both proceed and give the debugger a
session that can be corrupted by a console byte, and a `False` capability flag would stop the
agent from ever trying.

KGDB stopping the machine makes it incompatible with kdump *during a session*: a panic while
the debugger is attached traps into the debugger instead of the crash kernel. That is usually
what a developer wants — a live stopped machine beats a postmortem — but it means the two
capture paths are alternatives within a Run, not complements. #1829 owns kdump and #1828 owns
KGDB, and both operate on the same host.

The security boundary being the OOB credential rather than a port ACL means a compromised OOB
endpoint yields kernel-level debug access to the host. That is already true of the plane —
whoever controls the service processor can power the machine and read its console — so KGDB
adds no boundary, but it does raise what is behind the existing one, which is why ADR-0539
requires those credentials to resolve through the SecretRegistry and register for redaction.

Reusing the gdb-MI engine and `select_gdb_binary()` means both architectures get breakpoints,
single-step, backtrace, register and memory reads on day one, and it means a defect in the
engine is a defect for four transports rather than three. That has been the trade since
ADR-0083 extracted it, and it has held.

## Considered & rejected

- **Demultiplex the serial stream, following the kernel tree's `agent-proxy`.** Both consumers
  stay live, no refusals, no console gap — and KDIVE owns an RSP framer whose failure mode is
  mis-attributing a console byte as a packet, silently corrupting a debug session that a
  developer is trusting. The console output it preserves is output a stopped machine is
  largely not producing. If a later need makes the gap costly, a splitter is an additive change
  behind the same lease.
- **Fold KGDB into the existing `gdbstub` transport kind.** No core enum change and no
  allowlist entry. It gives an agent one name covering "stops a guest, host stays live" and
  "stops the machine, nothing answers", and an agent that has only seen the first will plan for
  it.
- **Add a fourth `TransportHandleKind`.** Superficially more honest, and it would widen the
  handle decode path, the `<kind>://host:port` round-trip, and every switch on realization —
  to describe a loopback TCP endpoint that speaks the GDB remote protocol, which is what
  `gdbstub` already names. The two literals are separate so that the agent-facing name and the
  realization can differ; this is that case.
- **Advertise `supports_crash_watch = False` on BYO.** One fewer dynamic refusal to design, at
  the cost of telling every agent that crash watch does not work on a provider where it works
  whenever no debugger is attached. A capability flag that is wrong in the common case is one
  agents learn to route around, and the workarounds they invent are worse than the refusal.
- **Give KGDB its own dedicated serial line, separate from the console.** It removes the
  contention entirely and requires every adopted host to have two service-processor-reachable
  serial ports. Most do not, and requiring it would exclude the PowerVM LPAR case, where the
  HMC virtual terminal is the channel.
- **Run gdb directly against the tty rather than bridging to a loopback port.** `target remote
  /dev/ttyS0` is a supported gdb form, and it would put the engine's process in direct
  ownership of a device the lease is supposed to arbitrate. Bridging keeps the lease the single
  arbiter and keeps the engine's interface identical across all four transports.
- **Skip KGDB and rely on in-target drgn plus vmcore postmortem.** Both are in the epic and
  neither gives breakpoints or single-step. A developer who can only inspect after the fact
  cannot ask the machine a question while it is at the interesting instruction, which is the
  capability real hardware was needed for.

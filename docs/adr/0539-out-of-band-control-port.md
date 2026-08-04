# 0539 — A typed out-of-band control port with Redfish, IPMI, and HMC drivers

## Status

Proposed

## Context

An adopted host ([ADR-0538](0538-byo-host-provider-package.md)) is reached in-band over SSH.
That path is exactly the one a kernel debugger destroys: a `force_crash` wedges the machine,
and the whole point of the exercise is to keep working afterwards. On every provider KDIVE has
today the recovery path is the hypervisor — `virDomainReset`, `virDomainDestroy`, an NMI
injected by QEMU. An adopted host has no hypervisor above it, so the recovery path is its
service processor.

The service processor also owns the serial console. Both the x86 SoL stream and the PowerVM
virtual terminal arrive through it, which makes it the channel that kdump progress, panic
output, magic SysRq injection, and KGDB ([ADR-0542](0542-kgdb-over-leased-serial-channel.md))
all travel over.

Three ecosystems have to be reachable, and they do not agree on much:

- **Redfish** — the DMTF HTTPS/JSON standard, on every BMC shipped in the last several years.
  Power actions are `POST` to a `ComputerSystem` action; the console is a Serial-over-LAN
  session, usually reached over IPMI SoL or a vendor WebSocket depending on the
  implementation.
- **IPMI** — the pre-Redfish RMCP+ protocol, still what a lab full of older machines answers
  on. `chassis power` and `sol activate` are the two verbs KDIVE needs.
- **HMC** — the PowerVM Hardware Management Console. It is not a per-host service processor at
  all: it manages many *managed systems*, each holding many *partitions*, and a host is
  addressed as a `(managed_system, lpar_name)` pair. Power is `chsysstate`; the console is
  `mkvterm`.

The HMC's addressing model is the reason a port is worth defining before a driver is written.
A protocol abstraction fitted to a BMC would assume "one endpoint, one host" and would need
reopening the moment PowerVM arrived. Epic #1814 sequences the HMC driver (#1822) early for
exactly that reason.

Credentials differ in kind from every secret KDIVE resolves today.
[ADR-0077](0077-qemu-tls-control-transport.md) resolves an x509 client cert and *materializes*
it to a private per-op pkipath, because libvirt TLS reads from disk; its control is the
on-disk lifetime, since a TLS layer consumes a key and never echoes it. An OOB credential is a
username and password presented to an HTTP endpoint or a CLI, and it can surface in a console
transcript, a command line, or an error body in a way a TLS key never does. The redaction
obligation is therefore stricter, not merely different, and
[ADR-0073](0073-forced-secret-resolution-redaction.md) already provides the mechanism:
register the resolved value before it is used, and every persisted transcript passes the
redactor.

There is one more property the port has to carry that a hypervisor never needed. The console
is a single physical channel and more than one consumer wants it — log capture, SysRq
injection, crash watch, KGDB. On libvirt each of those has its own path. Here they contend,
and the contention has to be expressible at the port rather than discovered by two consumers
writing to the same file descriptor.

## Decision

We will define a typed out-of-band driver port under `src/kdive/providers/byo_host/oob/`,
with three implementations behind it — Redfish, IPMI, HMC — and will make the console channel
a **leased** resource on that port.

**The port addresses a host, not an endpoint.** A driver is constructed from one host's
`[byo_host.oob]` declaration and thereafter answers for that host alone. `endpoint` plus the
credential refs identify the service processor; `managed_system` and `lpar_name` are the HMC's
additional coordinates, resolved at construction so no caller downstream carries them. This is
what lets the HMC's managed-system-plus-partition model and a BMC's one-endpoint-one-host model
satisfy the same protocol: the difference is absorbed where the driver is built, not at every
call site.

**Two capabilities, both mandatory.** A driver supplies power actions and a serial console
channel. There is no partial driver: a service processor that cannot power-cycle the host
cannot recover a wedged kernel, and one that cannot present a console cannot carry SysRq or
KGDB. Both are the reason the plane exists. Survey work on what else Redfish, IPMI, and the
HMC expose — sensors, boot-device override, virtual media, firmware inventory, HMC dump
management — is #1816 and stays outside this port until a specific use justifies widening it.

**The console is leased, exclusively, with a named holder.** A consumer acquires the channel,
holds it for a bounded scope, and releases it. While a lease is held, a second acquirer is
refused with `TRANSPORT_CONFLICT` naming the current holder and the action that releases it.
Log collection is itself a lease holder rather than a privileged background reader, so
"the collector is pumping" and "KGDB is attached" are the same kind of fact and the console
read seam ([ADR-0429](0429-remote-console-read-seam.md)) can report which one is true. The
consequences of that choice for KGDB specifically are
[ADR-0542](0542-kgdb-over-leased-serial-channel.md); the lease itself is here because it is a
property of the channel, not of the debugger.

**A lease is persisted, expiring, and reclaimable — its holder can die.** Lease state lives in
Postgres, not in worker-local memory, because the process that holds it is exactly the process
that can vanish. It is a dedicated table keyed on the **Resource** — the serial channel belongs
to the physical host, and log collection is an ordinary holder, so neither a System key nor
`debug_sessions` (keyed on a run) can represent every holder. A unique constraint on the
resource makes acquisition an insert that either wins or conflicts, rather than a
read-modify-write two acquirers can race; this repository has built three lease tables for that
reason already. The table is claimed in the milestone's single migration, ahead of the entry
that first writes it. Every lease carries an expiry with the five-part contract
(unit, reference clock, scope, consequence, recovery), and a refusal names the holder, the
expiry, and the release action. A lease whose holding worker is no longer live is reclaimed by
the reconciler ([ADR-0021](0021-reconciler-loop-drift-repair.md)) rather than waiting for a
human.

This is not a new mechanism; it is the one the repository already built for the same failure.
[ADR-0086](0086-dead-worker-gdbstub-reconciler-reset.md) added a reconciler reset because a
worker that died mid-debug wedged remote-libvirt's single-client gdbstub until teardown, and
`src/kdive/reconciler/loop.py` sits in the portability allowlist for precisely that reason. A
console lease is the same shape with a worse blast radius: a stranded holder takes log
collection, SysRq, **and** crash watch down for the host, and every refusal names a remedy —
"release the holding session" — that nobody can perform, while being indistinguishable from
ordinary healthy contention. Without reclaim, the lease's central argument over a splitter
(that a refusal names its cause and its remedy) is false in the one case that needs it. The
work is named in the milestone decomposition alongside the mid-teardown drift arm, since the
two share the dead-worker detection and the same allowlist entry.

**Credentials resolve through the SecretRegistry and register for redaction before use.** The
declaration carries refs only, never material ([ADR-0012](0012-secret-backend.md),
[ADR-0087](0087-config-registry.md)). The worker resolves each ref at the op boundary,
registers the resolved value with the redaction registry
([ADR-0073](0073-forced-secret-resolution-redaction.md)) *before* the first call that could
echo it, and releases the scope only after redact-and-persist. No OOB credential is
materialized to disk: unlike ADR-0077's x509 pair, nothing here reads from a file, so the
resolve→materialize→cleanup path is not entered and its failure modes are not inherited.
Credentials are passed to a driver by argument or environment, never on a command line, since
an argv is readable by any process on the worker and appears in a subprocess error.

**The plane fails closed.** An unreachable endpoint, a rejected credential, or a driver that
cannot establish a console fails with the most specific existing `ErrorCategory` —
`TRANSPORT_FAILURE` for reachability, `AUTHORIZATION_DENIED` for a rejected credential,
`CONTROL_FAILURE` for a power action the processor refuses, `CONFIGURATION_ERROR` for a
malformed declaration. There is no silent fallback to the in-band SSH path. The whole value of
the plane is that it works when in-band access is gone; a fallback would make an OOB failure
indistinguishable from a healthy host until the moment it mattered.

**Power windows carry the full limit contract.** An OOB power-cycle and its subsequent
readiness wait are limits handed to an agent, so each states unit, reference clock, scope,
consequence, and recovery action, per the AGENTS.md rule. A BMC power-on to SSH-reachable on
real metal is tens of seconds to minutes — firmware POST, not a VM boot — and an agent given a
bare relative number will route around a wall it invented.

## Consequences

The port is the epic's widest interface commitment. #1818 (Redfish), #1821 (IPMI), and #1822
(HMC) are all written against it, and #1826 (console), #1827 (control), #1830 (teardown), and
#1828 (KGDB) all consume it. That is why #1822 is sequenced immediately after the port rather
than last: the HMC is the implementation most likely to prove the shape wrong, and it lands
while a revision is still cheap.

Making log collection an ordinary lease holder rather than a background reader is a change of
posture from remote-libvirt, where a reconciler-resident collector streams the console
continuously. On an adopted host the collector can be preempted, so a console artifact may
have a gap. That gap is reported rather than papered over — ADR-0429 built the read seam
precisely so an empty read is never mistaken for "the kernel printed nothing", and a
lease-shaped gap is the same class of fact.

Refusing to materialize credentials to disk keeps BYO out of ADR-0077's cleanup-on-every-exit
obligation, which is the part of that design most able to leave a private key behind after a
worker dies. The trade is that a driver implementation must accept in-memory credentials;
`ipmitool` and the HMC's SSH CLI both do, through an environment variable and a key
respectively, and Redfish is an HTTP header.

Three drivers is three code paths to keep behaviorally equal, and only two of them (Redfish
and HMC) are covered by a live proof (#1833, #1834). IPMI is exercised against a simulator and
whatever lab hardware is available. That asymmetry is recorded rather than resolved: IPMI
exists for hosts predating Redfish, and a lab that has such a host is the lab that can prove
it.

A leased console makes crash-watch and SysRq refusable at a moment when nothing is wrong with
the host, which is a new failure mode for an agent to encounter. It is preferable to the
alternative — two consumers reading the same stream and each seeing a fraction of it — because
a refusal names its cause and its remedy, and interleaved bytes name neither.

## Considered & rejected

- **Use each protocol's client library directly at the call sites, with no port.** Three
  protocols with three addressing models would put `if kind == "hmc"` branches through power,
  console, teardown, and debug. The HMC's partition addressing would leak furthest, because it
  is the only one where "the host" is two identifiers.
- **Model the port on a BMC and special-case the HMC.** It is the shape that falls out of
  writing Redfish first, and it fails on the one driver whose model differs. Resolving the
  managed-system and partition coordinates at driver construction costs nothing and removes
  the special case entirely.
- **A single driver over `ipmitool` for x86, treating Redfish as optional.** IPMI is
  universally available and would have been one driver instead of two. It is also deprecated
  by every vendor shipping today, unencrypted in its most-deployed configurations, and its SoL
  implementations vary more than Redfish's. Redfish is the driver the x86 live proof depends
  on; IPMI is the compatibility path.
- **Keep lease state in worker-local memory.** Simplest, and it makes the failure
  unrecoverable: a dead worker's lease is unreadable by anyone, so nothing can even report who
  holds the channel, let alone reclaim it. The state has to outlive the holder to be reclaimable
  at all.
- **Store the lease as a namespaced `resources.capabilities` key**, as the teardown cordon
  reason is. It would need no table and no schema budget. A jsonb key carries no unique
  constraint, so acquiring becomes a read-modify-write that two workers can interleave and both
  believe they hold — on the one resource whose whole purpose is to have a single holder. The
  cordon reason tolerates that shape because it has one writer and no contention.
- **Rely on expiry alone, with no reconciler reclaim.** A lease that expires does eventually
  free the channel, and until it does every consumer is refused with a remedy nobody can
  perform. ADR-0086 reached the same conclusion for the gdbstub and added the reconciler arm;
  reusing that shape costs one drift check and removes the wait entirely.
- **Let the console be shared, with each consumer reading the stream it wants.** One serial
  channel cannot serve two readers without a splitter, and a splitter that mis-frames one byte
  corrupts a debugger session. The lease states the constraint instead of hiding it. What a
  splitter would buy is examined in ADR-0542, where the KGDB case makes the trade concrete.
- **Fall back to in-band SSH power control (`systemctl reboot`) when the service processor is
  unreachable.** It would mask the exact failure the plane exists to survive: a host whose OOB
  endpoint is dead would look healthy through every ordinary operation and be unrecoverable at
  the first crash. `doctor` reports OOB reachability before allocation instead (#1824).
- **Widen the port now to the full Redfish surface — sensors, virtual media, boot override,
  firmware inventory.** Each has a plausible use and none has a caller. #1816 surveys the
  surface and files what is worth adding; a port sized to a survey nobody has run yet would be
  sized wrong.
- **Materialize OOB credentials to a private per-op path, following ADR-0077.** That design
  exists because libvirt TLS reads certificates from disk. Nothing here does, so it would add
  a private file with a cleanup obligation and no consumer.

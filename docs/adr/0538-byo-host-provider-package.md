# 0538 — One `byo_host` provider package for both architectures, and `ResourceKind.BYO_HOST`

## Status

Proposed

## Context

Every provider KDIVE has is a hypervisor client. `local_libvirt` defines a domain on the
worker's own libvirtd, `remote_libvirt` defines one over `qemu+tls://`, `fault_inject`
synthesizes one. All three answer `Provisioner.provision`
(`src/kdive/providers/ports/lifecycle.py:88`) by *creating* a machine.

Epic #1814 adds a provider that adopts a machine the operator already runs: an x86 host with
a BMC, or a ppc64le PowerVM LPAR reached through an HMC. The question this record settles is
the package shape — one provider or two — and the resource kind that admits it.

KDIVE has two precedents pointing opposite ways.

**A package per platform.** [ADR-0076](0076-remote-libvirt-provider-package.md) gave
`remote_libvirt` its own package rather than a `libvirt_common` layer shared with
`local_libvirt`, accepting bounded duplication of libvirt API calls to keep the two
independent. AGENTS.md carries that forward: future provider families "should follow that
path unless a new ADR justifies broader registry-based dispatch."

**One provider, arch-keyed.** The ppc64le work (epic #1139) added a second architecture to
`local_libvirt` without a second package. `arch_traits()`
(`src/kdive/domain/platform/arch_traits.py:85`) is one table with a row per arch, ADR-0343
keys kernel artifacts by arch, and `select_gdb_binary()`
(`src/kdive/providers/shared/debug_common/gdbmi/policy/arch.py:59`) picks the debugger by
arch. Adding POWER was rows and branches, not a package.

The two precedents split on *what varies*. ADR-0076's two providers vary in their transport
throughout: every libvirt call differs between a local connection and a `qemu+tls://` one, so
the shared surface was thin and the duplication bounded. The ppc64le work varied in a handful
of platform facts against one otherwise-identical driver.

An adopted x86 host and an adopted PowerVM LPAR sit on the ppc64le side of that split. They
differ in exactly two planes:

- **The service processor.** Redfish or IPMI against a per-host BMC on x86; an HMC that
  addresses a partition within a managed system on POWER. Power actions and the serial
  console both go through it.
- **The bootloader.** `grubby` against a GRUB2/EFI installation on x86; grub2-PReP or
  petitboot on a PowerVM LPAR.

Everything else agrees. Both are reached in-band over SSH. Both take a kernel by pulling it
in-target from a presigned GET and installing it into the running OS
([ADR-0078](0078-object-store-in-target-install-seam.md),
[ADR-0082](0082-remote-install-in-guest-kernel.md)). Both confirm readiness by a boot-id
change. Both crash by magic SysRq and capture by kdump. Both run in-target drgn
([ADR-0085](0085-drgn-live-transport-generalization.md)) and worker-side vmcore
postmortem. Both restore a baseline kernel at release.

The resource kind is not a free choice either. `ResourceKind`
(`src/kdive/domain/catalog/resources.py:17`) is a closed enum backed by a `CHECK (kind IN
(...))` constraint, and `tests/db/test_resource_kind_parity.py:25` asserts the CHECK set by
*exact equality* while `:33` asserts every admitted kind resolves to a buildable runtime. A
kind is therefore a coupled unit: enum value, migration, and registered runtime land together
or CI fails.

Two hazards attach to a new kind, both already recorded as bugs that shipped once:

- Admission resolves a cost-class coefficient fail-closed, so a cost class with no seeded row
  denies every allocation. `src/kdive/db/schema/0032_remote_cost_class_coefficient.sql:1-9` is
  the remote-libvirt instance of that defect being fixed after the fact.
- `resolve_accel()` (`src/kdive/services/systems/validation.py:50`) fail-*opens* when a
  resource advertises no guest arches. A BYO host that does not publish its architecture would
  admit a ppc64le profile onto x86 metal and fail at install, long past the point where the
  agent could pick a different host.

## Decision

We will add **one** provider package, `src/kdive/providers/byo_host/`, serving both
architectures behind **one** `ResourceKind.BYO_HOST = "byo-host"`, and will isolate the two
varying planes behind seams inside that package rather than behind a package boundary.

**One package, arch-keyed.** The service-processor difference lives behind the typed
out-of-band driver port ([ADR-0539](0539-out-of-band-control-port.md)), which is the seam
whose three implementations — Redfish, IPMI, HMC — are the only place the x86/POWER split is
visible for power and console. The bootloader difference lives behind one arch-keyed
bootloader module inside the install plane. Adopt, boot-readiness, control, capture, retrieve,
debug, and teardown are written once. Splitting on architecture would duplicate that entire
spine to vary two planes, which is the shape ADR-0076 rejected a shared layer to *avoid* — the
duplication there was bounded and here it would be the whole provider.

**One resource kind.** `ResourceKind.BYO_HOST = "byo-host"` describes how KDIVE relates to the
machine — it adopts rather than creates — which is the fact admission, teardown, and the
reconciler branch on. Architecture is a property of the host, carried in
`Resource.capabilities` where the scheduler already reads it, not a second kind. A kind per
architecture would double the CHECK set, the parity test's expectations, the composition
entries, and the cost-class seeds, to encode a fact that already has a home.

**The kind lands as a coupled unit.** One migration widens `resources_kind_check` to admit
`'byo-host'` **and** seeds a cost-class coefficient row in the same file, so the fail-closed
admission path has a value from the moment the kind is admissible. The migration merges only
alongside a registered, buildable `ProviderRuntime` — fail-closed stub ports are enough to
satisfy the parity test, and #1817 is cut that way deliberately.

**The declared architecture reaches `Resource.capabilities`.** Discovery publishes the single
arch from the host's `[[byo_host]]` declaration, so `resolve_accel()` stops fail-opening and
admission rejects an arch-mismatched profile at `systems.create` rather than at install.

**Registration is bind-only and opt-in.** The provider registers with `creates=False`
(`src/kdive/providers/core/discovery_registration.py:39`), leaving `reconcile_resources`
(`src/kdive/inventory/reconcile/resources.py:150`) the sole creator of BYO Resource rows, and
the runtime composes only when an operator supplies `[[byo_host]]` configuration — the same
opt-in shape remote-libvirt uses. A deployment that declares no BYO host has no bookable BYO
resource.

**This rides the existing dispatch seam.** `byo_host` satisfies the same typed
`ProviderRuntime` ports ([ADR-0063](0063-typed-provider-runtime.md)) and registers behind the
same `ProviderResolver` ([ADR-0071](0071-per-kind-provider-runtime-registry.md)). No new
dispatch architecture is proposed, which is the condition AGENTS.md sets before a provider
family may depart from the remote-libvirt path.

## Consequences

The x86 and POWER halves of the epic can be built and proven independently even though they
share a package: #1818 and #1821 add x86 drivers, #1822 adds the HMC driver, and #1833/#1834
prove each arch on real hardware. The shared spine means a defect fixed for one arch is fixed
for both, and it also means a change to the spine risks both — which is why the two live
proofs are separate exit criteria rather than one.

The HMC is the driver most likely to stress the port shape, because it addresses a partition
within a managed system rather than a host with a service processor of its own. #1822 is
sequenced early for that reason: if the port must change, it changes before four planes are
written against it.

Advertising one architecture per BYO resource makes a host single-arch by construction. That
is a true statement about metal, and it is what closes the `resolve_accel()` fail-open. It
also means a host whose declared arch is wrong is rejected at `systems.create` with a
readable mismatch rather than at install — the failure moves earlier, where a different host
is still selectable.

Seeding the cost-class coefficient in the widening migration couples a pricing value to a
schema change. The value is an operator decision (#1814 open question 6) and a later
correction is an ordinary forward-only migration. Carrying it here is the cheaper error: a
missing row denies every BYO allocation with a message about cost classes rather than about
the host, which is what made the remote-libvirt instance take a second PR to find.

One kind means one row in the portability gate's `CAPTURE_COVERAGE` table
(`scripts/m2_portability_gate.py:39`) rather than two, and one entry in the composition map.
The gate's drift-guard test fails the moment `byo-host` is registered without that row, which
is why #1820 follows #1817 directly instead of trailing the epic.

Nothing here commits KDIVE to PowerNV or OpenPOWER bare metal. The ppc64le half is PowerVM
LPARs reached through an HMC. A future PowerNV host has a BMC and would be an x86-shaped BYO
host with a ppc64le arch — reachable through this design, but unproven by it.

## Considered & rejected

- **Two packages, `byo_x86` and `byo_powervm`, following ADR-0076.** The precedent transfers
  by name and not by substance. ADR-0076 split two providers whose every libvirt call differed;
  these two share SSH in-band access, the in-target install seam, boot-id readiness, SysRq,
  kdump, drgn, vmcore postmortem, and baseline-restore teardown. The duplication would be the
  spine rather than a bounded API surface, and every spine fix would need applying twice with
  nothing to catch a missed one.
- **A `ResourceKind` per architecture (`byo-x86`, `byo-powervm`).** Architecture is already a
  scheduling fact carried in `Resource.capabilities`, and `resolve_accel()` reads it there. A
  second kind would encode it a second time, doubling the CHECK set, the parity-test
  expectations, the composition entries, and the cost-class seeds — and creating the
  possibility of the two representations disagreeing.
- **Extend `remote_libvirt` with an adopt mode.** It is the closest existing provider by
  mechanism (presigned in-target install, boot-id readiness), but its whole control plane is
  libvirt over `qemu+tls://`. An adopted host has no libvirtd to talk to, so the extension
  would be a second provider wearing the first one's package name, and ADR-0076's independence
  decision would have to be reopened to justify it.
- **Reuse `ResourceKind.REMOTE_LIBVIRT`.** Admission, teardown, and the reconciler branch on
  kind, and their behavior genuinely differs: a remote-libvirt System is destroyed at release
  while a BYO host is restored and returned. Sharing the kind would put that difference behind
  a capability flag read at every branch, which is a kind by another name.
- **Introduce registry-based provider dispatch for this family.** AGENTS.md leaves the door
  open for an ADR that justifies it. Nothing in this epic needs it: BYO registers one runtime
  into a resolver seam that already exists, exactly as remote-libvirt did, and the falsifiable
  hypothesis ADR-0076 set is worth measuring a third time rather than abandoning.
- **Defer the cost-class seed to a follow-up migration.** It is what happened for
  remote-libvirt, and `0032_remote_cost_class_coefficient.sql` is the record of the repair. The
  failure is silent in the direction that matters: every allocation is denied for a reason that
  names cost classes, not the new provider.

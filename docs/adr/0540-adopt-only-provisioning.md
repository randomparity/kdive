# 0540 — Adopt-only provisioning and the `adopted-host` boot method

## Status

Proposed

## Context

`Provisioner.provision` (`src/kdive/providers/ports/lifecycle.py:88`) creates a machine on
every provider KDIVE has. `ResourceKind.BYO_HOST` ([ADR-0538](0538-byo-host-provider-package.md))
introduces a provider that must not: the host is a lab machine, a loaner, or a PowerVM LPAR
that PowerVM itself created, provisioned by a pipeline the operator already owns. This record
fixes what `provision` means when the machine already exists, and the profile contract that
expresses it.

**The boot method has no correct existing value.** `BootMethod`
(`src/kdive/profiles/provisioning.py:86`) has two values and both are wrong here.
`direct-kernel` means the platform boots a kernel binary directly and requires
`kernel_source_ref` (`:454`); an adopted host boots through its own bootloader and the kernel
arrives at install time. `disk-image` means the operator staged a base-OS image that
provisioning deploys (`:385`, `:449`); nobody staged anything — the OS was installed out of
band, possibly years ago.

The pairing validator makes this structural rather than cosmetic, though not in the direction a
reader expects. `_pair_boot_method_with_provider` (`:394-403`) is a **biconditional**:
`remote = provider.remote_libvirt_section is not None`, `disk_image = boot_method is
DISK_IMAGE`, and it raises when the two disagree. A third boot-method value does not break it —
it slips through. An `adopted-host` profile with a `byo_host` section evaluates `False != False`
and raises nothing, and so would `adopted-host` paired with a local-libvirt section. (A profile
with *no* provider section never reaches this check — `_require_exactly_one_provider` (`:306-315`)
rejects it first, and entry 3 must extend that validator for `byo_host_section` anyway.) The
check is not too strict for a third value; it is silent about one.
That is the argument for the generalization, and it means the work is *adding* a constraint that
does not exist rather than loosening one that does.

**Verification has to happen at more than one moment, and one of those moments forbids I/O.**
`reconcile-systems --check` (`src/kdive/inventory/cli.py:66`) validates a declaration touching
neither Postgres nor S3, which is [ADR-0121](0121-decouple-migrate-validate-systems.md)'s
contract and is what makes it runnable in a deploy pipeline. Live facts about a host — is SSH
up, does the service processor answer, is the declared kernel installed — cannot be checked
there. They also cannot be checked *only* at allocation time, because an operator wants to
know a host is usable before an agent waits on it. And they cannot be checked *only* at
pre-flight, because the world drifts between a `doctor` run and a `provision`.

**The baseline kernel has to be nameable.** Release restores the host to the kernel the
operator considers its own ([ADR-0541](0541-baseline-restore-or-cordon-teardown.md)), and
teardown cannot restore what it cannot name. Two identifications are available: a version
string the operator declares, or a fact captured from the running host at first adopt.
Captured is self-maintaining but records whatever happened to be booted at that moment — and
the moment a captured value is most likely to be wrong is exactly after a previous teardown
failed and left a KDIVE-installed debug kernel in place. The host would then restore to that
kernel indefinitely, with nothing reporting the substitution.

**`arch_traits()` is mostly a libvirt table.** `src/kdive/domain/platform/arch_traits.py:52`
carries six fields. Four — `machine` (`q35`/`pseries`), `pin_nic_slot`, `kvm_cpu_mode`,
`emit_acpi_features` — are domain-XML rendering facts with no meaning on metal. Of the two
that generalize, `default_crashkernel` is genuinely arch-keyed and BYO wants it; but
`console_device` is a *per-host* fact on real hardware, since which UART the BMC redirects
(`ttyS0` or `ttyS1`) is a firmware setting, not an architecture property. So the number of
fields a BYO host reads from the arch table is one.

**Two runtime defaults are actively wrong for an adopted host.**
`platform_root_cmdline` defaults to `"root=/dev/vda"` (`src/kdive/providers/core/runtime.py:152`),
which is a claim about a platform-owned disk layout that an adopted host's own bootloader
owns instead. And `binding` defaults to `None` (`runtime.py:162`), so `for_resource()` returns
the runtime unchanged (`:174-183`); a provider serving many hosts would then resolve every
operation against one arbitrary host. Remote-libvirt overrides both
(`src/kdive/providers/remote_libvirt/composition.py:390`, `:391`,
[ADR-0183](0183-provider-aware-platform-root-cmdline.md),
[ADR-0187](0187-remote-libvirt-per-op-resource-selection.md)) for the same two reasons.

## Decision

**`provision` adopts.** For a BYO host it validates preconditions against the live machine,
records the facts it established on the System row, and returns a stable handle. It installs
no OS, writes no image, changes no boot order, and touches no firmware setting. A System on a
BYO host is a *claim* on a machine, not a machine.

**A third `BootMethod` value, `adopted-host`,** and the pairing validator generalizes from a
biconditional to an explicit provider-to-boot-method map. `boot_method` describes where the
bootable OS comes from, and adopt-only is a genuinely third answer to that question: KDIVE
created it, the operator staged an image for KDIVE to deploy, or KDIVE never created it at
all. Keeping it distinct is what lets admission reject a `disk-image` profile aimed at a BYO
host — a rejection that reusing `disk-image` would silently turn into an accept.
`kernel_source_ref` is optional on `adopted-host` for the same reason it is optional on
`disk-image`: the lane does not read it. The `direct-kernel` requirement is left alone,
because it catches a real local-libvirt misconfiguration and loosening it to accommodate a
provider that does not use direct-kernel boot would be paying for BYO with a check that
protects something else.

**Preconditions are checked at three moments, each doing only what it can.**

1. **Deploy time, schema only.** `reconcile-systems --check` accepts a well-formed
   `[[byo_host]]` block and rejects a malformed one with an `entry.field: msg` error,
   touching neither Postgres nor S3. ADR-0121's no-I/O contract is preserved exactly; no live
   probing is added there.
2. **Operator pre-flight, live.** A `doctor` contribution registered through
   `diagnostic_provider_contributions()`
   (`src/kdive/providers/assembly/diagnostics.py:12`) probes SSH reachability, OOB endpoint
   reachability, credential validity, architecture match, bootloader flavor, kdump/fadump
   readiness, and baseline-kernel presence — before any allocation, naming the specific defect
   for each.
3. **Adopt time, live, re-checked.** `provision` runs the same precondition module again,
   because the world drifts between a `doctor` run and an allocation. Results are recorded on
   the System row, so what adopt established is auditable rather than inferred.

The pre-flight and adopt checks are one module with two callers in this record; ADR-0541's
teardown step 3 makes a third. That shared module is why #1824 (doctor) follows #1823 (adopt)
rather than preceding it: writing the checks twice is how they come to disagree.

**`baseline_kernel` is operator-declared and verified at adopt.** The `[[byo_host]]`
declaration carries the version string, and its presence in the host's bootloader is a
precondition at both `doctor` and `provision`. A host whose declared baseline has been patched
away fails to adopt, naming the missing version — which is the operator updating a declaration,
not KDIVE guessing. This mirrors the `vcpus`/`memory_mb` rule at
`src/kdive/inventory/model.py:208`, where a host without a declared ceiling is un-grantable:
a declaration is the auditable statement of what the operator intends, and a fact captured
from a machine KDIVE has been crashing is not.

**`arch_traits()` does not split.** BYO reads `SUPPORTED_ARCHES` to validate a declared
architecture and `default_crashkernel` through the existing accessor, and takes
`console_device` from the host's own declaration because on metal it is a firmware setting
rather than an architecture property. That is one arch-keyed field, which does not justify a
second table to keep in step when an arch is added — the single-table design exists to make
adding an arch one row rather than several edits. The module docstring is amended to mark
which fields are libvirt domain-render facts and which are platform-portable, so the next
reader does not have to re-derive it. If a second portable consumer ever needs a second
portable field, the split is a later decision made with two examples instead of none.

**Two runtime fields are set explicitly, not defaulted.** `platform_root_cmdline=None` —
the adopted host's own bootloader owns its root device, and inheriting one via
`grubby --copy-default` is the same mechanism remote-libvirt relies on. And
`binding=ResourceBindingCapabilities(...)` — BYO serves many hosts, so the per-op rebind hook
is mandatory rather than optional. Both are stated here because both are facts adopt resolves,
and both fail quietly if omitted: a wrong `root=` produces a kernel that boots to an
unreachable root, and a missing rebind hook silently drives the wrong machine.

**One System per host, and one declaration per machine.** `concurrent_allocation_cap` defaults
to 1: a force-crash takes the whole machine, so a second concurrent System on the same host
would be crashed by the first one's Run without either knowing.

The cap alone does not deliver that invariant, and the gap is worth stating because the
mechanism looks sufficient. A Resource is identified by `(kind, name)`
(`src/kdive/db/schema/0030_systems_inventory.sql:33`), so two `[[byo_host]]` entries with
different `name` values but the same SSH target, the same OOB `endpoint`, or the same
`(managed_system, lpar_name)` pair are two schedulable Resources — each correctly capped at
one — driving one physical machine. The realized failure is cross-tenant and is exactly what
the cap exists to prevent: tenant A's `force_crash` takes down tenant B's System, B's Run
reports a kernel fault it did not cause, and teardown then restores a baseline underneath B
mid-Run. The HMC case is the easiest to hit by accident, because a partition is addressed by
two identifiers and an operator may reasonably name one LPAR twice to place it in two pools.

So a host's **physical coordinates are unique across `[[byo_host]]` entries**: the SSH target,
the OOB endpoint, and `(managed_system, lpar_name)` where present.
`reconcile-systems --check` rejects a duplicate with an `entry.field: msg` error. This is a
comparison over the parsed document and touches nothing, so ADR-0121's no-I/O contract is
preserved — the check belongs at deploy time precisely because a live probe could not tell two
declarations of one machine apart from two machines that happen to answer alike.

## Consequences

Both changes land in `src/kdive/profiles/`, which is **outside** the portability gate's
`CORE_PREFIXES` (`scripts/m2_portability_gate.py:44-53` covers `domain/`, `db/`, `jobs/`,
`reconciler/`, `services/`, `store/`, `security/`, `mcp/`). So `adopted-host` and the pairing
generalization cost no allowlist entry, and the milestone's gated touch-points are enumerated in
the design document rather than here. That does not make them free: the generalization from a
biconditional to a map is provider-agnostic work that pays for itself at the fourth provider,
and it is the kind of change ADR-0076's hypothesis expects a new provider to force. It is the
gating claim that would have been wrong, and adding an allowlist entry for a path the gate never
inspects is the rot #1835 exists to stop.

Two layers, not three moments of one check. The deploy-time arm is schema-only and shares no
code with the live module — it catches a typo without infrastructure, which is exactly why it
may not probe. The live predicate is then evaluated twice here: pre-flight catches a dead host
before an agent waits on one, and adopt catches the drift between them.
[ADR-0541](0541-baseline-restore-or-cordon-teardown.md) adds a third caller at teardown step 3.
The cost is one shared module and the discipline of not letting its callers diverge.

A declared baseline kernel is a declaration that can go stale, and it will: operators patch
hosts. The failure is loud and early — adopt refuses, naming the version it could not find —
rather than late and silent, which is what a captured value produces. Recovering is an edit to
`systems.toml` and a re-run of `reconcile-systems`, which is the workflow that declaration
already lives in.

Not splitting `arch_traits()` leaves a table whose name says "VM-provisioning traits" with a
consumer that provisions no VM. The docstring amendment is what keeps that honest, and it is
the cheaper honesty: a split would be a core change to a portability-gated module, decided
before any BYO code exists, to separate one field from five.

**The uniqueness check narrows the aliasing hole; it does not close it.** The comparison is
exact over declared strings, so `lab7` and `lab7.example.com`, or a BMC named once by hostname
and once by address, are two distinct coordinate tuples that pass while driving one machine —
and the failure that follows is the cross-tenant one above, not a lesser one. Normalizing the
cheapest collisions (lower-case the host part, strip a trailing dot) is worth doing and does not
change the shape: the operator's declaration remains the only place aliasing can be caught, and
an operator who spells one host two ways gets no signal. This is an accepted residual rather
than an oversight, recorded so entry 2's implementer knows the check is a narrowing rather than
a proof.

`concurrent_allocation_cap = 1` makes a BYO host's capacity binary. A lab with four machines
offers four concurrent Systems, not four times some multiplier. That is what the hardware
actually offers, and admission's existing per-resource cap expresses it with no new mechanism.

Adopt's established facts need a home the System row does not have today: `systems` carries the
submitted `provisioning_profile` and typed columns, and no free-form bag. The milestone's single
migration adds one (`systems.byo_adopt_facts jsonb`), claimed with the rest of the schema rather
than in a second migration — the reasoning is in the milestone design document, and it is the
same strictly-ascending-numbers argument that keeps this epic to one schema-touching entry.

Recording adopt's established facts on the System row means a later operation can tell what
was true at adopt without re-probing — which is what lets teardown compare the host it is
returning against the host it received, and lets the reconciler tell a mid-teardown host from
a healthy one.

## Considered & rejected

- **Reuse `boot_method: disk-image`, widening the pairing to admit either provider.** No new
  enum value, and the in-target install-and-reboot iteration model really is identical. But
  the value's documented meaning — an operator-staged base-OS image that provisioning
  deploys — becomes false for BYO, the `kernel_source_ref` guidance text at `:385` goes wrong,
  and admission loses the ability to reject a genuine disk-image profile pointed at a BYO
  host. The pairing has to be generalized either way, so the saving is one enum value against
  a lane whose name lies.
- **Reinterpret `direct-kernel`, making `kernel_source_ref` optional.** It reads as the
  smallest change and is the largest: `direct-kernel` means the platform boots a kernel binary
  directly, which an adopted host never does, and dropping the `kernel_source_ref` requirement
  removes a check that catches a real misconfiguration on the two providers that do use the
  lane. BYO would be paid for out of local-libvirt's validation.
- **Capture `baseline_kernel` from the running host at first adopt.** Self-maintaining across
  operator patching, and no declaration to keep in step. It records whatever was booted at
  adopt, and the case where that is wrong is the case that matters: after a failed teardown
  the running kernel is a KDIVE debug build, so the host's "baseline" silently becomes the
  artifact of a failure, and every subsequent release restores it.
- **Declare `baseline_kernel` and verify it only at teardown.** Cheapest to build and defers
  the failure to release time, when the host is already crashed and the operator is not
  watching. Adopt-time verification moves the same failure to the moment a different host is
  still selectable.
- **Add live probing to `reconcile-systems --check`.** It is where an operator would most
  like to learn a host is unreachable, and it would break ADR-0121's no-I/O contract, which is
  what makes that command runnable in a deploy pipeline with no Postgres and no network. The
  `doctor` contribution serves the same need without taking the property away.
- **Split `arch_traits()` into portable and libvirt-render halves now.** It is honest typing:
  a metal consumer cannot then read `machine` and receive `q35`. It is also a core change to a
  portability-gated module, decided with one portable consumer and one portable field, and it
  creates two tables that an arch addition must update together — the coupling the single
  table exists to prevent.
- **Lift only `default_crashkernel` and `SUPPORTED_ARCHES` into a platform-neutral module.**
  Smaller than a full split and worse: it produces the two-table coupling without the benefit
  of having separated the libvirt-only fields from the portable ones.
- **Detect aliased declarations live, at adopt, instead of at `reconcile-systems --check`.** A
  probe would have to prove two reachable endpoints are the same machine, which it cannot do
  reliably — a shared machine-id or boot-id is evidence, not proof, and two genuinely distinct
  hosts restored from one image share both. The declaration is where the operator's intent is
  legible, and comparing declared coordinates is exact **over strings**, which is a narrower
  claim than uniqueness over machines — see the residual in Consequences.
- **Let a BYO host carry a `concurrent_allocation_cap` above 1 for partitioning cases.**
  No partitioning case survives a force-crash, which is the operation the provider exists for.
  A host that could safely host two independent debug Systems is a hypervisor, and KDIVE has
  two providers for that already.

# 0583 — External Run boot uses prepared recovery points

## Status

Proposed

## Context

`InstallRequest` carries a combined kernel/modules bundle, an optional initrd, a composed command
line, and immutable object versions into every provider. The providers currently give those inputs
different meanings. Local-libvirt extracts `boot/vmlinuz`, injects modules, stages an optional
initrd, and writes direct-kernel domain XML. Remote-libvirt downloads the bundle inside the guest,
regenerates an initrd, and selects a GRUB entry. That difference prevents a finalized external
build's kernel/initrd pair from naming one portable Run boot.

The remote System must continue to provision and recover through its disk image and GRUB, while an
iterative Run may direct-kernel boot. Switching the domain definition is a durable state change: a
worker can die after changing the provider but before recording success. Recovery therefore needs
an exact pre-activation state and a reconciliation rule. The shared contract cannot carry libvirt
XML, storage-volume names, host paths, presigned URLs, or network-boot concepts because later
providers do not share them.

PR #2104 proposed a standalone System-profile INITRD input and permanent remote rejection. That PR
closed without merging and its decision is not part of the repository. This decision replaces
that unpublished direction: external Run builds supply an optional initrd paired with their kernel
bundle; System-baseline INITRD input remains absent.

## Decision

External Run boot is split into three provider-neutral operations: materialize a validated immutable
`ExternalBootPlan`, prepare a recovery point, and activate the materialization. Cleanup is a fourth
idempotent operation. Shared values contain only immutable artifact identities, architecture,
kernel release, the complete ordered kernel argument set, a versioned root specification, a module
installation obligation, and opaque provider references returned by those operations.

The boot plan is one immutable set. Its identity hashes the schema version, Run/build ownership,
architecture, kernel bundle object key and version, bundle digest, kernel release, optional initrd
object key/version/digest, root specification, ordered command line, and complete module-install
obligation. An initrd is valid only as part of this set; it has no independent activation identity.
Materialization must extract
`boot/vmlinuz` from the combined bundle, validate its architecture and release against the plan,
compute the extracted bytes' SHA-256 digest, and satisfy the plan's module-install obligation. The
compressed bundle is never itself a bootable kernel.

The version-1 module obligation has one mode: `system-root-tree`. It names the kernel release and a
`module-source-manifest-v1` digest of the bundle's exact `lib/modules/<release>/` subtree. The
manifest sorts relative UTF-8 paths by encoded bytes; rejects absolute paths, `.`/`..`, duplicates,
hard links, devices, sockets, FIFOs, and escaping symlinks; and admits only directories, regular
files, and contained relative symlinks. Each entry records normalized path, type, permission bits,
and regular-file size/SHA-256 or symlink target; uid, gid, and timestamps are excluded.
Materialization validates that source manifest, stages the tree, runs required indexing, and computes
`installed-module-tree-v1` with the same walker over the final tree. Generated indexes such as
`modules.dep` belong to installed identity, not source identity. The provider returns the installed
digest, and target state identity binds it.

Materialization does not change the System. Recovery preparation records whether the
release-qualified target is absent or saves its exact prior tree behind the opaque recovery
reference. Activation atomically
stops the domain through the provider control plane, verifies it inactive, then publishes the staged
tree at `/lib/modules/<release>` before applying and booting the target definition. Failure to reach
inactive leaves `prepared` unchanged and mutates neither tree nor definition. An exact existing tree
may be reused; a different tree for the same release is replaced, not rejected. Recovery restores
the saved tree or removes the new tree when the target was previously absent. This preserves
same-release kernel iteration and prevents either running kernel from observing the other's modules.
A provider unable to quiesce, stage, replace, verify, and restore the tree rejects before recovery
preparation. “Preserve the disk overlay” below means keep the same attached overlay and device
definition; it does not promise that the guest filesystem is byte-immutable. The optional initrd
never substitutes for this obligation.

The root specification is a versioned, closed data shape. Version 1 records the target architecture,
one `root=` value, the ordered root-related arguments required by the image, and provenance with an
authority class and immutable source identity. Build-produced facts and bounded stage inspection are
verified authorities. Operator catalog data extends the existing typed attestation path with the
same root value, ordered arguments, architecture, schema version, and immutable image identity; the
current attestation fields alone are insufficient. No second untyped declaration path is added.
Unknown versions, missing facts, stale source identities, conflicting root arguments, and an
architecture mismatch fail before materialization or activation and name the recovery action. A
pre-schema image remains eligible for its existing GRUB boot but not external Run boot.

Before changing boot state, the provider prepares both sides of the compare-and-set. It creates a
durable recovery point representing the exact current persistent boot configuration and prior module
tree, renders but does not apply the target configuration, and returns provider-computed source- and
target-state identities plus an opaque recovery reference. A state identity covers both the boot
definition and the release-qualified module-tree identity. Libvirt definition identity is a
versioned two-part comparison over persistent/inactive XML. The **preserved digest** canonicalizes
the entire inactive definition after removing only the provider-owned external-boot fields
`/domain/os/kernel`, `/domain/os/initrd`, and `/domain/os/cmdline`; no other subtree, attribute,
alias, address, device, firmware field, backing/auth/encryption field, or QEMU argument is excluded.
The **boot projection** is canonical JSON for those three fields, distinguishing absence from an
empty value. XML canonicalization removes syntax-only whitespace, attribute order, and namespace
prefix choices but preserves element order and content. Preparation reads the source inactive XML,
computes its preserved digest, clones it, changes only the three boot fields, and computes the target
boot projection. Observation repeats the same split. A changed preserved digest is always a third
state; source or target requires both the shared preserved digest and the matching boot projection.
Live XML is never an identity input. Remote's recovery point stores the exact inactive disk/GRUB
definition behind the provider seam, so shared state never interprets its XML. Deterministic
identifiers make repeated prepare calls for the same System, Run, plan identity, and source state
return the same point and target identity.

Remote preparation also proves that the source is an owned disk/GRUB baseline: its inactive boot
projection has no kernel, initrd, or cmdline, and KDIVE metadata binds it to this System. A source
carrying external-boot fields is admissible only while a matching durable activation row owns it;
that row must recover under the System lock before another prepare. An unowned external definition
enters `recovery_conflict` and is never captured as a new source point.

Core persists the plan identity, materialization reference, recovery reference, both provider state
identities, and activation state before calling activate. The state machine is
`prepared -> activating -> active`, with
`activating|active -> recovering -> recovered`,
`prepared -> abandoned`, `activating|active|recovering -> recovery_conflict`, and failure metadata
on an operation attempt.
Transitions and provider calls run under the existing per-System advisory lock. Activation is
compare-and-set from the recovery point's source-state identity: the provider refuses changed state
or materialization/recovery references belonging to another System, Run, or plan. On an `activating`
record after worker loss, reconciliation compares the persistent definition and module-tree
identities with both recorded states. The complete target state completes `active`; the complete
source state completes `recovered`. A mixed state whose every component equals its recorded source
or target component is an activation-owned partial and may be restored to source. Any absent,
unreadable, or third component identity enters `recovery_conflict` for operator resolution instead
of overwriting provider state. The portable plan identity is never compared directly with provider
definition bytes. Runtime readiness and running-kernel identity are separate observations and never
decide which persistent definition won.

Recovery restores a usable disk/GRUB baseline, not only persistent bytes. When an active Run becomes terminal
and the System remains reusable, terminalization enters `recovering`, stops the domain through the
provider control plane, verifies it is inactive, restores the prior module tree and persistent
definition, boots that definition, and requires a fresh boot plus the existing System readiness
contract before committing `recovered`. The exact GRUB-selected kernel is guest bootloader state and
is not knowable from an inactive domain definition, so it is not an identity gate; a recovery that
cannot reach readiness after bounded retries remains `recovering` for operator action and retains
all evidence. A recovery retry repeats the sequence idempotently; concurrent terminalization
serializes under the System lock. The recovery point and materialized artifacts cannot be deleted
before `recovered`. System teardown instead destroys the domain before cleaning the recovery point
and materialization, because a definition that will be destroyed need not be restored or rebooted.
Artifacts remain while the Run can retry, are deleted idempotently on those ordered paths, and are
swept by deterministic ownership after worker death. A partial materialization is either atomically
published under its final identity or discoverable as an owned partial and removed; it is never
activated.

`prepared -> abandoned` is the pre-activation disposal path. Run terminalization or reconciliation
may take it only after retries are no longer possible and provider observation still equals the
recorded source-state identity. Cleanup then removes the unused recovery point and
materialization and commits `abandoned`. A target or third identity fails closed as
`recovery_conflict`; absence or an unreadable identity remains retryable. The same per-System lock
serializes abandonment with activation and teardown.

Local-libvirt adapts its existing staging and direct-kernel XML behavior behind these operations.
Remote-libvirt uploads per-System/per-Run kernel and optional initrd artifacts, resolves provider-local
paths internally, records its disk/GRUB recovery point, and activates direct-kernel XML without
changing the disk overlay, networking, guest-agent channel, console, gdbstub, or capture devices. A
test-only non-libvirt implementation consumes the shared value types and returns opaque references;
it proves the boundary contains no libvirt type without claiming that the shape is sufficient for
HTTP/iPXE.

## Consequences

- External Run boot has one artifact-pair and command-line meaning across providers. Remote initial
  provisioning remains disk/GRUB boot, and existing images without root provenance remain usable on
  that path.
- Catalog-attested root provenance requires a typed extension to the existing attestation model and
  its serialized inventory shape. Old records remain readable but cannot authorize external boot
  until restaged, rebuilt, or explicitly re-attested with the new versioned fields.
- Recovery stores the exact state being replaced, including a same-release module tree, so
  configuration drift cannot silently change the rollback target. Provider-specific recovery bytes
  require bounded storage, tenant ownership, redaction, retention, and reaping behind the provider
  seam.
- Core gains durable activation state and reconciliation work. This is necessary because provider
  activation and database commits cannot share a transaction.
- Retries compare immutable plan and materialization identities. A reused object key with another
  version, digest, architecture, release, root specification, initrd pairing, or module obligation
  is rejected rather than overwritten.
- Module source identity and installed identity are distinct. The portable source digest covers
  validated bundle input; the installed digest includes provider-generated indexes and is what
  activation and recovery compare.
- A provider-side change outside KDIVE's System lock is preserved as `recovery_conflict`. Recovery
  is therefore fail-closed and may require an operator to choose between the recorded point and the
  newly observed definition.
- Libvirt state identity compares the canonical full preserved inactive definition plus the three
  external-boot fields and module-tree content, never live XML. External boot still requires the
  running-kernel identity proof; GRUB recovery requires fresh-boot readiness because its bootloader
  selection is not part of the inactive definition.
- ADR-0082's in-guest GRUB install remains the provisioning/recovery mechanism but no longer defines
  iterative remote Run boot once this decision is implemented.

## Considered & rejected

- **Keep provider-specific Run boot and defer a shared contract until a bare-metal provider exists.**
  judgment: issue #2105 requires the same finalized external build to have one meaning across both
  current libvirt providers and a non-libvirt boundary proof now; deferral leaves the existing
  kernel/initrd divergence and root ambiguity intact.
- **Add remote direct-kernel rendering but leave recovery implicit in disk/GRUB provisioning.**
  judgment: a worker death between provider activation and its database commit leaves no durable
  fact that distinguishes the intended external definition from the definition to restore, so this
  narrower adapter cannot meet the required crash-consistent recovery behavior.
- **Re-render disk/GRUB recovery state from the current profile and provider configuration.**
  verified: `src/kdive/providers/remote_libvirt/lifecycle/xml.py` renders network, machine, storage,
  gdbstub, SSH-forward, console, and guest-agent settings from live configuration, while teardown
  already reads provider facts from domain XML to survive configuration drift. Re-rendering later
  can therefore produce a different definition from the one activation replaced.
- **Store libvirt XML in the shared boot contract.** judgment: this makes a provider-neutral seam
  carry one provider's transport and prevents the non-libvirt boundary proof the issue requires.
- **Treat the combined kernel/modules bundle as the bootable kernel.** verified:
  `src/kdive/providers/local_libvirt/lifecycle/install.py` extracts `boot/vmlinuz` before assigning
  the direct-kernel XML `<kernel>` path; the bundle is an archive, not executable kernel bytes.
- **Give kernel and initrd independent activation identities.** judgment: independent identities
  permit a valid artifact from one finalized build to be paired with another and cannot enforce the
  issue's paired-artifact requirement.
- **Keep remote iterative Run boot on the in-guest GRUB helper.** judgment: it regenerates caller
  initrd bytes and preserves an implicit root command line, so the same finalized external build
  cannot have one meaning across providers.
- **Add a standalone System-profile INITRD input.** verified: closed PR #2104 demonstrates that
  shape and rejects it for remote-libvirt; issue #2105 and epic #1423 explicitly exclude the surface
  in favor of the existing external Run-build lane.

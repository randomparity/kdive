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
durable recovery point representing the exact currently defined boot configuration, renders but does
not apply the target definition, and returns provider-computed source- and target-definition
identities plus an opaque recovery reference. For remote-libvirt, that point contains the exact
inactive disk/GRUB domain definition, stored behind the provider seam; the shared state never
interprets its XML. Deterministic identifiers make repeated prepare calls for the same System, Run,
plan identity, and current definition return the same point and target identity.

Core persists the plan identity, materialization reference, recovery reference, both provider
definition identities, and activation state before calling activate. The state machine is
`prepared -> activating -> active`, with
`activating -> recovering -> recovered`, `activating|recovering -> recovery_conflict`, and failure
metadata on an operation attempt. Transitions and provider calls run under the existing per-System
advisory lock. Activation is compare-and-set from the recovery point's source-definition identity:
the provider refuses a changed definition or a materialization/recovery reference belonging to
another System, Run, or plan. On an `activating` record after worker loss, reconciliation compares
the provider-observed definition identity with both recorded provider identities. The target identity
completes `active`; the source identity completes `recovered`. Recovery may replace only the target
identity with the recorded source definition. Any absent, unreadable, or third identity enters
`recovery_conflict` for operator resolution instead of overwriting provider state. The portable plan
identity is never compared directly with provider definition bytes, and readiness is never used to
guess which definition won.

Recovery restores the recorded point before declaring the System usable. The recovery point remains
until the Run is terminal and no recovery is in flight. Materialized artifacts remain while the Run
can retry, are deleted idempotently on Run/System teardown, and are swept by deterministic ownership
after worker death. A partial materialization is either atomically published under its final identity
or discoverable as an owned partial and removed; it is never activated.

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
- Recovery stores the exact state being replaced, so configuration drift cannot silently change the
  rollback target. Provider-specific recovery bytes require bounded storage, tenant ownership,
  redaction, retention, and reaping behind the provider seam.
- Core gains durable activation state and reconciliation work. This is necessary because provider
  activation and database commits cannot share a transaction.
- Retries compare immutable plan and materialization identities. A reused object key with another
  version, digest, architecture, release, root specification, initrd pairing, or module obligation
  is rejected rather than overwritten.
- A provider-side change outside KDIVE's System lock is preserved as `recovery_conflict`. Recovery
  is therefore fail-closed and may require an operator to choose between the recorded point and the
  newly observed definition.
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

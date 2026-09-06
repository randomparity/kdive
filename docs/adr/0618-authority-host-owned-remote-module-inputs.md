# 0618 — Authority host owns remote module appliance inputs

## Status

Accepted (2026-09-06)

## Context

Remote PREPARE crosses from a worker that holds a read-only attempt verifier to an authenticated
provider authority that owns the fixed libvirt binding. A worker-supplied root-volume key,
appliance image, host path, or remote monotonic deadline would either extend worker authority or
misrepresent a value whose clock is local to another process.

The installed remote module appliance is a digest-verified direct-kernel bundle: fixed
architecture-specific `vmlinuz` and `initramfs.cpio` assets, plus the System root, source, and
scratch volumes. It is not a pool appliance volume.

## Decision

The authority host derives the System overlay volume and its identity from its fixed libvirt
binding, and derives the appliance digest by rereading the installed verified bundle manifest.
It persists the resulting closed operation descriptor with the begin receipt and accepts execute
only when it exactly matches that descriptor. The worker receives that descriptor with the receipt
and verifies its System, Run, plan, and source-manifest fields before retaining its ADR-0605
verifier through completion.

The authority converts the caller's bounded budget to its own monotonic deadline once at begin and
reuses that deadline for exact replay. A worker monotonic timestamp is never compared directly to
the authority host clock.

## Consequences

Provider authority provisioning must supply only fixed appliance assets and the fixed libvirt
binding. The remote appliance renderer uses direct kernel/initrd boot and its three owned disks;
it does not require a phantom appliance pool volume. Worker jobs retain no provider path, host
credential, root-volume selector, or appliance selector.

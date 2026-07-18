# ADR 0386 — Live-test framework and arch-additive runner topology

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-18
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

The `live_vm` test tier — boot a real libvirt domain, run a provider operation
against it, tear it down — is the only automated coverage of the product's
core boundary: booting a real kernel, crashing it, and introspecting the
result. Fakes cannot reach this boundary. Today the tier has two failures:

1. **No shared harness.** Roughly ten provider tests each re-derive the same
   boot / wait / teardown sequence, the panic-wait loop exists in three copies,
   and the environment knowledge that makes any of it work (`qemu:///session`,
   short `XDG_RUNTIME_DIR`, modular libvirt daemons, SELinux `virt_image_t`)
   lives only in one test file and in maintainer memory. Each new live test
   relearns it.
2. **It never runs.** The `live-vm` CI job is `workflow_dispatch`-only, stages
   no guest image, and sets no environment, so it skips even when triggered.
   The core boundary ships with no gate.

The project's primary target is ppc64le. x86_64 is a cost-effective
proof-of-concept phase. The design must therefore treat processor architecture
as a first-class, additive dimension, not an afterthought.

This ADR governs the epic
[Live-test framework](../design/2026-07-18-live-test-framework.md) and the
top-level test-strategy decisions it implies; it has no single implementing PR
and is Accepted once the topology it describes has landed.

## Decision

We will build a thin, arch-parameterized `live_vm` harness that is the single
reusable way to boot a throwaway libvirt domain, wait for a condition
(`active` / `panic` / `ssh`), and tear it down, and that fixes an explicit
**environment contract** (resolved libvirt mode, env vars, `XDG_RUNTIME_DIR`,
daemon model, per-environment guest confinement, staged image + debuginfo
location) shared by every live-VM test. The `live_vm` marker spans two families
— throwaway-domain tests served by the harness, and provisioned-System tests
that need a live stack plus the `KDIVE_S3_*` object store — so the contract and
the nightly distinguish them and fail loud on missing required env rather than
skipping a family to green.

We will run the live tiers on an **arch-additive** topology: emulated
`live_vm_tcg` on a hosted `ubuntu-latest` runner for breadth, and native-KVM
`live_vm` on per-arch self-hosted runners for depth. These are two vehicles, not
one harness: `live_vm_tcg` rides the existing live-stack spine (ADR-0353), which
already resolves KVM-vs-TCG from the host arch and — needing no `/dev/kvm` — runs
on the hosted runner once the compose backends + S3 are up; the new
`boot_throwaway_domain` harness serves only the throwaway-domain `live_vm`
family. Self-hosted runners are selected by arch label (`[self-hosted, kvm, x64]` now,
`[self-hosted, kvm, ppc64le]` as the target drop-in), both on Rocky Linux 10,
so adding an architecture is a new label, matrix entry, and an arch branch in
the harness's domain-XML builder (machine type, console device, kernel format) —
not a topology rewrite.

## Consequences

Easier:

- New live-VM tests call one harness instead of re-deriving boot boilerplate,
  and the environment quirks are applied automatically.
- The product's core boundary gains a real gate: TCG on every pull request or
  nightly, native KVM nightly on the self-hosted host.
- Adding the ppc64le runner is additive — same runner-selection topology, an
  arch branch the harness carries (machine type, console, kernel format), new
  arch label — so the primary target is unblocked by construction.

Harder / new obligations:

- A self-hosted runner must be provisioned and kept healthy (Rocky Linux 10,
  libvirt/qemu/drgn/crash, staged images, matching debuginfo, warm persistent
  storage). Codified as reproducible host setup, not tribal knowledge.
- The self-hosted job must be gated to `schedule` + `workflow_dispatch` and kept
  off fork pull requests, because self-hosted plus fork PR is arbitrary code
  execution on the host. The `KDIVE_S3_*` credentials for the provisioned-System
  family ride repo/organization secrets, which those trusted events can read.
- The nightly must declare which `live_vm` families it runs and fail loud on
  missing required env, or it reports green while skipping a whole family.
- Guest-image and debuginfo provisioning becomes a maintained input to CI — a
  self-hosted warm store and a separate hosted-runner image set that fits 14 GB.

No database migration: this is test infrastructure only.

## Alternatives considered

- **Delete the `live_vm` tier.** Rejected: it is the only automated coverage of
  the boot/crash/introspect boundary that is the product's reason to exist.
  Deleting it leaves that boundary permanently unverified for a debugging tool.
- **Keep the tier but leave it dispatch-only and manual.** Rejected: an
  always-skipped suite is not a gate, rots between manual runs, and reads as
  coverage in a green run when it is none.
- **Run the KVM tier on hosted runners.** Rejected on the binding ground that
  GitHub documents nested virtualization as unsupported and experimental and
  KVM/libvirt is reported to fail there (software emulation is the only reliable
  hosted path), and native-silicon depth needs real hardware regardless. Disk is
  not the deciding factor — a vmcore and its debuginfo are the same size under
  either accelerator, and the hosted runner's larger `/mnt` scratch can stage big
  images — so the hosted runner is kept for the TCG tier, where no `/dev/kvm` is
  needed.
- **x86-only harness now, generalize later.** Rejected: retrofitting arch into a
  hardened harness is the expensive path, and ppc64le is the primary target.
  Parameterizing arch from the start makes the POWER runner a drop-in.

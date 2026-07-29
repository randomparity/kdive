# 0484 — Guest images ship SELinux permissive, as one accepted posture on both build paths

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** [#1632](https://github.com/randomparity/kdive/issues/1632)
- **Reconciles:** [ADR-0251](0251-local-multidistro-rootfs-catalog.md) (the `guest_mac` provenance
  field), [ADR-0287](0287-per-distro-capability-tags.md) (`guest_mac = "selinux-permissive"` → the
  `selinux` capability tag) and [ADR-0345](0345-unified-customization-boot.md) (the offline seal's
  `selinux=` path — permissive tolerates the repack-dropped labels, so `normalize` withholds
  `/.autorelabel` and the *provision* boot is what relabels). None of those
  decisions changes; this ADR records the second, independently-introduced permissive site and
  states the single posture both sites now express.

## Context

kdive's guests ship with `SELINUX=permissive` in `/etc/selinux/config`. That is true in **two
places, introduced separately, for two different reasons**, and until now neither was recorded as a
posture decision — one was an implementation detail of a repack, the other a bug fix.

**Site 1 — the local rootfs build pipeline.** `kdive.images.families.rhel` writes
`SELINUX=permissive` twice (`_SELINUX_PERMISSIVE_SED` in `customize_steps`,
`_SELINUX_PERMISSIVE_CONFIG` in `normalize`). The reason is mechanical: the tar → bare-ext4 repack
drops SELinux xattrs, so every file in the image is unlabeled. `normalize` touches `/.autorelabel`
so the first boot runs `restorecon`, and permissive is what lets that boot get far enough to do it
— under enforcing, an image with no labels denies its way to a dead boot, including the
host-written `authorized_keys`. This is declared: the build pipeline records
`guest_mac = "selinux-permissive"` as provenance (ADR-0251) and derives the ADR-0287 `selinux`
capability tag from it, so `images.describe` reports the posture. `rootfs_build.py` also passes
`selinux=family.guest_mac.startswith("selinux")` into the offline seal (ADR-0345).

**Site 2 — the Ansible-built remote base image.** `deploy/ansible/roles/guest_base_image/tasks/`
`build_one.yml`'s "Relax SELinux to permissive" task applies the same `sed` via `virt-customize`.
The reason is different: kdive drives the remote guest through **`guest-exec`**, so every helper
inherits the confined `virt_qemu_ga_t` domain. Until #1610 this task ran only in the `virt-builder`
branch, which is why the `cloud-image` builds (rocky, ubuntu) stayed enforcing and could never
complete an install; commit `4dc795e6b` hoisted it out of that branch. Its `when:` is deliberately
**not** source-gated, so it now runs for all three sources (`virt-builder`, `cloud-image`,
`scratch`), guarded only by `if [ -f /etc/selinux/config ]` — which makes it a no-op on
Debian-family images, whose MAC is AppArmor (ADR-0287, `guest_mac = "apparmor"`).

### The confinement problem is larger than one denied `connect()`

The issue frames site 2 as a workaround for a single denied syscall: `kdive-install-kernel` fetches
the kernel over a presigned object-store URL, `virt_qemu_ga_t` denies outbound `connect()`, and the
install fails as `install_failure`. That is real, and it is genuinely opaque — the domain is
confined enough that `security_getenforce()` is denied too, so the helper cannot even report that
SELinux is the cause.

**But it is not the whole problem, and treating it as such would make the targeted fix look like a
drop-in replacement that it is not.** The repo's own runbook already records a second, independent
denial: `virt_qemu_ga_t` **cannot read `/lib/modules`**, which the install helper writes along with
`/boot`. And the helper is not a leaf — it runs `depmod`, `dracut` and `grubby`, whose execution
**transitions to other SELinux domains**. Three helpers (`kdive-install-kernel`,
`kdive-capture-vmcore`, `kdive-drgn`) perform privileged system mutation this way. So the
confinement surface is: at least two distinct denials in `virt_qemu_ga_t` itself, plus an unbounded
set in the child domains the helpers transition into. Any per-domain relaxation of
`virt_qemu_ga_t` alone — including `semanage permissive -a virt_qemu_ga_t`, which the runbook
previously suggested as a "keep the rest enforcing" option — does not cover those children.

The relevant question is therefore not "is one boolean cheaper than image-wide permissive", but
"is there a relaxation that covers the transitions, and can we prove it here". This ADR answers:
there is a credible candidate, and no, we cannot prove it in this change.

## Decision

### 1. Permissive is the accepted posture for kdive guests, on both build paths

Not a workaround pending a fix, and not an oversight: an accepted posture, for the reason that
these are **disposable crash-test guests whose entire purpose is to be panicked and captured**.
They are single-tenant, provisioned per-System as an overlay on the base, and torn down after the
run. They run kdive's three helpers and a kernel under test; they hold no user data and serve no
production traffic. MAC confinement inside such a guest protects nothing kdive is trying to
protect, while costing every install and capture path a class of opaque, after-the-fact failures.

The two sites keep their two mechanisms — the local repack needs permissive at first boot to
relabel at all, the remote base image needs it so `guest-exec`-spawned helpers can mutate the
system — but they are hereby **one stance**, not two accidents. Where they differ is only in *what*
forces permissive, not in *whether* it is intended.

### 2. The remote task stays un-source-gated

`build_one.yml`'s permissive task deliberately applies to `virt-builder`, `cloud-image` and
`scratch` alike. Source-gating it is exactly the bug #1610 fixed: the `cloud-image` guests
inherited enforcing and failed every install, and nothing in the failure named SELinux. The
`if [ -f /etc/selinux/config ]` guard is the correct discriminator — it keys on whether the image
*has* SELinux at all, which is the only property that matters here, and leaves AppArmor images
untouched.

### 3. The security cost, stated plainly

**A kdive guest runs with mandatory access control disabled.** Nothing inside it is confined by
SELinux: not the guest agent, not kdive's three helpers, not the kernel under test, not anything
else the operator installs into the base image. Denials are logged (permissive, not disabled) but
nothing is enforced.

Three specific consequences, none of which this ADR closes:

- **The base image is long-lived even though the Systems are not.** The disposability argument
  covers the per-run overlay; the operator-staged base qcow2 sits on the remote host indefinitely
  and every System built from it inherits the posture.
- **The kernel under test is untrusted-by-construction.** kdive exists to run kernels that crash.
  An unconfined guest gives such a kernel — and anything that reaches it — the full guest.
  Containment for kdive is the *virtualization* boundary and the host's own posture, not the
  guest's MAC. That boundary is where remote-host hardening belongs.
- **The posture is invisible on the remote path.** The local pipeline declares it
  (`guest_mac` provenance, ADR-0287 `selinux` capability tag, surfaced by `images.describe`). The
  Ansible-built remote base image carries no provenance record at all — `guest_mac` is written only
  by `providers/local_libvirt/rootfs_build.py` — so an operator inspecting a remote staged image
  cannot see the posture from kdive. This asymmetry is recorded, not fixed; closing it means giving
  operator-staged images a provenance channel, which is a larger change than this ADR.

### 4. Documentation says it once

The rationale had two prose homes — the role's inline comment and
`docs/operating/runbooks/remote-libvirt-host-setup.md` §5 — which had already drifted apart: the
role's comment named the `connect()` denial, the runbook named the `/lib/modules` denial, and
neither named both, so each read as a complete explanation while contradicting the other's scope.

The runbook keeps the one `sed` line its manual `virt-builder` recipe genuinely needs, cites this
ADR for the reasoning instead of restating it, and states that the Ansible role already applies the
same change so the manual step is a manual-path instruction only. The role's comment keeps the
mechanism it sits next to — it is the closer-to-code home — corrected to name both denials and the
child-domain transitions, and cites this ADR for the posture. Neither the behavior, the task's
`when:` condition, nor the manual recipe changes.

The runbook's suggestion to "instead try `semanage permissive -a virt_qemu_ga_t`" is **removed**
rather than kept: per the Context, it does not cover the child-domain transitions, and the runbook
itself said so in the same paragraph. Advice the repo contradicts two sentences later is worse than
no advice; the future path below replaces it.

### 5. Targeted unconfinement is the named future path, gated on live verification

If kdive later wants enforcing guests, the candidate is **not** a per-domain permissive and **not**
a hand-written policy module for one `connect()`. It is the pair already present in stock Fedora
targeted policy:

- the boolean **`virt_qemu_ga_run_unconfined`**, and
- the file type **`virt_qemu_ga_unconfined_exec_t`** applied to `/usr/local/sbin/kdive-*`.

Unlike per-domain permissive, this is intended to let the agent run a designated executable
*outside* the confined domain, which is the shape that would cover the `depmod`/`dracut`/`grubby`
transitions rather than stopping at `virt_qemu_ga_t`'s boundary.

**Precondition, stated as a precondition and not as a plan:** those semantics must be *verified*,
not assumed. Adopting them requires a real base-image build with the helpers installed and
relabeled, and a full `runs.install` + capture + live-drgn pass over `guest-exec` against a remote
`qemu+tls` host, on each guest family in the catalog (the boolean and type are Fedora/RHEL-family;
Rocky and CentOS Stream ship the same targeted policy, Ubuntu is AppArmor and out of scope). This
campaign has no such host and did not run that proof, so switching now would trade a posture that
is proven end-to-end for one that is merely plausible. That is the reason this ADR keeps
permissive, and the exact thing a future change must supply to reverse it.

## Consequences

- The posture is recorded once, in one place, for both sites. A later reader finds a decision
  rather than inferring an oversight from a `sed` in an Ansible task.
- Nothing about how images are built changes. No behavior, no task ordering, no `when:` condition,
  no code. The diff is an ADR, a runbook edit, an index row and one comment line.
- The security cost is now written down in the terms an operator evaluating kdive for their
  environment needs: MAC is off inside the guest, the base image outlives the Systems, and the
  isolation kdive relies on is the hypervisor boundary. An operator who cannot accept that has the
  information to say so before deploying, instead of discovering it from a `sed`.
- The remote path's missing provenance is an acknowledged gap. `images.describe` tells the truth
  about locally built rootfs images and says nothing about operator-staged ones.
- Reversing this needs the live proof named in decision 5, not an argument. That is deliberate:
  the failure mode of a wrong answer here is an opaque post-crash failure with the evidence gone,
  which is the same failure mode #1610 spent five kernel rebuilds inside.

## Alternatives considered

- **Switch now to `virt_qemu_ga_run_unconfined` + `virt_qemu_ga_unconfined_exec_t`.** The right
  long-term shape, and the reason it is decision 5's future path rather than this ADR's decision:
  it cannot be proven without a base-image build and a `guest-exec` install on a real remote
  `qemu+tls` host, which this campaign does not have. Shipping it unproven would replace a
  verified-working posture with an untested one whose failures land after the crash.
- **`semanage permissive -a virt_qemu_ga_t` (per-domain permissive).** Superficially the "keep the
  rest enforcing" answer, and what the runbook used to suggest. It relaxes exactly one domain,
  while the helpers' `depmod`/`dracut`/`grubby` children transition into others — so it is likely
  insufficient, and its insufficiency shows up as the same opaque post-install failure. It also
  needs `policycoreutils-python-utils` in every base image.
- **A hand-written policy module allowing `virt_qemu_ga_t` outbound `connect()`.** Fixes the one
  denial the issue names and leaves the `/lib/modules` denial and every child-domain transition
  in place. It would take the issue's premise at face value, which is the specific error this ADR
  set out to correct.
- **Source-gate the Ansible task back to `virt-builder` only.** Restores the #1610 bug verbatim:
  `cloud-image` guests silently return to enforcing and fail every install.
- **Leave it undocumented.** The status quo the issue objects to, and the weakest option: the two
  sites had already drifted into two incompatible explanations of the same posture, and a reader of
  either one would have concluded the other was a mistake.

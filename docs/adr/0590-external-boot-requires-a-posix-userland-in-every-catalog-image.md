# 0590 — External boot requires a POSIX userland in every catalog image

## Status

Accepted (2026-09-02)

## Context

ADR-0583 requires external boot to observe the running guest's architecture, `uname` release, and
GNU build ID, and states that nothing else substitutes for it. It names no in-guest binaries.
Neither does any other accepted decision: ADR-0188 §4 describes the in-guest contract as
`qemu-guest-agent`, the family helpers, and `curl`/`tar` under systemd, and stops there.

**How that observation is taken is #2110's design, not merged code.** Its activation design spawns
`/usr/bin/uname` and `/usr/bin/cat` through a `guest-exec` allowlist keyed on absolute paths. No
such call exists in `src/` at the time of this decision, and #2110's own spec records the
two-program allowlist as a residual it chose, with a single purpose-built helper as the live
alternative. This record binds to those two paths because that is the design the platform is
building toward.

#2160 asked whether `bare-kdive-remote-base` can satisfy the observation. Its scratch recipe passes
`--setopt=install_weak_deps=False`, names `busybox`, and never names `coreutils` on either the dnf
or the debootstrap path. The binaries are nonetheless present, through hard dependencies the recipe
does not name — on Fedora 44 `systemd` carries `Requires: coreutils`, and both paths are
`coreutils` files; on the Debian path `debootstrap --variant=minbase` installs the
`Priority: required` set, of which `coreutils` is a member.

So every catalog image satisfies the requirement today and none declares it. On the dnf path that
rests on `systemd`'s transitive dependency graph, which upstream systemd has been shrinking, and
nothing would notice its removal; on the Debian path `coreutils` is Essential, so the fact is
guaranteed but still unstated. Either way the failure mode is badly placed: an image that lost the
binaries builds, stages, and registers clean, then fails per Run at identity proof on the readiness
deadline, several layers from the cause.

The requirement is also **image-wide**, since identity proof runs against whatever image backs the
System — so a rule scoped to the scratch entry would leave the other three unaddressed.

## Decision

We will require every image in `kdive_image_catalog` to carry `/usr/bin/uname` and `/usr/bin/cat`
as executable files, and hold the build to it rather than assume it.

1. **Every catalog image carries a POSIX `uname` and `cat` at those two absolute paths**, alongside
   the `qemu-guest-agent`, family helpers, and `curl`/`tar` that ADR-0188 §4 already requires. The
   paths are part of the contract, not just the programs: the allowlist is keyed on absolute paths,
   so an image exposing the applets only at `/usr/sbin/busybox` fails the proof while appearing to
   have `uname` and `cat`. `bare-kdive-remote-base` is the entry that surfaced this, but the
   constraint binds every entry and was written down nowhere before this record.

   **This does not reopen ADR-0188 §4.** ADR-0481 reserved §4's userland *composition* — whether
   busybox is the userland — for "its own decision taken with a build host in hand", and no such
   host exists yet. Composition is a different axis from requirement, and this record moves only the
   second: it adds an obligation that §4's existing composition already satisfies on every build
   path, and removes nothing. busybox stays in the scratch installroot, and the produced image is
   unchanged. If a later decision wants to *drop* busybox, ADR-0481's reservation still governs it.
2. **The scratch recipe names `coreutils` explicitly**, on both the dnf and the debootstrap path.
   The *declaration* is scoped to that recipe because scratch is the only rootfs this repository
   composes — the other three entries take their userland from a base image this repository
   downloads rather than assembles, so naming a package there would reinstall a present one and pin
   nothing. Those three rest on item 3 alone. Naming it here is contents-neutral against both
   scratch paths as they stand, and keeps the fact true when an implicit supplier stops supplying
   it.
3. **The build verifies the contract on each qcow2 it produces, immediately before staging it.**
   Every build path is checked, not only scratch. An image that fails *this build* is never copied
   into the storage pool, so it never becomes a confirmed volume, never reaches an `[[image]]`
   block, never backs a System, and cannot reach Run activation.

`bare-kdive-remote-base` is therefore **admissible** for external-boot Runs.

## Consequences

- The requirement is enforced where it is knowable. No later *metadata* gate could check it: the
  catalog, `systems.toml`, and the `image_catalog` row all carry claims, not contents, and
  `Capability` tags are explicitly build *claims* rather than verified facts. An in-guest probe over
  the `guest-exec` seam could check contents directly, and is declined as out of surface rather than
  impossible — see the rejection list.
- **The check runs only on a build.** It carries the staging copy's guard, because the local qcow2
  it inspects exists only when a build ran. So it does not fire for *any* already-staged volume —
  not merely those staged before this decision, but every one on every later run, until
  `force_image_rebuild=true`. Nothing records which images have been checked: no marker in the
  image, no fact, no field in the `[[image]]` block. Accepted rather than engineered around, for
  the reason above — past the build, a marker would be another claim rather than a check.
- Verification runs under `virt-customize`, so it boots an appliance and **writes to the image it
  verifies**: `virt-customize` resets a random seed and relabels for SELinux on every invocation.
  The task is `changed_when: false` because it asserts rather than customizes; that describes its
  contract, not the qcow2's bytes. It also inherits `virt-customize`'s OS-inspection requirement.
- This record binds to two paths #2110's design chose and has not merged. If #2110 lands naming a
  different program, the build gate verifies paths nothing reads, and this record must be superseded
  rather than quietly reinterpreted.
- **The residual is uncovered, and accepted as such.** For an image staged before this decision, one
  staged and never rebuilt, or one built outside `guest_base_image`, nothing checks the contract.
  #2160's Non-goal paragraph describes a #2110 exit-127 mitigation covering that case; #2110's
  design disclaims it. That seam never uses a shell — `guest-exec` spawns the program directly — so
  a missing program fails the spawn and surfaces as a retryable `TRANSPORT_FAILURE` that retries to
  the readiness deadline, with the same signature as an unreachable agent. #2110's spec says so
  outright: "it is not a mitigation either, and this design claims none." So such an image degrades
  to an indistinguishable retry-until-deadline, and this decision does not change that.
- A build host now fails loudly and early on an image it would previously have staged — the
  intended trade against one `READINESS_FAILURE` per Run at the readiness deadline.
- The scratch path stays `UNVALIDATED`. Nothing here is confirmed against a built scratch image: no
  scratch-capable host exists in CI or on available hardware, and the dependency reasoning above is
  read off packaging metadata and Debian policy. The first real `playbooks/image.yml` run turns the
  new task from an assertion into evidence, and is the first thing that could falsify the reasoning.
- ADR-0188 is **not** edited to carry a forward amendment note, though ADR-0481 set that precedent
  in the same §4. A merged record takes only a supersession banner, and this supersedes nothing.
  This record is reachable from the code that implements it — the recipe comments, the catalog
  entry, and `deploy/ansible/README.md` all name it — but §4 read on its own still describes the
  older, shorter contract, and nothing there points here.

## Considered & rejected

- **Exclude `bare-kdive-remote-base` from the external-boot catalog.** verified: the premise does
  not hold. On the dnf path, `rpm -q --requires systemd | grep coreutils` on Fedora 44
  (systemd-259.8-1.fc44) returns `coreutils` — a hard `Requires`, absent from `--recommends`, so
  `install_weak_deps=False` does not exclude it — and `rpm -qf /usr/bin/uname /usr/bin/cat` returns
  `coreutils-9.10-5.fc44.x86_64` for both. On the Debian path, `coreutils` is **Essential** (Debian
  Policy §3.8, `packages.debian.org/trixie/coreutils`), and `debootstrap`'s own scripts derive
  minbase's set from `Priority: required` plus apt
  (`/usr/share/debootstrap/scripts/debian-common`, debootstrap 1.0.140). Excluding the image would
  remove a capability it has.
- **Do nothing; let a non-conformant image fail at identity proof.** verified: there is no failure
  to rely on that names the cause. #2110's spec ("Failure behaviour") states it adds no special case
  for a missing program and "claims none" as a mitigation; a missing program fails the `guest-exec`
  spawn and retries to the readiness deadline as `TRANSPORT_FAILURE`. So the null option costs a
  per-Run stall with the same signature as an unreachable agent, against a fact the build already
  knows.
- **Name `coreutils` and stop, without the per-build check.** judgment: the declaration reaches only
  the scratch installroot, which is the one rootfs this repository composes. The other three entries
  would be left with no control at all, and the scratch pin would still be unproven until a host ran
  it. The check is what makes the claim falsifiable.
- **Probe the guest over the `guest-exec` seam at System registration or first boot.** judgment: it
  would genuinely cover the residual the build check cannot — an already-staged volume, or an image
  built outside this role — by observing contents rather than metadata, and would move the failure
  from per-Run to per-registration. Declined as surface: that seam is #2110's and unmerged, and
  `src/kdive/providers/remote_libvirt/lifecycle/` is outside this change. Worth revisiting once
  #2110 lands.
- **Also name `coreutils` in `kdive_image_defaults.packages` and the ubuntu override**, so all four
  entries declare it. verified: those lists are passed to `virt-builder --install` and
  `virt-customize --install`, which run the *image's* package manager against a base image this
  repository downloads; `coreutils` is already installed in every such image, so the entry would
  reinstall a present package and pin nothing about the base.
- **Declare external-boot eligibility as an image `Capability` tag and gate at admission.**
  verified: `Capability` is documented in `src/kdive/domain/catalog/images.py:17-23` as "a build fact
  — the tooling is present — not a liveness guarantee", and
  `roles/remote_libvirt_facts/templates/systems_toml_block.j2` emits no `capabilities` key at all,
  so the gate would read an empty list for every remote image.
- **Assert the contract in the `guest_base_image` admission block.** judgment: admission runs over
  catalog metadata before any image exists, so it could assert only that an entry *claims*
  conformance.
- **Drop `busybox` now that `coreutils` is named.** judgment: out of scope. ADR-0481 reserved
  exactly this, and Decision item 1 states why the present change stays on the other side of that
  reservation.
- **Verify with `guestfish --ro` or `virt-ls` instead.** verified: it would avoid the write recorded
  above — `virt-customize` changed a test qcow2's sha256 on a check that only ran `test -x` — but
  neither returns non-zero for an absent path, so the task would parse stdout and carry its own
  `failed_when`. judgment: a new failure shape for a write that resets a random seed on an image the
  same play built four `virt-customize` invocations ago. Reconsider if a later change needs the
  image bit-stable across verification.

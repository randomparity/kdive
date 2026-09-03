# 0590 — External boot requires a POSIX userland in every catalog image

## Status

Accepted (2026-09-02)

## Context

ADR-0583 requires external boot to observe the running guest's architecture, `uname` release, and
GNU build ID, and states that nothing else substitutes for it. It names no in-guest binaries.
Neither does any other accepted decision: ADR-0188 §4 describes the in-guest contract as
`qemu-guest-agent`, the family helpers, and `curl`/`tar` under systemd, and stops there.

**How that observation is taken is #2110's design, not merged code.** Its activation design spawns
`/usr/bin/uname` and `/usr/bin/cat` through a `guest-exec` allowlist keyed on absolute paths; `rg`
over `src/` finds neither path, so nothing merged reads them yet. #2110
considered and **rejected** the obvious alternative — an `observe` subcommand on the in-guest
`kdive-install-kernel` helper — because that helper ships in the base image, so it would reach only
re-imaged guests and an existing System would fail identity proof for a deployment reason. What
#2110 records as a residual is narrower: that the allowlist names two general-purpose binaries
rather than one purpose-built helper. So the two paths are a settled choice on an unmerged branch,
not an open one.

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
as executable files, and hold both the build and the provisioning run to it rather than assume it.
The two checks sit at the two points where the fact is knowable and still cheap to act on: the
qcow2 a build produces, and the pool volume a render is about to declare.

1. **Every catalog image carries, as executable files at the absolute paths the external-boot
   identity proof spawns, the programs that proof runs** — currently `/usr/bin/uname` and
   `/usr/bin/cat` per #2110 — alongside the `qemu-guest-agent`, family helpers, and `curl`/`tar`
   that ADR-0188 §4 already requires. The general obligation is the contract; the two literals are
   the instance it has today. The *paths* are part of it, not just the programs: the allowlist is
   keyed on absolute paths, so an image exposing the applets only at `/usr/sbin/busybox` fails the
   proof while appearing to have `uname` and `cat`. `bare-kdive-remote-base` is the entry that
   surfaced this, but the constraint binds every entry and was written down nowhere before this
   record. Stating it generally means a change to #2110's chosen programs updates the enforcement
   task, not this decision.

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
4. **`remote_libvirt_facts` verifies each staged volume before declaring it an `[[image]]`.** Item
   3 alone is close to inert against the case that motivated this record. It carries the staging
   copy's guard — the local qcow2 it inspects exists only when a build ran — so on a host that
   skips the build, and on every `site.yml` run thereafter, it never fires. The volumes that
   worried us are exactly the ones it cannot see: staged before this decision, or built elsewhere.
   The stat that confirms a volume is present is the last point before it becomes a catalog row a
   System can select, so the check belongs there.

   Three properties make this safe to run against a *staged* volume, and they are requirements
   rather than preferences:

   - **Read-only.** Item 3's instrument, `virt-customize`, rewrites the image on every invocation.
     That is acceptable against a build workdir and not against a volume other Systems clone from
     — same check, different blast radius. `guestfish --ro` opens the disk read-only.
   - **Unprivileged**, like the stat beside it. A root-owned 0644 volume in a 0711 pool directory
     is readable without privilege, and libguestfs falls back to TCG where `/dev/kvm` is not
     available, so this adds no escalation to a `site.yml` task that has none.
   - **A failed inspection is not a verdict.** An inspected volume that lacks the programs is
     omitted from the fragment, exactly like an absent one. A volume that could not be inspected
     at all — no libguestfs, an unreadable pool, a disk with no recognisable OS — **fails the
     play**. Folding the second into the first would let one broken host emit an empty but valid
     fragment and break provisioning everywhere with nothing naming the cause. This mirrors the
     rule the volume stat already follows: a genuinely unreadable pool fails loudly rather than
     reporting a false absence.

   The verdict is cached against the volume's size and mtime and the program list's own identity,
   because an appliance launch is roughly two seconds per image and steady state must not pay it
   per image per run. Restaging a volume or changing the required programs moves the key, so a
   stale verdict is never inherited. The read-only property is what makes this work at all: three
   consecutive `guestfish --ro` runs left a test volume's sha256 *and* mtime byte-identical, while
   a single `virt-customize` changed its sha256 — an instrument that wrote would move the mtime
   its own cache key is built from and re-inspect every image on every run.

   The check refuses an **empty** program list rather than enforcing one. guestfish given no
   commands exits 0 with no output, which would read as "every volume conformant" — the one way
   this control could fail open, and the difference between a check that is absent and one that
   only appears to be there.

   **This half checks presence, not the execute bit.** `is-file` answers without failing on an
   absent path; reading the mode needs `stat`, which *fails* on one, collapsing "not there" into
   "could not look" — the single distinction this role must keep. Item 3 tests executability with
   `test -x` where a failure is unambiguous. The narrowing is deliberate and bounded: the shape
   this residual actually produces is an absent program, not a present non-executable one. It also
   follows symlinks, because a busybox image supplying `/usr/bin/cat` as a link to
   `/usr/sbin/busybox` **does** satisfy the proof — the kernel follows the link on exec.

`bare-kdive-remote-base` is therefore **admissible** for external-boot Runs.

## Consequences

- The requirement is enforced where it is knowable. No later *metadata* gate could check it: the
  catalog, `systems.toml`, and the `image_catalog` row all carry claims, not contents, and
  `Capability` tags are explicitly build *claims* rather than verified facts. An in-guest probe over
  the `guest-exec` seam could check contents directly, and is declined as out of surface rather than
  impossible — see the rejection list.
- **The build check runs only on a build**, because it carries the staging copy's guard: the local
  qcow2 it inspects exists only when a build ran. It does not fire for any already-staged volume
  until `force_image_rebuild=true`. That gap is why Decision 4 exists, and the stage-time check is
  keyed on the volume rather than on a marker in the image: past the build, a marker in the image
  would be another claim rather than a check.
- **`site.yml` now needs libguestfs on each managed host.** This is a new prerequisite on the
  provisioning path, not only on a build host, and a host without it fails the play by design
  rather than silently declaring nothing. `remote_libvirt_facts_userland_verify=false` turns the
  stage-time check off for an operator who accepts unverified volumes; it is an escape hatch, not
  a default, and it changes nothing about the build-time half.
- **The cache is on the managed host, per invoking user**, under
  `~/.cache/kdive/external-boot-userland`. Losing it costs one appliance launch per image, never
  correctness: an absent marker means the check runs, not that a volume passes.
- Verification runs under `virt-customize`, so it boots an appliance and **writes to the image it
  verifies**: `virt-customize` resets a random seed and relabels for SELinux on every invocation.
  The task is `changed_when: false` because it asserts rather than customizes; that describes its
  contract, not the qcow2's bytes. It also inherits `virt-customize`'s OS-inspection requirement.
- This record binds to two paths #2110's design chose and has not merged. If #2110 lands naming a
  different program, the build gate verifies paths nothing reads, and this record must be superseded
  rather than quietly reinterpreted.
- **The residual is now narrow rather than uncovered.** #2160's Non-goal paragraph describes a
  #2110 exit-127 mitigation covering the case of an image that reaches a Run unchecked; #2110's
  design disclaims it. That seam never uses a shell — `guest-exec` spawns the program directly —
  so a missing program fails the spawn and surfaces as a retryable `TRANSPORT_FAILURE` that
  retries to the readiness deadline, with the same signature as an unreachable agent. #2110's spec
  says so outright: "it is not a mitigation either, and this design claims none." #2160's
  Correction 2 draws the consequence this record adopts: build-time and stage-time verification
  are the only real defence, and this decision supplies both halves. What remains uncovered is a
  volume replaced in the pool *after* a render, without a subsequent `site.yml` run — the fragment
  then describes a volume that no longer exists in the form it was checked in. Nothing observes a
  pool between runs, and a check that ran per Run would be #2110's seam rather than this one.
- A build host now fails loudly and early on an image it would previously have staged — the
  intended trade against one `READINESS_FAILURE` per Run at the readiness deadline.
- The scratch path stays `UNVALIDATED`. Nothing here is confirmed against a built scratch image: no
  scratch-capable host exists in CI or on available hardware, and the dependency reasoning above is
  read off packaging metadata and Debian policy. The first real `playbooks/image.yml` run turns the
  new task from an assertion into evidence, and is the first thing that could falsify the reasoning.
- ADR-0188 **is** edited to carry a forward amendment note, as ADR-0481 did in the same §4. A
  merged record is append-only outside `## Status`, and `docs/adr/README.md` prescribes the append
  for exactly this case: a `### Amendment (YYYY-MM-DD)` block on the level-2 section a later
  decision qualifies. §4 enumerates what the guest owes and this record adds to that enumeration,
  so §4 read on its own would otherwise describe a contract that is no longer complete. The
  amendment states the obligation and points here; it does not restate the decision. This record is
  additionally reachable from the code that implements it — the recipe comments, the catalog entry,
  and `deploy/ansible/README.md` all name it.

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
  would cover the residual the build check cannot — an already-staged volume, or an image built
  outside this role — by observing contents rather than metadata. Declined as surface: that seam is
  #2110's and unmerged, and `src/kdive/providers/remote_libvirt/lifecycle/` is outside this change.
  Item 4 reaches the same residual from the provisioning side instead, without touching an unmerged
  seam and without needing the guest to be running. A live probe would still catch the one case
  item 4 cannot — a volume swapped after the render — so this stays worth revisiting once #2110
  lands, on a narrower question than it was declined on.
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
- **Reuse item 3's `virt-customize` check at stage time rather than a second instrument.**
  verified: it would inspect the right thing, and it writes to what it inspects — a random seed
  reset and an SELinux relabel on every invocation, observed as a changed sha256 on a test qcow2.
  Against a build workdir that is noise; against a pool volume that other Systems clone from it is
  a mutation of shared state performed by a read path. Rejected on that alone, before cost.
- **Assert the contract in the `guest_base_image` admission block.** judgment: admission runs over
  catalog metadata before any image exists, so it could assert only that an entry *claims*
  conformance.
- **Drop `busybox` now that `coreutils` is named.** judgment: out of scope. ADR-0481 reserved
  exactly this, and Decision item 1 states why the present change stays on the other side of that
  reservation.
- **Verify with `guestfish --ro` or `virt-ls` instead, in the build check (item 3).** verified: it
  would avoid the write recorded above — `virt-customize` changed a test qcow2's sha256 on a check
  that only ran `test -x` — but neither returns non-zero for an absent path, so the task would
  parse stdout and carry its own `failed_when`. judgment: a new failure shape for a write that
  resets a random seed on an image the same play built four `virt-customize` invocations ago.
  Note this is rejected **for item 3 only**. Item 4 adopts `guestfish --ro` precisely because the
  trade inverts against a staged volume: there the write is the disqualifying property and
  stdout-parsing is the acceptable cost. The property that makes it work — an absent program is a
  normal `false` on stdout with rc 0, while a failed inspection leaves stdout empty with rc 1 — is
  what lets item 4 keep "not there" and "could not look" apart.

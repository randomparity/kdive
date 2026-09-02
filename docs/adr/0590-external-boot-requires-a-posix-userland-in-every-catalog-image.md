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

So every catalog image satisfies the requirement today and none declares it. Four images rest on
one distro's transitive dependency graph, which upstream systemd has been shrinking, and on a
Debian priority level — nothing states the dependency and nothing would notice its removal. The
failure mode is badly placed too: an image that lost the binaries builds, stages, and registers
clean, then fails per Run at identity proof on the readiness deadline, several layers from the
cause.

The requirement is **path-keyed** — a busybox image carrying the applets only at
`/usr/sbin/busybox` does not satisfy an allowlist of absolute paths — and **image-wide**, since
identity proof runs against whatever image backs the System.

## Decision

We will require every image in `kdive_image_catalog` to carry `/usr/bin/uname` and `/usr/bin/cat`
as executable files, and hold the build to it rather than assume it.

1. **The contract extends ADR-0188 §4's in-guest contract.** Alongside `qemu-guest-agent`, the
   family helpers, and `curl`/`tar`, a conformant image carries a POSIX `uname` and `cat` at those
   two absolute paths. "Bare but conformant" now includes them; busybox stays in the scratch
   installroot.
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

- The requirement is enforced where it is knowable. Image contents are invisible to the catalog, to
  `systems.toml`, and to the runtime `image_catalog` row, so no later gate could check this — only
  restate the assumption. `Capability` tags are explicitly build *claims*, not verified facts.
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
  different program, the build gate verifies paths nothing reads, and this record must be
  superseded rather than quietly reinterpreted. #2110's exit-127 mitigation stays regardless: it
  covers what this cannot — an image staged and never rebuilt, or built outside `guest_base_image`.
- A build host now fails loudly and early on an image it would previously have staged — the
  intended trade against one `READINESS_FAILURE` per Run at the readiness deadline.
- The scratch path stays `UNVALIDATED`. Nothing here is confirmed against a built scratch image: no
  scratch-capable host exists in CI or on available hardware, and the dependency reasoning above is
  read off packaging metadata and Debian policy. The first real `playbooks/image.yml` run turns the
  new task from an assertion into evidence, and is the first thing that could falsify the reasoning.
- ADR-0188 is **not** edited to carry a forward amendment note, though ADR-0481 set that precedent
  in the same §4. A merged record takes only a supersession banner, and this supersedes nothing. A
  reader arriving at §4 reaches this record through the recipe comments, the catalog entry, and
  `deploy/ansible/README.md`, all of which name it. The cost: §4 read alone still describes the
  older, shorter contract.

## Considered & rejected

- **Exclude `bare-kdive-remote-base` from the external-boot catalog.** verified: the premise does
  not hold. `rpm -q --requires systemd | grep coreutils` on Fedora 44 (systemd-259.8-1.fc44) returns
  `coreutils`, and `rpm -qf /usr/bin/uname /usr/bin/cat` returns `coreutils-9.10-5.fc44.x86_64` for
  both; `coreutils` is `Priority: required` in Debian and Ubuntu, which
  `debootstrap --variant=minbase` installs in full. Excluding the image would remove a capability it
  has.
- **Leave it and rely on #2110's exit-127 mitigation.** judgment: that converts a build-time fact
  into a per-Run failure at the readiness deadline. #2160 says it directly — a clear failure on an
  image that should never have been admitted is still a failure. Right floor, wrong ceiling.
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
- **Drop `busybox` now that `coreutils` is named.** judgment: out of scope, and ADR-0481 reserved
  exactly this — reopening §4's userland "deserves its own decision taken with a build host in
  hand". Naming `coreutils` does not reopen it: busybox stays and the produced image is unchanged.
- **Verify with `guestfish --ro` or `virt-ls` instead.** verified: it would avoid the write recorded
  above — `virt-customize` changed a test qcow2's sha256 on a check that only ran `test -x` — but
  neither returns non-zero for an absent path, so the task would parse stdout and carry its own
  `failed_when`. judgment: a new failure shape for a write that resets a random seed on an image the
  same play built four `virt-customize` invocations ago. Reconsider if a later change needs the
  image bit-stable across verification.

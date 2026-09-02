# External-boot guest userland contract — design

- **Issue:** #2160 (part of #2105)
- **ADR:** [ADR-0590](../../adr/0590-external-boot-requires-a-posix-userland-in-every-catalog-image.md)
  — the decision, its evidence, consequences, and rejected alternatives, none of them restated here.
- **Date:** 2026-09-02

## Answer

`bare-kdive-remote-base` **is** admissible for external-boot Runs. The issue's premise — that
`/usr/bin/uname` and `/usr/bin/cat` are absent — does not survive checking: `systemd` hard-requires
`coreutils` on the dnf path, and `coreutils` is `Priority: required` on the `debootstrap
--variant=minbase` path. Excluding the image would remove a capability it has. The real defect is
that admissibility is **undeclared**.

## Changes

1. `build_scratch.yml` names `coreutils` in both bootstrap invocations.
2. `build_one.yml` verifies both paths on each produced qcow2, in one task covering every build
   path, immediately before the staging copy — so a failing image never becomes a staged volume and
   therefore never reaches an `[[image]]` block, a System, or a Run.
3. ADR-0590 records the contract.
4. The `bare-kdive-remote-base` catalog entry in `all.yml` gains a comment naming ADR-0590.
5. `deploy/ansible/README.md` gains an operator subsection stating the contract, the check, and the
   `force_image_rebuild` re-verify path, and amends the scratch caveat bullet.

Items 4 and 5 are load-bearing: ADR-0590 declines to add a forward amendment note to ADR-0188 §4 on
the ground that a reader reaches it through the recipe comments, the catalog entry, and the README.
Omitting either makes that argument false.

## Failure behaviour

The task exits non-zero naming both required paths (not only the missing one), what reads them, and
the package that supplies them, before the copy runs — so a failed image leaves no staged volume.
Its skip behaviour and the limits of that are ADR-0590, Consequences.

## Security

No trust boundary is added or widened, so no threat model is owed. Naming `coreutils` in a "bare"
image nominally adds guest userland but does not in fact — it is already installed transitively on
both paths. The new task drives the same `virt-customize` mechanism four existing tasks in the file
use, against an image the same play just built, with no non-literal value in its command.

## Testing

`tests/deploy/test_guest_base_image_external_boot_userland.py` parses both task files as YAML and
asserts: `coreutils` on each bootstrap path (with `busybox` still present); one verification task
naming both required paths; that task ordered before the staging copy; and its `when` guard equal
to the staging copy's, so no build path is exempt.

## What this cannot verify

The scratch path is `UNVALIDATED` and no scratch-capable host exists in CI or on available
hardware. Unproven here: that a scratch image builds, installs its bootloader, or boots; that the
produced rootfs contains the two paths (the dependency reasoning is read off packaging metadata and
Debian policy, not off a built image); and that the new task passes or fails correctly against a
real scratch qcow2. Verified here: the recipes declare `coreutils`, the task exists with the right
guard and ordering, and the lint, syntax, and harness gates pass. A real `playbooks/image.yml` run
on a Fedora or Debian-family host is what turns the task into evidence.

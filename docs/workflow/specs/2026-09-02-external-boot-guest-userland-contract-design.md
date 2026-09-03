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
4. The `bare-kdive-remote-base` catalog entry in `all.yml` gains a comment naming ADR-0590, and
   `all.yml` declares the program list once as `kdive_external_boot_userland_programs` — both
   enforcement points read that declaration, so neither can drift from the other.
5. `deploy/ansible/README.md` gains an operator subsection stating the contract, both checks, the
   three render-time outcomes, and the `force_image_rebuild` re-verify path, and amends the scratch
   caveat bullet.
6. ADR-0188 §4 gains the `### Amendment (2026-09-02)` block `docs/adr/README.md` prescribes, naming
   the added obligation and linking ADR-0590.
7. `remote_libvirt_facts` verifies each staged volume before declaring it an `[[image]]`
   (ADR-0590 Decision 4). Item 2 carries the staging copy's guard, so on a host that skips the
   build it never fires — which is every host with an already-staged volume, and the exact case
   #2160 is about. Without item 7 the fix is close to inert.

   The instrument is `guestfish --ro`, not item 2's `virt-customize`: the latter writes to what it
   inspects, which is noise against a build workdir and a mutation of shared state against a pool
   volume. Three outcomes, kept apart: conformant → declared; inspected and non-conformant →
   omitted; **not inspectable at all → the play fails**. The verdict is cached under
   `~/.cache/kdive/external-boot-userland`, keyed on the volume's size and mtime and the program
   list, so steady state launches no appliance.

Items 4, 5 and 6 are the discoverability set. Item 6 is the one a reader of §4 reaches without
already knowing ADR-0590 exists; items 4 and 5 are the pointers from the code and the operator
docs. The record is reachable from all three.

## Failure behaviour

**Build time.** The task exits non-zero naming both required paths (not only the missing one), what
reads them, and the package that supplies them, before the copy runs — so a failed image leaves no
staged volume.

**Render time.** A non-conformant volume is omitted from the fragment, which stays valid TOML and
simply declares fewer images; the omission comment carries the reason and `force_image_rebuild=true`
as the remedy. A volume that cannot be inspected fails the play instead, naming the volume, the
guestfish exit status and stderr, and that the check could not run — deliberately not a verdict
about the image. This rests on a property of guestfish verified against the real tool: an absent
program is `false` on stdout with rc 0, while a failed inspection leaves stdout empty with rc 1.

## Security

No trust boundary is added or widened, so no threat model is owed. Naming `coreutils` in a "bare"
image nominally adds guest userland but does not in fact — it is already installed transitively on
both paths. The build task drives the same `virt-customize` mechanism four existing tasks in the
file use, against an image the same play just built, with no non-literal value in its command.

The render-time check widens no privilege: it runs unprivileged like the volume stat beside it, and
`--ro` means it cannot alter the pool volume it reads. Its one new capability is parsing an
untrusted disk image with libguestfs, which already runs against these same images on the build
path. It adds a prerequisite — `libguestfs` on each managed host — recorded in ADR-0590 and the
operator README.

## Testing

`tests/deploy/test_external_boot_userland_contract.py` parses the task files and `all.yml` as YAML
and asserts, for the build half: `coreutils` on each bootstrap path (with `busybox` still present);
one verification task naming both required paths; that task ordered before the staging copy; its
`when` guard equal to the staging copy's; that the instrument is `virt-customize` and the disk it
opens is the qcow2 being staged; and — running the task's own shell fragment against a tmp_path
tree — that it accepts a conformant tree and refuses each of absent, partially absent, and present
but non-executable. For the render half: that both roles read one declared program list; that the
volume is opened `--ro` and never with `virt-customize`; that `followsymlinks:true` is set, since a
busybox image supplying the applets as symlinks is conformant; that an uninspectable volume stops
the play with a message naming the cause; and that the check is gated by a cache keyed on the
volume's size and mtime.

`deploy/ansible/tests/run-remote-libvirt-facts-render.sh` drives the real role end to end against a
guestfish double, asserting the rendered fragment is valid TOML in every case. Its four pre-#2160
cases are unchanged; five more cover a non-conformant volume, a non-conformant *default* volume, an
uninspectable volume failing the play, an empty program list failing the play, and the cache gate —
inspecting twice, then not at all, then once more after the volume is restaged. No case pre-creates
the verdict cache directory, so every one of them also covers the fresh-host path.

Beyond the double, the following were measured against real guestfish 1.60.1 on the development
host: three `guestfish --ro` runs leave a volume's sha256 and mtime byte-identical while one
`virt-customize` changes its sha256; an uninspectable volume fails the play through the role's own
message; a conformant volume renders and then, on a second run, launches no appliance; and the
default `~/.cache/kdive/external-boot-userland` path expands and is created correctly.

## What this cannot verify

The scratch path is `UNVALIDATED` and no scratch-capable host exists in CI or on available
hardware. Unproven here: that a scratch image builds, installs its bootloader, or boots; that the
produced rootfs contains the two paths (the dependency reasoning is read off packaging metadata and
Debian policy, not off a built image); and that the build task passes or fails correctly against a
real scratch qcow2.

CI has no libguestfs, so the render harness exercises the role's classification against a double
rather than a real appliance. The three outcome shapes the double reproduces — and the
symlink-following behaviour the unit test locks — were each observed from guestfish 1.60.1 against
purpose-built qcow2s on the development host before being encoded; that the same shapes hold on an
arbitrary managed host is reasoned, not proven here. Whether libguestfs can read a **root-owned**
0644 volume through a 0711 pool directory unprivileged was reasoned from POSIX permissions and
confirmed only for a user-owned file, since this host grants no passwordless root.

A real `playbooks/image.yml` run on a Fedora or Debian-family host, and a real `site.yml` run
against a host with a staged pool, are what turn both tasks into evidence.

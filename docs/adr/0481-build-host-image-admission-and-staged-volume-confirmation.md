# ADR 0481 — Build-host image admission, and confirming a volume before declaring it staged

- **Status:** Accepted
- **Date:** 2026-07-28
- **Deciders:** KDIVE maintainers
- **Amends:** [ADR-0188](0188-ansible-image-catalog.md) §2 (per-host selection) and §4 (the
  "bare but conformant" scratch image)
- **Issue:** #1629 (found while proving #1610)

## Context

`playbooks/image.yml` on a Rocky 10 host dies with `Error: Unable to find a match: busybox`.
The scratch build path names `busybox` as an unconditional element of its
`dnf --installroot` argv (`roles/guest_base_image/tasks/build_scratch.yml`), because ADR-0188
§4 defines the bare image as a busybox userland under systemd. busybox is a Fedora/EPEL
package; the RHEL/Rocky/Alma base repositories do not carry it. `host_vars/rock10-big.yml`
declares `host_images: [rocky-10-kdive-remote-base, bare-kdive-remote-base]`, so one
undeliverable entry failed the whole play and cost that host the image it *could* build.

The gate that would have caught this cannot be a **family** gate. `ansible_os_family` is
`RedHat` for both Fedora and Rocky, and only Fedora ships busybox in its base repos — a
family-granular rule either fails Rocky (the bug unfixed) or excludes Fedora (a regression
against a host that builds the image today).

The second half is independent and worse. `roles/remote_libvirt_facts` emitted
`[image.source] kind = "staged"` for **every** selected catalog entry, with no check that the
volume exists. That role runs from `site.yml`, which never builds images — the build is the
separate, deliberately opt-in `playbooks/image.yml` (ADR-0188, "considered & rejected"). So
the emitted `systems.toml` fragment asserted staged volumes the host may never have produced.
`InventoryDoc.parse` accepts such a fragment: it checks image-identity uniqueness and
`base_image` reference integrity, not host state. A phantom row therefore looks like an
available image right up until a System tries to boot it and fails at provision time.

## Decision

1. **A catalog entry may declare `host_distros`.** A list of `ansible_distribution` values
   whose package repositories can produce that entry. An entry that omits the field is
   unconstrained. `bare-kdive-remote-base` declares `[Fedora, Debian, Ubuntu]`. Derived
   `kdive_buildable_images` / `kdive_unbuildable_images` in `group_vars/all.yml` split the
   host's selection, preserving catalog order.

2. **An unbuildable entry skips; an unbuildable *default* fails.** `guest_base_image` loops
   the build over `kdive_buildable_images` and reports each skipped entry with the reason.
   The one exception is `host_default_image`: a host that cannot build the image its
   `[[remote_libvirt]].base_image` names has no usable block at all, so that is an operator
   error and asserts.

3. **Architecture stays fail-fast.** `arches` is a property of the *product* — asking for an
   x86_64-only image on ppc64le has no correct outcome — while `host_distros` is a property
   of the *builder*: the entry is fine, this host just cannot produce it. ADR-0188 §2's
   fail-fast arch rule is unchanged.

4. **`kind = "staged"` is confirmed, not assumed.** `remote_libvirt_facts` stats each
   selected image's volume in `storage_pool_target` — the same check `build_one.yml` already
   uses for its own idempotence — and the template emits an `[[image]]` block only for
   volumes that stat back. Selected-but-absent images are listed as `# OMITTED` in the
   artifact's header with the playbook to run.

5. **An incomplete fragment fails closed at load, and says so.** If the default image is
   among the omitted, the fragment's `base_image` names an image it does not declare, so
   `InventoryDoc.parse` rejects it (`names undeclared image`) instead of accepting a claim
   that only breaks later at provision. The template marks that case `# INCOMPLETE` and names
   the cause. The role warns rather than fails, because the documented usage order runs
   `site.yml` (step 2) before `playbooks/image.yml` (step 3) and a fresh host legitimately
   has nothing staged; re-running `site.yml` afterwards regenerates a complete fragment.

## Consequences

- `rock10-big` keeps its two-entry `host_images`: `playbooks/image.yml` now stages
  `rocky-10-kdive-remote-base` and skips the bare image with a message, and the emitted
  `systems.toml` declares only the rocky image. #1610's workaround — omitting the bare entry
  from the inventory and explaining it in a comment — is no longer needed.
- The scratch/bare path remains **unvalidated** (ADR-0188 consequences; no scratch-capable
  test host). This ADR does not change what that path builds — busybox stays in the
  installroot — it changes where it is attempted. Whether the bare image boots is still a
  hardware-only acceptance check on a Fedora or Debian-family host.
- The facts fragment is now host-state-dependent: two runs of `site.yml` against the same
  inventory can emit different `[[image]]` sets if an image was staged in between. That is
  the point — the file describes the host, not the inventory — but it means the artifact must
  be re-rendered after a build rather than treated as a pure function of `host_vars`.
- `site.yml` on a fresh host now emits a fragment with no `[[image]]` blocks and an
  `# INCOMPLETE` marker, where before it emitted four staged claims. The fragment was never
  loadable in truth; it is now honestly unloadable rather than dishonestly loadable.
- Two new Ansible role harnesses (`deploy/ansible/tests/run-guest-base-image-admission.sh`,
  `run-remote-libvirt-facts-render.sh`) drive the real tasks against the real catalog under
  `just test-ansible`, following the ADR-0201 `gdbstub_acl` prune harness pattern. No
  `src/kdive/**` change and no migration.

## Considered & rejected

- **Source busybox from EPEL on RHEL-family hosts.** EPEL is a repository dependency that
  exists nowhere under `deploy/` today; adding one for a single package on an unvalidated
  build path is new supply-chain surface for no proven gain.
- **Drop busybox from the scratch installroot** (Rocky's `bash`/`coreutils` cover a shell).
  This changes what "bare but conformant" means under ADR-0188 §4, which names busybox
  explicitly, and the scratch path is documented unvalidated — so the redefined image would
  be unproven until a host runs `playbooks/image.yml`. Reopening §4 deserves its own decision
  taken with a build host in hand, not a side effect of a repo-availability fix.
- **Gate on `ansible_os_family`.** Cannot express the constraint: Fedora and Rocky share the
  `RedHat` family and differ on exactly the package at issue.
- **Fail the facts role when a volume is missing.** `playbooks/image.yml` needs the storage
  pool `site.yml` defines, so the build cannot precede the facts play; a hard failure would
  make first bring-up impossible.
- **Emit the `[[image]]` block commented out.** A commented block is not a declaration, so it
  buys nothing over the `# OMITTED` list and invites uncommenting the exact claim the host
  cannot back.

# Runbook: M2.4 image & rootfs lifecycle

The operator guide to the image catalog: building and publishing public base images, registering
project-private uploads, the half-published-state reconciliation the platform runs automatically,
and the one capability CI cannot prove — a local-libvirt rootfs built through the in-process
Python build plane on a real host.

See [ADR-0092](../../adr/0092-image-rootfs-lifecycle.md) (the `image_catalog` table, row-first
publish, the `RootfsBuildPlane` port) and
[ADR-0093](../../adr/0093-private-image-uploads.md) (project-private uploads, quota, reference-guard,
extend-fence). The design spec is
`docs/archive/superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`; its "Exit criteria"
section is the source of truth this runbook tracks.

## What CI already proves (and what it cannot)

`tests/images/test_exit_criteria.py` drives criteria 2–4 through the **real** publish/upload
services, the **real** reconciler sweeps, and the **real** async catalog resolver over the
disposable-Postgres fixture; only the object store (no MinIO) and the libguestfs guest-contract
`inspect` probe (no guestfish) are faked, the same way the M2.3 doctor proof fakes only its leaf
probes. Criterion 1 is proven adjacent to each kernel build plane in
`tests/providers/{local,remote}_libvirt/test_build.py`.

| # | exit criterion | CI proof |
|---|----------------|----------|
| 1 | a no-op kernel patch **fails** patch-applied verification, both kernel build planes | `test_exit_criterion_noop_patch_fails_patch_applied_verification` in each plane's `test_build.py` (real `git apply` over a `.git`-less workspace) |
| 2 | each half-published state is reconciled | `test_half_published_object_without_row_is_reconciled` (leaked-object sweep) / `test_half_published_row_without_object_is_reconciled` (dangling-row sweep; an object-less `defined` baseline is skipped) |
| 3 | private isolation; expiry auto-prune; reference guard | `test_private_upload_resolves_only_within_owning_project`, `test_expired_private_image_is_auto_pruned`, `test_expired_private_referenced_by_live_system_is_not_pruned` |
| 4 | non-conforming upload rejected (named reason) + over-quota denied, both audited | `test_non_conforming_upload_is_rejected_with_named_reason`, `test_over_quota_upload_is_denied` |
| 5 | local-libvirt rootfs build through the Python plane, operator-run live stack | **this runbook** (env-gated, not CI) |

What CI **cannot** prove: the local-libvirt `RootfsBuildPlane` runs `virt-builder` / `virt-tar-out`
/ `virt-make-fs` / `guestfish` against a real qcow2 — minutes of libguestfs work that needs a host
with KVM/libvirt and the virt tooling. CI exercises the plane's orchestration and provenance
contract with those tools stubbed (`tests/images/planes/test_local_libvirt_plane.py`); the real
libguestfs path is what this runbook adds. Running criterion 5 is band-gate evidence, not a CI
check — a clean skip in CI is correct.

## Criterion 5: build a real rootfs through the Python plane (operator-run)

On a host with KVM/libvirt and the libguestfs virt tools installed, an operator who is **not** the
author builds a kdive-ready rootfs through the in-process plane and records that it boots.

### 1. Build the image

`build-fs` drives `LocalLibvirtRootfsBuildPlane` directly (the Python successor to the deleted
bash rootfs builder): it customizes a base image (sshd + the kdive-managed authorized key + the
`kdive-ready` serial-readiness unit + the guest packages), repacks to a no-partition-table
whole-disk ext4 qcow2, normalizes fstab/crypttab/guest-SELinux, and records the pinned inputs as
provenance. On success it prints exactly one line to **stdout** — the `KDIVE_GUEST_IMAGE` wiring
for the live spine — while the human summary (the destination path and the `sha256:` content
digest) goes to **stderr** (the logger). That split makes the command's stdout `eval`-safe.

> **How the packages get installed (ADR-0345, #1147).** For the `rhel` family (Fedora/RHEL),
> `build-fs` no longer runs the guest's `dnf` inside the host-arch libguestfs appliance — it
> repacks + normalizes the base first, injects the family customization as a one-shot firstboot
> unit (file-level, arch-safe), then **boots the image once** (KVM natively, TCG for a foreign
> arch such as ppc64le on an x86_64 host) so the guest self-installs its packages, and seals the
> result. This makes foreign-arch image builds possible and keeps native builds on the guest's own
> package manager. A build boot needs guest network egress for the package fetch; a failed in-guest
> install surfaces the guest's error via the console tail rather than a silent timeout. The
> `debian` family still uses the offline `virt-customize` path until its own follow-up (#1167).

> **Agent-selectable disk requires a rebuilt image (ADR-0312, #985).** An `allocations.request`
> may size the guest disk via `disk_gb` (a custom triple or the `debug` shape). The platform grows
> the per-System overlay to that size at provision, and cloud-init's `resize_rootfs` grows the
> guest filesystem to fill it on first boot. That growth only happens on an image built with
> `resize_rootfs` enabled — **rebuild each rootfs with `build-fs` to gain it**. The build
> self-check refuses an image whose baked cloud-init drop-in has `resize_rootfs` off, so a freshly
> built image always has it; an older on-disk image grows its virtual disk but leaves the extra
> space unformatted until rebuilt. The per-request disk ceiling is derived live from the free
> capacity of `/var/lib/kdive/rootfs` (no operator env); a request over it is a
> `configuration_error`. remote-libvirt and fault-inject do not size disk this way and are not
> bounded.

Flags that shape the build:

- `--image NAME` is required. It selects a row from
  `fixtures/local-libvirt/rootfs_catalog.toml`, which owns the image name, distro, release,
  architecture, kind, family customizer, and pinned base source.
- `fedora-kdive-ready-44` is the kdump-capable debug rootfs. `fedora-kdive-build-44` is the
  cataloged build-host toolchain image. Add new images to the catalog rather than passing
  ad hoc distro/release flags.
- `--workspace DIR` (default `/var/lib/kdive/build/images`) is where the build stages and
  publishes the qcow2. Point it at a **user-writable** path to build first-run without a
  privileged `mkdir` of the root-owned default. A missing/un-writable workspace fails with an
  actionable message (the directory and a suggested `install -d` command), not a traceback.

```bash
python -m kdive build-fs \
  --image fedora-kdive-ready-44 \
  --workspace ~/.local/share/kdive/build/images \
  --package drgn --package kexec-tools --package makedumpfile
```

Build a build-host toolchain image instead:

```bash
python -m kdive build-fs \
  --image fedora-kdive-build-44 \
  --workspace ~/.local/share/kdive/build/images \
  --dest /var/lib/kdive/rootfs/local/fedora-kdive-build-44.qcow2
```

To build and export `KDIVE_GUEST_IMAGE` in one step, capture stdout with `eval` (the stderr
summary still prints to your terminal):

```bash
eval "$(python -m kdive build-fs \
  --image fedora-kdive-ready-44 \
  --workspace ~/.local/share/kdive/build/images \
  --dest /var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2)"
# KDIVE_GUEST_IMAGE is now exported, pointing at the --dest path above
```

Record the printed `sha256:` digest — it is the image identity (a rootfs image has no kernel
`build_id`). For the default root-owned `--dest` an OS admin pre-creates the output directory once
and makes it writable by the build user; the per-build write and the final `chmod 0644` are
unprivileged. Under SELinux the output file also needs the `virt_image_t` label so the `qemu` user
can read it under `qemu:///system` (a host-side file label, independent of the guest-internal
SELinux the plane disables).

### 2. Exercise it on the live stack

Point the live-stack suite's fixtures at the built image and the kernel tree, then run the spine —
the booting `live_stack` tests provision a System on `local-libvirt` from this rootfs, so a
successful spine run is the evidence the plane-built image boots and is debuggable. If you used
the `eval` form above, `KDIVE_GUEST_IMAGE` is already exported; otherwise set it by hand — this is
exactly the line `build-fs` prints on stdout:

```bash
export KDIVE_GUEST_IMAGE=/var/lib/kdive/rootfs/local/fedora-kdive-ready-44.qcow2
bash scripts/fetch-kernel-tree.sh
export KDIVE_KERNEL_SRC=/path/to/kernel-tree
export KDIVE_LIVE_SSH_TARGET=<host>          # the criterion-5 env gate
just stack-up                                # bring up backends + migrate (see the live-stack runbook)
just test-live-stack                         # runs the `live_stack` suite (skips cleanly if ungated)
```

Without `KDIVE_LIVE_SSH_TARGET` (and the guest image / kernel tree), the `live_stack` preflight
skips with an actionable reason — which is the correct outcome in CI. Do **not** un-gate these
tests to make a run pass: the gate is what keeps the libguestfs/KVM dependency out of normal CI.

### 3. (Optional) publish it to the catalog

The same plane runs inside the `IMAGE_BUILD` job behind the operator verb; publishing promotes the
built image to a public, row-first catalog entry that the async resolver hands to provisioning:

```bash
kdivectl images build   --provider local-libvirt --name fedora-kdive-ready-44 \
                        --arch x86_64 --releasever 44 \
                        --source-image-digest sha256:<base> \
                        --capabilities agent,kdump,drgn,helpers
kdivectl images publish --provider local-libvirt --name fedora-kdive-ready-44 \
                        --arch x86_64 --releasever 44 \
                        --source-image-digest sha256:<base> \
                        --capabilities agent,kdump,drgn,helpers
kdivectl images list
```

`build`/`publish` authorize as `platform_operator`. The build worker runs the same plane this
runbook drove inline, validates the guest contract (libguestfs inspection — a build missing
agent/kdump/drgn/helpers is rejected, never published), and publishes row-first.

## Local rootfs catalog entries and kdump capability (ADR-0251)

`build-fs --image <name>` resolves a row from the file-authoritative
`fixtures/local-libvirt/rootfs_catalog.toml`. Each row pins its base (a `virt-builder` template or
a sha256-pinned cloud-image URL) and carries a `kdump_capable` flag. The RHEL-family entries
(#823) reuse the `rhel` customizer; on EL 8/9 `makedumpfile`/`kdumpctl` come from `kexec-tools`
(no standalone packages) and EL 8 pulls `drgn` from EPEL. The Debian entries (#824) use the
`debian` customizer (apt; `kdump-tools.service`; `ssh.service`; `python3-drgn`; AppArmor instead of
SELinux, needing no relabel; cloud-init disabled via `/etc/cloud/cloud-init.disabled`).

`kdump_capable` is **kernel-relative** to the current default from-source target (a v7.0-class
x86_64 kernel): it is `true` only when the makedumpfile the build installs from that release's
repos is **≥ 1.7.9** (the first release that filters a v7.0 vmcore). It is a curated, dated
snapshot of a mutable upstream, not live truth — when a release ships makedumpfile ≥ 1.7.9 a fresh
build silently becomes capable while the flag lags until re-verified. The runtime
`kdump_core_incomplete` remediation (raised on the actual harvest) is the ground truth.

| catalog `--image` | base | makedumpfile (build-time) | `kdump_capable` (v7.0) | default `kdump` `vmcore.fetch` |
|---|---|---|---|---|
| `fedora-kdive-ready-44` | Fedora 44 | 1.7.9 | **yes** | complete filtered core |
| `fedora-kdive-ready-43` | Fedora 43 | 1.7.8 | no | `kdump_core_incomplete` → `host_dump` |
| `rocky-kdive-ready-8` | Rocky 8.10 | 1.7.2 (in `kexec-tools`) | no | `kdump_core_incomplete` → `host_dump` |
| `rocky-kdive-ready-9` | Rocky 9.8 | 1.7.6 (in `kexec-tools`) | no | `kdump_core_incomplete` → `host_dump` |
| `rocky-kdive-ready-10` | Rocky 10.2 | 1.7.8 | no | `kdump_core_incomplete` → `host_dump` |
| `centos-stream-kdive-ready-9` | CentOS Stream 9 | 1.7.6 (in `kexec-tools`) | no | `kdump_core_incomplete` → `host_dump` |
| `centos-stream-kdive-ready-10` | CentOS Stream 10 | 1.7.8 | no | `kdump_core_incomplete` → `host_dump` |
| `debian-kdive-ready-12` | Debian 12 (bookworm) | 1.7.2 | no | `kdump_core_incomplete` → `host_dump` |
| `debian-kdive-ready-13` | Debian 13 (trixie) | 1.7.6 | no | `kdump_core_incomplete` → `host_dump` |

Versions verified against distro package indexes on 2026-06-26 (the guard test
`tests/images/test_rootfs_catalog.py` asserts each row's flag matches its documented makedumpfile
version). A `kdump_capable = no` entry still completes the rest of the lifecycle
(provision/build/install/boot) and captures via the explicit `host_dump` method; only the default
in-guest `kdump` filtered-core path is affected for a v7.0-class kernel.

## Operator verbs (`kdivectl images`)

| verb | actor | authz | what it does |
|------|-------|-------|--------------|
| `images list` | member / operator | RBAC-filtered | public rows + the caller's project's private rows |
| `images upload --project P --name N --arch A --quarantine-key K [--lifetime-seconds S]` | project member | per-project | register a quarantined upload as a project-private image |
| `images delete <image_id>` | member / operator | project-scoped; operator cross-project via break-glass | delete an unreferenced private image |
| `images build` / `images publish` | operator | `platform_operator` | enqueue `IMAGE_BUILD` / promote to a public catalog row |
| `images prune --expired [--reason R]` | operator | `platform_admin` break-glass | force the expired-private sweep now |
| `images extend <image_id> --seconds S [--reason R]` | operator | `platform_admin` break-glass | re-arm a private image's lifetime |

`prune` is destructive and requires the explicit `--expired` flag. An unprivileged or
cross-project invocation is denied **and audited** (the deny path writes the audit row before
touching the pool).

### Project-private uploads

An upload lands as a quarantined object (ADR-0048 ingest), then `images upload` validates its guest
contract and registers it project-private with a required `expires_at` (clamped to
`KDIVE_IMAGE_PRIVATE_LIFETIME_MAX_SECONDS`):

- A non-conforming image (missing agent/kdump/drgn/helpers) is **rejected with the missing element
  named**, while still quarantined — it is never registered and never leaves the quarantine prefix.
- The per-project quota (`KDIVE_IMAGE_PRIVATE_MAX_COUNT` + `KDIVE_IMAGE_PRIVATE_MAX_BYTES`) is
  enforced fail-closed under the project lock; an over-cap upload is **denied and audited**.
- A registered private image resolves **only within its owning project** and shadows a same-name
  public image there; another project resolves only the public one.

## Reconciliation (automatic)

The reconciler runs three deadline-guarded image sweeps each pass (counts surface on the
`ReconcileReport` as `leaked_images` / `dangling_images` / `expired_private_images`). Publish is
row-first (the catalog row is written before the object), so a live publish is never raced.

- **leaked images** — an object under the `images/` prefix with **no catalog row**, older than the
  publish grace (`KDIVE_IMAGE_PUBLISH_GRACE_SECONDS`, default 3600), is deleted.
- **dangling rows** — a non-`defined` row whose object HEAD is missing **past its publish deadline**
  (`pending_since + grace`) is removed. An object-less `defined` baseline is object-less by design
  and never dangling — it is skipped.
- **expired private images** — a private row with `expires_at < now()` is pruned (object + row),
  but is **reference-guarded** (an image a non-terminal System still references through its
  `provisioning_profile` catalog rootfs is skipped — its expiry defers) and **extend-fenced** (the
  `expires_at` is re-read under a per-row lock, so a concurrent operator `images extend` is
  honored). The object is deleted before the row, so a crash strands at most a dangling row the
  dangling sweep heals — never a rowless object.

To force the expired-private sweep immediately (e.g. to reclaim quota now), an operator runs
`kdivectl images prune --expired` (`platform_admin` break-glass).

## Runtime tool installs on local-libvirt (operator-gated egress)

An agent that has root in the guest (`systems.authorize_ssh_key`) can install tools at runtime
(`dnf`/`apt install trace-cmd`, `bpftrace`, `gcc`, kernel-headers, …) — **but only if the guest can
reach its distro mirrors**. On local-libvirt the guest NIC is a loopback-forwarded SSH channel with
QEMU `restrict=on`, which blocks **all** guest-initiated egress by default (ADR-0218 §1), so a
runtime install fails with `Could not resolve host: …`. This is the secure default and is unchanged
unless you opt in.

To let a local-libvirt resource's guests install tools at runtime (ADR-0313, #1031):

1. Set `guest_egress = true` on that resource's `[[local_libvirt]]` block in `systems.toml`. The
   block's `name` must match the discovery-created resource name — read it from `kdive resources
   list` (or the `resources` catalog); a mismatch is silently ignored and egress stays off.
2. Reconcile (`kdive reconcile-systems`, the deploy `migrate` step, or the reconciler loop).
3. **Re-provision** the System (the flag renders `restrict=off` into the domain XML at provision;
   it does not retrofit a running guest). New Systems pick it up on first boot.

`guest_egress` is resolved at provision time from the **worker's** `systems.toml`. If the worker runs
as a different user than the operator/reconciler (e.g. a root worker), make sure both read the same
file — set `KDIVE_SYSTEMS_TOML` to a shared absolute path rather than relying on the per-user XDG
default (`$HOME/.config/kdive/systems.toml`), or the worker will read a different inventory and the
opt-in silently stays off.

**Security — what you are accepting.** `restrict=off` drops the QEMU-level egress block, so an
agent-supplied (untrusted) kernel can send outbound traffic through the NIC. The QEMU block is no
longer the boundary — **your network zone's firewall is**. Enable this only for resources that live
in a lab micro-zone whose network firewall already restricts egress. When the flag is absent or
false, behavior is exactly as before (`restrict=on`, no egress).

**Note:** `restrict=off` opens the *route*; the guest still needs to DHCP the NIC and populate
`/etc/resolv.conf` (SLIRP hands out `10.0.2.15` with DNS at `10.0.2.3`). The `kdive-ready` images do
this under direct-kernel boot; if a custom image does not bring the NIC up, runtime installs still
fail even with egress enabled.

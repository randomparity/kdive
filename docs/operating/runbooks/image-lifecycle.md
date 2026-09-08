# Image and rootfs lifecycle

Build and publish base images, register project-private uploads, and manage image retention.
Kernel compilation and packaging have a separate owner: the
[external-build guide](../external-build-upload.md).

## Build a local rootfs

`build-fs` drives `LocalLibvirtRootfsBuildPlane` directly (the Python successor to the deleted
bash rootfs builder): it customizes a base image (sshd + the kdive-managed authorized key + the
`kdive-ready` serial-readiness unit + the guest packages), repacks to a no-partition-table
whole-disk ext4 qcow2, normalizes fstab/crypttab/guest-SELinux, and records the pinned inputs as
provenance. On success it prints exactly one line to **stdout** — the `KDIVE_GUEST_IMAGE` wiring
for the live spine — while the human summary (the destination path and the `sha256:` content
digest) goes to **stderr** (the logger). Only use the printed export after the command succeeds.

> **How the packages get installed (ADR-0345, #1147, #1167).** `build-fs` never runs the guest's
> `dnf`/`apt-get` inside the host-arch libguestfs appliance — it repacks + normalizes the base
> first, injects the family customization as a one-shot firstboot unit (file-level, arch-safe),
> then **boots the image once** (KVM natively, TCG for a foreign arch such as ppc64le on an x86_64
> host) so the guest self-installs its packages with its own package manager (`dnf` on the `rhel`
> family, a non-interactive `apt-get` after an `apt-get update` on the `debian` family), and seals
> the result. This makes foreign-arch image builds possible and removes the build host's dependency
> on the libguestfs appliance network (`passt`). A build boot needs guest network egress for the
> package fetch; a failed in-guest install surfaces the guest's error via the console tail rather
> than a silent timeout.
>
> The boot path extracts the base's baseline kernel with the libguestfs **Python binding**, so
> `build-fs` needs `import guestfs` to work from the venv it runs in — for every family, not only
> the kdump capture path that first required it. Follow the [cross-platform prerequisites](../../development/cross-platform.md)
> for the invoking interpreter; a missing binding
> fails the build with `missing_dependency` before the boot starts.

> **Building a foreign-arch image is slower (TCG).** When the target arch is not the build
> host's arch (e.g. a `ppc64le` image on an `x86_64` host), the customization boot runs under
> QEMU TCG emulation instead of KVM — slower than native execution. The host needs the foreign arch's QEMU
> system emulator installed (`qemu-system-ppc` on Fedora/Debian, `qemu-ppc` on openSUSE; see the
> [cross-architecture guests](../platform-support.md#cross-architecture-guests) table for every distro).
> The completion poll waits `KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S` (default `1800`, the
> native-KVM base) scaled by `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default `10.0`) for a TCG
> build, so raise the window if a large foreign-arch package set does not finish in time.

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
- `fedora-kdive-ready-44` is a debug rootfs. `fedora-kdive-build-44` is the
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

After a successful build, copy the printed `export KDIVE_GUEST_IMAGE=...` line into the shell
that will run the live proof. Do not evaluate an unchecked command substitution: a failed build
could leave a previous image selected.

Record the printed `sha256:` digest — it is the image identity (a rootfs image has no kernel
`build_id`). For the default root-owned `--dest` an OS admin pre-creates the output directory once
and makes it writable by the build user; the per-build write and the final `chmod 0644` are
unprivileged. Under SELinux the output file also needs the `virt_image_t` label so the `qemu` user
can read it under `qemu:///system` (a host-side file label, independent of the guest-internal
SELinux the plane disables).

## Verify the built image

Follow [live-stack setup](live-stack.md) to configure and start the runtime, then the
[live-testing guide](live-testing.md) to select a boot proof and its fixtures. Set
`KDIVE_GUEST_IMAGE` to this successful build's destination in the proof environment and record
its digest. Confirm that the intended proof executed with that image; a skipped test does not
prove it boots. Building an image alone does not exercise provisioning, boot, or debugging.

## Publish a catalog image

The same plane runs inside the `IMAGE_BUILD` job behind the operator verb; publishing promotes the
built image to a public, row-first catalog entry that the async resolver hands to provisioning:

```bash
kdivectl images publish --provider local-libvirt --name fedora-kdive-ready-44 \
                        --packages crash --packages drgn
kdivectl images list
```

`publish` authorizes as `platform_operator` and covers the whole build/validate/publish path —
there is no separate build verb (ADR-0461). Re-issuing it for the same `provider`/`name` returns
the job already in flight. The build worker runs the same plane this runbook drove inline,
validates the guest contract (libguestfs inspection — a build missing agent/kdump/drgn/helpers is
rejected, never published), and publishes row-first.

## Catalog inputs and runtime capability

[`fixtures/local-libvirt/rootfs_catalog.toml`](../../../fixtures/local-libvirt/rootfs_catalog.toml)
owns local build inputs, pinned base sources, and curated guest-tool versions. Family customizers
own package installation; avoid copying their package/version tables into another guide.

Runtime capture capability depends on the target kernel and available evidence. Use
`images.describe` and the [images guide](../../guide/toolsets/images.md) to distinguish supported,
unsupported, and unverified results. Catalog metadata does not prove that a particular guest's
capture kernel is armed or that a capture succeeded.

## Operator verbs (`kdivectl images`)

| verb | actor | authz | what it does |
|------|-------|-------|--------------|
| `images list` | member / operator | RBAC-filtered | public rows + the caller's project's private rows |
| `images upload --project P --name N --arch A --quarantine-key K [--lifetime-seconds S]` | project operator/admin | owning project | register a quarantined upload as a project-private image |
| `images delete <image_id>` | project operator/admin | owning project; no cross-project force path | delete an unreferenced private image |
| `images publish --provider P --name N [--packages PKG]...` | operator | `platform_operator` | enqueue `IMAGE_BUILD`, which builds, validates, and promotes to a public catalog row |
| `images prune-expired --expired --reason R` | operator | `platform_admin` break-glass | force the expired-private sweep now |
| `images extend <image_id> --seconds S --reason R` | operator | `platform_admin` break-glass | re-arm a private image's lifetime |

`prune-expired` requires the explicit `--expired` flag and an audited reason. Project-scoped
image mutations require the owning project's role; a platform role does not grant a
cross-project delete path.

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
`kdivectl images prune-expired --expired --reason "<why>"` (`platform_admin` break-glass). The
`--reason` is the audited break-glass justification the tool requires; the CLI refuses the call
without it rather than sending an empty one.

## Runtime tool installs on local-libvirt (operator-gated egress)

An agent that has root in the guest (`systems.authorize_ssh_key`) can install tools at runtime
(`dnf`/`apt install trace-cmd`, `bpftrace`, `gcc`, kernel-headers, …) — **but only if the guest can
reach its distro mirrors**. On local-libvirt the guest NIC is a loopback-forwarded SSH channel with
QEMU `restrict=on`, which blocks **all** guest-initiated egress by default (ADR-0218 §1), so a
runtime install fails with `Could not resolve host: …`. This is the secure default and is unchanged
unless you opt in.

To let a local-libvirt resource's guests install tools at runtime (ADR-0313, #1031):

1. Set `guest_egress = true` on that resource's `[[local_libvirt]]` block in `systems.toml`. The
   block's `name` must match the discovery-created resource name — read it from `kdivectl resources list` (or the `resources` catalog); a mismatch is silently ignored and egress stays off.
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

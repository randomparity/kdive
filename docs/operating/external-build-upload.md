# Preparing artifacts for the build lane

**This is the build lane (ADR-0234): build locally, upload, `runs.complete_build`.**
No operator-staged source tree or build host is needed.

The build lane ingests a kernel you built yourself: `runs.create` with a `build_profile`
records the Run, you upload the artifacts, then `runs.complete_build` finalizes the Run. This
page is the recipe for shaping those artifacts so they pass validation on the first try.

The validator rejects a malformed upload with a precise message, but only **after** the
upload round-trip — so the cost of getting the shape wrong is a wasted upload, not just an
error. Each rule below names the rejection it prevents.

## Choosing your kernel config

**The kernel config is yours to choose.** Because you build the kernel locally, you decide
which Kconfig symbols are enabled before you upload — a debug kernel is one you built with the
debug options turned on. The validator constrains only the artifacts' **structure** (bzImage
magic, gzip layout, a `lib/modules` member); it never rejects a build over your `.config`.
There is no allowed-config allowlist and no required-symbol gate: enable what the
investigation needs. One non-blocking exception: if you upload an `effective_config` that
provably lacks the symbols needed to mount the root filesystem and boot (`EXT4_FS` and
`VIRTIO_BLK` — root is `/dev/vda` ext4 on virtio-blk), `runs.complete_build` still succeeds but
returns a `data.missing_boot_config` advisory naming the missing symbols, so a kernel that
cannot boot is not silently accepted.

A useful debug set to start from:

```
CONFIG_KASAN=y            # slab/stack out-of-bounds and use-after-free detector
CONFIG_KASAN_INLINE=y
CONFIG_KCSAN=y            # data-race detector
CONFIG_FAULT_INJECTION=y  # failslab / fail_page_alloc via debugfs
CONFIG_FAILSLAB=y
CONFIG_FAIL_PAGE_ALLOC=y
CONFIG_DEBUG_INFO_DWARF5=y  # DWARF/BTF: required for drgn to resolve any symbol (see below)
CONFIG_DEBUG_INFO_BTF=y     # BTF: what in-guest drgn-live reads
CONFIG_PROVE_LOCKING=y    # lockdep
```

**`drgn` needs debuginfo to resolve any symbol.** For `drgn-live` introspection
(`introspect.run` / `introspect.script`), build with `CONFIG_DEBUG_INFO_BTF=y`: in-guest drgn reads
BTF from `/sys/kernel/btf`, so a defconfig kernel without it resolves nothing. DWARF in the kernel
`.config` alone does not help drgn-live — the DWARF `vmlinux` is not on the guest rootfs. For
host-side DWARF introspection (offline `introspect.from_vmcore` and gdb), build with
`CONFIG_DEBUG_INFO_DWARF5=y` and also upload `vmlinux`. A drgn-live session or introspect over a
kernel with neither BTF nor an uploaded `vmlinux` returns a non-fatal `missing_debuginfo` warning.

`CONFIG_DEBUG_INFO_BTF=y` in the `.config` is necessary but not sufficient: BTF-only drgn-live
introspection also depends on the guest image's drgn build being able to load that BTF at runtime
(older in-guest drgn versions can fail to load a newer kernel's BTF). When the config advertises
BTF but the in-guest drgn cannot actually resolve symbols, `introspect.run` / `introspect.script`
return a non-fatal `debuginfo_unloadable` warning naming the likely cause; remediate by booting a
BTF-capable guest image with a newer drgn, or by uploading a matching `vmlinux`.
See `artifacts.feature_config_requirements` for the per-feature `CONFIG_*` manifest.

## The `kernel` artifact: one combined gzip tar

There is **one required artifact, named `kernel`**: a single gzip-compressed tar holding the
boot image and the matching module tree. There is no separate `modules` artifact — the module
tree rides inside the `kernel` tar.

The `boot/vmlinuz` payload format is keyed by the **target architecture** you declare in the
build profile (`arch`, default `x86_64`) at `runs.create`:

| `arch` | `boot/vmlinuz` is | Rule |
|---|---|---|
| `x86_64` | the **bzImage** (`arch/x86/boot/bzImage`), renamed | must carry the bzImage `HdrS` magic at offset `0x202` — the `vmlinux` ELF is **not** accepted here |
| `ppc64le` | the **stripped ELF kernel** (powerpc has no bzImage — this is what Fedora/RHEL install as `/boot/vmlinuz-<ver>`) | must be a 64-bit little-endian ELF whose `e_machine` is `EM_PPC64`; the unstripped DWARF `vmlinux` belongs in the optional `vmlinux` artifact, not here |

The tar must also contain:

| Member | What it is | Rule |
|---|---|---|
| `lib/modules/<release>/…` | the `make modules_install` tree for that kernel | at least one real kernel-module file (`*.ko`, `.ko.xz`, `.ko.gz`, or `.ko.zst`) under `lib/modules/<release>/` must be present — a bare directory or a `modules.dep` with no module is rejected |

The declared `arch` and the payload must agree: an x86 bzImage under `arch: ppc64le`, or an ELF
(or non-ppc64 ELF) under `arch: x86_64`, is rejected. Learn the exact per-arch magic bytes from
`artifacts.expected_uploads` (`contracts.kernel.layout[boot/vmlinuz].formats_by_arch`).

Two further rules come from how the artifact is validated and consumed:

- **gzip specifically** — a plain `.tar`, `.tar.xz`, or `.tar.zst` is rejected
  (`kernel artifact is not a gzip-compressed combined tar`).
- **put `boot/vmlinuz` first** — validation scans at most the first 128 MiB of *decompressed*
  output (a gzip-bomb guard), so the `lib/modules` header must appear within that window. The
  boot image is small and listed first in the recipe below, so the module tree is reached
  immediately; a tar that front-loads a very large file before `lib/modules` can fail with
  `kernel combined tar has no lib/modules member within the scan bound`.
- **drop the back-reference symlinks** — `make modules_install` plants `build` and `source`
  symlinks under `lib/modules/<release>/` that point at absolute paths in your build tree.
  Exclude them; left in, they become dangling links inside the guest.

### The recipe (x86_64)

This is the exact `tar` invocation the platform's own build planes use. Run it from a built
kernel tree, with `MODROOT` pointing at the staging root you passed to
`make modules_install INSTALL_MOD_PATH=…`:

```bash
KBUILD=.                 # the built kernel tree (contains arch/x86/boot/bzImage)
MODROOT=/tmp/modstage    # INSTALL_MOD_PATH from `make modules_install` (holds lib/modules/<release>)

tar -czf kernel.tar.gz \
  --exclude='*/build' --exclude='*/source' \
  --transform='s|^arch/x86/boot/bzImage$|boot/vmlinuz|' \
  -C "$KBUILD"  arch/x86/boot/bzImage \
  -C "$MODROOT" lib/modules
```

`--transform` renames the bzImage to `boot/vmlinuz` in the archive; the two `--exclude`s drop
the back-reference symlinks; listing `arch/x86/boot/bzImage` before `lib/modules` keeps the
boot image first.

### The recipe (ppc64le)

powerpc has no bzImage — the boot member is the **stripped** ELF kernel. Strip the build-tree
`vmlinux` first (the unstripped one carries full DWARF and is hundreds of MB, which pushes
`lib/modules` past the validator's decompress scan bound), then tar the stripped copy:

```bash
KBUILD=.                 # the built kernel tree (contains the top-level vmlinux)
MODROOT=/tmp/modstage    # INSTALL_MOD_PATH from `make modules_install`

"${CROSS_COMPILE}strip" -s "$KBUILD/vmlinux" -o /tmp/vmlinuz   # stripped, bootable, tens of MB

tar -czf kernel.tar.gz \
  --exclude='*/build' --exclude='*/source' \
  --transform='s|^vmlinuz$|boot/vmlinuz|' \
  -C /tmp        vmlinuz \
  -C "$MODROOT"  lib/modules
```

Declare `arch: ppc64le` in the build profile at `runs.create`. The unstripped DWARF `vmlinux`
goes in the optional `vmlinux` artifact (below), not the boot member.

## Optional artifacts

| Name | When to upload | Notes |
|---|---|---|
| `vmlinux` | to enable kernel-debugging / DWARF introspection | the uncompressed kernel ELF with debug info. If you upload it you **must** declare a `build_id` in `runs.complete_build`, and it must match the ELF's GNU build-id note, or the upload is rejected. |
| `effective_config` | to record the `.config` you built with | the kernel `.config` used for the build, ≤ 1 MiB. Stored for provenance; never rejected, but if it provably lacks the boot-required symbols (`EXT4_FS`, `VIRTIO_BLK`) `runs.complete_build` returns a non-blocking `missing_boot_config` advisory. |
| `initrd` | when booting needs a specific initramfs | the initial ramdisk image. |

## The upload flow

1. `artifacts.expected_uploads` — confirm the accepted names for the `run` owner-kind.
2. Build `kernel.tar.gz` with the recipe above (plus any optional artifacts).
3. `artifacts.create_run_upload` — declare each artifact `{name, sha256 (base64), size_bytes}`
   and receive one upload item per artifact. Each item contains `refs.upload_url` and
   `data.required_headers`; objects over the single-PUT limit can be declared with `chunks`.
4. PUT each object to its presigned URL, sending **exactly** the headers in
   `data.required_headers` and nothing else.
5. `runs.complete_build` — finalize. The server validates every uploaded object (shape, magic,
   manifest `sha256`/`size_bytes`, and the `vmlinux` build-id) before the Run becomes
   installable.

Each `artifacts.create_run_upload` call replaces the previous manifest for the Run. If you
correct one artifact, redeclare every artifact that should remain part of the build.

### Pitfall: extra headers break the signature

The presigned URL is signed over a fixed header set. If your HTTP client injects a header
that isn't in `data.required_headers` — most commonly a default `Content-Type` — the PUT
fails with `403 SignatureDoesNotMatch`. `curl -d`/`--data-binary` is a common trap: it adds
`Content-Type: application/x-www-form-urlencoded` and can mangle binary bodies. Use `curl -T`
to upload the file, and explicitly clear `Content-Type` since it is never one of the
`required_headers`:

```bash
curl -T kernel.tar.gz -H 'Content-Type:' \
  -H 'x-amz-checksum-sha256: <b64>' -H 'x-amz-meta-sensitivity: sensitive' \
  -H 'x-amz-meta-retention-class: build' "$UPLOAD_URL"
```

Replace the `x-amz-*` headers above with the exact set from `data.required_headers` for that
upload item — the names and values are per-artifact.

A mismatch between a declared `sha256`/`size_bytes` and the stored object is rejected
(`uploaded artifact disagrees with its manifest`), so checksum the bytes you actually PUT.

## Related

- [`artifacts` tool reference](../guide/reference/artifacts.md) — `create_run_upload`,
  `expected_uploads`, and chunked-upload parameters.
- [`runs` tool reference](../guide/reference/runs.md) — `runs.create` build profiles and
  `runs.complete_build`.

# Preparing artifacts for the external-build lane

The external-build lane (`runs.create` with a `build_profile` whose `source="external"`)
ingests a kernel you built yourself instead of building one on a worker. You upload the
artifacts, then call `runs.complete_build` to finalize the Run. This page is the recipe for
shaping those artifacts so they pass validation on the first try.

The validator rejects a malformed upload with a precise message, but only **after** the
upload round-trip — so the cost of getting the shape wrong is a wasted upload, not just an
error. Each rule below names the rejection it prevents.

## The `kernel` artifact: one combined gzip tar

There is **one required artifact, named `kernel`**: a single gzip-compressed tar holding the
boot image and the matching module tree. There is no separate `modules` artifact — the module
tree rides inside the `kernel` tar.

The tar must contain:

| Member | What it is | Rule |
|---|---|---|
| `boot/vmlinuz` | the **bzImage** (`arch/x86/boot/bzImage`), renamed | must carry the bzImage `HdrS` magic at offset `0x202` — the `vmlinux` ELF is **not** accepted here |
| `lib/modules/<release>/…` | the `make modules_install` tree for that kernel | at least one `lib/modules/` member must be present |

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

### The recipe

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

## Optional artifacts

| Name | When to upload | Notes |
|---|---|---|
| `vmlinux` | to enable kernel-debugging / DWARF introspection | the uncompressed kernel ELF with debug info. If you upload it you **must** declare a `build_id` in `runs.complete_build`, and it must match the ELF's GNU build-id note, or the upload is rejected. |
| `effective_config` | when the Run's build profile carries config requirements | the kernel `.config` used for the build, ≤ 1 MiB; validated against the profile's required symbols. |
| `initrd` | when booting needs a specific initramfs | the initial ramdisk image. |

## The upload flow

1. `artifacts.expected_uploads` — confirm the accepted names for the `run` owner-kind.
2. Build `kernel.tar.gz` with the recipe above (plus any optional artifacts).
3. `artifacts.create_run_upload` — declare each artifact `{name, sha256 (base64), size_bytes}`
   and receive a presigned PUT per artifact. Objects over the single-PUT limit can be declared
   with `chunks`.
4. PUT each object to its presigned URL.
5. `runs.complete_build` — finalize. The server validates every uploaded object (shape, magic,
   manifest `sha256`/`size_bytes`, and the `vmlinux` build-id) before the Run becomes
   installable.

A mismatch between a declared `sha256`/`size_bytes` and the stored object is rejected
(`uploaded artifact disagrees with its manifest`), so checksum the bytes you actually PUT.

## Related

- [Staging kernel source for `runs.build`](build-source-staging.md) — the **server**-build
  lane (let a worker build the kernel) instead of uploading one.
- [`artifacts` tool reference](../guide/reference/artifacts.md) — `create_run_upload`,
  `expected_uploads`, and chunked-upload parameters.
- [`runs` tool reference](../guide/reference/runs.md) — `runs.create` build profiles and
  `runs.complete_build`.

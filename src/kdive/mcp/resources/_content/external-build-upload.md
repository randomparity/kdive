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
magic, gzip layout, a `lib/modules` member); it never inspects or constrains your `.config`.
There is no allowed-config allowlist and no required-symbol check: enable what the
investigation needs.

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
| `effective_config` | to record the `.config` you built with | the kernel `.config` used for the build, ≤ 1 MiB. Stored for provenance; never validated against a symbol list. |
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

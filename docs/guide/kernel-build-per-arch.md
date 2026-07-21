# Per-architecture kernel build hints

Kernel packaging for the build lane varies by **target architecture**, and one difference is
easy to miss until it costs a full rebuild: what the `boot/vmlinuz` member of the upload must
actually be. Get it wrong and `runs.complete_build` rejects the upload *after* the whole
build-and-upload round-trip. This page is the at-a-glance per-arch reference so you shape the
boot image correctly the first time.

- Declare the target arch in the `build_profile` at `runs.create` (`arch`, default `x86_64`).
  The declared arch and the boot image must agree, or finalize rejects the upload.
- For the full procedural recipe (the exact `tar` invocation, `pigz`, member ordering, the
  upload flow) read the build lane:
  resource://kdive/docs/operating/external-build-upload.md — this page complements it, it does
  not repeat it.
- For the machine-readable byte contract (magic offsets per arch), call
  `artifacts.expected_uploads` and read
  `contracts.kernel.layout[boot/vmlinuz].formats_by_arch`.

## What differs by architecture

| `arch` | `boot/vmlinuz` must be | kdump `crashkernel` default |
|---|---|---|
| `x86_64` | the **bzImage** (`arch/x86/boot/bzImage`), renamed | `256M` |
| `ppc64le` | the **stripped ELF `vmlinux`** (powerpc has no bzImage) | `512M` |

The per-install `crashkernel` defaults, when a System gives no override, are
`512M on ppc64le, 256M on x86_64`. Everything else about the upload — one combined gzip tar,
the module tree, member order — is the same across arches (see the last section).

## x86_64

`boot/vmlinuz` is the **bzImage** — `arch/x86/boot/bzImage` from your built tree, renamed to
`boot/vmlinuz` in the tar. It carries the bzImage `HdrS` magic at offset `0x202`; the raw
`vmlinux` ELF is **not** accepted in the boot member (it belongs in the optional `vmlinux`
artifact). The bzImage is already stripped and compressed, so it is small and does not risk
the validator's decompress scan bound.

- Boot-member format the validator enforces: `bzImage`.
- kdump `crashkernel` default: `256M`.
- Native builds need no cross toolchain. If you cross-build, use
  `ARCH=x86_64 CROSS_COMPILE=x86_64-linux-gnu-` (or your distro's equivalent triple).

## ppc64le

powerpc has **no bzImage**. `boot/vmlinuz` is the **stripped ELF `vmlinux`** — the same thing
Fedora/RHEL install as `/boot/vmlinuz-<ver>`. The validator requires a 64-bit little-endian
ELF whose `e_machine` is `EM_PPC64`.

**Strip the build-tree `vmlinux` before you package it.** The unstripped `vmlinux` carries
full DWARF (hundreds of MB); left unstripped it pushes the `lib/modules` member past the
validator's 128 MiB decompress scan bound, and the upload is rejected as if `lib/modules` were
missing. Strip it to the bootable image first:

```bash
"${CROSS_COMPILE}strip" -s "$KBUILD/vmlinux" -o /tmp/vmlinuz   # stripped, bootable, tens of MB
```

Then tar `/tmp/vmlinuz` as `boot/vmlinuz`. The **unstripped** DWARF `vmlinux` is not discarded
— upload it as the optional `vmlinux` artifact (with a matching `build_id`) when you need
kernel debugging or offline vmcore analysis. It just does not go in the boot member.

- Boot-member format the validator enforces: `ppc64le ELF (vmlinux)`.
- kdump `crashkernel` default: `512M` (POWER's kdump floor is roughly double x86, so the kdump
  kernel does not OOM before `makedumpfile` runs).
- Cross-building from x86 uses `ARCH=powerpc CROSS_COMPILE=powerpc64le-linux-gnu-`.

## Same for every architecture

These rules do not vary by arch — only the `boot/vmlinuz` payload above does:

- **One combined artifact named `kernel`:** a single gzip tar holding `boot/vmlinuz` plus the
  `lib/modules/<release>/` tree. There is no separate `modules` upload.
- **gzip specifically** — a plain `.tar`, `.tar.xz`, or `.tar.zst` is rejected.
- **`boot/vmlinuz` first** — validation scans at most the first 128 MiB of *decompressed*
  output (a gzip-bomb guard), so the `lib/modules` header must fall within it; list the boot
  image before the module tree.
- **At least one real module** — a `*.ko`, `.ko.xz`, `.ko.gz`, or `.ko.zst` under
  `lib/modules/<release>/`; a bare directory or a lone `modules.dep` is rejected.
- **Drop the back-reference symlinks** — exclude the `build` and `source` symlinks
  `make modules_install` plants under `lib/modules/<release>/`.

The exact `tar` recipe that produces this layout for each arch, plus the `pigz` fast path and
the upload flow, is in resource://kdive/docs/operating/external-build-upload.md.

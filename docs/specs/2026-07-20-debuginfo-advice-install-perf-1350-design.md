# debuginfo advice + install-path performance (#1350)

Status: Draft
Issue: #1350
ADR: [0399](../adr/0399-single-pass-kernel-bundle-and-scratch-staging.md)

## Problem

One agent session exposed three connected gaps. An agent enabled
`CONFIG_DEBUG_INFO_DWARF5=y` for a boot-time panic reproducer whose only evidence
was a serial-console message — an investigation that needs no post-boot symbol
resolution. DWARF5 embeds debug tables in every `.ko`, inflating the module tree
from tens of MB to ~2 GB. The combined kernel tar then reached 2 GB, and the
install path made that worse:

1. **The `debuginfo` feature advice gives no when/cost signal.** The
   `FEATURE_REQUIREMENTS` `debuginfo` summary
   (`kernel_config/requirements.py`) reads only *"Resolve symbols for gdb/drgn
   debugging (build with DWARF or BTF)."* It is surfaced by
   `artifacts.feature_config_requirements`, which `runs.create` lists in
   `suggested_next_actions` on every Run. An agent has no basis to tell an
   advisory-only recommendation from a situationally appropriate one, so it
   enables DWARF5 by default.

2. **The install path decompresses the combined tar twice.**
   `extract_boot_vmlinuz` and `repack_modules_subtree`
   (`providers/local_libvirt/lifecycle/boot/kernel_bundle.py`) each open and
   fully stream the combined gzip tar independently. On a DWARF-bloated 2 GB tar
   on ppc64le this compounds to a second full decompression before the
   libguestfs inject starts.

3. **Large install intermediates are pinned to the persistent staging root.**
   The combined tar and the repacked modules tar (and, for a debuginfo run, the
   fetched `vmlinux`) are transient scratch, but they live in the per-Run
   `KDIVE_INSTALL_STAGING` directory alongside the persistent `kernel`/`initrd`.
   An operator with RAM headroom cannot point that scratch at a tmpfs without
   also moving the artifacts libvirt's `<kernel>` element must be able to read
   for the System's lifetime.

## Goals

- The `debuginfo` advice names **when** it is useful and **the cost** it carries,
  so an agent reading the manifest can decide against DWARF5 for a
  console-log-only investigation.
- The install path decompresses the combined tar **once**, not twice, when a
  modules subtree is needed (kdump or debuginfo runs — exactly the bloat case).
- An operator can route the large, transient install intermediates to a named
  temp location (e.g. a tmpfs mount) **without** moving the persistent
  boot artifacts, and with **no behavior change when the option is unset**.

## Non-goals

- **Streaming fetch-and-extract (issue's 2b).** `store.get_artifact` returns the
  whole object as `bytes`; streaming the S3 body straight into the tar extractor
  would rework a shared store contract (sensitivity metadata, redaction, error
  mapping) for a secondary time-to-first-byte win. Deferred to #1351 and
  recorded as a rejected alternative in ADR-0399. This spec's tmpfs-scratch
  option is *complementary*, not a substitute — see the memory tradeoff below.
- **Making `artifacts.feature_config_requirements` conditional on investigation
  type.** No clean investigation-type→introspection signal exists at
  `runs.create` time; gating the suggestion on one is speculative. The manifest
  stays unconditional and is made self-describing instead (rejected alternative
  in ADR-0399).
- **Changing the combined-tar upload validator or its scan bound** (ADR-0396).
  This spec is the *install-side* read of an already-validated tar.
- **Enforcing DWARF omission.** The advice is advisory; kdive never validates the
  uploaded config and an agent may still enable DWARF5.

## Design

### Part 1 — `debuginfo` advice (agent-facing)

Rewrite the `debuginfo` `summary` in `FEATURE_REQUIREMENTS` to name the use case
and the cost, e.g.:

> Enable only for live drgn/gdb symbol resolution or offline vmcore analysis.
> Embeds DWARF tables in every .ko - can grow the module tree 10-50x and slow
> upload and install. Omit for boot-time crash reproducers and console-log
> investigations where no post-boot introspection is needed.

(ASCII in the code string literal to match the sibling `FEATURE_REQUIREMENTS`
summaries and avoid terminal/encoding surprises; the prose form may stay
typographic.)

The summary flows verbatim through `feature_manifest()` into the
`artifacts.feature_config_requirements` response `data`, which is the text the
agent reads. No wrapper-docstring change is needed for the summary itself, but
the manifest tool wrapper docstring is checked to confirm it does not contradict
the new advice. `suggested_next_actions` on `runs.create` is left unchanged
(unconditional) per the non-goal above.

### Part 2a — single-pass kernel-bundle extraction

Replace `extract_boot_vmlinuz` and `repack_modules_subtree` with one
`extract_kernel_bundle(combined_tar, kernel_dest, modules_dest)` that opens the
combined tar **once** and, in a single forward `capped_tar_members` walk:

- extracts the first `boot/vmlinuz` member to `kernel_dest` (verbatim, arch-opaque,
  temp-then-rename), and
- when `modules_dest is not None`, repacks the `lib/modules/` subtree into it,
  returning whether a modules subtree was found.

When `modules_dest is None` (the common non-kdump/non-debuginfo run) only the boot
member is extracted — still one pass. Every security bound is preserved
unchanged: `capped_tar_members` (member-count bomb), `reject_oversize_member` on
both the boot member's declared size and the cumulative module-tree size, and the
`..`-path skip on module members. The extract-in-loop discipline
(`extractfile(member)` consumed before advancing the iterator) is the exact
pattern `repack_modules_subtree` already uses under `r:gz`, so the single-pass
merge decompresses the gzip stream exactly once with no backward seek.

`extract_boot_vmlinuz` and `repack_modules_subtree` are removed (replace, don't
deprecate); `install.py` imports/`__all__` and the kernel-bundle tests move to
`extract_kernel_bundle`.

### Part 2c — configurable scratch staging (`KDIVE_INSTALL_SCRATCH`)

Add a worker Setting `KDIVE_INSTALL_SCRATCH`. **Unset ⇒ it resolves to the
`KDIVE_INSTALL_STAGING` root**, so `scratch_dir == staging_dir` and behavior is
byte-identical to today. When set, the per-Run scratch dir is
`{scratch_root}/{system_id}/{run_id}` and the install routes only the transient
intermediates there:

| file | location | lifetime |
|---|---|---|
| `kernel.tar.gz` (combined) | scratch | deleted after extract |
| `modules.tar.gz` (repacked) | scratch | deleted after inject |
| `vmlinux` (debuginfo, pre-inject) | scratch | deleted after inject |
| `kernel` (`<kernel>` element) | **staging** | System lifetime |
| `initrd` (`<initrd>` element) | **staging** | System lifetime |

Each temp+final rename pair stays within one directory (one filesystem), so no
cross-device `rename` fault is introduced when scratch is a separate mount. The
per-Run scratch dir is created with the same permission/OS error contract as the
staging dir, naming `KDIVE_INSTALL_SCRATCH` in the remedy. Intermediate cleanup
is extended to also remove the fetched `vmlinux` (today it is leaked — harmless
on disk, but leaked RAM on a tmpfs scratch).

### Memory tradeoff (documented together with 2b)

tmpfs scratch trades disk for RAM: `get_artifact` already materializes the whole
object as `bytes`, so during fetch the 2 GB is briefly resident and then freed
once written to disk. With scratch on tmpfs those same bytes stay resident for
the whole extract+inject window, plus the repacked modules tar — up to ~4 GB
resident, multiplied by concurrent installs. The RAM-free win is the deferred 2b
streaming path. The `KDIVE_INSTALL_SCRATCH` help text and the operator docs state
this explicitly and guide sizing tmpfs against host RAM and worker concurrency,
so an operator does not enable tmpfs and OOM a shared worker on a 2 GB tar.

## AI-surface note

The `debuginfo` summary is agent-facing *advice text* served through a read-only
tool; it is not an LLM call, prompt, retrieval path, or classifier that kdive
operates. No model-eval plan applies. The success signal is a unit assertion that
the manifest entry names both the use case (live drgn/gdb / offline vmcore) and
the cost (per-`.ko` DWARF growth), so the when/cost signal cannot silently
regress.

## Acceptance criteria

1. `artifacts.feature_config_requirements` returns a `debuginfo` entry whose
   summary names the live/offline use case **and** the per-`.ko` growth cost;
   a test asserts both substrings.
2. `extract_kernel_bundle` extracts `boot/vmlinuz` byte-identically (x86_64 and
   ppc64le members, incl. a `./`-prefixed member) and, given a `modules_dest`,
   repacks the `lib/modules/<ver>/` subtree; given `modules_dest=None` it extracts
   only the boot member and touches no modules tar.
3. The combined tar is decompressed once: a `tarfile.open` counter that records
   path+mode shows the combined-tar path opened exactly once in read mode for a
   modules-needed run (the repacked modules tar's write-mode open is separate).
4. All existing bounds hold on the merged function: member-count bomb →
   `CONFIGURATION_ERROR`; oversize boot member → `CONFIGURATION_ERROR`; oversize
   cumulative module tree → `CONFIGURATION_ERROR` with the `.part` temp cleaned;
   missing boot member → `INFRASTRUCTURE_FAILURE`; corrupt tar →
   `INFRASTRUCTURE_FAILURE`.
5. With `KDIVE_INSTALL_SCRATCH` unset, install writes intermediates to the staging
   dir (unchanged). With it set to a distinct dir, `kernel`/`initrd` land in
   staging while `kernel.tar.gz`/`modules.tar.gz`/`vmlinux` land in scratch and
   are removed after use; an unwritable scratch root fails with
   `CONFIGURATION_ERROR` naming `KDIVE_INSTALL_SCRATCH`.
6. `just ci` green (lint, ty whole-tree, tests, doc guards). The generated config
   docs include the new setting (`just config-docs`).

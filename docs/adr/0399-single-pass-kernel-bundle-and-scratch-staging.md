# 0399 — Single-pass kernel-bundle extraction, configurable scratch staging, and self-describing debuginfo advice

Status: Proposed

## Context

A boot-time panic reproducer needs no post-boot symbol resolution — its evidence
is a serial-console message. An agent nonetheless enabled
`CONFIG_DEBUG_INFO_DWARF5=y`, which embeds DWARF tables in every `.ko` and grew
the module tree to ~2 GB, so the combined `kernel` tar reached 2 GB and the
install was needlessly slow (#1350). Three install-path facts turned an avoidable
config choice into a slow install:

1. The `debuginfo` entry in `FEATURE_REQUIREMENTS`
   (`kernel_config/requirements.py`) summarised only *what* debuginfo does, not
   *when* it is worth its cost. `artifacts.feature_config_requirements` serves
   that summary and `runs.create` lists the tool in `suggested_next_actions` on
   every Run, so the advice an agent reads gave no signal to omit DWARF5 for a
   console-log-only investigation. (`AGENTS.md`: the agent acts on the text the
   tool surfaces; guidance it cannot read it will not follow.)

2. `extract_boot_vmlinuz` and `repack_modules_subtree`
   (`.../boot/kernel_bundle.py`) each open and fully decompress the combined gzip
   tar. On a 2 GB DWARF tar that is two full decompressions before libguestfs
   injects.

3. The transient install intermediates (combined tar, repacked modules tar, the
   fetched `vmlinux`) live in the persistent per-Run `KDIVE_INSTALL_STAGING`
   directory next to the `kernel`/`initrd` that libvirt's `<kernel>`/`<initrd>`
   must read for the System's lifetime. An operator cannot move the scratch to a
   tmpfs without also moving the persistent boot artifacts onto volatile storage.

## Decision

**1. Make the `debuginfo` advice self-describing; keep the suggestion
unconditional.** Rewrite the `debuginfo` `summary` to name the use case (live
drgn/gdb symbol resolution or offline vmcore analysis) and the cost (DWARF in
every `.ko`; 10–50× module-tree growth; slower upload/install) and to say to omit
it for boot-time crash reproducers and console-log investigations. The summary is
surfaced verbatim by `artifacts.feature_config_requirements`. The tool stays in
`runs.create`'s `suggested_next_actions` unconditionally.

**2. Single-pass `extract_kernel_bundle`.** Replace `extract_boot_vmlinuz` and
`repack_modules_subtree` with one function that opens the combined tar once and,
in a single forward `capped_tar_members` walk, extracts the first `boot/vmlinuz`
member to a kernel destination and — when a `modules_dest` is given — repacks the
`lib/modules/` subtree into it, returning whether modules were found. Every
existing bound is preserved on the merged path: the member-count cap
(`capped_tar_members`), `reject_oversize_member` on both the boot member's
declared size and the cumulative module-tree size, the `..`-path skip, and
temp-then-rename for both outputs. When no modules are needed (the common
non-kdump/non-debuginfo run) only the boot member is read — still one pass. The
two old functions are removed (replace, don't deprecate); callers, `__all__`, and
the kernel-bundle tests move to `extract_kernel_bundle`.

**3. Configurable scratch staging (`KDIVE_INSTALL_SCRATCH`).** Add a worker
Setting for a scratch root that defaults, when unset, to the
`KDIVE_INSTALL_STAGING` root — so `scratch_dir == staging_dir` and behavior is
byte-identical to today. When set, only the transient intermediates
(`kernel.tar.gz`, `modules.tar.gz`, pre-inject `vmlinux`) go to
`{scratch_root}/{system_id}/{run_id}`; the persistent `kernel`/`initrd` stay in
the staging dir. Each temp+final rename pair stays within one directory, so no
cross-device rename is introduced when scratch is a separate mount. The scratch
dir is created with the same permission/OS error contract as the staging dir,
naming `KDIVE_INSTALL_SCRATCH` in the remedy, and intermediate cleanup is extended
to remove the fetched `vmlinux` (previously leaked).

**4. Document the tmpfs/streaming memory tradeoff together.** Because
`store.get_artifact` materialises the whole object as `bytes`, a tmpfs scratch
holds those same bytes resident for the extract+inject window (plus the repacked
modules tar) — up to ~4 GB, multiplied by concurrent installs. The RAM-free win
is streaming fetch (deferred, below). The setting's help text and operator docs
state this and guide sizing tmpfs against host RAM and worker concurrency.

## Consequences

- The bloat is attacked at the source (advice) and mitigated on the path
  (one decompression instead of two; optional tmpfs scratch on RAM-rich hosts).
- The default install path is unchanged when `KDIVE_INSTALL_SCRATCH` is unset; the
  scratch split is opt-in and the persist-vs-volatile boundary keeps the
  `<kernel>` path on durable storage.
- One fewer public function pair to keep in sync; the security bounds live in one
  place on the merged path.
- A new worker Setting requires regenerating the config docs (`just config-docs`).
- tmpfs scratch is a sharp tool: it trades disk for RAM and can OOM a shared
  worker on a 2 GB tar. The tradeoff is documented, not gated in code — operators
  opt in with the memory story in front of them.

## Considered & rejected

- **Streaming fetch-and-extract (issue's 2b).** Stream the S3 body straight into
  the tar extractor, avoiding both the 2 GB `bytes` materialisation and the
  on-disk copy. This is the genuine RAM-free win but reworks the shared
  `store.get_artifact` contract (sensitivity metadata, redaction, error mapping)
  for a benefit secondary to the doubled-decompression fix. Deferred to #1351
  with its own ADR; the tmpfs-scratch option here is complementary (Decision 4),
  not a substitute.
- **Gate `artifacts.feature_config_requirements` on investigation type.** Suggest
  the debuginfo advice only when the Run's profile implies live introspection. No
  clean investigation-type→introspection signal exists at `runs.create`; inventing
  one is speculative. Making the manifest self-describing (Decision 1) lets the
  agent decide from the text it already reads.
- **Force the scratch onto tmpfs / a fixed temp dir.** Rejected: the RAM tradeoff
  makes a forced default unsafe on shared workers. The seam defaults to today's
  behavior and is opt-in.
- **Keep two passes but cache the decompressed tar.** Rejected: holding the whole
  decompressed 2 GB to reuse across two passes is the RAM cost we are avoiding;
  one forward pass needs no cache.

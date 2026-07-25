# Durable staging and a re-verified reuse path for the uploaded rootfs base (#1526)

- **Issue:** [#1526](https://github.com/randomparity/kdive/issues/1526)
- **ADR:** [ADR-0443](../adr/0443-durable-rootfs-staging-and-reuse-recheck.md)
- **Status:** implemented

## Problem

Two gaps on the investigation-scoped uploaded-rootfs staging path
(`providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`) compound into silent data
corruption.

**The publish is not durable.** `stage_uploaded_rootfs` closes the `.partial` writer and calls
`os.replace(partial, dest)` with no `fsync` of the file and none of the containing directory.
`os.replace` is atomic against concurrent *readers*, not against a host crash: under ext4's default
`data=ordered` with delayed allocation the rename can reach the journal while the data blocks
behind it have not, leaving `dest` at full length with zeros or stale blocks in it. Both stagers —
identity and gzip — have this shape, each opening its own writer.

**The reuse path verifies nothing.** `fetch_uploaded_rootfs` treats any present `dest` as
authoritative: `if dest.is_file(): return dest`, once before the fetch lock and again after it. No
checksum, no format probe, no size.

Together they are worse than either alone. A crash mid-stage leaves a corrupt qcow2 at the
content-addressed path; ADR-0441 §5 makes that path shared by every System in the investigation, so
every subsequent guest silently boots off it until the investigation closes — and the checksum
machinery is skipped *precisely because* the file exists. Nothing in the pipeline ever looks at the
base again.

Surfaced by the `/challenge` review on #1520, which flagged it as out of scope for that refactor.

## Requirements

- **R1** — A staged base's bytes are durable before the rename that publishes them, on both the
  identity and the gzip stager.
- **R2** — The rename itself is durable, so a crash cannot resurrect the consumed `.partial`. Scoped
  to a staging directory that already exists: the first stage into a fresh investigation can still
  lose `<investigation_id>/` whole, which takes `dest` and the `.partial` with it and is therefore
  the benign case R2 exists to avoid (ADR-0443 §1, stated exclusion).
- **R3** — The reuse fast path re-verifies a present base before returning it, on **both** sides of
  the fetch lock, and re-stages one that fails.
- **R4** — The re-verification is O(1) in the base size. A base can be tens of GiB and the check
  runs on every System provision in the investigation.
- **R5** — The re-verification never false-rejects a base the staging path itself produced, or it
  would re-download on every provision forever.
- **R6** — The durability cost falls only on bases that are actually published — including bases
  rejected by the format gate, which runs after the stager returns.
- **R7** — An unreadable base is reported, not silently re-downloaded; a rejected one is logged.

## Design

### Sync the partial before the rename, and the directory after

`_durable_replace` replaces the bare `os.replace(partial, dest)`: it `fsync`s the partial, renames,
then `fsync`s the staging directory — the flush → `fsync` → `os.replace` shape
`inventory/writeback.py` already uses for the systems TOML. The stagers are untouched.

Placing the sync at the publish point rather than at each stager's writer close is what satisfies R6
in full. Every gate — each stager's checksum and the shared qcow2-magic gate — has already raised by
then, so no rejection pays for a flush. A per-stager sync would miss the format gate specifically,
since it runs after the stager returns: a checksum-valid raw or vmdk upload of up to 50 GiB would be
flushed and then discarded. `_fsync_path` opens the partial `O_WRONLY` (POSIX lets `fsync` on a
read-only descriptor return `EBADF`).

The directory sync is not about the data — an absent `dest` after a crash simply re-stages — but the
same directory entry carries the partial's unlink, so without it a lost rename can resurrect the
partial as a SENSITIVE orphan.

Both raise `OSError`, which the existing `except OSError` in `stage_uploaded_rootfs` already maps to
the uniform `INFRASTRUCTURE_FAILURE` naming `dest`.

### Re-verify on reuse with the format gate that already exists

`_starts_with_qcow2_magic` is extracted from `_require_qcow2_magic` as the single implementation of
the format probe. `_require_qcow2_magic` (the staging gate) keeps raising `CONFIGURATION_ERROR`;
`_reusable_staged_base` (the reuse gate) wraps the same probe. Both `if dest.is_file()` sites in
`fetch_uploaded_rootfs` become `if _reusable_staged_base(dest, system_id=...)`; a rejected base falls
through to the existing lock-and-stage path, whose `os.replace` supersedes it, so no separate unlink
is needed (and would not help — see the ADR's accepted residue on superseding an inode a guest holds
open).

The gate distinguishes "no usable base" from "cannot look" (R7). `FileNotFoundError`,
`NotADirectoryError`, and `IsADirectoryError` — the set `is_file()` answered `False` for — return
`False`; every other `OSError` raises `_unreadable_base_fault`. `is_file()` was a `stat` needing only
traverse permission, while the probe `open`s the file, so `EACCES`/`EMFILE`/`EIO` are newly
reachable and none of them means the base is corrupt.

### Log the rejection

The module gains `_log = logging.getLogger(__name__)` (the package convention) and each rejection
site emits a `WARNING` naming `dest`, the investigation, and the System. The re-stage succeeds, so
the log line is the *only* evidence the durability bug ever fired.

The post-lock site is gated on `not stale_on_arrival` — the pre-lock `dest.exists()`. Nothing
between the two checks repairs `dest`, so an ungated sibling-attributed line would fire on every
ordinary stale-base rejection and blame a race that never happened. The re-verification runs
unconditionally on both sides; only the attribution is gated.

The reuse-read fault (`_unreadable_base_fault`) is separate from `_staging_fault`: nothing is being
staged, so the message names a present-but-unreadable file rather than a failed download.

The magic read is a 4-byte read (R4) and is exactly what the staging path already asserted about
the same bytes, so it cannot false-reject a base this code produced (R5).

**What the gate does and does not catch.** It catches truncation, an empty file, garbage written over
the path, and a whole-file-zeroed base. It does *not* catch damage past the first four bytes — and
because the rename follows the completed write, a multi-GiB base's dirty residue at crash time is
its tail, so the head-intact/tail-torn base is the expected crash survivor and passes. The reuse
gate is therefore a partial re-validation of bases staged before this change, not a crash-torn-base
detector; the `fsync` is what closes the crash case going forward. ADR-0443 §3 states this in full
and #1539 tracks the sidecar completion marker that would close the residue.

Two alternatives are ruled out by the data rather than by preference:

- **Size comparison.** `artifacts.uncompressed_size` is an upper *bound*, not an exact size —
  `strip_gzip_to_writer` caps decompressed output at it and accepts less — and it is NULL on the
  identity path (`uncompressed_size is only meaningful with a transport encoding`). An equality gate
  would therefore reject good bases and re-download them on every provision, violating R5.
- **Full checksum re-verify.** Correct but O(filesize) on every guest start, which would undo the
  point of staging once per investigation. Violates R4.

## Acceptance criteria

- **AC-1** — Staging a base `fsync`s the partial, *then* renames, *then* `fsync`s the directory —
  asserted as an inode-tagged syscall sequence, not merely as "a sync happened". Holds on the gzip
  stager as well as the identity one.
- **AC-2** — A staging attempt that fails verification performs no `fsync` — covering both the
  checksum arm (raised inside the stager) and the format arm (raised after it returns).
- **AC-3** — A full-length zeroed base at `dest` (the crash-torn shape), a base truncated below the
  magic, an empty file, and a garbage document are each rejected on reuse and re-fetched from the
  object store; the caller receives the re-staged bytes.
- **AC-4** — A base that appears during the lock wait is reused when valid and re-staged when torn,
  proving the post-lock call site re-verifies too.
- **AC-5** — The reuse check reads a bounded number of bytes of the base, not the whole file.
- **AC-6** — A valid present base is still a cache hit that takes neither the lock nor the store.
- **AC-7** — A present base the probe cannot read (`EACCES`) raises `INFRASTRUCTURE_FAILURE` naming
  `dest`, attempts no download, leaves the base untouched, and says it could not *read* the base
  rather than that it failed to stage one.
- **AC-8** — Every reuse rejection emits a `WARNING` naming `dest`. The sibling-attributed line is
  emitted **only** when the base appeared during the lock wait, and never on an ordinary stale-base
  rejection.

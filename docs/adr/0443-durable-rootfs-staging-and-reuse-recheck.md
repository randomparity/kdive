# ADR 0443 — The staged rootfs base is fsynced before publish and re-verified on reuse

- **Status:** Accepted
- **Date:** 2026-07-24
- **Amends:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 — the staging path's
  atomicity guarantee gains a durability half (`fsync` before `os.replace`, directory `fsync`
  after), and the content-addressed reuse fast path stops treating a present file as authoritative.
  Every other ADR-0441 decision is untouched; ADR-0442's reclaim order and gate are unaffected.
- **Depends on:** [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) (the qcow2-magic
  format gate this reuses as the reuse check), [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md)
  (the content-addressed shared staging path whose reuse this hardens).
- **Spec:** [`../specs/2026-07-24-durable-rootfs-staging-and-reuse-recheck-1526-design.md`](../specs/2026-07-24-durable-rootfs-staging-and-reuse-recheck-1526-design.md)

## Context

ADR-0441 §5 states the staging correctness guarantee as *atomicity*: each fetcher writes a unique
`<token>.<uuid>.partial` and `os.replace`s it onto `<token>.qcow2` only after verify, so "`dest` is
only ever a verified base". That is true against a concurrent reader and false against a host
crash.

`os.replace` is atomic with respect to other processes observing the directory entry. It says
nothing about the order in which the *data* behind that entry reaches stable storage. On a default
ext4 `data=ordered` mount with delayed allocation, a multi-GiB write followed immediately by a
rename can commit the rename to the journal while the data extents are still unallocated or
unwritten. The post-crash result is a file at `dest` of the correct length whose contents are zeros
or stale blocks. Nothing in the pipeline distinguishes it from a good base.

The second half is what turns that from a bad provision into a persistent one. `fetch_uploaded_rootfs`
short-circuits on `if dest.is_file(): return dest`, before the fetch lock and again after it. It
verifies nothing — no checksum, no format probe, no size. Under ADR-0441 §5 the staging path is
content-addressed and shared by *every* System in the investigation, so the first torn base becomes
the base for all of them, for the life of the investigation. The checksum machinery is bypassed
precisely because the file exists, and the base is never re-examined: `qemu-img create -b` will
happily build an overlay on a zeroed backing file, and the failure surfaces as an unbootable or
subtly wrong guest, attributed to the kernel under test.

The two defects also mask each other diagnostically. Without the re-check, a durability bug leaves
no trace — the reuse path reports success. Without the durability fix, a re-check that rejects the
base looks like a flaky object store.

This is the deployment ADR-0441 §8 scopes the feature to: a single local-libvirt host whose worker
stages the base and whose guests boot off it. There is no replication and no second copy; the local
file is the only thing standing between an upload and a guest.

## Decision

### 1. `fsync` the partial before the rename, and the staging directory after

A `_durable_partial_writer` context manager replaces the bare `partial.open("wb")` in both stagers.
It yields the writer and, after the body returns, `flush()`es and `os.fsync()`s the descriptor.
`_durable_replace` then performs the `os.replace` and `fsync`s `dest.parent`.

This is not a new pattern in the repo — `inventory/writeback.py` already writes the systems TOML as
flush → `fsync` → `os.replace`. What is new is applying it to a file whose loss is silent rather
than loud, and adding the directory sync, which the TOML writer omits because a lost inventory
rename is self-evident on the next read.

The directory `fsync` is deliberately *not* justified by the data. A rename lost to a crash leaves
no `dest`, and an absent `dest` is simply re-staged — benign. It is justified by the *other* half of
the same directory entry: the rename consumes the `.partial` name, and losing that unlink resurrects
a multi-GiB SENSITIVE partial as an orphan. Syncing the pair together is one metadata sync per
staged base.

Both helpers raise `OSError`, which `stage_uploaded_rootfs`'s existing `except OSError` already maps
to the uniform `INFRASTRUCTURE_FAILURE` naming `dest`. A directory sync that fails after a
successful rename therefore fails the provision while leaving a good `dest` behind — the next fetch
reuses it, which is the honest outcome: the bytes are fine, only their durability is unproven.

The sync sits *after* the `yield`, so a stager that raises skips it. The identity path's checksum
comparison moves inside the writer context to take advantage of that: a mismatched multi-GiB
download is discarded by the existing `finally` without first being flushed to disk. The gzip path
already raises from inside its writer context. The cost of the fix is therefore one full flush per
*published* base and none per rejected one.

### 2. The reuse fast path re-applies the qcow2-magic gate

`_starts_with_qcow2_magic` is extracted from `_require_qcow2_magic` as the single implementation of
the format probe, and `_reusable_staged_base` wraps it for the reuse path, returning `False` on any
`OSError`. Both `if dest.is_file()` sites become `if _reusable_staged_base(dest)`.

Extracting rather than writing a second probe is the point: the repo has already been bitten by a
duplicated magic check that was written as an equality against a whole prefix and was therefore a
dead guard (the ELF-magic case, #1383/ADR-0412). One implementation, two callers, one raising and
one returning a bool.

The re-check runs on **both** sides of the lock. A re-check on the pre-lock site alone would leave
the hole open for exactly the fetchers that queued — the ones a torn base is most likely to be
handed to, since a sibling crashing mid-stage is what produces both the torn base and the queue.

A rejected base needs no unlink. It falls through to the existing lock-and-stage path, whose
`os.replace` overwrites it in place, so there is never a window in which the investigation has no
base at all.

### 3. Magic-only, not size and not checksum

The check is O(1) because it runs on the per-System provision hot path against a base of up to
50 GiB (the ADR-0437 canonical-object cap), once per System in the investigation.

**Size was evaluated and is not available.** `artifacts.uncompressed_size` is an upper *bound*, not
an exact size — `strip_gzip_to_writer` caps decompressed output at it and accepts less — and it is
NULL on the identity path, where the declaration validator rejects it outright
(`uncompressed_size is only meaningful with a transport encoding`). An equality gate would
false-reject bases this very code produced and re-download them on every provision, converting a
rare corruption into a permanent one. A `<=` gate is satisfied by every truncation and catches
nothing. Size is therefore not a weaker option to be traded off; it is not an option.

**Checksum was evaluated and is unaffordable.** Re-hashing tens of GiB before every guest start
directly negates ADR-0441's decision to stage once per investigation.

What magic-only buys, precisely: it catches a zeroed head, a truncation below four bytes, an empty
file, and a garbage document written over the path. What it does not catch: corruption confined to
the interior of a base whose first four bytes survived. Delayed allocation makes the zeroed-head
case the common one — a rename that beat its data typically beat *all* of it — but the residue is
real and is stated here rather than papered over.

That residue is why the ordering of the two halves matters. Decision 1 removes the crash window that
produces a torn base at all; decision 2 is the net for a base staged by code that predates decision
1, and for corruption arriving by some other route (a bad disk, an operator's `cp`, a half-restored
backup). Neither is sufficient alone, and the reuse gate is explicitly *not* claimed to be a
verification of the base — only a rejection of the shapes a broken one takes.

## Consequences

- A host crash mid-stage can no longer leave a base that silently backs every guest in the
  investigation. This is the outcome the ADR exists for.
- One `fsync` of the full base and one of the staging directory per published base. On the identity
  path the data is already being written through the page cache during a multi-GiB download, so the
  sync is largely a wait for writeback that would have happened anyway; the wall-clock cost is
  bounded by the disk, not added to it. It is paid once per investigation per checksum, never per
  System.
- Bases staged by pre-ADR-0443 code are not trusted. A torn one is re-downloaded once, silently and
  correctly. A good one passes the magic gate and is reused as before, so there is no mass
  re-download on upgrade.
- The reuse fast path now performs a file `open` + 4-byte read where it performed a `stat`. Both are
  a single syscall pair against warm dentry cache; the provision path around it opens the file for
  `qemu-img` moments later regardless.
- `_require_qcow2_magic` and `_reusable_staged_base` share one probe, so a future format change
  (a second magic, a version check) lands in one place and cannot leave the reuse gate behind.
- Interior corruption of a base whose header survives is still reused. Detecting it requires reading
  the base, which is the trade decision 3 declines. If that residue ever needs closing, the shape is
  decision 3's rejected sidecar marker, not a hot-path checksum.

## Considered & rejected

- **A sidecar completion marker** — write and `fsync` a `<token>.ready` sibling only after the base
  is durable, and gate reuse on the *marker* rather than on the base. Strictly the most rigorous
  option: it catches interior corruption from a crash too, because a crash before the marker means
  no marker regardless of what the base looks like, and pre-ADR-0443 bases self-heal by re-staging
  once. Rejected for this change on two grounds. First, blast radius: the marker is a second file in
  the per-investigation staging directory, so `jobs/handlers/artifacts/rootfs_reclaim.py` must
  unlink it in `_unlink_staged_base` **and** account for it in `sweep_investigation_staging_dir`,
  whose `rmdir` of the now-empty directory would otherwise fail forever and leak one directory per
  investigation — a reclaim regression introduced by a durability fix, in a file this change
  otherwise does not touch. Second, it does not subsume decision 1: a marker written and synced
  after a *non*-synced base still admits a crash that loses the base's data and keeps both the
  rename and the marker, because nothing ordered them. The marker is a complement to the `fsync`,
  not a substitute, and its incremental value over `fsync` + magic is the interior-corruption
  residue alone. That is not worth a cross-file reclaim change here; it is recorded as the shape to
  reach for if the residue proves real.
- **Re-verify the checksum on reuse.** The only check that fully answers "is this the base I
  staged". O(filesize) on every guest start against a base up to the 50 GiB cap, which negates
  ADR-0441's stage-once decision. Rejected in decision 3.
- **Compare `dest`'s size to `artifacts.uncompressed_size`.** Rejected in decision 3 as unavailable
  rather than merely weak: the column is an upper bound and is NULL on the identity path.
- **Run `qemu-img check` on reuse.** Reads only qcow2 metadata, so it is sub-linear and would catch
  more than the magic. But it spawns a subprocess per provision, adds a hard dependency on
  `qemu-img`'s exit-code taxonomy to the fetch path, and still does not verify the data clusters —
  a poor trade for the increment over a 4-byte read.
- **`O_DSYNC`/`O_SYNC` on the partial instead of a closing `fsync`.** Makes every one of the
  4 MiB streaming writes synchronous, serializing the download against the disk for the whole
  multi-GiB transfer rather than syncing once at the end. Strictly worse for the same guarantee.
- **Sync only the directory, not the file.** A misreading of what the rename guarantees: it would
  make the *name* durable while leaving the data it points at unwritten, which is the bug.
- **Leave the reuse path alone on the grounds that decision 1 makes a torn base impossible.** True
  only for bases staged after this change, and only for crashes. It would leave every base staged by
  currently-deployed code permanently trusted, which is the population most likely to contain one.

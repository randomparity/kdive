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

`_durable_replace` replaces the bare `os.replace(partial, dest)`. It `fsync`s the partial, renames
it onto `dest`, and then `fsync`s `dest.parent`. The stagers are untouched and keep writing through
a plain `partial.open("wb")`.

This is not a new pattern in the repo — `inventory/writeback.py` already writes the systems TOML as
flush → `fsync` → `os.replace`. What is new is applying it to a file whose loss is silent rather
than loud, and adding the directory sync, which the TOML writer omits because a lost inventory
rename is self-evident on the next read.

**The sync belongs at the publish point, not at each stager's writer close.** This is the decision
that keeps the cost honest. Every verification gate — each stager's checksum and the shared
qcow2-magic gate — has already run and raised by the time `_durable_replace` is called, so *no*
rejection path pays for a flush. Syncing as each stager closed its writer would look equivalent and
is not: the format gate runs *after* the stager returns, so a checksum-valid upload that is a raw or
vmdk image rather than a qcow2 — an ordinary operator mistake, up to the 50 GiB canonical cap —
would be flushed in full and then unlinked. It also leaves durability at one site instead of one per
codec, so a future third codec cannot forget it.

The partial's writer is closed by then, so `_fsync_path` opens a fresh descriptor on the same inode;
its bytes are in the page cache and are flushed identically. The partial is opened `O_WRONLY` rather
than `O_RDONLY` because POSIX leaves `fsync` on a read-only descriptor free to return `EBADF`;
Linux happens to allow it, but a durability guarantee should not rest on that.

The directory `fsync` is deliberately *not* justified by the data. A rename lost to a crash leaves
no `dest`, and an absent `dest` is simply re-staged — benign. It is justified by the *other* half of
the same directory entry: the rename consumes the `.partial` name, and losing that unlink resurrects
a multi-GiB SENSITIVE partial as an orphan. Syncing the pair together is one metadata sync per
staged base.

Both syncs raise `OSError`, which `stage_uploaded_rootfs`'s existing `except OSError` already maps
to the uniform `INFRASTRUCTURE_FAILURE` naming `dest`. A directory sync that fails after a
successful rename therefore fails the provision while leaving a good `dest` behind — the next fetch
reuses it, which is the honest outcome: the bytes are fine, only their durability is unproven.

**Stated exclusion.** `dest.parent` is `<uploads_dir>/<investigation_id>/`, and `fsync`ing a
directory does not make that directory's own link in *its* parent durable. So on the first stage
into a fresh investigation, a crash can still lose `<investigation_id>/` entirely. This is left
unfixed rather than closed with a second sync, because losing the directory loses `dest` and the
consumed `.partial` **together** — which is precisely the state both halves of the directory-sync
argument are designed for. The next fetch finds nothing, re-stages, and there is no orphan to
resurrect. The guarantee is therefore "the rename is durable within an already-durable staging
directory", and adding a syscall to make a formal statement true while changing no outcome is not a
trade worth making.

### 2. The reuse fast path re-applies the qcow2-magic gate

`_starts_with_qcow2_magic` is extracted from `_require_qcow2_magic` as the single implementation of
the format probe, and `_reusable_staged_base` wraps it for the reuse path. Both `if dest.is_file()`
sites become `if _reusable_staged_base(dest, system_id=...)`.

Extracting rather than writing a second probe is the point: the repo has already been bitten by a
duplicated magic check that was written as an equality against a whole prefix and was therefore a
dead guard (the ELF-magic case, #1383/ADR-0412). One implementation, two callers, one raising and
one returning a bool.

The re-check runs on **both** sides of the lock. A re-check on the pre-lock site alone would leave
the hole open for exactly the fetchers that queued — the ones a torn base is most likely to be
handed to, since a sibling crashing mid-stage is what produces both the torn base and the queue.

**"Cannot read" is not "corrupt."** `dest.is_file()` was a `stat`, needing only traverse permission
on the parent; the format probe `open`s the file, so it can fail for reasons that say nothing about
the base — `EACCES` under a worker/staging-user asymmetry of the shape ADR-0442 documents in this
same subsystem, `EMFILE` under descriptor exhaustion, a transient `EIO`. `_reusable_staged_base`
therefore returns `False` only for the errors that mean *there is no usable base at this path*
(`FileNotFoundError`, `NotADirectoryError`, `IsADirectoryError`) and raises everything else through
`_unreadable_base_fault` (its own message, for the reason decision 4 gives) as an
`INFRASTRUCTURE_FAILURE`. Swallowing those as a cache miss would be strictly worse than the code
being fixed: the fault is persistent, so every provision in the investigation would serialize on the
fetch lock and re-download a multi-GiB base, forever, with no error and no log.

This is deliberately **narrower** than the `is_file()` it replaces, which swallowed every `OSError`
alike — that narrowing is the decision above. In one respect it must stay exactly as **wide**,
though: `is_file()` also answered `False` for a path that is not a regular file, and the probe must
too, so the mode is checked before the `open`. Opening a FIFO for reading blocks until a writer
appears, so a probe without that check would hang the provision thread indefinitely — and at the
post-lock call site it would hang *holding* the fetch advisory lock, on an autocommit connection
with no `idle_in_transaction_session_timeout` to break it, wedging every sibling System on that
(investigation, checksum). Nothing in kdive creates a non-regular file there, but a stat the old
code already paid for is not worth trading for a deadlock.

A rejected base needs no unlink; it falls through to the lock-and-stage path, whose `os.replace`
supersedes it, so there is never a window in which the investigation has no base at all.

**Accepted residue — a superseded base a guest still holds open.** `os.replace` does not overwrite
bytes in place; it repoints the directory entry at the *new* inode and drops the last link to the
old one. If some QEMU has the rejected base open as a backing file, that inode survives with zero
links until the guest exits: charged to `df`, matching no path, and therefore unreachable by
ADR-0442's path-based `_unlink_staged_base` and `sweep_investigation_staging_dir` — the same
"charged to `df` yet invisible to every path-matching tool" shape `_unlink_orphan_partials` already
warns about for partials. Unlinking the rejected base first would not help: an unlink drops the link
identically and orphans an open inode just the same. The exposure is bounded by the holding guest's
lifetime and is the right side of the trade — the alternative is knowingly booting every new System
in the investigation off a base that failed its format gate.

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
file, and a garbage document written over the path. What it does not catch: damage confined to
anything past the first four bytes.

**That residue is larger than it looks, and it is stated here rather than papered over.** The rename
is not concurrent with the write — the stagers stream the whole object through a buffered writer and
close it, and only then does the magic gate run and the rename happen. A multi-GiB download takes
minutes, over which `dirty_background_ratio` and `dirty_expire_centisecs` force continuous writeback
and `dirty_ratio` throttles a writer that outruns it, so by rename time most of the file is already
allocated and on disk. What is still dirty is the **tail**. The expected crash-torn survivor of a
large base is therefore head-intact and tail-zeroed — which starts with `QFI\xfb` and passes this
gate. The whole-file-zeroed shape the gate does catch is the small-object case, where the entire
write fit inside the dirty window.

So for the population decision 2 is the net for — bases staged by code that predates decision 1 —
the net catches the head-damaged shapes and misses the likeliest large one. The gate is still worth
having (truncation, empty, garbage, and non-crash corruption from a bad disk or a stray `cp` are all
real), but it is not a crash-torn-base detector for multi-GiB bases and must not be read as one. The
sidecar completion marker under *Considered & rejected* is the design that would close it; #1539
tracks it, rather than a conditional on the residue "proving real". An operator who knows a host
crashed while an investigation was staging a base should delete that investigation's staging
directory rather than trust this gate to have caught it.

That residue is why the ordering of the two halves matters. Decision 1 removes the crash window that
produces a torn base at all; decision 2 is the net for a base staged by code that predates decision
1, and for corruption arriving by some other route (a bad disk, an operator's `cp`, a half-restored
backup). Neither is sufficient alone, and the reuse gate is explicitly *not* claimed to be a
verification of the base — only a rejection of the shapes a broken one takes.

### 4. A rejection is logged, because it is otherwise undetectable

The module gains `_log = logging.getLogger(__name__)` — the settled convention in this package
(`lifecycle/rootfs/customization_boot.py`, `jobs/handlers/artifacts/rootfs_reclaim.py`) — and each
rejection site emits a `WARNING` naming `dest`, the investigation, and the System.

Without it the fix is unmeasurable. The re-stage *succeeds*, so no error is raised and no job fails;
the only symptom of the durability bug firing is a provision that took one multi-GiB download longer
than it should have. The ADR's own Context argues that the two defects masked each other
diagnostically, and shipping the detection while discarding the detection signal would preserve
exactly that. It is also the anchor for the pathological case: a base that keeps failing the gate
(a dying disk, a stray `cp`) presents as an unbounded loop of serialized re-downloads, and one log
line is the difference between diagnosing that and staring at object-store egress.

The two sites log distinguishably, and the post-lock one is **gated on the base not having been
there on arrival**. Nothing between the two checks repairs `dest`, so an ungated post-lock warning
would fire on every ordinary stale-base rejection and attribute the commonest case to a racing
fetcher that never existed — inverting the signal, since the louder message would then accompany the
quieter condition and the genuinely concurrent case would be unidentifiable. As written, a pre-lock
line means a stale unusable base was already there; the sibling line means one *appeared while we
waited*, so a sibling just published something that does not verify. The re-verification itself
still runs unconditionally on both sides; only the attribution is gated.

No metric is added — the log line carries the identifiers, and a counter with no dimension to slice
by would not answer a question the line does not.

The reuse-read fault gets its own message rather than reusing `_staging_fault`. Nothing is being
staged on that path, and "failed to stage the uploaded rootfs" would send an operator to the object
store when the likeliest trigger is the ADR-0442 permission asymmetry and the actionable fix is the
ownership of a file that is already present and probably intact.

## Consequences

- A host crash mid-stage can no longer leave a base that silently backs every guest in the
  investigation. This is the outcome the ADR exists for.
- One `fsync` of the full base and one of the staging directory per published base. On the identity
  path the data is already being written through the page cache during a multi-GiB download, so the
  sync is largely a wait for writeback that would have happened anyway; the wall-clock cost is
  bounded by the disk, not added to it. It is paid once per investigation per checksum, never per
  System.
- Bases staged by pre-ADR-0443 code are **partially** re-validated. A head-damaged one is
  re-downloaded once with a `WARNING` naming it; a good one passes and is reused, so there is no
  mass re-download on upgrade. But a large base torn only in its tail — per decision 3, the likeliest
  crash survivor — passes the gate and is reused silently, exactly as before. The upgrade does not
  retroactively clean a host that already crashed mid-stage; only #1539 would.
- An unreadable staged base now fails the provision with an `INFRASTRUCTURE_FAILURE` where the old
  `is_file()` would have reused it (if `stat` succeeded) or silently re-downloaded it. That is a new
  loud failure on a genuinely broken host, and it is the intended trade against a silent
  re-download loop.
- The reuse fast path now performs a file `open` + 4-byte read where it performed a `stat`. Both are
  a single syscall pair against warm dentry cache; the provision path around it opens the file for
  `qemu-img` moments later regardless.
- `_require_qcow2_magic` and `_reusable_staged_base` share one probe, so a future format change
  (a second magic, a version check) lands in one place and cannot leave the reuse gate behind.
- Damage past the first four bytes of a base is still reused, and per decision 3 that covers the
  expected crash-torn shape of a multi-GiB base staged by pre-ADR-0443 code. Detecting it requires
  reading the base, which is the trade decision 3 declines on the hot path. #1539 tracks the sidecar
  completion marker, which closes it without one.

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
  not a substitute, and its incremental value over `fsync` + magic is the past-the-header residue
  alone — which decision 3 establishes is the *expected* shape for a large pre-ADR-0443 base, not a
  hypothetical one. It is therefore filed as **#1539** rather than left conditional; it is deferred
  only because the cross-file reclaim change does not belong in this diff.
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
- **Sync inside each stager as its writer closes** (a `_durable_partial_writer` context manager).
  The first shape this change took, and it reads as the tidier one — durability adjacent to the
  writes it covers. Rejected in decision 1: the shared qcow2-magic gate runs *after* the stager
  returns, so a checksum-valid non-qcow2 upload would be flushed in full before being rejected, and
  the sync would have to be repeated in every future codec.
- **Also `fsync` the parent of a newly created per-investigation staging directory.** Would make
  the durability statement formally complete. Rejected in decision 1 as a syscall that changes no
  outcome: losing that directory loses `dest` and the `.partial` together, which is the benign case.
- **Sync only the directory, not the file.** A misreading of what the rename guarantees: it would
  make the *name* durable while leaving the data it points at unwritten, which is the bug.
- **Leave the reuse path alone on the grounds that decision 1 makes a torn base impossible.** True
  only for bases staged after this change, and only for crashes. It would leave every base staged by
  currently-deployed code permanently trusted, which is the population most likely to contain one.

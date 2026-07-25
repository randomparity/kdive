# A sidecar completion marker gates staged-rootfs reuse (#1539)

- **Issue:** [#1539](https://github.com/randomparity/kdive/issues/1539)
- **ADR:** [`../adr/0451-staged-rootfs-completion-marker.md`](../adr/0451-staged-rootfs-completion-marker.md)
- **Supersedes:** ADR-0443 §2/§3's magic-only reuse gate (the gate is *widened*, not replaced).
- **Coordinates with:** ADR-0452 (#1544), whose Consequences name this change and the shape it must take.

## Problem

ADR-0443 shipped two halves. Decision 1 — `fsync` the partial before `os.replace` and the staging
directory after — is the real fix and is complete. Decision 2 — re-apply the qcow2-magic probe
before reusing a present staged base — is a **weaker net than a magic gate looks**, and ADR-0443 §3
records why in its own words: the rename follows the *completed* write, so by rename time writeback
has already flushed most of a multi-GiB base and the dirty residue is its **tail**. The expected
crash survivor of a large base is head-intact and tail-zeroed. It starts with `QFI\xfb`, passes
`_reusable_staged_base`, and is reused silently with no WARNING.

The whole-file-zeroed shape the gate does catch is the small-object case, where the entire write fit
inside the dirty window. So the net catches the shapes that are least likely and misses the one
ADR-0443 §3 establishes is *expected*.

The affected population is bases staged by code that predates ADR-0443, on a host that crashed
mid-stage — which is exactly the population decision 2 exists to be the net for. Under ADR-0441 §5's
content-addressed reuse, one such base backs **every** System in the investigation until it closes,
with the checksum machinery bypassed *because* the file exists, and the symptom surfaces as an
unbootable or subtly wrong guest attributed to the kernel under test.

## Blast radius today

- `providers/shared/runtime_paths.py` — `staged_rootfs_path` is the naming authority for
  `<uploads_dir>/<investigation_id>/<token>.qcow2`. A sibling needs its own helper here so the
  writer, the gate, the reclaim unlink and the sweep glob cannot drift.
- `providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py` — `_durable_replace` (publish),
  `_reusable_staged_base` (reuse gate, both fetch-lock sides; it becomes `_staged_base_rejection`,
  see below), `_sibling_already_published` (skip-publish probe).
- `jobs/handlers/artifacts/rootfs_reclaim.py` — `_unlink_staged_base` (per-row reclaim) and
  `sweep_investigation_staging_dir` (drain tail).

`sweep_investigation_staging_dir` is the file #1544 landed in hours ago. Its `rmdir` now logs a
WARNING whenever the directory survives for a reason the pass has not already reported (ADR-0452
§7), so a sidecar matching neither `*.partial` nor `*.qcow2` would fire that WARNING on **every**
drained investigation and leak one directory apiece. ADR-0452's Consequences record this explicitly:
"#1539 must sweep its own marker rather than rely on it."

## Requirements

1. A base whose **tail** is zeroed after a simulated crash is rejected on reuse and re-staged, rather
   than backing an overlay. This is the acceptance criterion and the shape the central test must
   construct — a test that only checks marker presence/absence misses the issue.
2. Reuse keeps everything ADR-0443 decision 2 already catches. The marker is added to the gate, not
   substituted for the magic probe.
3. Reclaim leaves no marker behind, and the per-investigation staging directory is still removed when
   it drains — with no new unexplained-survivor WARNING from ADR-0452 §7.
4. The marker sweep must not be `flock`-gated. `unlink_partial_if_unheld` answers "is a live writer
   still holding this multi-GiB partial"; a marker is a zero-byte file no writer holds across a
   download, and gating it would make a leaked marker indistinguishable from a held partial at the
   `rmdir` — the one distinction ADR-0452 §5 built the returned flag to preserve.
5. Ordering must be crash-safe on **re-stage**, not only on first stage: a surviving marker plus a
   crash inside the publish must never leave marker-present over a base this code did not finish.

## Design

### The marker

`<uploads_dir>/<investigation_id>/<token>.ready`, a zero-byte sibling of `<token>.qcow2`.

`runtime_paths.py` gains `STAGED_ROOTFS_MARKER_SUFFIX = ".ready"` and
`staged_rootfs_marker_path(base)`, derived from the base path rather than from
`(investigation_id, token)` again: `_durable_replace` holds only `dest`, and a second
`(investigation_id, token)` overload would be a parallel derivation to keep in step for no caller.
The glob the sweep runs is built from the same constant.

Zero-byte is deliberate. The marker's *existence* is the whole signal; content would be a second
thing to keep consistent with the base and nothing would verify it. `.ready` rather than a dotfile so
it is visible to `ls` and to an operator following ADR-0443 §3's "delete that investigation's staging
directory" advice.

### Publish order in `_durable_replace`

```
_fsync_path(partial, O_WRONLY)          # 1. the base's data is durable  (ADR-0443 decision 1)
marker.unlink(missing_ok=True)          # 2. any stale marker goes first
_fsync_path(dest.parent, O_RDONLY)      # 3. ...and its absence is durable before the rename
os.replace(partial, dest)               # 4. publish
_fsync_path(dest.parent, O_RDONLY)      # 5. rename + partial-unlink durable (ADR-0443 decision 1)
_write_completion_marker(marker)        # 6. create + fsync the marker
_fsync_path(dest.parent, O_RDONLY)      # 7. the marker's link is durable
```

Every crash point leaves one of two states: **no marker** (re-stage, correct), or **marker over a
base whose data was fsynced at step 1** (reuse, correct). Steps 2–3 are what make that true on a
*re-stage*: without them a stale marker from a previous stage of the same token could be durable
while the new rename is not, so a crash would restore the *old* base under a marker that now
attests to a base the pass had rejected. Two directory syncs are added to a path that already pays
one, once per investigation per checksum — never per System.

Step 6 `fsync`s the marker itself even though it has no data blocks. It costs one syscall pair and
removes the need to reason about whether a zero-length file's inode is covered by the directory sync
alone.

### The reuse gate

`_reusable_staged_base` becomes `S_ISREG(dest)` **and** marker present **and** qcow2 magic, and is
renamed `_staged_base_rejection`: it returns the slug naming *which* gate rejected rather than a
bool. That return type is load-bearing rather than cosmetic. The gate now has three rejection
reasons that mean opposite things to an operator — a failed format gate says the durability bug
fired or the base was corrupted by other means, while a missing marker on the first provision after
an upgrade is expected and needs no action — and on upgrade the second is the reason for *every*
base in the tree. A single message keyed on the format gate would turn the one-time upgrade cost
into a fleet-wide false durability alarm.

Keeping the magic probe is not redundancy. The marker is a **completion** witness, not an
**integrity** witness: it says "a stage of this base ran to a durable finish", which is what closes
the crash-torn population. It says nothing about damage arriving *after* the publish — a dying disk,
a stray `cp`, a half-restored backup — which is the other population ADR-0443 §3 names, and for
which the magic probe is still the only net in the reuse path. Dropping it to "gate on the marker
rather than on the base" (the issue's phrasing) would trade one residue for another.

The marker probe is a `stat` inside the existing `try`, so it inherits ADR-0443 decision 2's error
taxonomy unchanged: `FileNotFoundError`/`NotADirectoryError`/`IsADirectoryError` mean *no usable
base at this path* and are a cache miss; every other `OSError` raises `_unreadable_base_fault` as an
`INFRASTRUCTURE_FAILURE`, because a marker this process cannot `stat` (`EACCES` under the ADR-0442
uid asymmetry, `EIO`) is an operator-visible fault and swallowing it as a cache miss would produce
the silent perpetual re-download loop that decision explicitly rejects. `details` carries the probed
path, so an operator is not sent to the base when the marker is what failed.

### `_sibling_already_published` requires the marker too

That probe answers "is a good base already here, so do not swap the inode out from under a booting
guest". Under the new gate a marker-less base is one the reuse path will *reject*, so skipping the
publish on it would leave the investigation with a base every future fetch re-downloads and
re-rejects, forever. Requiring the marker makes the losing fetcher publish and complete it instead.

**That branch is not rare.** It is the *deterministic* first re-provision of every base staged
before this change — present, magic-passing, marker-less, so the re-stage `os.replace`s over it —
and only secondarily the sibling that died between its `os.replace` and its marker write, which is
a sub-millisecond window. The cost is ADR-0443 §2's already-accepted orphan-inode residue, escalated
from a rare race to one occurrence per (investigation, token) whose base a running guest holds. See
*What this does not fix*. The probe's `except OSError: return False` polarity is untouched: an
unreadable marker answers "publish", which can only remove work, never fail a download that already
succeeded.

### Reclaim: `_unlink_staged_base`

The marker is unlinked **before** the base, inside the same `OSError`-propagating region — only
`FileNotFoundError` is suppressed, per side, so any other fault still defers the whole checksum
before the object or the row is deleted (ADR-0442 §4). Marker-first is the conservative order: it
can only ever leave "base without marker" (re-staged, correct), never "marker without the base it
attests to".

### Reclaim: the drain-tail sweep

`sweep_investigation_staging_dir` gains a third glob, `*.ready`, run **last** — after the partials
and the unowned bases, immediately before the `rmdir`. It is a plain per-candidate `unlink`, not
`unlink_partial_if_unheld`: see requirement 4. The walk logs an `OSError` like the other two
(ADR-0452 §7); a per-candidate unlink fault logs a WARNING; a *successful* collection does not, since
`_unlink_unowned_base`'s WARNING one line up is already the publish-after-reclaim signal and a
marker beside a collected base would only double it.

Running last minimises — it cannot close — the window in which the doomed fetcher ADR-0452 §6
describes publishes between the base sweep and the marker sweep. Either ordering leaves the same
residue; #1558 is what removes the race.

## What this does not fix

- **Post-publish corruption is still only magic-gated.** The marker attests to completion, not to
  the bytes staying good. A base tail-damaged by a dying disk *after* a durable publish passes both
  halves of the gate. Closing that needs a checksum re-verify, which ADR-0443 §3 declines on the hot
  path for reasons that are unchanged.
- **Every pre-marker staged base becomes non-reusable on upgrade** — one full re-download per
  (investigation, token) that survives the upgrade. This is a real operational consequence and is
  stated in the ADR rather than left to surprise an operator. It is also the only way the fix can
  work: the whole point is that a pre-ADR-0443 base cannot be shown intact by anything cheaper.
- **That re-stage peaks at two copies of the base on the staging volume, and ADR-0450's
  `_require_staging_free_space` can refuse it outright.** The old base is still there while the new
  `<token>.<uuid>.partial` is written beside it, and the precheck demands `base_bytes` plus a 1 GiB
  floor — so a volume provisioned for one copy refuses the upgrade re-stage with an
  `INFRASTRUCTURE_FAILURE` that burns all three attempts in milliseconds and dead-letters.
  **Confirm the staging volume holds two copies of the largest live base before deploying, or close
  the affected investigations first.** The precheck is advisory rather than a reservation
  (ADR-0450), so concurrent stagers in different investigations each pass against the same bytes.
- **One orphaned inode per replaced base whose guest is still running** — the `os.replace` drops the
  last link while QEMU holds it open, charged to `df` and matching no path. ADR-0443 §2's accepted
  residue, escalated by the upgrade from a rare race to once per (investigation, token). A capacity
  fault, not a correctness one, and bounded by the holding guest's lifetime.
- **During the upgrade window an investigation with a pre-marker base cannot provision while the
  object store is unreachable**, where it previously provisioned off the cached base with no store
  call at all. Fold store reachability into the same pre-deploy check as the free space above.
- **The doomed-fetcher publish race** (ADR-0452 §6 / #1558) now covers the marker as well as the
  base. Same bound, same fix.
- **Rolling back leaks one staging directory per investigation.** A release without the `*.ready`
  glob cannot see the markers this one wrote; run `find <uploads_dir> -name '*.ready' -delete`.
- Nothing about `_unlink_orphan_partials`, the fetch lock, or the reclaim gate's row-state
  classification.

## Test plan

- **Central (the acceptance criterion).** Build the actual crash-torn shape: a full-length base whose
  first four bytes are the qcow2 magic and whose tail is zeroed. Assert (a) that the *old* predicate
  — `S_ISREG` + `_starts_with_qcow2_magic` — accepts it, so the test pins the defect rather than
  restating the fix, and (b) that `fetch_uploaded_rootfs` rejects it, re-downloads, and leaves a good
  base with a marker.
- A base with no marker is re-staged even though its magic is intact (the pre-upgrade population).
- A base whose marker is present but whose magic fails is still rejected (the magic probe is kept).
- Both fetch-lock sides: the sibling-appeared-during-the-lock-wait path rejects a marker-less base.
- `_durable_replace` ordering: a recording `fsync`/`replace` spy pins unlink → dirsync → replace →
  dirsync → marker → dirsync. Faults are then armed on the partial sync, the pre-rename directory
  sync, the post-rename directory sync, the marker write and the stale-marker unlink in turn; none
  leaves a marker over a base this code did not finish, the two marker steps name the *marker* in
  the fault rather than the base, and the marker write stays fatal rather than publishing an
  unmarked base.
- An unreadable marker raises `INFRASTRUCTURE_FAILURE`, not a cache miss.
- `_sibling_already_published` publishes over a marker-less sibling base and skips a marked one.
- `_unlink_staged_base` removes both, propagates a non-`ENOENT` fault from either, and a fault on the
  marker leaves the base (so the checksum defers before the object delete).
- The drain sweep removes markers and the `rmdir` succeeds with **no** unexplained-survivor WARNING;
  a live-held partial still returns `True` and the marker sweep does not change that answer.
- A marker sweep is not `flock`-gated: a marker whose `flock` would block is still collected.

## Alternatives considered

See the ADR. In short: gating on the marker *alone* (loses the non-crash-corruption net), naming the
marker so it falls inside the existing `*.qcow2` glob (drags it into `_unlink_unowned_base`'s
publish-after-reclaim WARNING), putting the completion witness in an `artifacts` column (a migration,
and a DB row cannot witness a local filesystem's durability), and re-verifying the checksum on reuse
(ADR-0443 §3, unchanged).

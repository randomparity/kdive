# ADR 0451 — A sidecar completion marker gates staged-rootfs reuse

- **Status:** Accepted
- **Date:** 2026-07-25
- **Supersedes:** [ADR-0443](0443-durable-rootfs-staging-and-reuse-recheck.md) §2/§3's magic-only
  reuse gate. The gate is *widened*, not replaced: the magic probe stays, and decision 1's `fsync`
  stays load-bearing. ADR-0443 records this design under its own *Considered & rejected* and files it
  as #1539.
- **Amends:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 — the content-addressed
  staging path gains a second file per base.
- **Amends:** [ADR-0442](0442-rootfs-reclaim-worker-job.md) §4/§7 — the per-row reclaim unlinks two
  paths, and the drain-tail sweep runs a third glob.
- **Coordinates with:** [ADR-0452](0452-flock-guarded-reclaim-staging-sweep.md) §7, whose
  unexplained-survivor `rmdir` WARNING this change must not fire on every drained investigation.
- **Spec:** [`../specs/2026-07-25-staged-rootfs-completion-marker-1539-design.md`](../specs/2026-07-25-staged-rootfs-completion-marker-1539-design.md)

## Context

ADR-0443 §3 states its own residue plainly and this ADR is the follow-through, not a re-litigation.
The rename follows the *completed* write: the stagers stream the whole object through a buffered
writer and close it, and only then does the magic gate run and the rename happen. A multi-GiB
download takes minutes, over which `dirty_background_ratio` and `dirty_expire_centisecs` force
continuous writeback and `dirty_ratio` throttles a writer that outruns it. By rename time most of the
file is allocated and on disk, and what is still dirty is the **tail**.

So the expected crash survivor of a large base is head-intact and tail-zeroed. It starts with
`QFI\xfb`, passes `_reusable_staged_base`, and is reused with no error and no WARNING. The
whole-file-zeroed shape the gate *does* catch is the small-object case, where the entire write fit
inside the dirty window. The net catches the unlikely shapes and misses the likely one.

The population this matters for is bases staged by code predating ADR-0443 decision 1, on a host that
crashed mid-stage — which is exactly the population decision 2 exists to be the net for. Under
ADR-0441 §5's content-addressed reuse one such base backs **every** System in the investigation until
it closes, with the checksum machinery bypassed *because* the file exists. The symptom is an
unbootable or subtly wrong guest, attributed to the kernel under test.

ADR-0443 deferred the fix on blast radius rather than merit, and named the radius: the marker is a
second file in the per-investigation staging directory, so `rootfs_reclaim.py` must unlink it in
`_unlink_staged_base` **and** account for it in `sweep_investigation_staging_dir`, whose `rmdir`
would otherwise fail forever and leak one directory per investigation.

That radius has since moved. #1544 (ADR-0452) landed in that same function: the partial sweep is now
`flock`-gated, a non-empty staging directory is a *legitimate* outcome, the sweep returns whether a
live writer held a partial, and the `rmdir` logs a WARNING whenever the directory survives for a
reason the pass has not already reported. ADR-0452's Consequences record the consequence for this
change in one sentence — a sidecar matching neither `*.partial` nor `*.qcow2` would fire that WARNING
on every drained investigation, so #1539 must sweep its own marker rather than rely on decision 7 to
make the leak audible.

## Decision

### 1. The marker is a zero-byte `<token>.ready` beside `<token>.qcow2`

`providers/shared/runtime_paths.py` gains `STAGED_ROOTFS_MARKER_SUFFIX = ".ready"` and
`staged_rootfs_marker_path(base)`. It sits with `staged_rootfs_path`, which is the naming authority
for the staging tree, so the writer, the reuse gate, the reclaim unlink and the sweep glob all derive
from one place and cannot drift — the failure mode #1383/ADR-0412 already cost this repo once with a
duplicated magic check.

It is derived from the **base path**, not from `(investigation_id, token)` a second time.
`_durable_replace` holds only `dest`, and a parallel `(investigation_id, token)` overload would be a
second derivation to keep in step for no caller that needs it.

Zero-byte is deliberate. The marker's *existence* is the entire signal. Content — the token, a
timestamp, the staging uuid — would be a second thing to keep consistent with the base, verified by
nothing, and would invite a reader to trust it. `.ready` rather than a dotfile because ADR-0443 §3's
standing operator advice is to inspect and delete a suspect investigation's staging directory, and a
hidden file undermines that.

### 2. Publish writes the marker last, and unlinks any stale one first

`_durable_replace` becomes:

```
_fsync_path(partial, O_WRONLY)          # 1. the base's data is durable    (ADR-0443 decision 1)
marker.unlink(missing_ok=True)          # 2. any stale marker goes first
_fsync_path(dest.parent, O_RDONLY)      # 3. ...and its absence is durable before the rename
os.replace(partial, dest)               # 4. publish
_fsync_path(dest.parent, O_RDONLY)      # 5. rename + partial unlink durable (ADR-0443 decision 1)
_write_completion_marker(marker)        # 6. create + fsync the marker
_fsync_path(dest.parent, O_RDONLY)      # 7. the marker's link is durable
```

Every crash point leaves one of two states, and both are correct: **no marker**, so the next fetch
re-stages; or **a marker over a base whose data was made durable at step 1**, so the next fetch
reuses it.

**Steps 2–3 are the re-stage case and they are not decoration.** ADR-0443's residue is stated for a
first stage; a re-stage of the same token can begin with a marker already present, because the token
is a content address and the same base may have been staged, marked, and later damaged by something
other than the crash this closes. Without step 3 the stale marker's *removal* could still be in the
journal when the crash lands while the rename is durable — or the reverse — and the recovered state
would be the previous base under a marker that now attests to a base this pass had already rejected.
Ordering the unlink's durability ahead of the rename removes that state entirely rather than
reasoning about a particular filesystem's metadata ordering, which is exactly the kind of derived
invariant ADR-0446 and ADR-0452 exist to delete.

**Step 5 is why the marker cannot simply share the final directory sync**, which would save one
syscall and look equivalent. Collapse them and a crash can leave the marker's link durable while the
rename is not — and the recovered state is then the *previous* base marked complete. That base is
exactly the one this pass rejected, so the collapse would mark a known-bad base as good: the bug,
with fewer syscalls.

Step 6 `fsync`s the marker itself even though a zero-length file has no data blocks. One syscall pair
buys not having to reason about whether its inode is covered by the directory sync alone.

Two directory syncs are added to a path that already pays one. They are paid once per investigation
per checksum, never per System, and they are metadata syncs on a directory holding at most a handful
of entries — immaterial beside the `fsync` of a base up to the 50 GiB canonical cap that precedes
them.

### 3. Reuse requires the marker **and** keeps the magic probe

`_reusable_staged_base` becomes `S_ISREG(dest)` and marker-present and qcow2-magic.

Gating on the marker *instead of* the base — the issue's own phrasing, and ADR-0443's — would be a
straight trade of one residue for another. **The marker is a completion witness, not an integrity
witness.** It says a stage of this base ran to a durable finish, which is what closes the crash-torn
population. It says nothing about damage arriving *after* the publish: the dying disk, the stray
`cp`, the half-restored backup that ADR-0443 §3 names as the second population the gate is for, and
for which the magic probe remains the only net on the reuse path. Keeping both costs one `stat` and
one 4-byte read on a path that opens the base for `qemu-img` moments later regardless.

The marker probe is a `stat` inside the existing `try`, so it inherits ADR-0443 decision 2's error
taxonomy without restating it: `FileNotFoundError`, `NotADirectoryError` and `IsADirectoryError` mean
*there is no usable base at this path* and return `False`; every other `OSError` raises
`_unreadable_base_fault` as an `INFRASTRUCTURE_FAILURE`. A marker this process cannot `stat`
(`EACCES` under the worker/staging-user asymmetry ADR-0442 documents in this same subsystem, a
transient `EIO`) is an operator-visible fault, and answering it as a cache miss would produce exactly
the silent, perpetual, fetch-lock-serialized multi-GiB re-download loop decision 2 rejects. The fault
carries the **probed path** in `details` and its message no longer asserts which of the two files
failed, so an operator is not sent to inspect the base when it was the marker that could not be read.

A directory sitting at the marker path answers `False` rather than raising: `stat` succeeds and
`S_ISREG` is what rejects it. Nothing in kdive creates one, and a `FIFO` there is harmless because
the marker is never opened — only `stat`ed — which is why this probe does not need ADR-0443 decision
2's hang argument.

### 4. `_sibling_already_published` requires the marker too

That probe answers "a good base is already here, so do not swap the inode out from under a guest that
may already be booting off it". Under decision 3 a marker-less base is one the reuse gate will
**reject**, so skipping the publish on it would hand the investigation a base that every future fetch
re-downloads and re-rejects for as long as it lives — trading a bounded orphan inode for an unbounded
re-download loop, which is the wrong direction and the same trade ADR-0443 decision 2 refuses one
function over.

The case is reachable only when a sibling died between its `os.replace` and its marker write, a
sub-millisecond window. The cost when it fires is ADR-0443 §2's already-accepted residue — the
superseded inode survives with zero links until the holding guest exits — paid once, after which the
base is marked and reused normally.

The probe's `except OSError: return False` polarity is untouched. An unreadable marker answers
"publish", which can only remove work; it can never add a failure to a download that already
succeeded, which is that function's whole stated contract.

### 5. Reclaim unlinks the marker before the base, in the same fault region

`_unlink_staged_base` unlinks `<token>.ready` and then `<token>.qcow2`, suppressing
`FileNotFoundError` on each and letting **every** other `OSError` propagate — so a fault on either
still defers the whole checksum before the object or the row is deleted, which is ADR-0442 §4's
order and the reason #1522 exists.

Marker-first is the conservative order. Interrupted after the marker it leaves "base without marker",
which the next fetch re-stages: correct, at the cost of one download. Interrupted the other way it
would leave "marker without the base it attests to" — harmless today, because decision 3 also
requires `S_ISREG(dest)`, but it is a state whose safety depends on a second condition holding, and
there is no reason to create it when the opposite order costs nothing.

### 6. The drain-tail sweep gets a third glob, deliberately **not** `flock`-gated

`sweep_investigation_staging_dir` globs `*.ready` after the partials and the unowned bases and
immediately before the `rmdir`, unlinking each candidate directly rather than through
`unlink_partial_if_unheld`.

The gate must not be reused here, and this is the interaction with #1544 that decides the shape of
this change. `unlink_partial_if_unheld` answers one question — is a live writer still holding this
multi-GiB partial across a download — and its `True` is the one outcome ADR-0452 §5 proved *provably
transient*, which is why `_finish_drained_investigation` retains `rootfs_cleanup_pending_at` on it and
on nothing else. A marker is a zero-byte file created and closed in microseconds; no writer holds one
across anything. Routing it through the gate would let a marker's `EACCES` or `EWOULDBLOCK` return
`True` and pin an investigation's drain marker on a file whose liveness is meaningless, and would
make a *leaked marker* and a *held partial* indistinguishable at the `rmdir` — collapsing exactly the
distinction ADR-0452 §5 introduced the returned flag to preserve.

A per-candidate unlink fault logs a `WARNING`, matching decision 7 of ADR-0452 — this pass is the last
collector and no step of it is silent. A *successful* collection does not log: `_unlink_unowned_base`
already `WARNING`s one line up for the publish-after-reclaim condition that is the only way a base
survives to the drain tail, and a marker beside it would only double the line. The walk itself logs an
`OSError` like the other two, for ADR-0452 §7's reason: `Path.glob` yields nothing for a directory it
cannot enumerate rather than raising.

Running the marker glob **last**, adjacent to the `rmdir`, minimises — it cannot close — the window in
which the doomed fetcher of ADR-0452 §6 publishes between the base sweep and the marker sweep. Either
ordering leaves the same residue in the other direction; #1558 is what removes the race, and stating
that is better than implying the ordering solves it.

## Consequences

- The crash-torn base is caught. A head-intact, tail-zeroed survivor of a mid-stage host crash is
  rejected on reuse and re-staged, which is the acceptance criterion #1539 states and the outcome
  ADR-0443 §3 says only this change would produce.
- **Every base staged before this change becomes non-reusable, so each surviving
  (investigation, token) pays one full re-download on upgrade.** This is a real operational
  consequence and it is stated here rather than left to surprise an operator watching object-store
  egress after a deploy. It is also unavoidable: the entire premise is that a pre-marker base cannot
  be shown intact by anything cheaper than re-staging it, and ADR-0443's own Consequences already
  concede that its magic gate re-validates that population only *partially*. The cost is bounded — it
  is once per base, not per System, and only for investigations still open across the upgrade — and
  the re-stage is the self-healing ADR-0443 predicted for this design.
- **Post-publish corruption is still only magic-gated.** A base tail-damaged by a dying disk *after* a
  durable publish passes both halves of the gate, because the marker witnesses completion rather than
  integrity. Closing that needs a checksum re-verify on the hot path, which ADR-0443 §3 declines for
  reasons this change does not alter. The residue is smaller than the one being closed — it needs
  damage arriving after a successful stage rather than a crash during one — but it is not zero and
  the reuse gate must still not be read as a verification of the base.
- Two extra directory `fsync`s and one file `fsync` per published base, plus one `stat` per reuse
  check. Paid once per investigation per checksum on the write side and once per System on the read
  side, against a path that already `fsync`s a base of up to 50 GiB.
- **ADR-0443 §1's stated outcome for a failed directory `fsync` changes, in the safe direction.**
  That ADR records a post-rename directory-sync failure as leaving `dest` published and intact, with
  "the next fetch reuses it, which is the honest outcome: the bytes are fine, only their durability
  is unproven". The sync that fails is now also the one that would have made the marker durable, so
  the next fetch **re-stages** instead. One extra download on a host whose disk is genuinely
  faulting, in exchange for never reusing a base whose durability is unproven — which is the
  direction the whole change moves in, and is stated here because it is a real behaviour change to a
  consequence another ADR wrote down. A failure of the *pre*-rename directory sync (step 3) aborts
  before publishing anything, which is the same outcome as a failed partial sync.
- The staging directory holds two files per base instead of one. Both reclaim paths account for it,
  and the ADR-0452 §7 `rmdir` WARNING stays quiet on a normal drain — which is the check that this
  change did not silently reintroduce the leak ADR-0443 deferred it for.
- `_sibling_already_published` can now publish over a sibling's marker-less base, orphaning an inode a
  guest may hold open (ADR-0443 §2's accepted residue). Reachable only when the sibling died inside
  its publish window; the alternative is an unbounded re-download loop.
- The doomed-fetcher publish race (ADR-0452 §6) now covers the marker as well as the base: a fetcher
  whose partial an earlier pass skipped can publish both after its own reclaim, and the deferred pass
  collects both. Same bound, same fix — **#1558**.
- No schema, no migration, no config setting, no new dependency, no MCP/RBAC surface, no new module.
  Not an AI surface.

## Considered & rejected

- **Gate reuse on the marker alone, dropping the qcow2-magic probe.** The literal reading of #1539 and
  of ADR-0443's rejected option. Rejected in decision 3: it trades the crash residue for the
  post-publish-corruption residue instead of closing one and keeping the net for the other, and the
  probe it would remove costs a 4-byte read on a path that opens the file for `qemu-img` anyway.
- **Name the marker so it falls inside an existing glob** (`<token>.ready.qcow2`, or reusing
  `*.partial`). Rejected. It would drag the marker into `_unlink_unowned_base`, whose `WARNING` means
  "a base outlived its `artifacts` row" and should never fire, and into `unlink_partial_if_unheld`,
  whose liveness answer is meaningless for a marker — decision 6. Avoiding one glob by corrupting the
  meaning of two is not a simplification.
- **Route the marker sweep through `unlink_partial_if_unheld` "for consistency".** Rejected in
  decision 6, and it is the trap this change most easily falls into now that #1544 has landed: it
  would let a marker pin an investigation's drain marker and would make a leaked marker
  indistinguishable from a held partial at the `rmdir`.
- **A completion column on the `artifacts` row instead of a sidecar.** Rejected. It needs a migration,
  and more importantly a database row cannot witness whether a *local filesystem's* rename and data
  became durable together — which is the entire question. A committed row over a lost `fsync` is the
  exact shape ADR-0443 decision 1 exists to prevent.
- **Write the marker before the rename, or fold it into the same directory `fsync`.** Rejected in
  decision 2. Either admits a state where the marker is durable and the base it attests to is not,
  which is the bug with extra syscalls.
- **Re-verify the checksum on reuse.** The only check that fully answers "is this the base I staged",
  and O(filesize) against up to 50 GiB on every guest start, negating ADR-0441's stage-once decision.
  Rejected in ADR-0443 §3 and unchanged here.
- **`qemu-img check` on reuse.** Rejected in ADR-0443's *Considered & rejected*: a subprocess per
  provision and a dependency on `qemu-img`'s exit-code taxonomy, still without reading the data
  clusters. The marker answers the crash question for two `stat`s.
- **Sweep the marker only, and let ADR-0452 §7's `rmdir` WARNING report the leak.** Rejected outright.
  ADR-0452 anticipated this change by name and said the opposite: decision 7 makes a leak *audible*,
  it does not make it acceptable, and a WARNING on every drained investigation would train an operator
  to ignore the one line that reports a genuinely unreadable staging tree.

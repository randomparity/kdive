# ADR 0450 — Refuse an uploaded-rootfs stage the staging filesystem cannot hold

- **Status:** Accepted
- **Date:** 2026-07-24
- **Amends:** [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) §2 — its #1520 amendment
  recorded that streaming moved the pressure from worker RAM onto staging disk and closed with
  "there is no free-space precheck on either staging path". There is one now. ADR-0438's codec
  dispatch, gate precedence, and qcow2-magic check are untouched.
- **Depends on:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) (the shared staging path
  and its per-(investigation, checksum) fetch lock, which is why this cannot be a reservation),
  [ADR-0443](0443-durable-rootfs-staging-and-reuse-recheck.md) (`uncompressed_size` is an upper
  bound, not a size — the fact the gzip budget rests on),
  [ADR-0437](0437-transport-encoding-canonical-object-model.md) (the declaration rules that decide
  which size each path can know).

## Context

ADR-0438 §2's #1520 amendment is explicit that streaming was a trade rather than a pure win: the
`.partial` now occupies its full size for the whole transfer, and "a rejected object — a failed
checksum, a non-qcow2 base, or the manifest-reaped identity fallback — is now written in full before
it is rejected, where before it consumed no disk at all."

Where those bytes land matters. `UPLOADS_DIR` (`/var/lib/kdive/rootfs-uploads`) and `ROOTFS_DIR`
(`/var/lib/kdive/rootfs`) are hardcoded siblings in `providers/shared/runtime_paths.py`, and there
is no provider-side knob to relocate either — `KDIVE_ROOTFS_DIR` is a `scripts/live-stack` variable
that the provider never reads. So unless an operator separately mounts one of them, the staging
partials share a filesystem with every live System's qcow2 overlay, and an oversized or invalid
upload degrades *running guests* rather than only failing its own provision.

**The blast radius is conditional, and the issue overstated it slightly.** An operator who mounts
`/var/lib/kdive/rootfs-uploads` separately confines the damage to the staging volume. The check
measures `dest.parent`'s own filesystem, so it is correct under either layout; only the
"degrades running guests" consequence depends on the shared-mount default.

Nothing on the staging path consulted free space at all before this: `rg 'disk_usage|statvfs' src/`
found a single hit, in `local_libvirt/discovery.py`, where it advertises the host disk *ceiling*
for admission control — a capacity advertisement, not a write-time gate.

## Decision

### 1. The check runs after the HEAD and before the partial exists

`stage_uploaded_rootfs` already HEADs the object to learn its size and checksum. The precheck sits
immediately after that (the size it budgets comes from there, and an object that was never uploaded
must still report `configuration_error`, not a disk message for a download that was never going to
happen) and immediately after `dest.parent.mkdir` (there is no filesystem to `statvfs` until the
staging directory exists), but before `_flocked_partial` creates anything. A refusal therefore
leaves no file, no lock, and no stream open.

### 2. What size is knowable is different on each path, and one case is knowable on neither

- **Identity.** `head.size_bytes` is *exact*: `_stage_identity` writes the stored object verbatim.
  `artifacts.uncompressed_size` is unavailable here by construction — ADR-0437's declaration
  validator rejects it outright on the identity path (`uncompressed_size_without_encoding`, "only
  meaningful with a transport encoding").
- **gzip.** The stored object is read through ranged GETs and never lands on disk; what occupies
  the filesystem is the *decompressed* output. Its budget is `uncompressed_size`, which ADR-0443
  establishes is an upper **bound** rather than a size — `strip_gzip_to_writer` caps decompression
  there and accepts less. Budgeting it therefore over-reserves at worst, which is the safe
  direction. Budgeting `head.size_bytes` here would *under*-reserve by the whole compression ratio
  and is the specific mistake this section exists to prevent.
- **Neither.** A gzip declaration carrying no `uncompressed_size` has no knowable requirement. The
  check is skipped rather than falling back to the stored size — that fallback is precisely the
  under-reservation above — and `_stage_gzip`'s existing `CONFIGURATION_ERROR` ("re-declare with
  the canonical object size") carries the failure, because that is the error an agent can act on.
  A free-space message computed from a number nobody knows would bury it.

### 3. The threshold is the required bytes plus a fixed 1 GiB floor

Not exact-fit: a check that admitted `free == required` would license a write that ends the volume
at exactly zero bytes free, which is the degraded state for the sibling overlays this guard exists
to protect, not the avoidance of it.

Not a percentage: it scales with the base, so 10% of the 50 GiB canonical cap would demand 5 GiB of
slack and refuse a stage onto a volume that comfortably holds it.

Not capped at the base size either, which is the tempting middle: a floor that shrinks for small
bases lets a stream of small stages walk the volume to zero one step at a time — the same harm
arriving slower. The floor is a property of the **volume**, not of the object, which is what makes
it a floor.

Two consequences are accepted rather than engineered around. A volume with less than 1 GiB free
refuses every uploaded-rootfs stage regardless of base size; that is deliberate, and it is a
behavior change for small-disk deployments that previously staged small bases successfully. And the
floor is one-sided — overlay creation under `ROOTFS_DIR`, the catalog rootfs cache, and the
console/pcap directories write the same volume with no equivalent gate, so it can still be driven
under the floor through every other lane. This guards the lane that writes tens of GiB at a time on
agent-supplied input; a volume-wide reserve across every writer is a different, larger decision.

No configuration setting is added. The floor is a constant with its reasoning at the definition;
adding a knob before anyone needs one would be a speculative surface on a P3 guard.

### 4. It is advisory, not a reservation — and #1525's own worked example stays open

This is the part that must not be overstated. The fetch lock is keyed per-(investigation,
checksum), so nothing serializes two fetchers staging *different* bases, and each passes its own
check against the same free bytes before either writes. Run #1525's worked failure through this
decision: 6 GiB free, two Systems in different investigations staging 4 GiB identity bases, both
compute `needed = 5 GiB <= 6 GiB`, both proceed, the volume fills. **That case is not prevented.**
Free space can also vanish between the `statvfs` and the write, and a live guest's overlay keeps
growing throughout.

What the check buys is the single-stager case, which is the common one: an oversized or invalid
object against a volume that was never going to hold it now fails immediately and attributably,
instead of after a multi-GiB write that takes the volume down with it. The real ENOSPC guard
remains `_staging_fault`.

The **kernel-enforced** alternative was evaluated and is deferred rather than dismissed:
`_flocked_partial` already yields an open descriptor on the partial, so
`os.posix_fallocate(guard_fd, 0, required)` would make the reservation real. Three costs put it out
of scope here, and #1546 tracks it. `_stage_identity` and `_stage_gzip` each open their own
`partial.open("wb")` — `O_TRUNC` — which deallocates the reservation, so it needs a writer-path
refactor against ADR-0446 §2's *deliberately* separate guard and writer handles. `posix_fallocate(3)`
is emulated by glibc where the filesystem has no native `fallocate`, turning a 50 GiB reservation
into a 50 GiB zero-write on exactly the near-full volumes at issue. And the gzip path's bound
over-reserves, so it would need an `ftruncate` after the verify.

Subtracting live sibling `*.partial` sizes from `free` was also rejected as a cheaper mitigation:
`f_bavail` already reflects what siblings have written, so subtracting it double-counts, while
missing what they have yet to write — the quantity that actually matters.

### 5. `INFRASTRUCTURE_FAILURE`, sharing the category the same condition already produces

Decisive reason: a mid-write ENOSPC already surfaces as `INFRASTRUCTURE_FAILURE` through
`_staging_fault`. Splitting the categories would make an agent's handling of one physical condition
depend on which side of a race window it was observed from. It is also right on the merits —
`_RETRYABLE_BY_CATEGORY` marks it retryable, and a bare re-invocation succeeds once an operator
frees space, with no change by the caller. `CONFIGURATION_ERROR` is non-retryable and would tell
the agent to re-declare an upload that is fine.

One consequence of failing *fast* on a retryable category is recorded rather than left to be
discovered: `jobs.fail` requeues with no backoff and `DEFAULT_MAX_ATTEMPTS` is 3, so a provision job
now spends all three attempts in milliseconds and dead-letters, where the pre-precheck ENOSPC spread
them over three multi-GiB downloads — an accidental window in which an operator freeing space got a
successful attempt 2 or 3. The remedy is unchanged (free space, provision again) but the provision
is re-issued rather than picked up by a later attempt, and the error says "re-issue". The error is
**not** marked `terminal`: a sibling stage finishing or the reclaim sweep can free space between
attempts, so a retry is not provably useless, and `terminal`'s documented meaning is that the
failure drove the target to a terminal state, which is false here.

### 6. The refusal names which of two conditions fired, and what its free-space figure is

Because the floor is a fixed volume-wide reserve, most refusals are *not* "your object is too big" —
a 20 MiB base against 900 MiB free would leave the volume almost untouched. A message blaming the
object in both cases would point an operator at the upload instead of at the floor, so the two read
differently: "the base does not fit at all" versus "the base itself fits; it is the floor that would
be breached".

`shutil.disk_usage(...).free` is `statvfs`'s `f_bavail` — space available to *unprivileged* users,
excluding the filesystem's reserved blocks, which is `df`'s `Avail` column. The staging worker often
runs as root and could write into that reserve; using the smaller figure is deliberate, because
those blocks exist to keep a full volume usable, which is the same thing this guard protects. The
error text says which figure it is, so an operator can reconcile it with the shell rather than
seeing a number that appears to contradict `df`.

### 7. A `statvfs` that itself faults degrades to staging, loudly

`EACCES` under the worker/staging-user asymmetry ADR-0442 documents in this same subsystem, a
transient `EIO`: the measurement fails rather than reporting a shortfall. This logs a `WARNING` and
stages anyway. It is `_flocked_partial`'s `ENOLCK` precedent (ADR-0446 §5) — turning a host quirk
that only disables an *advisory* check into a total uploaded-rootfs outage costs availability for no
safety at all, since the write stays guarded by the real ENOSPC either way.

## Consequences

- An oversized or invalid single upload is refused before its first byte instead of after a
  multi-GiB write onto a volume that also backs running guests — the acceptance criterion, and the
  narrower of the two cases #1525 describes. The concurrent case is explicitly still open (§4,
  #1546), stated in the code and here rather than left for a reader to derive from the lock's
  keying.
- A provision that would previously have *just* fit now fails when free space is under
  `required + 1 GiB`, and a volume under 1 GiB free refuses every uploaded-rootfs stage regardless
  of base size (§3).
- A provision job's three attempts are consumed in milliseconds rather than over three downloads,
  so the job dead-letters and the provision is re-issued rather than retried into freed space (§5).
- The gzip path over-reserves by the gap between `uncompressed_size` and the actual decompressed
  length. That is the safe direction and the only one available, since the exact length is not
  known until the decode finishes.
- One `statvfs` per staged base. Negligible against a multi-GiB download, and it does not run on
  the reuse fast path at all — the caller returns a present, verified base before reaching here.
- The module imports `disk_usage` from `shutil` rather than importing `shutil`, so a test's
  monkeypatch is scoped to this module instead of replacing `shutil.disk_usage` process-wide. An
  autouse fixture pins ample free space for the whole test module, so the three dozen staging tests
  that are not *about* the precheck do not silently acquire a dependency on the runner's free disk
  — a tmpfs `/tmp` in a small CI container sits under the floor.
- No schema, no migration, no config setting, no new dependency (`shutil` is stdlib), no MCP/RBAC
  surface change. The new failure is not added to the agent-facing upload constraints in
  `toolsets-artifacts.md`: those list rules an agent can *satisfy* (gzip only, `uncompressed_size`
  required, the 50 GiB cap), while host capacity is not something an agent can pre-check or route
  around. The error envelope carries the full contract at the point of failure and names the
  operator action. Not an AI surface.

## Considered & rejected

- **`posix_fallocate` on the partial, making the check a real reservation.** The strictly stronger
  mechanism, rejected here on cost and filed as #1546 rather than dismissed — §4 has the three
  costs (the writers' `O_TRUNC` deallocating it, glibc's zero-writing emulation on filesystems
  without native `fallocate`, and the gzip bound needing a post-verify `ftruncate`).
- **Subtracting live sibling `*.partial` occupancy from `free`.** Rejected as unsound, not merely
  insufficient: `f_bavail` already accounts for the bytes siblings have written, so subtracting
  them double-counts, and it still misses the bytes they have yet to write.
- **A percentage margin, or a margin capped at the base size.** Rejected in §3 — the first refuses
  legitimate large bases on large volumes, the second lets small stages walk a volume to zero.
- **`CONFIGURATION_ERROR` for the refusal.** Rejected in §5: non-retryable, and it tells the agent
  to fix a declaration that is correct.
- **A configurable floor.** Rejected as a speculative surface — no operator has asked for one, and
  the constant carries its reasoning at the definition site for whoever first needs to tune it.
- **Checking free space in `fetch_uploaded_rootfs` before taking the fetch lock**, so a doomed
  fetcher does not queue behind a sibling first. Rejected: it needs a second HEAD to learn the
  size, and the queued fetcher's most likely outcome is a cache hit on the base the sibling just
  published, which needs no space at all.

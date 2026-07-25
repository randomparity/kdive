# ADR 0446 — The orphan-partial sweep is gated on an `flock`, not on the fetch lock

- **Status:** Accepted
- **Date:** 2026-07-24
- **Amends:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 — the opportunistic
  crash-orphan sweep no longer derives its safety from the fetch advisory lock. ADR-0441's unique
  per-fetcher partial and its `os.replace` publish are untouched, as are ADR-0442's reclaim order
  and ADR-0443's durability half.
- **Depends on:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) (the shared staging path
  and the sweep this bounds), [ADR-0443](0443-durable-rootfs-staging-and-reuse-recheck.md) (the
  `_durable_replace` publish the writer's lock is now held across).
- **Spec:** [`../specs/2026-07-24-flock-guarded-orphan-partial-sweep-1524-design.md`](../specs/2026-07-24-flock-guarded-orphan-partial-sweep-1524-design.md)

## Context

ADR-0441 §5 gives the opportunistic sweep a one-line justification: it runs while holding the
per-(investigation, checksum) fetch lock, "which serializes downloads so no *live* sibling exists",
therefore any `<token>.*.partial` it finds is a killed worker's orphan and may be unlinked
unconditionally.

That is a conditional stated as an invariant. The fetch lock is a **session-scoped**
`pg_advisory_lock`, and a session lock is a property of a Postgres *connection*, not of the process
that took it. Postgres releases it the moment the backend exits — which it can do while the owning
process is very much alive and mid-download. The sweep then unlinks a live sibling's partial. That
fetcher keeps writing into an unlinked inode and fails at `os.replace` with `ENOENT`: a failed
provision after a completed multi-GiB download, with the written blocks charged to `df` yet
unreachable by any path-matching tool until the process exits. ADR-0441 §5 already carries the #1520
amendment recording this consequence and pointing here.

The window is the whole download, which is the worst possible shape for it: the connection sends
nothing between `pg_advisory_lock` and `pg_advisory_unlock`, for however many minutes a multi-GiB
transfer takes.

**Reachability, checked rather than assumed.** #1524 attributes the lost lock partly to "a PgBouncer
recycle". There is no pooler in this deployment: `docker-compose.yml` wires `server`, `worker`, and
`reconciler` directly at `postgres:5432`, and this fetch opens its own short-lived
`psycopg.connect` rather than drawing from the application pool — precisely the dedicated
non-pooled connection ADR-0095 carves out of ADR-0005's ban on session locks. What is left is
enough on its own:

- **Idle-connection reaping.** `psycopg.connect` sets no `keepalives_idle`, so the socket inherits
  `tcp_keepalives_idle = 0` (defer to the OS, 7200 s on Linux). NAT and conntrack middleboxes
  commonly evict an idle flow in 5–15 minutes. The drop is silent to the downloader.
- **Backend termination** — a Postgres restart, an administrative `pg_terminate_backend`, or the
  OOM killer.

So the defect is real and the fix is warranted; only the pooler half of the issue's causal story is
not. Tuning keepalives would narrow the first trigger and does nothing for the second, which is why
this ADR fixes the sweep rather than the connection.

## Decision

### 1. A live writer holds an exclusive `flock` on its own partial; the sweep skips what it cannot lock

Each fetcher creates its `<token>.<uuid>.partial` with `O_CREAT | O_EXCL` and takes
`fcntl.flock(fd, LOCK_EX | LOCK_NB)` on it *before* any stager writes a byte, holding that
descriptor open across the download, the checksum verify, the qcow2-magic gate, and
`_durable_replace`. The sweep opens each glob match and attempts the same non-blocking lock:
`BlockingIOError` means a live sibling owns it and the file is skipped; success means nothing holds
it and it is unlinked, exactly as before.

Liveness is now asserted by the operating system about the process that is actually writing, rather
than inferred from a database lock held by a connection that may already be gone. The sweep's
safety no longer depends on the fetch lock at all, which is the point: the fetch lock keeps its one
real job (collapsing the redundant download) and stops carrying a correctness burden it cannot bear.

Two *writers* never contend, because the partial names are already per-attempt unique
(`uuid4().hex`) — so this is a liveness marker, not a mutex, and it introduces no new serialization
between fetchers. The one contender that does exist is a sibling's sweep, in the creation window
§3 covers.

**Process death is the reason this beats every timing-based alternative.** The kernel releases an
`flock` when the holding descriptor is closed, including on process exit — normal, `SIGKILL`, or a
segfault. So the crash-orphan case ADR-0441 §5 introduced the sweep for still works, with no
timeout to wait out: a killed worker's partial is already unlocked by the time any sibling sweeps
it, and is collected on the very next fetch.

### 2. The writer's guard runs before the stagers, and preserves the created mode

The guard `open` uses mode `0o666` so umask application is byte-for-byte what `partial.open("wb")`
produced. This matters beyond tidiness: `os.replace` carries the partial's mode onto the published
base, and QEMU reads that base as the unprivileged `qemu` user. Tightening the guard's mode to
`0o600` — the reflex for a SENSITIVE file — would make every subsequently staged base unreadable to
the hypervisor and break provisioning outright.

`O_EXCL` is used rather than plain `O_CREAT`: a pre-existing file at a `uuid4` path is not a
condition to write through silently.

### 3. The two-syscall creation window fails loud instead of leaking silently

`open(O_CREAT|O_EXCL)` then `flock()` is two syscalls, and a sweeper that globs, opens, locks, and
unlinks strictly between them still destroys a live partial. Closing that fully would mean creating
and locking under a name the sweep glob does not match and renaming into place — which trades the
window for a `.partial.tmp` orphan no sweep collects, i.e. reintroduces the leak this whole
mechanism exists to bound.

The window has **two** interleavings and they end the same way. If the sweep already unlinked and
closed, the writer's `flock` succeeds and `os.fstat(fd).st_nlink` is zero. If the sweep still holds
its own lock, the writer's `flock` raises `EWOULDBLOCK` — and the sweep's very next syscall unlinks
the file, so retrying would win a lock on a file that is about to disappear. Both therefore raise
one attributable `ENOENT` naming the sweep, rather than streaming gigabytes into an inode no path
reaches (the first) or surfacing a bare `EWOULDBLOCK` that `_staging_fault` would render as "failed
to stage", pointing an operator at the object store over a purely local race (the second).

Reaching either requires a lost session lock *and* an interleave between two adjacent syscalls with
no I/O between them; what it buys is that the residue is a named, attributable failure rather than
the invisible-blocks leak the current code produces.

### 4. Every skip is logged, and the narrowing in reach is stated rather than claimed away

`_unlink_if_unheld` has three outcomes and they are materially different, so none of them is a bare
`return`.

*Held* (`EWOULDBLOCK`) is the correct action and also the **only** externally visible symptom of
the lost session lock: its other consequence is a redundant multi-GiB download that reads as
ordinary slowness. It gets a `WARNING`, for the reason ADR-0443 §4 logs a rejected base — the
operation succeeds, so the log line is the only evidence it ever fired.

*Cannot evaluate* — any other `OSError` from the `open` or the `flock` — is a real **narrowing** of
the unconditional `unlink` this replaces, and the honest statement is that reach is not entirely
unchanged. `unlink` needs write and execute on the *directory* and no permission on the file at
all; `open` needs to read the file. So a partial written under a uid asymmetry of the shape
ADR-0442 documents in this same subsystem, or one met with `EMFILE` under descriptor exhaustion
(likeliest exactly when many stagings are in flight), or any partial on a filesystem that answers
`ENOLCK`, was collected before and is not collected now. Those are left to the reclaim backstop
rather than unlinked blind, because a partial this process cannot open is one it cannot show is
dead, and unlinking it anyway is the bug being fixed. The `WARNING` is what keeps the narrowing
from being silent — without it an `ENOLCK` filesystem would retire the opportunistic sweep
altogether and nothing would say so.

*Absent* is the achieved post-state, not a fault: the reclaim-side backstop sweeps the same
directory.

### 5. A base a sibling already published is not superseded

Letting the losing fetcher survive its sweep means it now *reaches* the publish, and the sibling
that held the lock has by then normally finished, published `dest`, and had a per-System overlay
created against it. An unconditional `os.replace` would swap that inode out from under a running
guest. The bytes are identical — the base is content-addressed and checksum-verified — so this is
not corruption and not a wrong-image risk. The cost is that ADR-0443 §2's accepted residue
("superseding a base a guest holds open orphans the old inode until it exits") becomes reachable on
a brand-new trigger, for a base of up to the 50 GiB canonical cap, pinned by an open descriptor and
unreachable by every path-matching tool — which is the same invisible-blocks symptom this ADR's
Context cites as the harm being removed, re-created at the far end of the same scenario.

So the publish is skipped when `dest` already holds a base that passes the qcow2 gate. It is
unreachable on the ordinary path (the caller checked `dest` twice under the fetch lock and found
nothing), costs one O(1) probe after a download that already took minutes, and gets its own
`WARNING`.

The probe is deliberately **not** `_reusable_staged_base`, despite asking the same question of the
same file. That one raises on an unreadable `dest`, because answering "not reusable" there would
trigger a silent perpetual multi-GiB re-download. Here the polarity is reversed: answering
"publish" costs one `os.replace`, which is the behavior before this guard existed and which
*repairs* an unreadable `dest` — a rename needs permission on the directory, not on the file. Every
`OSError` therefore answers "publish", so the guard can only ever remove work and can never add a
failure to a download that already succeeded.

### 6. The sweep's `open` is `O_RDONLY | O_NONBLOCK`

`O_NONBLOCK` is a no-op on a regular file and is there for the same reason ADR-0443 §2 checks
`S_ISREG` before opening `dest`: opening a FIFO for reading blocks until a writer appears, and this
sweep runs *holding* the fetch advisory lock, so a hang here would wedge every sibling System on
that (investigation, checksum). Nothing in kdive creates a non-regular file at a `.partial` path,
and the unlink semantics for one are unchanged from today — but the code must not acquire a way to
hang that it did not have before.

## Consequences

- A live sibling's partial survives a sweep run by a fetcher that acquired the fetch lock after
  losing it — the acceptance criterion. The degradation from a lost session lock returns to
  ADR-0441 §5's originally stated one: a redundant download, never a failed provision. The
  redundant copy is then discarded rather than published (§5), so the sibling's base keeps its
  inode.
- Crash-orphan collection is unchanged in **latency** — still bounded by the next fetch of that base
  rather than by full investigation reclaim — and slightly narrowed in **reach**: a partial this
  process cannot `open` is no longer unlinked, where the previous unconditional `unlink` needed only
  directory permissions. §4 has the cases and the `WARNING` that makes each one visible; the reclaim
  backstop still collects them.
- **`fcntl.flock` is BSD `flock(2)` and that is load-bearing.** Its lock belongs to the open file
  *description*, so it survives each stager's own `partial.open("wb")` handle, the format gate's
  `"rb"` handle, and `_fsync_path`'s third descriptor being opened and closed on the same inode
  underneath it. POSIX record locks (`fcntl.lockf` / `F_SETLK`) drop a process's locks when *any*
  descriptor on the file closes, so swapping to them for a "more portable" API would silently
  unprotect the entire verify-and-publish window. A test sweeps in exactly that window — after the
  stager returns, before the publish — so the swap reddens instead of passing, which is the failure
  mode #1383's dead ELF-magic guard is the local precedent for.
- One extra file descriptor is held per in-flight staging operation, and two syscalls are added per
  swept candidate. Both are negligible against a multi-GiB download.
- **A hung-but-live fetcher's partial is not collected**, and that is correct rather than a
  shortcoming: the process is alive and may still finish. An mtime window would have reclaimed it
  and destroyed the download. It is bounded by the reclaim-side backstop when the investigation
  drains.
- The reclaim-side sweep (`sweep_investigation_staging_dir`) is deliberately **not** changed. It
  runs only once no committed rootfs row remains for the investigation, so no live fetcher for that
  base can exist and it needs no lock gate. Its file is also #1539's, and adding an unnecessary
  coupling there would be a regression risk taken for nothing.
- No schema, no migration, no config setting, no new dependency (`fcntl` is stdlib), no MCP/RBAC
  surface. Not an AI surface.

## Considered & rejected

- **An mtime window — skip partials modified within the provision timeout (#1524 option 1).**
  Rejected. It replaces a race with a tunable that is wrong in both directions under exactly the
  conditions that produce the bug. Too small and a stalled network read, a throttled or
  cgroup-suspended host, or a writer stuck behind writeback still gets its live partial destroyed —
  the failure this is meant to fix, now with a knob to misconfigure. Too large and a crash orphan of
  up to 50 GiB sits uncollected for the whole window, weakening the sweep ADR-0441 §5 added
  specifically so orphans would not wait for investigation reclaim. It is also wrong on facts
  outside the process's control: clock skew after an NTP step, and coarse mtime granularity on
  filesystems that do not carry sub-second timestamps. `flock` asks the kernel a question with a
  correct answer instead of guessing from a timestamp.
- **Do nothing; fix the connection instead (TCP keepalives, `keepalives_idle`).** Rejected as a
  substitute, reasonable as an independent hardening. Keepalives shorten the window for the
  idle-reap trigger and do nothing for backend termination, and a sweep whose correctness rests on
  a connection staying up is the defect, not the trigger.
- **Drop the opportunistic sweep and rely on reclaim alone.** Rejected: it would leave a SENSITIVE
  multi-GiB orphan on disk for the life of the investigation, which is the regression ADR-0441 §5
  introduced the sweep to avoid. The sweep is not the problem; its unconditional unlink is.
- **A PID or hostname sidecar naming the live writer.** Rejected: it needs liveness checking that is
  wrong across containers and PID namespaces, it does not self-clean on `SIGKILL`, and it is a
  second file in a directory whose `rmdir` is already load-bearing (#1539).
- **A `.lock` sidecar rather than locking the partial itself.** Rejected: two files whose lifetimes
  must be kept in step, a stale sidecar after a crash, and the same `rmdir` hazard — all to avoid
  locking the one file that already exists and already has exactly the lifetime wanted.

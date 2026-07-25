# Bound the rootfs orphan-partial sweep away from live partials (#1524)

- **Issue:** [#1524](https://github.com/randomparity/kdive/issues/1524)
- **ADR:** [ADR-0446](../adr/0446-flock-guarded-orphan-partial-sweep.md)
- **Date:** 2026-07-24

## Problem

`_unlink_orphan_partials` (`providers/local_libvirt/lifecycle/rootfs/rootfs_upload_fetch.py`)
glob-unlinks every `<token>.*.partial` in the per-investigation staging directory while holding the
per-(investigation, checksum) fetch lock. ADR-0441 §5 justifies the unconditional unlink on the
premise that the lock "serializes downloads so no *live* sibling exists".

The premise holds only while the lock does. The lock is a **session-scoped** `pg_advisory_lock` on
a connection that is TCP-idle for the entire multi-GiB download. If that session dies, Postgres
releases the lock while the owning process is still running and still writing its partial. A
sibling then acquires the lock, sweeps, and unlinks the live partial. The first fetcher writes on
into an unlinked inode and fails at `os.replace` with `ENOENT` — a failed provision after a
completed download, and until that process exits the blocks stay charged to `df` while being
invisible to every path-matching tool.

## Reachability, established against this codebase

The issue names "a PgBouncer recycle" as a trigger. **No pooler is in play.** `docker-compose.yml`
wires `server`, `worker`, and `reconciler` straight at `postgres:5432`, and `rootfs_upload_fetch`
opens its *own* short-lived `psycopg.connect(DATABASE_URL, autocommit=True)` rather than drawing
from the application pool. ADR-0005 anticipates a transaction-pooling PgBouncer and bans session
locks on *pooled* connections; ADR-0095 carves out session locks held on a dedicated non-pooled
connection, which is exactly what this one is. The pooler half of the issue's causal story does not
apply.

What remains, and is sufficient:

1. **Idle-connection reaping.** The session issues `pg_advisory_lock` and then sends nothing for the
   duration of the download. `psycopg.connect` sets no `keepalives_idle`, so the socket inherits the
   server's `tcp_keepalives_idle = 0` (defer to the OS, 7200 s on Linux) — far longer than the
   5–15 minute idle timeouts common to NAT/conntrack middleboxes and cloud NAT gateways. The drop is
   silent to the downloader.
2. **Server-side backend termination** — a Postgres restart, an administrative
   `pg_terminate_backend`, or the OOM killer taking the backend.

Both release the lock without the downloader noticing until its `pg_advisory_unlock`.

## Constraints established by reading the code

- **Partial names are already per-attempt unique:** `f"{dest.stem}.{uuid4().hex}.partial"`. No two
  fetchers ever name the same partial, so a per-partial exclusive lock is uncontended by
  construction — it is a liveness marker, not a mutex.
- **The staging directory is local and not operator-configurable.**
  `UPLOADS_DIR = /var/lib/kdive/rootfs-uploads` is a module constant in
  `providers/shared/runtime_paths.py`, on the local-libvirt provider's own host. Every process that
  can contend for a given partial is a worker process on that one host, so `flock`'s NFS caveat —
  which is about lock visibility *between clients* — cannot bite here even if an operator
  bind-mounts `/var/lib/kdive` onto NFS. (Linux emulates `flock` over NFS with whole-file POSIX
  locks and does propagate them between clients; under `-o local_lock=flock,all` it does not, but
  same-host processes still observe each other's locks in every mount configuration.)
- **The reclaim-side backstop is out of scope.** `sweep_investigation_staging_dir`
  (`jobs/handlers/artifacts/rootfs_reclaim.py`) runs only once no committed rootfs row remains for
  the investigation, so no live fetcher for that base can exist. It is also the file #1539 will
  edit; leaving it untouched keeps the serial queue on `rootfs_upload_fetch.py` clean.

## Decision

Per-partial `fcntl.flock(fd, LOCK_EX | LOCK_NB)`. See ADR-0446 for the argument and for why the
mtime-window alternative is rejected.

- Each fetcher creates its partial with `O_CREAT | O_EXCL` and takes an exclusive, non-blocking
  `flock` on it *before* any stager writes, holding the descriptor open across the download, the
  verify, and `_durable_replace`.
- The sweeper opens each glob match `O_RDONLY | O_NONBLOCK` and tries the same non-blocking
  `flock`. `BlockingIOError` means a live sibling still owns it — skip. Success means nothing holds
  it — unlink.

Crash orphans are still collected: the kernel drops every `flock` when the holding descriptor is
closed, including on process exit, so a `SIGKILL`ed worker's partial is unlocked by the time any
sibling sweeps it.

## Residual window, and how it is handled

`open(O_CREAT|O_EXCL)` and `flock()` are two syscalls; a sweeper that globs, opens, locks, and
unlinks strictly between them would still destroy a live partial. Closing it fully would take a
staged rename through a name the sweep glob does not match, which trades the window for an
uncollectable `.partial.tmp` orphan.

Instead the writer checks `os.fstat(fd).st_nlink` immediately after acquiring the lock. Zero means
its partial was unlinked in that window, and it raises rather than streaming gigabytes into an
inode no path reaches. That converts the residue from the silent invisible-blocks leak the current
code produces into a loud, attributable failure, and it requires a lost session lock *and* a
sub-millisecond interleave to reach at all.

## Tests (TDD)

Red against the current code first:

1. A sweep run while a sibling holds its partial's `flock` leaves that partial intact. This is the
   acceptance criterion and reproduces the lost-lock ordering: the sibling never released the fetch
   lock, the sweeper acquired it anyway.
2. The full `fetch_uploaded_rootfs` path with a live sibling partial present — the sweep is called
   from under the fetch lock, so the guard has to hold at the call site, not only in isolation.

Green-only (they pass before and after, and pin behavior the fix must not regress):

3. An unlocked crash orphan is still unlinked.
4. A partial whose lock holder has **exited** is unlinked — spawned as a real child process so the
   kernel, not the test, releases the lock.
5. A fetcher's own partial is locked while it stages, proven by a sweep from a second thread
   failing to remove it mid-download.
6. `st_nlink == 0` after acquiring the guard raises a staging fault and attempts no download.
7. Mixed directory: locked and unlocked partials side by side, only the unlocked one goes.
8. A sweep whose candidate vanishes between the glob and the open is a no-op, not an error.

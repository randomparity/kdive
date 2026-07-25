# The reclaim-side staging sweep is `flock`-gated too (#1544)

- **Issue:** [#1544](https://github.com/randomparity/kdive/issues/1544) — P2 bug
- **ADR:** [ADR-0452](../adr/0452-flock-guarded-reclaim-staging-sweep.md)
- **Completes:** [ADR-0446](../adr/0446-flock-guarded-orphan-partial-sweep.md), which fixed the
  fetch-side sweep and recorded this one as an open residual
- **Amends:** [ADR-0442](../adr/0442-rootfs-reclaim-worker-job.md) §7 — the drain tail's
  unconditional `rootfs_cleanup_pending_at` clear gains one exception

## Problem

There are two places that unlink a `<token>.*.partial`. ADR-0446 gated one of them on an `flock`.
The other — `sweep_investigation_staging_dir` in
`src/kdive/jobs/handlers/artifacts/rootfs_reclaim.py` — still runs

```python
with suppress(OSError):
    for partial in inv_dir.glob("*.partial"):
        partial.unlink(missing_ok=True)
```

with no liveness gate at all, and it is *broader* than the fetch-side sweep: it globs every token in
the investigation directory rather than one base's.

Its safety is **derived**, and the derivation has a hole. The reflex justification — the one
ADR-0442 §7 and this function's own docstring state — is that the sweep runs only once no committed
rootfs row remains for the investigation, so no live fetcher for that base can exist. But the row
count reaches zero only because `rootfs_base_reclaimable` classified the base as unpinned, and that
gate classifies by the System row's **state column** plus overlay-file presence:

- `_ROOTFS_REFERENCERS_SQL` filters `state <> 'torn_down'`, so a torn-down System is never even
  considered as a referencer.
- `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` is `{defined, provisioning, reprovisioning, restoring}`,
  so `failed` is outside it.
- A *provisioning* System has no overlay file yet, so `_overlay_pins_base` returns `False` too.

`PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` are both legal transitions
(`src/kdive/domain/capacity/state.py`). Meanwhile the download runs detached under
`asyncio.to_thread`, which cannot be cancelled and keeps writing regardless of what any other actor
does to the row — the same property `rootfs_upload_fetch.py`'s module docstring already notes about
`SIGTERM`. And nothing serializes the two: the fetch takes only the per-(investigation, checksum)
session lock, never the `INVESTIGATION` advisory lock the reclaim holds.

So a concurrent teardown, or any actor failing the System, drops the pin, lets the reclaim delete
the last rootfs row, and the very next statement sweeps a **live** partial. The consequence is
#1524's exactly: the fetcher writes on into an unlinked inode and fails at `os.replace`, with the
written blocks charged to `df` yet invisible to every path-matching tool until the process exits.

## Blast radius today

Reaching the defect needs the pin-dropping transition to land inside the download window, which is
minutes wide for a multi-GiB base — so it is rare but not exotic, and it is exactly the window an
operator tearing down a stuck provision acts in. `_require_still_linked(fd, partial,
window=_DOWNLOAD_WINDOW)` already names this sweep as one of its two causes and cites #1544, so the
fetcher reports the condition attributably; it just cannot survive it.

## Requirements

- **R1** — A partial a live writer holds under an exclusive `flock` survives
  `sweep_investigation_staging_dir`, including on the `PROVISIONING -> TORN_DOWN` ordering (pin
  dropped, last rootfs row reclaimed, sweep running while the detached download still writes).
- **R2** — An unheld crash orphan is still collected. The reclaim sweep is the backstop for
  everything the fetch-side sweep narrowed away, so its reach must not shrink.
- **R3** — The per-investigation staging directory is still removed once it drains.
- **R4** — The liveness primitive is **reused**, not re-implemented. Two copies of a two-syscall
  liveness test would drift, and the fetch-side one carries the whole ADR-0446 derivation in its
  docstring.
- **R5** — The drain-marker interaction is decided deliberately rather than discovered: a skipped
  live partial leaves `inv_dir` non-empty, so the `rmdir` fails `ENOTEMPTY`.
- **R6** — Every skip is logged, and the log text is correct for **both** call sites.

## Design

### The primitive moves to `providers/shared`

`_unlink_if_unheld` is provider-private in `local_libvirt`, and `src/kdive/jobs/` imports nothing
from that package (it does already import `kdive.providers.shared.runtime_paths`, so the shared
direction is sanctioned). It moves verbatim in behaviour to
`src/kdive/providers/shared/staging_partials.py` as `unlink_partial_if_unheld`, and
`_unlink_orphan_partials` calls the shared name. Nothing else about the fetch side changes.

### The log text becomes observation, not inference

The held-branch `WARNING` currently asserts a fetch-side cause: "this fetcher acquired the rootfs
fetch lock while a sibling was still downloading — its Postgres session was lost mid-transfer, and
this download is redundant". That is false on the reclaim path, where no fetch lock is involved at
all. The message states the observed fact instead — a live writer holds the partial, it is left in
place, and it is collectable once its holder exits — which is `_release_fetch_lock`'s own stated
principle in this same subsystem: report the observed state rather than the inferred cause, so a
conditional is not written down as an invariant. Both derivations stay in the docstring and in the
ADRs, where a reader who needs them will look.

### The drain marker is retained only for a *live-held* skip

`_finish_drained_investigation` sweeps and then unconditionally clears
`rootfs_cleanup_pending_at`. With a liveness gate the sweep can now leave something behind, so the
clear needs an answer.

`sweep_investigation_staging_dir` returns whether it skipped a partial **because a live writer holds
its `flock`**, and that single case retains the marker; the close-driven reconciler sweep then
re-issues the reclaim job past its 5-minute backoff and the next pass converges. Every other
outcome — swept, vanished, unopenable, unlinkable — clears the marker exactly as today.

The narrowness is the point in both directions:

- **Clearing unconditionally is wrong**, and not only cosmetically. The marker is the *only* thing
  that re-enqueues a reclaim job for a closed investigation, and the fetch-side opportunistic sweep
  only runs on the next fetch of that base — which never happens once the investigation is closed.
  So if the live holder is then killed mid-download, its multi-GiB SENSITIVE partial has no
  collector at all. That is precisely the leak the backstop exists to prevent.
- **Retaining on every non-drain is also wrong.** An unopenable or unlinkable partial (`EACCES`
  under the uid asymmetry ADR-0442 documents in this same subsystem, `EROFS`, `EIO`) is permanent
  until an operator acts, so retaining on it resurrects the never-clearing marker plus the
  re-fail-every-pass loop that ADR-0442's Context is written about. A held `flock` is the one
  outcome that is *provably* transient: the kernel releases it when the holding descriptor closes,
  including on process exit, normal or `SIGKILL`.

`rmdir` keeps its `suppress(OSError)` and stays where it is. `ENOTEMPTY` is then the achieved
post-state of a live-held skip rather than a surprise, and the retained marker is what brings the
next pass back to finish the job.

## What this does not fix

- **The `flock` guard is best-effort, because `_flocked_partial` degrades.** On a filesystem that
  cannot lock (`ENOLCK` on an NFS mount whose lock manager is down, `EOPNOTSUPP` on some FUSE and 9p
  backends) the fetcher stages *unguarded* with a `WARNING` — the documented ADR-0446 §5 degrade —
  and this sweep will then find nothing holding the partial and unlink it. The outcome there is the
  pre-ADR-0446 one, which is the point of degrading rather than failing, and
  `_require_still_linked`'s `_DOWNLOAD_WINDOW` message already names it as one of its two causes.
  This change removes the reclaim path from that message's causes, not the degrade.
- **The pin-dropping classification itself.** `rootfs_base_reclaimable` still lets
  `PROVISIONING -> TORN_DOWN` / `-> FAILED` drop the pin. This change makes the sweep's safety
  independent of that classification instead of correcting it, which is the same move ADR-0446 made
  on the fetch side.
- **A base a doomed fetcher publishes after the pin dropped.** If the live writer runs to
  completion it `os.replace`s its partial onto `<token>.qcow2` whose `artifacts` row the reclaim has
  already deleted, leaving a staged base nothing owns. That is a consequence of the pin-dropping
  transition, not of this gate — but this change does alter its shape, and the ADR says so rather
  than leaving it to be rediscovered. It is filed as a follow-up.

## Test plan

| Requirement | Test |
| --- | --- |
| R1 | A `<token>.*.partial` held under an exclusive `flock` survives the sweep, contents intact |
| R1 | The same, driven through the real handler on the `PROVISIONING -> TORN_DOWN` ordering: the System row is torn down, the last rootfs row is reclaimed, and the sweep runs against a held partial |
| R2 | An unheld crash orphan is still unlinked, including beside a held one in the same directory (an all-or-nothing gate fails this) |
| R2 | A partial whose holder process is `SIGKILL`ed is collected on the next sweep with no timeout |
| R3 | The staging dir is removed once it drains; a dir still holding a base is left in place |
| R5 | A live-held skip retains `rootfs_cleanup_pending_at`; a drained sweep clears it |
| R5 | An *unopenable* partial (no `flock` held) clears the marker, so a permanent fault cannot pin it forever |

## Alternatives considered

- **Import the provider-private helper from the job handler.** Rejected: a `jobs/` handler reaching
  into `providers/local_libvirt/lifecycle/rootfs/` inverts the dependency direction the package
  layout encodes, and `providers/shared/` exists for exactly this.
- **Write a second liveness helper in `rootfs_reclaim.py`.** Rejected outright: the two would drift,
  and the fetch-side docstring is where the derivation lives.
- **Gate the reclaim sweep on an mtime window instead.** Rejected for ADR-0446's reasons, unchanged
  here — a tunable that is wrong in both directions under exactly the conditions that produce the
  bug.
- **Fix `rootfs_base_reclaimable` so `PROVISIONING -> TORN_DOWN` cannot drop the pin.** Not a
  substitute. It is a real hardening and worth doing, but a sweep whose correctness rests on a
  state-column classification is the defect class both ADRs are removing; the `flock` asks the
  kernel a question with a correct answer.

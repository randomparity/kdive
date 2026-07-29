# ADR 0497 — Verify the object at finalize instead of conditionally deleting at the sweep

- **Status:** Accepted
- **Date:** 2026-07-29
- **Deciders:** KDIVE maintainers

## Context

[ADR-0455](0455-upload-prefix-orphan-sweep.md) §3 discloses a residual it declines to close: an
object PUT between the upload orphan sweep's per-key re-read and its `delete_object` is destroyed.
The sweep re-reads the key's mtime and re-runs `reclaimable_upload_keys` immediately before the
delete, so a row or a re-mint committed after the bulk classify protects its object — but a PUT
landing after that re-read has neither a row nor a fresh mtime the sweep will look at again, and
`store.delete` then runs unconditionally.

The losing interleaving is reachable in the code, not hypothetically. `UPLOAD_ORPHAN_ROOTS` is
derived from `UPLOAD_TENANT = "local"`, so the swept roots are `local/runs/` and
`local/investigations/` — and the **local-libvirt** provider writes its vmcore pair there
(`providers/local_libvirt/retrieve.py` is constructed with `tenant="local"`; its raw core goes
through `_put_stream` and its redacted sibling through `_put`). That lane mints **no** upload window
at all, so the manifest fence never applies to it:

1. A first `capture_vmcore` attempt streams `local/runs/<run>/vmcore-<method>` into the store and
   dies before `finalize_capture`. The object is now rowless and manifest-less, and stays that way.
2. More than `orphan_grace + upload_ttl` later (48h at the defaults) the sweep lists the root and
   classifies the key reclaimable — no `artifacts` row, no `upload_manifests` row, mtime past the
   threshold.
3. The re-read observes the old mtime; `reclaimable_upload_keys` re-confirms.
4. **A retried capture's `put_stream` completes.** The key now holds fresh, wanted bytes.
5. `store.delete` executes. The bytes are gone.
6. `finalize_capture` commits `artifacts` rows against an object that no longer exists — a dangling
   reference on a Run that reports success, which is the defect class
   [ADR-0453](0453-row-first-upload-reap.md) and ADR-0455 exist to keep out of the tree. Nothing
   raises.

The write in step 4 holds no lock the sweep could contend on, but not because there is no
server-side call to hold one across — there is. `precheck_run`
(`jobs/handlers/artifacts/vmcore.py`) deliberately releases the `LockScope.RUN` advisory lock
*before* the provider capture ([ADR-0244](0244-per-run-vmcore-capture.md)) precisely so a
multi-GiB stream is not held under it, and the sweep takes no Run lock at any point. Both halves
would have to change for a lock to fence this, which is what §3 weighs.

Two adjacent lanes are **not** the path in, and naming them keeps the scope honest:

- **remote-libvirt's presigned guest PUT is out of reach of the sweep entirely.** Its keys are built
  from `TENANT = "remote-libvirt"` (`providers/remote_libvirt/retrieve/common.py`), so
  `remote-libvirt/runs/<run>/vmcore-kdump` sits under no swept root and cannot be classified,
  re-read, or deleted by this repair. The verify this ADR adds still covers it, but there it guards
  only an operator delete or a bucket lifecycle rule.
- **`capture_traffic` writes under `local/runs/` but holds the Run lock across its PUT**
  (`jobs/handlers/control/capture_traffic.py::_store_capture`), so it is the one writer a shared
  fence would already reach.

This is also why the residual is not quite the shape of
[#1557](https://github.com/randomparity/kdive/issues/1557), even though the two are siblings: the
reaper side has no re-read at all.

## Decision

**We will not add a conditional delete. We will verify at finalize instead: `finalize_capture` heads
each object it is about to register and refuses to commit any row unless the store still holds that
object at the etag the capture observed.**

### 1. The conditional delete was measured against the deployed store and rejected on the result

The narrowest fix on paper is a conditional delete — pass the etag the re-read already observed
(ADR-0496 put it in scope at the delete site as `_CurrentObject.etag`, in the same `head_object`
response as the mtime, so the two cannot disagree about which bytes were examined) as S3
`If-Match` on `DeleteObject`, so a PUT inside the gap makes the delete fail rather than succeed.
`botocore` models `IfMatch` on `DeleteObject`, so it costs one keyword argument and a method on the
narrow `UploadOrphanStore` port.

**MinIO does not honour it.** Probed directly against both pinned releases — the test-infra
container (`minio/minio:RELEASE.2025-09-07T16-13-09Z`, `tests/store/conftest.py`) and the
deployed compose/Helm image (`minio/minio:RELEASE.2025-04-22T22-12-26Z`, `docker-compose.yml`,
`deploy/helm/kdive/values.yaml`) — with a `before-send` hook confirming the header reached the wire:

| arm | request | MinIO | object |
| --- | --- | --- | --- |
| stale etag (the race) | `DELETE` + `If-Match: "<etag of the overwritten bytes>"` | success, no error | **destroyed** |
| matching etag | `DELETE` + `If-Match: "<current etag>"` | success | deleted |
| absent key | `DELETE` + `If-Match: "<garbage>"` | success, no error | n/a |

The precondition is not evaluated and not rejected; it is ignored. A guard built on it would pass
every test written against an S3 stub, would read in the source as if the race were closed, and
would delete the object anyway on every deployment this repo actually ships — the phantom-feature
failure mode, with data loss behind it. Emitting the header anyway "for stores that honour it" was
rejected for the same reason: on a mixed fleet the sweep's behaviour would then be
backend-dependent and unstated, and the one backend we run is the one that ignores it.

We therefore do not widen the store port, and `ObjectStore.delete` keeps its `delete(self, key)`
shape (roughly twenty declarations in this tree conform to it).

### 2. What ships: finalize verifies the objects it is about to reference

`finalize_capture` takes an object-store port and, inside the same transaction and `LockScope.RUN`
advisory lock that inserts the rows, heads **both** objects the `CaptureOutput` names — the raw core
and its redacted sibling. Under local-libvirt both sit beneath `local/runs/<run_id>/` and are
therefore sweep candidates for the whole capture; the check is written against the `CaptureOutput`
rather than against a key prefix so it holds for every provider without knowing which tenant it
writes to. A key that is absent, or present at a different etag, raises `INFRASTRUCTURE_FAILURE`;
the transaction rolls back and **no** row is committed.

The comparison is on the etag, not on mere existence, and that is a deliberate strengthening: the
etag the capture observed is already carried on `StoredArtifact` by every producer — the store
returns it from local-libvirt's `put_stream`/`put_artifact`, and remote-libvirt's `_reference` reads
it from the post-upload HEAD — so comparing it also catches an object that was deleted *and re-PUT*
between the capture and the finalize, a case a presence check cannot see and which would commit a row
whose `etag` column is a lie.

The verify is the **last** thing the transaction does before committing. Ordering matters only for
the size of the residual window, not for correctness: the sweep's per-key re-check reads committed
rows, so an uncommitted insert protects nothing, and the object is protected the instant the
transaction commits. Putting the HEAD immediately before the commit therefore reduces the exposed
window from the length of a multi-GiB write to a HEAD plus a commit round trip. The steady-state cost
is one blocking store call per object under the Run lock, negligible next to the capture it follows;
the cost when that call *fails* is not negligible, and Consequences states it rather than burying it.

### 3. This mitigates the race; it does not close it

Stated plainly, because a mitigation recorded as a fix is worse than no record: **the sweep still
deletes the object.** What changes is that the loss stops being silent. Before this ADR the
sequence ended with a green Run and an `artifacts` row pointing at nothing; after it, the
`capture_vmcore` job fails with an `INFRASTRUCTURE_FAILURE` naming the key, and the retry — whose
`precheck_run` finds no raw core row — captures again from scratch. Against the issue's acceptance
criteria: "no `artifacts` row can be committed against an object the sweep deleted" holds; "the PUT
survives, or the delete fails loudly" does not, and cannot, for as long as the backing store ignores
the precondition.

The only remedy that would actually close it is a fence the sweep and the object-before-row writers
share, and there are two candidate fences with different reach.

A **shared advisory lock** would reach the local-libvirt vmcore write, since that write is
server-side — but it requires reversing ADR-0244's deliberate release of the Run lock before the
capture, which would hold `LockScope.RUN` for the length of a multi-GiB stream and block every other
Run-scoped operation behind it, and it requires the sweep to take a per-candidate Run lock it takes
none of today. It also does not generalise: `local/investigations/` is the rootfs upload lane's root
and its objects arrive as **presigned client PUTs**, where there genuinely is no server-side call to
hold a lock across.

A **row minted before the write** does generalise, needs no store capability, and reuses fences the
sweep already evaluates: an `upload_manifests` row for the `(runs, run_id)` owner, committed before
the capture's `put_stream` and dropped by the finalize, is protected by the sweep's manifest fence
unconditionally and for every writer under a swept root. That is a change on the mint side of a lane
that deliberately has no upload window today, it is the mechanism #1557 also wants, and it is left
unowned by this ADR rather than half-built here.

Re-probing MinIO on a future release for `If-Match` support is the other path back to §1's rejected
option.

### 4. The primitive is not reusable for #1557; the finding is

[#1557](https://github.com/randomparity/kdive/issues/1557) is the same TOCTOU on the reaper's
`_sweep_uncommitted_objects`, which deletes a phase-1 key list with no re-read at all. Nothing in
this ADR is directly reusable there, because nothing was built on the delete side — that is the
point of §1. What does carry over is the measurement: #1557 should not be specified around a
conditional delete either, and the row-fence in §3 is the mechanism both issues want. Its remaining
gap is wider than this one's, since it lacks even the re-read.

## Consequences

`finalize_capture`, `capture_handler`, and `vmcore.register_handlers` gain a required
`artifact_store` keyword, wired from `ObjectStoreAssembly.store` in `jobs/assembly.py` alongside
every other handler group that already takes one. Required rather than defaulted: a `None` default
that skips verification is a guard that silently does nothing, which is the failure mode §1 rejects,
and an `object_store_from_env()` fallback would make a worker's verification depend on ambient
environment at a site where the caller already has the store. The cost is the call-site churn in the
vmcore capture tests, which now construct a store that holds the objects their `CaptureOutput`
claims — a fake that is more faithful than the one they had.

A capture whose object genuinely vanished for an unrelated reason — an operator delete, a bucket
lifecycle rule — now fails the job where it previously succeeded with a dangling row. That is the
intended trade: the row was never usable, and `artifacts.get` on it failed later and further from
the cause.

Two further arms are reachable and are disclosed rather than smoothed over:

- **A transient HEAD fault now discards a completed capture.** `ObjectStore.head` raises
  `INFRASTRUCTURE_FAILURE` for any non-404, and the verify does not distinguish "the store answered:
  gone" from "the store could not be asked" — it cannot, because committing on an unanswerable HEAD
  reopens exactly the hole this closes. So a single 503 or socket timeout on either HEAD rolls back
  the inserts for a capture whose object is intact, and the job's retry re-captures from scratch:
  another multi-GiB stream. The verify itself is one round trip, but the *cost of its failure* is a
  whole re-capture, which is a worse amplification than the round trip suggests. Bounding it with a
  HEAD retry is deliberately not done here — it is a second mechanism to get right, and the arm is
  transient and self-healing — but the disclosure is the record for whoever sees it in production.
- **Two concurrent captures of the same Run whose bytes differ now fail one — or both — of the two
  dispatches.** Both write the same two deterministic keys, so the later write to each key wins. When
  one dispatch wins both keys, the other's finalize heads them, sees the winner's etags, refuses, and
  its retry replays into the row the winner committed. But each capture writes the raw core and the
  redacted dmesg as *separate* objects at different times, so the two writes can interleave in
  opposite orders — `raw` from B, `redacted` from A — and then **neither** dispatch matches on both
  objects, neither commits, and the retry re-captures from scratch rather than replaying. Previously
  both committed and one row carried an `etag` matching no bytes in the bucket, so refusing is the
  more truthful outcome either way, and `INFRASTRUCTURE_FAILURE` is retryable so nothing is lost —
  but the cost in the interleaved case is a whole extra capture, not a replay. Byte-identical cores
  still both succeed, which is what `test_concurrent_same_run_capture_writes_one_core` pins.
- **The verify's HEADs run under the Run advisory lock with botocore's default retry budget.** The
  store client is built with no explicit `Config`, so each HEAD can spend up to five attempts at a
  60-second read timeout before raising. Against a store that completed the multi-GiB write and then
  began blackholing HEADs, the finalize can therefore hold `LockScope.RUN` and one idle-in-transaction
  pool backend for minutes before rolling back. There is precedent in this tree — `capture_traffic`'s
  `_store_capture` holds the same lock across a full `put_artifact`, which is strictly heavier than a
  HEAD — and the condition is a store already too degraded to serve the capture plane, so this ships
  as a stated bound rather than a bespoke per-call timeout. Tightening it is a follow-on if a
  deployment ever sees it.

The sweep is untouched. ADR-0455 §3's residual paragraph is amended to record this resolution and to
name the MinIO measurement, so the next reader does not re-derive the rejected option. A same-key
gap test is added to the sweep's suite: the existing concurrency tests all fire their hook before a
*different* key's delete, so the same-key re-read→delete gap — the one this issue is about — was
never exercised. It now is, and it asserts the object is destroyed, which is the residual §3 states
rather than a fix.

## Alternatives considered

- **S3 `If-Match` on `DeleteObject`** — rejected on measurement, §1. Not a design disagreement: the
  header is ignored by both MinIO releases this repo pins.
- **An owner-scoped advisory lock shared by the sweep and the writers** — rejected on cost and
  reach, not on reachability (§3): it would fence the local-libvirt vmcore write, but only by
  reversing ADR-0244's release of the Run lock before a multi-GiB capture, and it cannot cover
  `local/investigations/`, whose objects arrive as presigned client PUTs with no server-side call at
  all.
- **An `upload_manifests` row minted before the capture's write** — the option that would actually
  close the race (§3), and deliberately not taken here. It changes the mint side of a lane that has
  no upload window by design, it is shared with #1557, and bundling it into a mitigation would leave
  neither properly reviewed.
- **A presence-only re-head at finalize** — rejected for the strictly cheaper etag comparison, §2.
  Presence alone cannot distinguish the object the capture wrote from a different object at the same
  key, and the row would then carry an `etag` that never matched the bytes.
- **Verifying in the provider's `_reference` instead** — rejected: that HEAD already happens, and it
  happens *before* the row commit by the full width of the window this ADR narrows. Moving the check
  earlier moves it into the gap rather than out of it.
- **Re-heading after the commit and deleting the row on mismatch** — rejected as a compensating
  action that can itself fail, leaving exactly the dangling row it exists to remove; refusing to
  commit needs no compensation.

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

The losing interleaving is reachable in the code, not hypothetically. `local/runs/<run_id>/` holds
the vmcore lane's two objects, and that lane mints **no** upload window at all:

1. A first `capture_vmcore` attempt PUTs `local/runs/<run>/vmcore-<method>` and dies before
   `finalize_capture`. The object is now rowless and manifest-less, and stays that way.
2. More than `orphan_grace + upload_ttl` later (48h at the defaults) the sweep lists the root and
   classifies the key reclaimable — no `artifacts` row, no `upload_manifests` row, mtime past the
   threshold.
3. The re-read observes the old mtime; `reclaimable_upload_keys` re-confirms.
4. **A retried capture's presigned PUT completes.** The key now holds fresh, wanted bytes.
5. `store.delete` executes. The bytes are gone.
6. `finalize_capture` commits `artifacts` rows against an object that no longer exists — a dangling
   reference on a Run that reports success, which is the defect class
   [ADR-0453](0453-row-first-upload-reap.md) and ADR-0455 exist to keep out of the tree. Nothing
   raises.

The write in step 4 holds no lock the sweep could contend on. `precheck_run`
(`jobs/handlers/artifacts/vmcore.py`) releases the `LockScope.RUN` advisory lock before the provider
capture, and the vmcore PUT is performed **by the guest** against a presigned URL — there is no
server-side call to wrap in a lock at all. That is what rules out the fence that would otherwise be
the obvious remedy, and it is why this residual is not the same shape as
[#1557](https://github.com/randomparity/kdive/issues/1557) even though the two are siblings:
`capture_traffic` PUTs inside `advisory_xact_lock(RUN)` and *would* be fenced by a shared lock, but
the cheapest path into this race is precisely the one a lock cannot cover.

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
and its redacted sibling, both of which sit under `local/runs/<run_id>/` and are both sweep
candidates for the whole capture. A key that is absent, or present at a different etag, raises
`INFRASTRUCTURE_FAILURE`; the transaction rolls back and **no** row is committed.

The comparison is on the etag, not on mere existence, and that is a deliberate strengthening: the
etag the capture observed is already carried on `StoredArtifact` (the remote-libvirt retriever's
`_reference` reads it from the post-upload HEAD), so comparing it also catches an object that was
deleted *and re-PUT* between the capture and the finalize — a case a presence check cannot see and
which would commit a row whose `etag` column is a lie.

The verify is the **last** thing the transaction does before committing. Ordering matters only for
the size of the residual window, not for correctness: the sweep's per-key re-check reads committed
rows, so an uncommitted insert protects nothing, and the object is protected the instant the
transaction commits. Putting the HEAD immediately before the commit therefore reduces the exposed
window from the length of a multi-GiB PUT to a HEAD plus a commit round trip. The cost is one
blocking store call under the Run lock, which is a rounding error next to the capture it follows.

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
share. For the vmcore lane that means giving the capture a row the sweep's existing fences already
respect — an `upload_manifests` row for the `(runs, run_id)` owner minted before the presigned URL
is handed to the guest, which the sweep's manifest fence protects unconditionally and which needs no
store capability at all. That is a change on the mint side of a lane that deliberately has no upload
window today, it is the same mechanism #1557 needs, and it is left unowned by this ADR rather than
half-built here. Re-probing MinIO on a future release for `If-Match` support is the other path back
to §1's rejected option.

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

The sweep is untouched. ADR-0455 §3's residual paragraph is amended to record this resolution and to
name the MinIO measurement, so the next reader does not re-derive the rejected option. A same-key
gap test is added to the sweep's suite: the existing concurrency tests all fire their hook before a
*different* key's delete, so the same-key re-read→delete gap — the one this issue is about — was
never exercised. It now is, and it asserts the object is destroyed, which is the residual §3 states
rather than a fix.

## Alternatives considered

- **S3 `If-Match` on `DeleteObject`** — rejected on measurement, §1. Not a design disagreement: the
  header is ignored by both MinIO releases this repo pins.
- **An owner-scoped advisory lock shared by the sweep and the writers** — the fence #1557 needs
  anyway, and rejected here because it does not reach this race: the vmcore PUT is performed by the
  guest over a presigned URL, so there is no server-side operation to hold a lock across. It would
  fence `capture_traffic`, which already holds the Run lock across its PUT and is therefore not the
  path in.
- **A presigned-URL-time `upload_manifests` row for the vmcore lane** — the only option that would
  actually close the race (§3), and deliberately not taken here. It changes the mint side of a lane
  that has no upload window by design, it is shared with #1557, and bundling it into a mitigation
  would leave neither properly reviewed.
- **A presence-only re-head at finalize** — rejected for the strictly cheaper etag comparison, §2.
  Presence alone cannot distinguish the object the capture wrote from a different object at the same
  key, and the row would then carry an `etag` that never matched the bytes.
- **Verifying in the provider's `_reference` instead** — rejected: that HEAD already happens, and it
  happens *before* the row commit by the full width of the window this ADR narrows. Moving the check
  earlier moves it into the gap rather than out of it.
- **Re-heading after the commit and deleting the row on mismatch** — rejected as a compensating
  action that can itself fail, leaving exactly the dangling row it exists to remove; refusing to
  commit needs no compensation.

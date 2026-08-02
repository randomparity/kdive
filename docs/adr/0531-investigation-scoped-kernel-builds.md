# 0531 — Investigation-scoped reusable kernel builds

## Status

Accepted (2026-08-01)

## Context

External kernel uploads are finalized onto one Run. Their artifact rows use
`owner_kind='runs'`, and the Run is the only durable handle for the validated kernel, optional
initrd, debuginfo, build id, cmdline, and provenance. A second System in the same Investigation
therefore needs another upload even when it should boot the identical build. Investigation close
already governs build-artifact reclamation, but the ownership model and `runs.create` contract do
not expose that lifetime.

ADR-0316 removed KDIVE's server-side kernel build lane. “Build once” here means the agent builds
outside KDIVE and uploads the resulting validated artifact set once; this decision does not restore
`runs.build` or any managed build host.

A reusable build is a set, not one object: installation needs the validated kernel and may also
need an initrd and debuginfo, while debug consumers need the same build id and provenance. A bare
object checksum cannot identify that complete set.

## Decision

Finalizing an external build creates an investigation-owned build record with immutable content.
Its public
`build_ref` is `<content_digest>.<generation>`. `content_digest` is the lowercase hexadecimal
SHA-256 of a versioned canonical document containing the validated artifact checksums and build
metadata. A chunked artifact uses its ordered validator-backed `(checksum, size)` vector and total
size; its advisory whole-object hash is excluded. `generation` is an opaque UUID minted for one
publication lifetime. The artifact catalog
rows use `owner_kind='investigations'` and the Investigation id.

The record stores one exact validated artifact set. Under the Investigation lock, a finalizer first
looks for an active, unexpired record with the same `content_digest`. If one exists it verifies the
canonical document and converges on that winner. Otherwise it mints a new generation and publishes
its candidate, even when an expired or reclaiming generation has the same digest. Only the selected
generation registers investigation-owned artifact rows. A convergence loser retains its uploaded
objects as uncommitted Run-prefix objects, deletes their exact versions after commit, and leaves a
failed delete to the existing prefix-orphan sweep. It never registers or deletes a selected
generation's objects. Reclaim marks one generation `reclaiming` before object deletion and deletes
the record only if that generation remains reclaiming; a new generation has distinct rows and
objects. The lock spans selection, publication, and the reclaim state transition.
Before deleting the catalog row, reclaim stores its Investigation, exact `build_ref`, and expiry in
a durable tombstone. Only the same Investigation consults that tombstone, preserving expired-handle
recovery after GC while unknown and cross-Investigation handles remain not found.

Validation is version-bound: the first HEAD captures each single-upload VersionId and every ranged
semantic read names it. Chunk verification similarly captures each chunk VersionId, server-side
copy names those exact versions, and multipart completion returns the final VersionId that later
validation and publication use. Outstanding upload URLs may create newer versions, but cannot
change the bytes a published generation validated. Install, raw download, GDB/module staging,
crash, and drgn consumers all read the persisted VersionIds; key-only reads remain solely for
legacy Run steps that predate version pins.

Every path that takes both scopes follows the repository order Investigation → Run.
`runs.complete_build` changes its final transaction to that order and rechecks the Run state and
upload-window identity under both locks before publication. `runs.install` and generation reclaim
use the same order. Run-only bind, cancel, and upload-reaper paths do not call an
Investigation-locked helper while holding the Run lock.

`runs.complete_build` still completes its source Run and additionally returns the `build_ref`.
`runs.create` accepts an optional `build_ref`. Under the Investigation lock it resolves only a
record owned by the requested Investigation, requires its target architecture and build profile to
match the new Run, and creates that Run with the immutable build result and succeeded build step
already attached. No object is copied and no upload window is minted. The response and wrapper
contract direct the caller to `runs.install` rather than the external-build upload sequence.

The reference and normal create inputs participate in idempotency. A missing, malformed,
cross-Investigation, or incompatible reference fails as `configuration_error` without revealing
whether another tenant owns a matching build. The source Run may be terminal or deleted later;
the build record, not that Run, is the reuse authority.

Investigation-close-plus-grace garbage collection deletes the build's investigation-owned artifacts
and then its build record. ADR-0234's independent TTL backstop also applies: the build record stores
an absolute `expires_at` stamped from the Postgres clock at completion using
`KDIVE_BUILD_ARTIFACT_RETENTION_DAYS` (days, per generation). `runs.complete_build` returns that deadline
and `server_time`. `runs.create` rejects a reference at or after the deadline with
`reason='build_ref_expired'`, the same two timestamps, and `runs.create` as its literal next tool.
The caller retries the same Investigation, System or target kind, and profile without `build_ref`;
that successful response provides the new Run id and directs the existing
`artifacts.create_run_upload` → upload → `runs.complete_build` recovery sequence. Reclaim locks the
Investigation and rechecks the deadline and that no live Run references the build before deletion.
Runs store the selected `build_ref`, so concurrent create versus reclaim is serialized and the
reference remains auditable.

Install is the artifact-use fence. `runs.install` acquires the Investigation lock before its Run
lock and atomically checks the generation deadline while enqueueing. A new or restaged install at
or after expiry fails with the same timestamps and recreate/re-upload recovery as expired reuse; an
idempotent no-op for the already installed variant remains callable because it reads no artifact.
Garbage collection defers while an install job for a referencing Run is queued or running. This
closes the admission-to-handler path for ordinary job completion and failure. Proving and recovering
artifact-use fences across worker process death or a provider thread that outlives its job state is
separate platform work tracked by [#1803](https://github.com/randomparity/kdive/issues/1803).

## Consequences

- One validated upload can back any number of compatible Runs and Systems in its Investigation.
- The public handle identifies the complete build set; callers do not assemble object references.
- Cross-Investigation reuse is rejected at the ownership predicate even when bytes are identical.
- Existing run-owned build rows remain readable and reclaimable; there is no backfill or dual
  creation path for new completions.
- Duplicate physical uploads can occur before the content identity is known. Only one becomes the
  durable build; losing object versions are best-effort deleted and remain covered by orphan repair.
- Investigation ownership permits reuse within the advertised absolute deadline; it does not waive
  ADR-0234's never-closed-Investigation storage bound.
- Re-uploading and completing identical content after expiry publishes a new generation and
  deadline. Looking up or reusing a build does not extend retention.
- Reuse bypasses build upload and validation because it selects an already validated immutable
  record. Install, boot, and debug behavior remain unchanged.
- A queued or running install pins its generation. A delayed first install or restage after expiry
  must recreate and upload; an already-installed unchanged variant remains an idempotent no-op.
- Worker-crash and provider-thread-lifetime fencing is intentionally outside this decision and is
  tracked by [#1803](https://github.com/randomparity/kdive/issues/1803).
- The schema gains an investigation-build catalog and a nullable `runs.build_ref` audit link.

## Considered & rejected

- **Pass a source Run id to `runs.create`.** Rejected because Run lifetime would remain the
  ownership authority and a mutable lifecycle object is not a content address for the build set.
- **Pass individual kernel, initrd, and debuginfo checksums.** Rejected because it lets callers
  compose a combination KDIVE never validated and omits build id, cmdline, and provenance.
- **Add `runs.reuse_build`.** Rejected because it adds a second post-create build transition and
  races System binding; create-time selection is atomic with admission and idempotency.
- **Widen `runs.install`.** Rejected because install should consume the Run's immutable build
  result, not mutate build identity while operating on a System.
- **Keep per-Run uploads.** Rejected because it does not satisfy upload-once reuse across Systems.
- **Remove the independent TTL.** Rejected because never-closed Investigations would accumulate
  sensitive multi-gigabyte builds without a bound, contrary to ADR-0234. The absolute deadline and
  explicit re-upload recovery make the retained limit agent-visible.

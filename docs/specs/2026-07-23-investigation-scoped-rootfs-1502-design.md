# Investigation-scoped agent-uploaded rootfs (#1502)

- **Issue:** [#1502](https://github.com/randomparity/kdive/issues/1502) — Re-scope agent-uploaded
  rootfs to investigation lifetime (reusable across Systems)
- **ADR:** [ADR-0441](../adr/0441-investigation-scoped-uploaded-rootfs.md) (supersedes ADR-0434 §1/§3/§4)
- **Status:** Draft (design-only; awaiting maintainer approval before implementation)
- **Date:** 2026-07-23

## Problem

An agent-uploaded rootfs (ADR-0434, #743) lives for **one System's lease**: committed
`owner_kind='systems'`, staged per-System, reclaimed only at that System's teardown. So (a) reusing a
custom debug rootfs across several Systems in one debugging effort re-uploads and re-stages the
multi-GiB image per System, and (b) a System that never cleanly tears down (a `failed` provision, #1501)
strands the SENSITIVE blob forever — `owner_kind='systems'` is exempt from every artifact-expiry
reconciler. The domain already scopes "reusable across Systems within one effort" as the
**Investigation**; build artifacts already reclaim on investigation close + grace (ADR-0234). An
uploaded rootfs is the same kind of investigation-scoped *input*.

## Goal

Re-scope the local-libvirt agent-uploaded rootfs from System-lease lifetime to **investigation
lifetime**: one upload, referenced by content checksum, provisions many Systems in the same
investigation; the host base is fetched at most once per host per (investigation, checksum); the object
and staged base are reclaimed on investigation close + grace, not per-System teardown; and
cross-investigation isolation is preserved (dedup is per investigation, not per host — isolation over
cross-investigation dedup).

## Non-goals

- **Kernel-build reuse across Systems.** Investigation ownership is built type-agnostic, but only rootfs
  is wired. Kernel reuse is an install-plane *reference* problem (kernel builds already reclaim on
  investigation close via `gc_investigation_artifacts`) — a **follow-up** that adopts this ownership.
- **Remote-libvirt parity (#1433/ADR-0440).** Remote's base is a libvirt volume, not an object-store
  row; investigation-scoping it is a separate design — a **follow-up**. Local-libvirt only here.
- **A shared cross-investigation base cache.** Reuse is within one investigation; the boundary is kept.
- **An eval plan.** No AI surface is added or changed (no LLM/prompt/retrieval/classifier).
- **A live-boot CI gate.** Download/stage/reclaim/close mechanics are unit/integration-tested; a local
  `live_vm` boot of a reused rootfs across two Systems is an optional manual proof (noted below).

## Design

See ADR-0441 for rationale; this section is the buildable shape and the failure contracts.

### Ownership and object model (ADR-0441 §1)

- Committed object: `owner_kind='investigations'`, `owner_id=<investigation_id>`, SENSITIVE,
  `retention_class='rootfs'`. Object key `artifact_key("local","investigations",<inv_id>,"rootfs-"<token>)`,
  where `<token>` is unpadded **base64url** of the declared SHA-256 (path/key-safe; the stored,
  DB-matched checksum stays canonical base64).
- `owner_kind` is unconstrained free text — **no schema constraint change** for the new kind.
- The new sweep is type-agnostic (`owner_id` *is* the investigation), but only rootfs is written at this
  kind. **No evidence** (console/vmcore/pcap/boot) is ever written `owner_kind='investigations'`.

### System↔Investigation binding (ADR-0441 §2)

- **Migration 0076:** `ALTER TABLE systems ADD COLUMN investigation_id uuid REFERENCES investigations(id)`,
  **nullable**. `domain/lifecycle/records.py` `System` gains `investigation_id: UUID | None = None`.
- `systems.define` / `systems.provision` gain optional `investigation_id`. Supplied ⇒ must name a
  non-terminal (OPEN/ACTIVE) investigation whose project **equals the System's own (Allocation)
  project** — cross-project binding is rejected (`configuration_error`). Same-project keeps the SENSITIVE
  base within one trust boundary and prevents the `close(force)` deadlock (a closer lacking the System's
  project role).
- **Invariant (admission-enforced):** a profile whose rootfs is `{"kind":"upload",...}` requires a bound
  `investigation_id`. An upload ref with no binding is a `configuration_error` naming the missing
  binding — never a late provision failure. Validated in `SystemAdmission` alongside the existing
  rootfs checks.

### Upload window + finalize (ADR-0441 §3)

New investigation `_UploadOwnerSpec` reusing `_create_upload` + `upload_manifests`:

1. `artifacts.create_investigation_upload(investigation_id, artifacts=[{name:"rootfs", sha256, size_bytes,
   encoding?, uncompressed_size?}])` — CONTRIBUTOR role on the investigation's project; accepts when the
   investigation is OPEN/ACTIVE; single-PUT only (ADR-0436), gzip transport-encoding accepted (ADR-0438/0439);
   replaces the `('investigations', inv_id)` manifest; audits the grant. Returns presigned PUT + the
   `#1336` deadline contract.
2. Agent PUTs the object (store verifies the signed `x-amz-checksum-sha256` at PUT).
3. `investigations.complete_rootfs_upload(investigation_id)` — finalize (symmetric with
   `runs.complete_build`): HEAD the object (`ChecksumMode=ENABLED`), reject a missing or checksum-less
   object (`configuration_error`), write the write-once `owner_kind='investigations'` `artifacts` row,
   delete the manifest, and return `data.checksum_sha256` — the handle for profiles. Idempotent: a second
   call with the row already present returns the same handle.

**Removed (replace, not deprecate):** `artifacts.create_system_upload`, `_SYSTEM_UPLOAD`,
`_commit_uploaded_rootfs`, `_system_accepts_upload`, `rootfs_upload_window_allowed`, and the
`systems.define` System-scoped upload window. `SYSTEM_ARTIFACT_NAMES` carried only `rootfs`.

### Reference + provision resolution (ADR-0441 §4/§5)

- `_UploadRootfs` = `{"kind":"upload", "checksum_sha256": <base64>}` (a required field now).
- `_materialize_uploaded_rootfs` resolves within the System's own investigation:
  `SELECT object_key FROM artifacts WHERE owner_kind='investigations' AND owner_id=<system.investigation_id>
  AND checksum_sha256=<ref>`; a miss ⇒ `configuration_error` naming the unresolved checksum.
- Stage to `rootfs-uploads/<investigation_id>/<token>.qcow2`, outside `allowed_roots`. Read-side SHA-256
  verify (ADR-0434 §2) and qcow2-magic gate (ADR-0438) unchanged. Present verified file reused → at most
  one download per host per (investigation, checksum). `.partial` + `os.replace` unchanged. The connectionless sync fetch
  opens its own short-lived connection to read the investigation's committed-object key + checksum (it no
  longer reads a per-System manifest; the row is durable post-finalize).

### Reclaim sweep (ADR-0441 §6)

**Two** sweeps in `reconciler/cleanup/gc.py`, mirroring ADR-0234's close-driven +
TTL-backstop pair (`gc_investigation_artifacts` + `gc_expired_build_artifacts`):

- **`gc_investigation_uploaded_rootfs` (close-driven)** — investigations with
  `cleanup_pending_at < now() - grace` (`KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`).
- **`gc_expired_investigation_rootfs` (TTL backstop)** — committed `owner_kind='investigations'`,
  `retention_class='rootfs'` objects older than `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS` on a
  never-closed investigation, so bases do not accumulate unbounded. (`retention_class` is only an
  S3-lifecycle label, not an enforced TTL — ADR-0234 — hence the reaper.)

Both reclaim **per checksum**, with the object + row + staged file on a **single liveness gate** —
*every System bound to the investigation that references that checksum is `torn_down`* (keys on
`torn_down`, **not** "terminal": only `torn_down` guarantees the overlay is gone; a `failed` System may
retain a live overlay and per ADR-0435 never runs teardown, so it is treated as **not drainable** —
deferred, never deleted-under). Gate holds → delete the object (best-effort) + row (fail-loud in txn)
**and** unlink `rootfs-uploads/<inv>/<token>.qcow2`. Gate fails → skip the checksum (`drained=False`,
retried next pass). Remove the empty `rootfs-uploads/<inv>/` dir once drained. Keeping object and file on
one gate means a reprovisioning / disk-fault-recovering System always finds the object present to
re-download.
- Both registered in the reconciler loop beside `gc_investigation_artifacts`.

**Removed:** the ADR-0434 §4 teardown rootfs reclaim (local file + S3 object + row) from the systems
teardown handler, **and** ADR-0435 §1's provision-failure reclaim of the (now shared) base — the
`uploaded_rootfs_exists`/`staged_pre` snapshot arm, which would delete a sibling's shared base on
failure. ADR-0435's baseline/overlay reclaim (per-System-private) stays. The upload-manifest reaper's
`systems` `{defined, failed}` arm (`reconciler/cleanup/uploads.py`) is **re-scoped to `investigations`**
(reap a stale investigation upload window's uncommitted object + manifest past its deadline).

### Investigation close coupling (ADR-0441 §7)

`close_investigation(force: bool = False)`:

- Enumerate bound live Systems: `systems WHERE investigation_id=<inv> AND state NOT IN (<terminal>)`.
- **Default (force=False):** any live ⇒ `configuration_error` listing their ids; refuse (state unchanged).
- **force=True:** for each bound live System, require the caller's role on that System's project (a
  System the caller cannot tear down ⇒ fail listing it), enqueue its teardown job, then close and set
  `cleanup_pending_at`.
- NULL-investigation Systems are never considered.

## Agent-facing flow (happy path)

```
investigations.open                          → inv
artifacts.create_investigation_upload(inv,[{name:"rootfs",sha256,size_bytes}])  → presigned PUT
  <agent PUTs the qcow2>
investigations.complete_rootfs_upload(inv)   → {checksum_sha256: C}
systems.define(alloc_A, profile{rootfs:{kind:"upload",checksum_sha256:C}}, investigation_id=inv)
systems.provision_defined(system_A)          → boots the base (downloaded once to host)
systems.define(alloc_B, profile{rootfs:{kind:"upload",checksum_sha256:C}}, investigation_id=inv)
systems.provision_defined(system_B)          → boots the SAME base (host cache hit, no re-download)
  ... debugging ...
investigations.close(inv)                    → ERROR: systems [A,B] still live
investigations.close(inv, force=True)        → teardown A,B enqueued; inv CLOSED
  ... grace elapses ...
reconciler gc_investigation_uploaded_rootfs  → object+row deleted; bases unlinked once A,B overlays gone
```

## Failure modes and contracts

| Scenario | Contract |
|---|---|
| Upload ref with no `investigation_id` binding | `configuration_error` at admission naming the missing binding |
| `checksum_sha256` not owned by the System's investigation | `configuration_error` at provision naming the unresolved checksum (isolation boundary) |
| Object missing / checksum-less at finalize | `configuration_error` (mirrors old `_commit_uploaded_rootfs`) |
| Downloaded bytes fail SHA-256 or qcow2-magic | `infrastructure_failure` / `configuration_error` (ADR-0434 §2, ADR-0438) unchanged |
| `close` with bound live Systems, no force | `configuration_error` listing them; investigation not closed |
| `close(force=True)`, a bound System the caller can't tear down | fail listing it; nothing torn down |
| Store fault deleting object in sweep | best-effort skip, retried next pass (marker stays set) |
| Base referenced by a bound System not yet `torn_down` (incl. a `failed` one) | object + row + file all deferred on one gate, retried next pass |
| Investigation never closed | TTL backstop `gc_expired_investigation_rootfs` reclaims past `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`, same gate |
| Provision of System A **fails** while sibling B reuses the same shared base | A's failure path does **not** unlink the shared base (ADR-0435 §1 arm superseded); B keeps a valid backing |
| Second `complete_rootfs_upload` (row already written) | idempotent; returns the same `checksum_sha256` |
| Cross-investigation read attempt (System in Y names Y-not-owned checksum) | resolution miss ⇒ `configuration_error`; no escape |

## Acceptance criteria (become tests in `/build-tdd`)

1. **Reuse:** two Systems bound to one investigation, same `checksum_sha256`, both provision; the host
   base file is written **once** (second provision is a cache hit — assert no second download).
2. **Isolation:** a System bound to investigation Y referencing a checksum owned only by investigation X
   fails resolution with `configuration_error`; no file under Y's staging dir is created.
3. **Binding invariant:** `{"kind":"upload"}` rootfs with no `investigation_id` is rejected at admission.
4. **Finalize:** `complete_rootfs_upload` writes the `owner_kind='investigations'` row, deletes the
   manifest, returns the checksum handle; is idempotent on re-call.
5. **Close-block:** `close` with a bound live System errors and lists it; the investigation stays open.
6. **Close-force:** `close(force=True)` enqueues teardown for each bound live System and closes.
7. **Sweep-reclaim:** past grace, the sweep deletes the object + row; asserts the row is gone.
8. **Liveness guard (`torn_down`-keyed):** the sweep does **not** unlink a base while a bound System
   referencing it is not `torn_down` — including a **`failed`** one (assert a `failed` referencer defers);
   it unlinks once every referencer is `torn_down`.
8b. **TTL backstop:** a never-closed investigation's committed rootfs object is reclaimed by
   `gc_expired_investigation_rootfs` past `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`, on the same gate.
9. **Shared-base failure safety:** with Systems A and B provisioning from the same checksum, a
   downstream failure in A's provision does **not** unlink the shared base (ADR-0435 §1 arm superseded);
   B's overlay keeps a valid backing. (The negative test must fail against the un-superseded arm.)
10. **#1501 residual closed:** a `ready`-then-stranded System's committed object is collected by the
    sweep, and a stale investigation upload window's uncommitted object is reaped by the re-scoped
    manifest reaper. (The *failed-provision* orphan itself is ADR-0435's, unchanged here.)
11. **Surface:** `artifacts.create_system_upload` is gone from the tool index/exposure/RBAC matrix;
    `artifacts.create_investigation_upload` + `investigations.complete_rootfs_upload` are present with the
    CONTRIBUTOR gate; migration 0076 round-trips.

## Rollout / rollback

- **Forward-only migration 0076** (add nullable column); rollback is a drop-column (no data loss for
  NULL-investigation Systems).
- Because this **removes** `create_system_upload` and changes the profile ref shape, it is a breaking
  change to any in-flight `{"kind":"upload"}` System — acceptable pre-1.0 and consistent with
  "replace, not deprecate." No dual-format shim.
- **No backfill (pre-1.0):** migration 0076 does not re-own pre-existing `owner_kind='systems'` rootfs
  objects, and the new sweep only sees `owner_kind='investigations'`. Removing the teardown reclaim would
  strand any legacy systems-owned rootfs object — safe only because no deployed instance holds one
  (greenfield, fresh DBs). If that ever fails, a one-time reaper of legacy systems-owned rootfs objects is
  a prerequisite.

## Open items for the plan

- Exact tool namespace for finalize (`investigations.complete_rootfs_upload` vs `artifacts.*`) — pick in
  the plan; keep create under `artifacts.*` to reuse `_create_upload`.
- The reconciler's filesystem access to `UPLOADS_DIR` (co-located host for local-libvirt) — assert in the
  plan that the sweep's file arm is a local-libvirt-host concern, matching where the base is staged.

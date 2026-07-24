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

- **Migration 0076:** `ALTER TABLE systems ADD COLUMN investigation_id uuid REFERENCES investigations(id)`
  (**nullable**); `ALTER TABLE investigations ADD COLUMN rootfs_cleanup_pending_at timestamptz` (the
  dedicated rootfs reclaim marker); a partial `CREATE UNIQUE INDEX … ON artifacts (object_key) WHERE
  owner_kind='investigations'` (finalize idempotency; partial so it never touches other owner kinds); and
  nullable `artifacts.encoding text` + `artifacts.uncompressed_size bigint` (the durable transport-encoding
  home, decision 3). `domain/lifecycle/records.py` `System` gains `investigation_id: UUID | None = None`;
  `Investigation` gains `rootfs_cleanup_pending_at`.
- `systems.define` / `systems.provision` gain optional `investigation_id`. Supplied ⇒ must name a
  non-terminal (OPEN/ACTIVE) investigation whose project **equals the System's own (Allocation)
  project** — cross-project binding is rejected (`configuration_error`). Same-project keeps the SENSITIVE
  base within one trust boundary and prevents the `close(force)` deadlock (a closer lacking the System's
  project role).
- **Write-once (NULL case explicit):** if `define` recorded **non-NULL**, `provision` must supply the same
  value or omit it (a differing value is rejected); if `define` recorded **NULL**, first `provision` may
  set it once, then it is immutable. An upload-ref profile forces non-NULL at define (admission invariant),
  so only non-upload Systems reach the NULL-at-define branch. The reclaim gate enumerates referencers by
  this column, so a mutable binding could drop a still-backing System and reclaim the base under its live
  overlay. Enforced at admission.
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
   `runs.complete_build`), run **under the investigation lock** and requiring the investigation
   **OPEN/ACTIVE** (serializes with `close_investigation`, so a finalize can't land a committed row on a
   just-closed investigation that neither sweep collects; a lost race → `configuration_error`, manifest
   left for the re-scoped reaper; a won race commits while OPEN and a later close sets
   `rootfs_cleanup_pending_at`): HEAD the object (`ChecksumMode=ENABLED`), reject a missing or checksum-less
   object (`configuration_error`), **assert the HEAD checksum == the declared checksum the `object_key` was
   minted from** (mismatch → `configuration_error` now, not a silent resolution miss at every provision),
   write the `owner_kind='investigations'` `artifacts` row via **`INSERT …
   ON CONFLICT DO NOTHING`** against the migration-0076 **partial UNIQUE index** on
   `artifacts(object_key) WHERE owner_kind='investigations'`, then re-SELECT and return
   `data.checksum_sha256` — the handle for profiles. The unique index makes two concurrent finalizes
   (retries/two agents) converge on **one** row (no duplicate rows for one content-addressed key);
   idempotent by construction, and resolution expects exactly one row. **The row also persists
   `encoding` + `uncompressed_size`** (copied from the manifest entry) — ADR-0438 keeps those only in the
   ephemeral manifest, which finalize deletes, so a post-finalize gzip provision would otherwise lose them.
   Delete the manifest.

**Removed (replace, not deprecate):** `artifacts.create_system_upload`, `_SYSTEM_UPLOAD`,
`_commit_uploaded_rootfs`, `_system_accepts_upload`, `rootfs_upload_window_allowed`, and the
`systems.define` System-scoped upload window. `SYSTEM_ARTIFACT_NAMES` carried only `rootfs`.
**Re-homed, not lost:** ADR-0439's transport-encoding advertisement — the `accepts_encoding=True` per-owner
declaration-item schema plus the `json_schema_extra`/`Field`/docstring/worked gzip example that lived on
`create_system_upload` — moves onto `create_investigation_upload` unchanged in shape, so the >5 GiB
gzip-rootfs surface does not silently regress to an unadvertised field.

### Reference + provision resolution (ADR-0441 §4/§5)

- `_UploadRootfs` = `{"kind":"upload", "checksum_sha256": <base64>}` (a required field now).
- **Resolution is content-addressed via `object_key`** — the `artifacts` table has **no checksum column**
  (and 0076 adds none). `_materialize_uploaded_rootfs` transcodes the ref's canonical-base64
  `checksum_sha256` → the unpadded-base64url `<token>`, builds the expected key
  `artifact_key("local","investigations",<inv>,"rootfs-"<token>)`, and looks up
  `SELECT object_key FROM artifacts WHERE owner_kind='investigations' AND owner_id=<system.investigation_id>
  AND object_key=<derived>`; the `owner_id` predicate is the isolation boundary, the derived-key match is
  the content address. A miss ⇒ `configuration_error` naming the unresolved checksum. The base64↔base64url
  transcoding is the single canonical, reversible mapping.
- Stage to `rootfs-uploads/<investigation_id>/<token>.qcow2`, outside `allowed_roots`. Read-side SHA-256
  verify (ADR-0434 §2) and qcow2-magic gate (ADR-0438) unchanged. Present verified file reused → at most
  one download per host per (investigation, checksum).
- **Concurrent-fetch guards (layered — correctness never depends on the lock):**
  - **Unique per-fetcher `.partial`** (`<token>.<uuid>.partial`, `os.replace`d after verify) so two
    concurrent downloaders never share a partial — the correctness guarantee against corruption, lock or no.
    The fetcher unlinks its own `.partial` in a `finally` on failure. A **crash-orphaned** `.partial` (a
    killed worker) is collected on two paths: **opportunistically** by the next fetcher (which, under the
    serializing lock, glob-unlinks other `<token>.*.partial` — no live sibling can exist), and as a
    **backstop** by the reclaim sweep (glob before empty-dir removal). Bounded by the next fetch, not only
    by full investigation reclaim.
  - **Deterministic session-scoped advisory lock** to avoid the redundant multi-GiB download ("written
    once"): `pg_advisory_lock` on the fetch's dedicated sync connection, held across check-and-download,
    keyed via the **session** keyspace helper `db.locks._session_lock_key(f"rootfs-fetch:{inv}:{token}")`
    (salted apart from the `_lock_key` xact keyspace so a long session lock never collides with a per-object
    xact op) — **not** `_lock_key` (xact keyspace) and **not** Python `hash()` (per-process salted →
    different keys across worker processes → silent no-op). Session-scoped (not `advisory_xact_lock`, which
    holds a txn open across the download → idle-in-transaction timeout). Re-check for the verified `dest`
    after acquiring (cache hit while waiting).
- The fetch reads `encoding`/`uncompressed_size` from the **durable `artifacts` row** (not the deleted
  manifest) and strips gzip per ADR-0438. It opens its own short-lived sync connection to resolve the
  committed-object key + encoding.

### Reclaim sweep (ADR-0441 §6)

**Two** sweeps in `reconciler/cleanup/gc.py`, mirroring ADR-0234's close-driven +
TTL-backstop pair (`gc_investigation_artifacts` + `gc_expired_build_artifacts`):

- **`gc_investigation_uploaded_rootfs` (close-driven)** — investigations with
  `rootfs_cleanup_pending_at < now() - grace` (`KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`). This is a
  **dedicated** marker (migration 0076), **not** the shared `cleanup_pending_at`: `gc_investigation_artifacts`
  nulls `cleanup_pending_at` the moment *its own* (`owner_kind='runs'`) worklist drains — immediately for
  an investigation with no build artifacts — which would drop the rootfs from the close-driven worklist
  before its overlays drain. Close sets both markers; the rootfs sweep clears only its own.
- **`gc_expired_investigation_rootfs` (TTL backstop)** — committed `owner_kind='investigations'`,
  `retention_class='rootfs'` objects older than `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS` on a
  never-closed investigation, so bases do not accumulate unbounded. (`retention_class` is only an
  S3-lifecycle label, not an enforced TTL — ADR-0234 — hence the reaper.)

Both reclaim **per checksum** on a gate with **two conditions**, both required for every bound System
referencing the checksum: (a) its per-System overlay file is **absent** on the host (backing hazard — a
filesystem probe, **not** a `systems.state` read), and (b) it is **not** in a pre-overlay or
re-materializing non-terminal state — `defined`, `provisioning`, `reprovisioning`, `restoring` — each of
which can read/re-create against the base with the overlay momentarily absent (ADR-0435's
`provisioning`-exclusion, generalized; matters mainly for the TTL backstop, since default close already
blocks on non-terminal states). Overlay-file absence is the backing safety condition; keying *it* on state
would let a `failed` System — terminal, never reaching `torn_down`, excluded from `repair_orphaned_systems`
— pin the base forever and defeat the TTL backstop. Condition (b) reads only the listed non-terminal
states, never terminal-ness, so it does not reintroduce the pin. Gate holds → reclaim in **pinned order**:
delete the object, **unlink** `rootfs-uploads/<inv>/<token>.qcow2`, and **only then** delete the row
(fail-loud in txn). Object-delete and unlink share one fault contract: an already-absent target (S3 404 /
`ENOENT`) is **success**, but any **real** fault (non-404 store error, real unlink error) **defers the
whole checksum** (`drained=False`, row kept) *before* the row delete — so "best-effort" means "404-tolerant",
**not** "continue past a fault and drop the row while the SENSITIVE object survives" (which would orphan it
beyond any reaper, the row being the only handle). The row is the worklist anchor, removed **last**;
idempotency + defer-on-fault make every partial failure (e.g. unlink succeeds then row-delete rolls back)
converge next pass rather than wedging. Gate fails → skip the
checksum (`drained=False`, retried). Remove the empty `rootfs-uploads/<inv>/` dir once drained.
- **Referencer enumeration (per checksum X):** `systems WHERE investigation_id=<inv> AND state<>'torn_down'`,
  parse each `provisioning_profile` rootfs ref, keep only `{"kind":"upload","checksum_sha256":X}`; an
  unparseable / no-rootfs / catalog / local / different-checksum profile is **not** a referencer of X (so
  one unrelated live System never pins X). For each real referencer, derive `overlay_path(system_id)` =
  `<ROOTFS_DIR>/<id>-overlay.qcow2` and stat it for condition (a).
- **Reconciler filesystem access (per-pass, fail-closed):** the probe reads overlays under `ROOTFS_DIR`
  and unlinks bases under `UPLOADS_DIR`. The reconciler **already** does host-FS access (`console_rotation`
  `os.stat`s console logs and tolerates non-co-location per-pass), so the rootfs sweeps reuse that model —
  **no startup registration gate**. Each pass **fails closed**: a missing overlay under a *present*
  `ROOTFS_DIR` is "overlay gone" (reclaimable), but an *absent/inaccessible* `ROOTFS_DIR` **defers the whole
  pass** (retried next pass — never "all overlays gone"), so a host-blind reconciler reclaims nothing rather
  than unlinking under live guests, and a host that gains the dirs later reclaims without a restart. The
  reconciler **ensure-creates** the (lazily-created) dirs at startup so a fresh, not-yet-provisioned host
  still reclaims. Local-libvirt is host-local (reclaim runs); a split topology defers until remote reclaim
  is designed (decision 8).
- **Condition-(b) drift guard:** the pre-overlay/re-materialize state set is a **named constant** beside
  the `SystemState` definition, with a **structural CI test** that fails when a new non-terminal state is
  added without being classified — so a future base-re-materializing state can't silently escape the gate.
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
- **force=True (all-or-nothing):** requires **admin on the bound Systems' project**. All bound Systems
  share the investigation's project (the same-project invariant), so this is a **single per-project admin
  check** (no per-System variance); it either passes or fails wholesale. The all-or-nothing order matters
  for the **enqueue step**: validate admin, then enqueue *all* teardowns and close in one txn (set **both**
  markers, `cleanup_pending_at` + `rootfs_cleanup_pending_at`), so an **enqueue error** on one of several
  teardowns aborts with **zero** enqueued and the investigation unchanged — never a partial reap. `force`
  is `_ADMIN` (matching `systems.teardown`), so it is not a contributor→admin escalation via close; a
  same-project *contributor* can neither default-close (live System blocks) nor force-close (needs admin)
  — a deliberate authz cost. Any successful close (default or force) sets both markers.
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
| HEAD checksum ≠ declared checksum at finalize | `configuration_error` now, not a silent resolution miss at every provision |
| Finalize races an investigation close | under the investigation lock, requires OPEN/ACTIVE — lost race rejects (manifest reaped), won race commits while OPEN + close marks it for reclaim |
| Crashed download leaves a `.partial` | `finally` unlinks on clean failure; a crash-orphan is glob-unlinked by the **next fetcher** (under the lock) and, as backstop, by the reclaim sweep — bounded by the next fetch, not the investigation's full life |
| Non-404 store fault deleting the object in the sweep | defers the whole checksum (row kept) **before** the row delete — the object is never orphaned without its handle |
| Reconciler not co-located / `ROOTFS_DIR` inaccessible | probe **fails closed per-pass** (defers, retries next pass) — reclaims nothing, never unlinks under a live guest; recovers when the dir becomes accessible |
| Downloaded bytes fail SHA-256 or qcow2-magic | `infrastructure_failure` / `configuration_error` (ADR-0434 §2, ADR-0438) unchanged |
| `close` with bound live Systems, no force | `configuration_error` listing them; investigation not closed |
| `close(force=True)`, caller lacks admin on one of several bound Systems | **all-or-nothing**: validate all admin first → zero teardowns enqueued, investigation unchanged, offending System listed |
| TTL backstop vs a `defined`/`provisioning` bound System (no overlay yet) | deferred by gate condition (b) — the base is not reclaimed under a pre-overlay referencer |
| Store fault deleting object in sweep | best-effort skip, retried next pass (marker stays set) |
| Base still backed by a referencing System's overlay file on the host | object + row + file all deferred on one gate, retried next pass |
| `failed` bound System referencing the base | deferred **only while its overlay remains**; drains once ADR-0435 reclaims the overlay (a state gate would pin it forever) |
| Bound System `reprovisioning`/`restoring` (overlay momentarily absent) | gate condition (b) defers — the base is not reclaimed mid-re-materialize |
| Base-file `unlink` fails after object delete | pinned order defers the whole checksum (`drained=False`); the row is kept as the retry anchor, never orphaning the file |
| Unlink succeeds, then row-delete rolls back | idempotent object-delete + unlink (404/`ENOENT`=success) → next pass converges to the row delete, no permanent wedge |
| Build-artifact sweep nulls `cleanup_pending_at` before rootfs overlays drain | rootfs sweep keys on its **own** `rootfs_cleanup_pending_at`, unaffected — close+grace reclaim still fires |
| System bound with a checksum after the TTL sweep enumerated (never-closed inv) | fail-closed: base may be reclaimed → later provision fails resolution (`configuration_error`, re-upload); no corruption |
| Investigation never closed | TTL backstop `gc_expired_investigation_rootfs` reclaims past `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`, same overlay-absence gate |
| Provision of System A **fails** while sibling B reuses the same shared base | A's failure path does **not** unlink the shared base (ADR-0435 §1 arm superseded); B keeps a valid backing |
| Second `complete_rootfs_upload` (row already written) | idempotent; returns the same `checksum_sha256` |
| Gzip investigation upload provisioned after finalize | encoding read from the durable row (persisted at finalize) → gunzipped → passes qcow2-magic gate |
| Two worker **processes** fetch the same base concurrently | deterministic `_lock_key` serializes (not `hash()`); unique `.partial` guarantees no corruption even if the lock is lost |
| Cross-investigation read attempt (System in Y names Y-not-owned checksum) | resolution miss ⇒ `configuration_error`; no escape |

## Acceptance criteria (become tests in `/build-tdd`)

1. **Reuse (incl. cross-process concurrent):** two Systems bound to one investigation, same
   `checksum_sha256`, both provision — sequentially **and concurrently across two processes** — with the
   host base written **once** (deterministic `_lock_key` serializes; assert no second download and no
   `.partial` corruption; the cross-process case must fail against a `hash()`-keyed lock).
2. **Isolation:** a System bound to investigation Y referencing a checksum owned only by investigation X
   fails resolution with `configuration_error`; no file under Y's staging dir is created.
3. **Binding invariant:** `{"kind":"upload"}` rootfs with no `investigation_id` is rejected at admission.
4. **Finalize:** `complete_rootfs_upload` writes the `owner_kind='investigations'` row, deletes the
   manifest, returns the checksum handle; idempotent on re-call **and under two concurrent calls** (the
   partial UNIQUE index converges them on one row — assert exactly one row).
4b. **Resolution by object_key:** a profile ref resolves by the derived content-addressed `object_key`
   (no checksum column); the base64→base64url transcoding round-trips.
4c. **Gzip after finalize:** an `encoding=gzip` investigation upload, finalized (manifest deleted), then
   provisioned, is gunzipped from the row-persisted encoding and passes the qcow2-magic gate (asserts the
   base is a valid qcow2, not verbatim gzip bytes).
4d. **Finalize vs close:** PUT + `close(force)` + `complete_rootfs_upload` — the finalize either rejects
   (investigation terminal → manifest reaped) or, if it committed first, the object/row is reclaimed by
   the close-driven sweep; **no orphaned SENSITIVE object** survives either ordering.
5. **Close-block:** `close` with a bound live System errors and lists it; the investigation stays open.
6. **Close-force (atomic):** `close(force=True)` with **admin** on the (single, shared) bound-Systems'
   project enqueues teardown and closes; a same-project **contributor** is refused (no escalation, admin
   fails wholesale). For the atomicity path, inject an **enqueue error** on one of several teardowns and
   assert **zero** teardowns are enqueued and the investigation stays open (all-or-nothing — no partial reap).
6b. **Default-close marker:** the tidy path — tear down each bound System individually, then **default**
   `close` (no force) — sets `rootfs_cleanup_pending_at` in the shared `_close_locked` path, and the
   close-driven sweep subsequently reclaims (not only the force path; else the object waits for the slow TTL).
7. **Sweep-reclaim:** past grace, the sweep deletes the object + row; asserts the row is gone.
8. **Liveness guard (overlay-absence):** the sweep does **not** reclaim a base while a referencing
   System's overlay file is present; it reclaims once the overlay is gone. Critically, a **`failed`**
   referencer whose overlay has been reclaimed **must eventually drain** (assert drainage — a state-keyed
   gate that defers a `failed` System forever is the regression this test forbids).
8b. **TTL backstop:** a never-closed investigation's committed rootfs object is reclaimed by
   `gc_expired_investigation_rootfs` past `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`, on the same gate
   (so it is not defeated by a stuck `failed` System).
8c. **Pre-overlay / re-materialize guard:** the TTL backstop does **not** reclaim a base while a bound
   System referencing its checksum is `defined`/`provisioning`/`reprovisioning`/`restoring` (gate
   condition (b)); it reclaims once that System is terminal-with-overlay-gone.
8d. **Marker independence:** an investigation with a drained build artifact **and** a still-overlay-backed
   rootfs base still reclaims the rootfs close-driven after the overlay drains — i.e. `gc_investigation_artifacts`
   nulling `cleanup_pending_at` does not starve the rootfs sweep (which keys on `rootfs_cleanup_pending_at`).
8e. **Pinned-order unlink failure:** with the object deleted, a forced `unlink` failure leaves the
   `artifacts` row intact and the checksum retried next pass (no orphaned file with no row).
8f. **Enumeration precision:** a bound live System referencing a *different* checksum (or a `catalog`
   rootfs) does **not** pin checksum X's base — X reclaims while that unrelated System stays live.
8g. **Idempotent reconverge:** with the object deleted and file unlinked, a forced row-delete failure
   re-runs to a clean drain (missing object/file counted as success, not a defer — no permanent wedge). A
   non-404 object-store fault instead **defers** (row kept), retried next pass.
8h. **Crashed-download `.partial`:** a stale `<token>.*.partial` left by a killed fetch is swept
   (glob-unlinked) before empty-dir removal — not stranded.
8i. **Fail-closed per-pass probe:** a sweep pass with an inaccessible `ROOTFS_DIR` reclaims **nothing**
   (defers), never treating a missing root as "all overlays absent"; a later pass, once the dir is
   accessible, reclaims (no restart needed); the reconciler ensure-creates the dirs at startup.
9. **Shared-base failure safety:** with Systems A and B provisioning from the same checksum, a
   downstream failure in A's provision does **not** unlink the shared base (ADR-0435 §1 arm superseded);
   B's overlay keeps a valid backing. (The negative test must fail against the un-superseded arm.)
10. **#1501 residual closed:** a `ready`-then-stranded System's committed object is collected by the
    sweep, and a stale investigation upload window's uncommitted object is reaped by the re-scoped
    manifest reaper. (The *failed-provision* orphan itself is ADR-0435's, unchanged here.)
11. **Surface:** `artifacts.create_system_upload` is gone from the tool index/exposure/RBAC matrix;
    `artifacts.create_investigation_upload` (advertising the re-homed ADR-0439 `accepts_encoding=True`
    declaration schema + encoding `Field`/example) + `investigations.complete_rootfs_upload` are present
    with the CONTRIBUTOR gate; migration 0076 round-trips.

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
- Reconciler filesystem access (now specified in the Reclaim sweep section): the probe reads overlays
  under `ROOTFS_DIR` and unlinks bases under `UPLOADS_DIR`, both local-libvirt-host-only — the plan must
  assert the reconciler runs co-located with that host.

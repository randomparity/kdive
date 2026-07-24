# ADR 0441 — Investigation-scoped agent-uploaded rootfs: reusable across Systems, reclaimed on investigation close

- **Status:** Proposed
- **Date:** 2026-07-23
- **Supersedes:** [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) decisions §1
  (System-owned object + provision-time download by System key), §3 (per-System staging), and §4
  (lease-scoped teardown reclaim); the System-scoped rootfs *upload window* of
  [ADR-0048](0048-external-build-artifact-ingestion.md) §5/§6 as it applies to rootfs; and
  [ADR-0435](0435-reclaim-failed-provision-artifacts.md)'s uploaded-rootfs arms — its provision-failure
  reclaim of the staged base (§1, the `uploaded_rootfs_exists`/`staged_pre` snapshot) and its
  `systems`-owner manifest-reaper relaxation (§, the `{defined, failed}` gate) — both of which reason
  about a *per-System-private* base that no longer exists (decisions 5–6 below). ADR-0434 §2 (read-side
  checksum verify), the [ADR-0438](0438-rootfs-transport-strip-streaming-fetch.md) qcow2-magic gate, §5
  (`upload` outside `accepted_component_sources`), and §6 (dead-guard removal) are **retained**;
  ADR-0435's baseline/overlay provision-failure reclaim (per-System-private) is **retained**.
- **Depends on:** [ADR-0234](0234-external-build-default-and-contributor-role.md) (the
  investigation-close + grace reclaim model `gc_investigation_artifacts` this generalizes),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the agent→S3 upload transport and the
  `_UploadOwnerSpec`/`upload_manifests` machinery this re-scopes to a new owner),
  [ADR-0060](0060-per-system-rootfs-overlay.md) (the per-System overlay whose backing file the
  staged base is), [ADR-0017](0017-object-store-client-interface.md) (the object store).
- **Spec:** [`../specs/2026-07-23-investigation-scoped-rootfs-1502-design.md`](../specs/2026-07-23-investigation-scoped-rootfs-1502-design.md)

## Context

ADR-0434 (#743) made a `{"kind": "upload"}` local System boot an agent-uploaded qcow2, but scoped
the whole lifecycle to **one System's lease**: the object is committed `owner_kind='systems'`, the
base is staged per-System, and both are reclaimed only at that System's teardown. That has two
costs #1502 targets:

- **No reuse.** A custom debug rootfs an investigation wants to boot on several Systems (reproduce
  across kernels/arches, re-provision after a crash) must be **re-uploaded and re-staged per
  System**, paying the multi-GiB upload + download each time.
- **Fragile reclaim.** Reclaim hinges on per-System teardown. ADR-0435 (#1501) already closed the
  *failed-provision* leak — it reclaims the staged base and the uncommitted object when `provision()`
  raises, and relaxed the manifest reaper to collect a `failed` upload System. What that does **not**
  reach is a System that reaches `ready` and is then stranded without a clean teardown: its **committed**
  `owner_kind='systems'` object is exempt from every artifact-expiry reconciler, so nothing collects it.
  More fundamentally, the whole reclaim model is teardown-shaped, which is the wrong shape once one
  object is meant to outlive any single System.

The domain already has the right scope for "reusable across Systems within one debugging effort":
**Investigation → Runs → Systems**. Build artifacts are already `owner_kind='runs'` and reclaimed on
investigation close + grace via `gc_investigation_artifacts` (ADR-0234). An uploaded rootfs is
conceptually the same kind of investigation-scoped *input*.

Three facts shape the decision. (1) The `artifacts.owner_kind` column is **unconstrained free
text** (`0001_init.sql`), so a new owner kind needs no schema constraint. (2) A **System has no
investigation link** today — it is created against an Allocation, and the only System↔Investigation
path is `runs(investigation_id, system_id)`, bound possibly *after* provision. (3) A per-System
overlay keeps its **base file open as the qcow2 backing file for the guest's whole lifetime**
(ADR-0060), so a base cannot be deleted while any live overlay backs onto it — and `close_investigation`
today does **not** tear down a bound System, so a System can outlive its investigation's close.

## Decision

### 1. Own the uploaded rootfs at investigation scope, content-addressed

The committed object is written `owner_kind='investigations'`, `owner_id=<investigation_id>`, with a
**content-addressed** object key `artifact_key("local", "investigations", <inv_id>, "rootfs-"<token>)`
where `<token>` is a path/key-safe rendering (base64url, no padding) of the declared SHA-256. Content
addressing lets one investigation hold **more than one** base (multi-arch reproduce) and keys the
per-host cache by checksum. The object is SENSITIVE, exactly as before.

The **ownership and reclaim *timing*** are artifact-type-agnostic — `owner_id` *is* the investigation, so
close+grace and the TTL backstop (decision 6) apply to anything owned at investigation scope with no
`artifacts→runs→investigations` join. The **liveness *gate*** in decision 6, by contrast, is
rootfs-specific: it exists only because a rootfs base is a live qcow2 backing file. Only **rootfs** is
wired in this change; reusing a *kernel build* across Systems is a separate **install-plane reference**
problem (kernel builds are already investigation-lifetime for reclaim via `gc_investigation_artifacts`,
and an installed kernel is not a live backing file, so a future adoption would reclaim on close+grace/TTL
*without* the overlay-liveness gate — it must supply its own predicate or none, not inherit rootfs's).
That follow-up adopts the ownership and timing, not the rootfs gate. **Evidence is never written at investigation scope** — console/vmcore/pcap/boot
stay System/Run-owned — so ADR-0234's "never reclaim crash evidence" constraint holds structurally:
the new sweep only ever sees `owner_kind='investigations'`, and no evidence is written there.

### 2. Bind a System to an investigation with a nullable `systems.investigation_id`

Migration **0076** adds `investigation_id uuid REFERENCES investigations(id)` to `systems`, **nullable**
— a classic allocation-only System keeps it NULL and is unaffected by everything below. `systems.define`
and `systems.provision` gain an **optional** `investigation_id`; an agent working in an investigation
sets it. This is an *advisory* binding (a System belongs to an Allocation for capacity; the
investigation link scopes the uploaded-rootfs trust boundary and the close coupling), not a second
capacity owner.

Binding is validated at define/provision: a supplied `investigation_id` must name an investigation in a
non-terminal state (OPEN/ACTIVE) whose project **equals the System's own (Allocation) project** — a
cross-project binding is rejected at admission. This same-project rule does double duty: it keeps a
SENSITIVE, investigation-owned base within one project's trust boundary (no P-inv object backing a
P-sys guest), and it forecloses the `close(force)` deadlock decision 7 would otherwise admit (a closer
holding the investigation's project but not the System's could neither close nor force-close). A
profile that references an uploaded rootfs (decision 4) **requires** a bound `investigation_id`; the two
are validated together at admission (a `{"kind":"upload"}` rootfs with no investigation binding is a
`configuration_error` naming the missing binding, never a late provision failure).

`systems.investigation_id` is **write-once**: set at define (or first provision) and immutable thereafter
— `provision`'s optional value must equal what `define` recorded, and there is no rebind. The decision-6
gate enumerates the Systems that reference a base by this column, so a mutable binding could silently drop
a System that still backs the base out of the gate's enumeration and let the base be reclaimed under its
live overlay. The invariant is stated here and enforced at admission because the gate's correctness
depends on it, not on the mere absence of a rebind tool today.

### 3. The upload window opens against the investigation, with an explicit finalize

The rootfs upload is decoupled from any System. A new `_UploadOwnerSpec` for
`owner_kind='investigations'` drives two tools that reuse the ADR-0048 `_create_upload` machinery and
`upload_manifests` table (owner `('investigations', inv_id)`):

- **`artifacts.create_investigation_upload(investigation_id, [decl])`** — accepts when the investigation
  is OPEN/ACTIVE and the caller holds its project's CONTRIBUTOR role; mints the presigned single-PUT
  (chunking rejected, ADR-0436; gzip transport-encoding accepted and stripped, ADR-0438/0439), replaces
  the investigation's manifest, and audits the grant.
- **`investigations.complete_rootfs_upload(investigation_id)`** — the explicit finalize (symmetric with
  `runs.complete_build`): HEADs the object for its stored checksum, verifies it is present and
  checksum-bearing, writes the write-once `owner_kind='investigations'` `artifacts` row, deletes the
  manifest, and returns the **`checksum_sha256` handle** the agent puts in each System's profile. This
  replaces the provision-time `_commit_uploaded_rootfs`: the row now exists **before** any System
  provisions, because multiple Systems reference it.

The System-scoped upload path is **removed, not deprecated** (`artifacts.create_system_upload`, the
`_SYSTEM_UPLOAD` spec, `_commit_uploaded_rootfs`, `_system_accepts_upload`, the
`rootfs_upload_window_allowed` policy hook, and the `systems.define` upload window). `SYSTEM_ARTIFACT_NAMES`
accepted only `rootfs`, so nothing else rode that lane.

### 4. Reference by checksum; resolve only within the System's own investigation

`_UploadRootfs` becomes `{"kind": "upload", "checksum_sha256": <base64>}`. At provision,
`_materialize_uploaded_rootfs` resolves the object with a lookup pinned to the System's own
investigation:

```
SELECT object_key FROM artifacts
 WHERE owner_kind = 'investigations'
   AND owner_id   = <system.investigation_id>
   AND checksum_sha256 = <ref.checksum_sha256>
```

A System in investigation *Y* can therefore only name a base **its own investigation owns** — the
cross-investigation no-escape boundary is enforced at the SQL predicate, not by directory layout alone.
A miss (no such object in this investigation) fails fast with `configuration_error` naming the
unresolved checksum, exactly as the old missing-object guard did.

### 5. Per-investigation content-addressed staging, outside `allowed_roots`

The fetch stages to `rootfs-uploads/<investigation_id>/<token>.qcow2`, still **outside**
`allowed_roots` (ADR-0434 §3 no-escape, now at investigation granularity: a staged image is never a
`local` staged-path candidate for any System). The verify (ADR-0434 §2 read-side SHA-256) and the
ADR-0438 qcow2-magic gate are unchanged. A present verified file is **reused**, so the base is
downloaded **at most once per host per (investigation, checksum)** and shared by every System in the
investigation — the reuse #1502 asks for. Dedup is deliberately scoped to the investigation, not the
host: two investigations on one host that upload identical bytes each stage their own copy — isolation
(decision 4) is chosen over cross-investigation dedup. `.partial` + `os.replace` atomicity is unchanged.

Because the base is now **shared and investigation-owned**, it is no longer "created by" the provision
that happened to stage it: an individual provision's failure path (ADR-0435 §1) must **not** reclaim it,
or a failing System A would delete the base a concurrently-provisioning or already-booted sibling B
reuses. ADR-0435's `uploaded_rootfs_exists`/`staged_pre` snapshot arm is therefore superseded — the
shared base is reclaimed **only** by the decision-6 sweep, under the liveness guard. ADR-0435's
baseline/overlay reclaim (those artifacts stay per-System-private) is unchanged.

### 6. Reclaim on investigation close + grace, with a TTL backstop, via a liveness-gated sweep

Reclaim moves from per-System teardown to **two** reconciler sweeps, mirroring the pair ADR-0234
deliberately used (`gc_investigation_artifacts` **and** its TTL backstop `gc_expired_build_artifacts`).
Adopting only the close-driven half would let a never-closed investigation accumulate SENSITIVE rootfs
objects forever — a regression against ADR-0434, whose teardown reclaim fired regardless of close — so
the TTL backstop is mandatory here too:

- **`gc_investigation_uploaded_rootfs` (close-driven)** — investigations whose `cleanup_pending_at` is
  older than `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`.
- **`gc_expired_investigation_rootfs` (TTL backstop)** — committed `owner_kind='investigations'`,
  `retention_class='rootfs'` objects older than `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS` whose
  investigation was **never** closed. (`retention_class` is only an S3-lifecycle label, not an enforced
  expiry — ADR-0234 Context — so the reaper enforces the wall.)

Both are deferred, audited, drain-and-retry, and reclaim **per checksum**, with the object + row +
staged file for one checksum reclaimed **together on the same liveness gate**: *no bound System that
references that checksum still has its per-System overlay present on the host* — a filesystem probe of
each referencing System's overlay path, **not** a `systems.state` read. Gate holds → delete the object
(best-effort) + row (fail-loud-in-txn, ADR-0234/0434) **and** unlink the staged base under
`rootfs-uploads/<inv>/`. Gate fails → skip the whole checksum this pass (`drained=False`, retried next
pass); remove the empty `rootfs-uploads/<inv>/` dir once drained.

The gate keys on **overlay-file absence, not System state**, because the overlay file *is* the exact
thing that holds the base open as a qcow2 backing file (ADR-0060) — so its absence is the precise,
sufficient safety condition, and no state value is a faithful proxy for it. In particular a `failed`
System is terminal but per ADR-0435 can never run teardown (`failed -> torn_down` is not a legal edge)
and is excluded from the reconciler's orphan teardown (`repair_orphaned_systems` treats `failed` as
terminal), so its row persists indefinitely; keying on state would let a single `failed` System pin the
SENSITIVE base **forever**, defeating even the TTL backstop. Keying on overlay-absence instead lets the
base drain the moment ADR-0435's provision-failure reclaim removes the `failed` System's overlay,
regardless of its stuck state — while still deferring safely whenever any overlay actually remains (the
genuinely stuck case an operator must resolve, surfaced as an un-drained marker). The gate is a
point-in-time probe, not a persistent refcount (a stale read only defers one pass); keeping the object+row
and file on one gate means a bound System that must re-materialize (reprovision, or a host base lost to a
disk fault) always finds the object present to re-download.

The per-System teardown rootfs reclaim (ADR-0434 §4) is **removed**, and so is ADR-0435's
provision-failure reclaim of the (now shared) base (decision 5). #1501's *failed-provision* orphan was
already closed by ADR-0435; what this removes is the residual **committed-object** exemption. The
upload-manifest reaper's `systems` arm (ADR-0435's `{defined, failed}` relaxation) is **re-scoped to
`investigations`**: it reaps a stale investigation upload window's uncommitted object + manifest past its
deadline.

### 7. Investigation close will not leave its bound Systems running

`close_investigation` gains bound-System coupling, scoped strictly to Systems with
`investigation_id = <this investigation>` (NULL-investigation Systems are untouched):

- **Default:** if any bound System is in a non-terminal state, close **fails** with a
  `configuration_error` listing them and refuses — the investigation stays OPEN/ACTIVE.
- **`close(force=True)`:** enqueue a teardown job for each bound live System (each gated on the caller
  holding that System's project role — a System the caller cannot tear down makes force fail listing
  it, rather than silently skipping), then close and set `cleanup_pending_at`.

Force teardown is async, so a just-force-closed investigation may briefly carry `cleanup_pending_at`
while teardowns drain; the decision-6 liveness guard is what keeps the base-file reclaim safe across
that window (and across a stuck teardown). The two mechanisms compose: close removes the *root cause*
(Systems outliving close), the guard is the *safety net*.

### 8. Remote-libvirt (#1433/ADR-0440) stays per-System-lease for now

Remote's supplied base is a libvirt **volume** on a remote host, not an object-store artifact row, and
is reclaimed by `delete_volume` at teardown (ADR-0440). Bringing it to investigation scope is a
separate, larger change (remote has no `owner_kind` row for the sweep to see and the staging host is
not the reconciler host). It is a **follow-up**; this ADR changes local-libvirt only, and the ADR-0428
parity waivers stay accurate.

## Consequences

- One agent upload provisions **>1 System in the same investigation** with no re-upload, and the host
  base is fetched **at most once per host per (investigation, checksum)**. The #1502 acceptance criteria
  are met for local-libvirt.
- Reclaim is **investigation-close-driven with a TTL backstop**, closing the residual #1501 exposure
  ADR-0435 did not reach — a committed object on a `ready`-then-stranded System — and preventing a
  never-closed investigation from accumulating objects (the ADR-0234 pairing). No `owner_kind` is left
  exempt from every reaper. The object and its host base share one lifetime and one liveness gate, so a
  live-referenced System can always re-materialize.
- Cross-investigation isolation is **stronger and simpler to reason about**: the SQL resolution
  predicate (decision 4) is the boundary, backed by per-investigation staging (decision 5).
- **New migration** (0076, `systems.investigation_id`), **new MCP surface**
  (`artifacts.create_investigation_upload`, `investigations.complete_rootfs_upload`), **removed MCP
  surface** (`artifacts.create_system_upload`), a **new close parameter** (`force`) with a new refusal
  path, **two new reconciler sweeps**, and a **new setting** (`KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`,
  reusing `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`). This is a larger blast radius than ADR-0434 by
  design — it is a re-scope, not a bugfix.
- Not an AI surface (no LLM/prompt/retrieval/classifier), so no eval plan is required.
- **No backfill — pre-1.0, fresh DBs.** Migration 0076 adds a nullable column only; it does not re-own
  pre-existing `owner_kind='systems'` rootfs objects, and the new sweep filters strictly on
  `owner_kind='investigations'`. Removing the teardown reclaim therefore *would* strand any already-committed
  systems-owned rootfs object. This is safe **only** because KDIVE is pre-1.0 greenfield (no deployed
  instance holds such objects), which is also what makes the `create_system_upload` removal and the
  profile-ref change acceptable. If that assumption ever fails, a one-time reaper of legacy systems-owned
  rootfs objects is required before this lands; it is out of scope here on the stated pre-1.0 assertion.
- **Residual — cross-investigation identical rootfs is not deduped.** Two investigations on one host that
  upload the same bytes each store the SENSITIVE object and stage the base separately (isolation over
  dedup, decision 4/5). An operator opening a fresh investigation per kernel version while reusing one
  debug rootfs pays per-investigation storage/bandwidth; a shared cross-investigation cache is rejected on
  the trust boundary.
- **Residual — advisory System→Investigation binding.** `systems.investigation_id` is nullable and not a
  capacity owner; a System still belongs to an Allocation. A profile referencing an uploaded rootfs
  requires the binding, but the column itself permits NULL, so the "upload ref ⇒ binding present"
  invariant is enforced at admission, not by the schema.
- **Residual — object-store reclaim is best-effort.** As in ADR-0234/0434, a store fault leaves an
  object the sweep retries next pass; the `artifacts`-row delete (the download handle) is fail-loud.
- **Residual — a genuinely-orphaned overlay defers its base.** A base is reclaimable only once no
  referencing System's overlay file remains. In the normal path teardown (or ADR-0435's failure reclaim)
  removes the overlay and the base drains. If an overlay is left on disk that neither teardown nor
  ADR-0435 could remove (e.g. a `failed` System whose overlay reclaim itself failed), the base is deferred
  — correctly, since deleting it under a present overlay would corrupt it — and both sweeps surface it as
  an un-drained marker for an operator, the same way a stuck `gc_investigation_artifacts` marker does.

## Considered & rejected

- **Reuse `owner_kind='runs'` (own the base on one Run).** Rejected: a base shared by Systems across
  sibling Runs would be "owned" by one arbitrary Run, misdescribing the sharing scope, and the
  `gc_investigation_artifacts` join reclaims on *that Run's* investigation close — fine when it is the
  same investigation, but the semantics lie. `owner_id = investigation_id` makes the owner equal to the
  sharing scope and removes the join.
- **Keep `owner_kind='systems'` and add a shared content-addressed host cache only.** Rejected: it
  delivers download reuse but not *object* reuse (still one committed object per System), keeps the
  teardown-only reclaim and the #1501 orphan, and a cross-System shared cache would break ADR-0434's
  per-lease isolation without the investigation boundary to replace it.
- **Resolve the investigation from the driving Run at provision instead of a `systems` column.**
  Rejected: Systems are provisioned independently of Runs (a Run may bind *after* provision), so there
  is often no Run in scope at materialize time; there is nothing to resolve against.
- **Self-describing reference `{"investigation_id":…, "checksum_sha256":…}` with project-scoped authz.**
  Rejected: it avoids the migration but relaxes the boundary from *cross-investigation* to *cross-project*
  (two investigations in one project could share a base). #1502 asks to keep cross-investigation
  isolation; the nullable column buys the stronger boundary for one migration.
- **Content-addressed persistent refcount for base reclaim.** Rejected: it enables slightly-earlier
  reclaim of an unreferenced base mid-investigation, at the cost of stored refcount state and
  decrement-vs-new-reference races, for marginal benefit while the investigation is open. The
  point-in-time overlay-absence probe at sweep time gives the same safety with no stored state.
- **Gate base reclaim on `systems.state` (`torn_down`) instead of overlay-file absence.** Rejected: a
  `failed` System is terminal, can never reach `torn_down` (`failed` is a state-table sink), and is
  excluded from `repair_orphaned_systems`, so a state gate would let one `failed` System pin the SENSITIVE
  base **forever** — defeating even the TTL backstop and reintroducing the exact leak this ADR closes.
  Overlay-file absence is the true safety condition (the overlay is what holds the base open), drains a
  `failed` System the moment ADR-0435 reclaims its overlay, and needs no faithful state proxy.
- **Let a running System outlive its investigation's close (naive close + grace).** Rejected: the
  overlay backing-file dependency means the sweep could delete a base under a live guest. Blocking close
  on bound live Systems (with `--force` to reap) fixes the root cause; the liveness guard covers the
  async-teardown window.
- **A `CLOSING` investigation state for force-reap.** Rejected for this change as heavier than needed:
  it gives the strongest "CLOSED ⇒ no live Systems" invariant and removes the liveness guard, but adds a
  new state + a reconciler transition. Block-by-default + `--force` + the guard reaches the same safety
  with no state-machine addition; the `CLOSING` state stays available if a future need justifies it.
- **Bring remote-libvirt (#1433) to investigation scope in lockstep.** Rejected here: remote's base is
  a libvirt volume with no `owner_kind` row and a non-reconciler staging host; it is a separate design.
  Filed as a follow-up so remote and local re-converge deliberately, not by forcing an ill-fitting model
  now.
- **Gate ADR-0435's per-call failure reclaim of the shared base on the liveness predicate (keep the
  arm).** Rejected in favor of removing it outright (decision 5): an investigation-owned base is not a
  provision's to reclaim at all, so a liveness-gated per-call unlink would duplicate the sweep's job and
  add a second place the same shared object is deleted. Sweep-only reclaim keeps one owner of that
  decision.
- **Fix only the #1501 residual leak, defer reuse (minimal path).** A cheaper change closes just the
  committed-object exemption — give the systems-owned rootfs object a TTL, or add an `owner_kind='systems'`
  rootfs arm to the existing artifact-expiry reaper — with no migration, no removed MCP surface, and no
  profile-ref break. Rejected as the framing for *this* issue: #1502 is the **reuse** issue (its title),
  and reuse genuinely needs an owner scope above the System — the *object*, not just the download, must be
  shared, which the minimal path does not deliver. The leak is an incidental beneficiary, not the driver.
  The reaper arm stays a valid standalone option if reuse is ever descoped; it is listed here so the
  coupling is a deliberate choice, not an unweighed default.
- **Do nothing.** Rejected: the per-System model's re-upload cost is the primary motivation, and its
  reclaim is teardown-shaped — the wrong shape for an object meant to outlive any single System. (ADR-0435
  already fixed the *failed-provision* orphan; #1502 is about reuse and the residual committed-object
  exemption, not that orphan.)

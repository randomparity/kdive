# Implementation plan — Investigation-scoped agent-uploaded rootfs (#1502)

- **Issue:** [#1502](https://github.com/randomparity/kdive/issues/1502)
- **Spec:** [`../../specs/2026-07-23-investigation-scoped-rootfs-1502-design.md`](../../specs/2026-07-23-investigation-scoped-rootfs-1502-design.md)
- **ADR:** [ADR-0441](../../adr/0441-investigation-scoped-uploaded-rootfs.md) (supersedes ADR-0434 §1/§3/§4)
- **Branch:** `feat/investigation-rootfs-1502` off `main`
- **Pre-assigned numbers:** migration **0076**, ADR **0441**
- **Guardrails (run before every commit):** `just lint`, `just type` (whole-tree), `just test`; full
  gate `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test) before push. CI runs the
  sub-recipes individually.
- **Status:** Not started (design-only run; execution awaits maintainer go-ahead).

## Shape

Six phases. **Green boundary is the *phase*, not every intra-phase commit** — a re-scope that removes the
old upload path cannot leave the tree green after a partial swap. Two coupling constraints override naive
phase ordering:

- **The upload-lane swap is atomic (Phases 2 + 3 land as one PR).** Removing the System-scoped upload path
  (Task 2.3) leaves a `{"kind":"upload"}` System unprovisionable until the replacement resolution (Task
  3.3) and required-`checksum_sha256` ref shape (Task 3.1) exist, and the new tools' exposure/tool_index/
  RBAC/behavior-map registration must land **with** the tools (Phase 2), not deferred to Phase 6. So
  Task 2.3 **also deletes the old ADR-0434 upload-provision tests and the old tool's exposure/tool_index/
  RBAC/behavior-map entries in the same commit**, and Phase 6's Task 6.1 covers only the *doc*
  regeneration (RBAC-matrix doc, agent-index) — not tool registration. Green holds at the Phase 2+3 PR
  boundary, not between Task 2.3 and Task 3.3.
- **Phase 4's reclaim removal must not land without Phase 5.** Phase 5 is the **sole producer** of the
  `rootfs_cleanup_pending_at` marker the Phase 4 close-driven sweep consumes; the existing `_close_locked`
  sets only `cleanup_pending_at`. Until Phase 5 lands, the close-driven sweep is inert end-to-end, and
  Task 4.2 removes the teardown + ADR-0435 reclaim — so Phases 4 and 5 land together (or Phase 5 first),
  never Phase 4's reclaim removal alone (else close+grace silently does nothing and only the TTL backstop
  reclaims). Phase 4's AC-7/AC-8 unit tests seed the marker directly.

- **All-or-nothing merge: Phases 2–5 land as one PR (or a stacked PR merged to main together).** There is
  **no leak-free proper prefix** once `create_system_upload` is removed: after a Phase 2+3-only merge, the
  new path creates `owner_kind='investigations'` objects but the old teardown reclaim (still present until
  Task 4.2) only targets `owner_kind='systems'`, and the new sweeps don't exist until Phase 4 — so any
  `complete_rootfs_upload` on main in that window orphans a SENSITIVE multi-GiB object with **no
  reclaimer**. Therefore Phases 2–5 merge atomically; Phase 1 (additive schema) may precede. Phase 6's
  code-schema-derived doc regeneration (`just docs`/`rbac-matrix`/`doc-constants`/`cli-verbs`) is the **tail
  commit of the same PR**, not a later merge — the `just ci` `*-check` gates read live tool schemas and red
  the PR until regenerated. Within that PR, order Phase 5 (the `rootfs_cleanup_pending_at` producer)
  **before** Task 4.2 (the teardown-reclaim removal) so no intermediate commit removes reclaim before its
  replacement produces. **Invariant to assert:** no `owner_kind='investigations'` rootfs object can exist
  on main without a live reclaimer.
- **Bisectability under no-squash.** The repo keeps individual commits (no squash) so `git bisect` can pin
  a regression. Prefer **add-new-then-remove-old within a single commit** so each commit is individually
  green (Task 2.3 does the tool swap this way); collapse an irreducible red window into **one** commit (the
  whole upload-lane swap as a single commit) rather than a run of red commits, and where one is truly
  unavoidable, state in the PR that those commits bisect as a unit.

Phase 1 (schema) is the prerequisite for all. TDD throughout: write the failing test named in each task's
acceptance criteria first. No dual-format shim (pre-1.0, "replace, not deprecate").

---

## Phase 1 — Schema + domain binding

### Task 1.1 — Migration 0076: `systems.investigation_id`
- **Fits:** ADR-0441 §2 — the System↔Investigation binding the whole reference model resolves against.
- **Files:** `src/kdive/db/schema/0076_systems_investigation_id.sql` (new); regenerate any schema
  snapshot/reference the repo checks in.
- **Do:** `ALTER TABLE systems ADD COLUMN investigation_id uuid REFERENCES investigations (id);` (nullable,
  no default); index it (close-coupling + liveness queries filter by it). `ALTER TABLE investigations ADD
  COLUMN rootfs_cleanup_pending_at timestamptz;` — the dedicated rootfs reclaim marker (Task 4.1; must not
  reuse `cleanup_pending_at`, which `gc_investigation_artifacts` owns/clears). `CREATE UNIQUE INDEX … ON
  artifacts (object_key) WHERE owner_kind='investigations';` — **partial** (never touches other owner
  kinds), backing finalize idempotency (Task 2.2). `ALTER TABLE artifacts ADD COLUMN encoding text, ADD
  COLUMN uncompressed_size bigint;` (nullable, absent ⇒ identity) — the durable transport-encoding a
  post-finalize gzip provision reads (Task 2.2/3.3).
- **Acceptance:** `just test` migration round-trip passes; `test_migrate.py` sees 0076 as the new head;
  a NULL-investigation System row inserts unchanged.
- **Rollback:** 0076 creates five objects (two `systems`/`investigations` columns, the partial UNIQUE
  index, `artifacts.encoding` + `uncompressed_size`). It is **forward-only** (pre-1.0, consistent with the
  "No backfill" stance): a clean drop is safe only on a DB with **no** `owner_kind='investigations'` rootfs
  rows; once finalize has committed such rows, dropping the columns/index loses transport-encoding data +
  the idempotency guard and orphans staged bases, so there is no supported down-migration then.
- **Convention:** forward-only `NNNN_*.sql`; monotonic; `test_migrate.py` may hardcode the migration list.

### Task 1.2 — Domain record + repository
- **Fits:** carry the new column into the domain layer.
- **Files:** `src/kdive/domain/lifecycle/records.py` (`System.investigation_id: UUID | None = None`
  **and** `Investigation.rootfs_cleanup_pending_at: datetime | None = None`, mirroring the sibling
  `cleanup_pending_at`); `src/kdive/db/repositories.py` (SYSTEMS + INVESTIGATIONS read/write of the new
  columns).
- **Acceptance:** a System persisted with an `investigation_id` round-trips; an Investigation's
  `rootfs_cleanup_pending_at` round-trips; existing tests pass with the fields defaulting to `None`.

---

## Phase 2 — Investigation-scoped upload window + finalize (replaces System-scoped)

### Task 2.1 — `owner_kind='investigations'` in the upload machinery
- **Fits:** ADR-0441 §3 — reuse `_create_upload`/`upload_manifests` for a new owner.
- **Files:** `src/kdive/artifacts/upload_manifest.py` (`UploadOwnerKind` adds `"investigations"`;
  `INVESTIGATION_UPLOAD_OWNER`); `src/kdive/mcp/tools/catalog/artifacts/uploads.py` (new
  `_INVESTIGATION_UPLOAD` `_UploadOwnerSpec` with **`accepts_encoding=True`**, `_investigation_project`,
  `_investigation_accepts_upload` → OPEN/ACTIVE); object key/prefix helpers for the content-addressed
  `rootfs-<token>` name. **Re-home ADR-0439's encoding advertisement** — the per-owner declaration-item
  schema (base + encoding properties), `Field`/docstring/worked gzip example — onto the new tool so the
  gzip surface does not regress to an unadvertised field.
- **Acceptance:** `create_investigation_upload` mints a single-PUT presigned URL for a `rootfs`
  declaration on an OPEN investigation; rejects a CLOSED one (`owner_not_accepting_upload`); rejects
  chunked (`chunking_not_supported`, ADR-0436); accepts gzip encoding (ADR-0438/0439).
- **Convention:** CONTRIBUTOR role; audit inside the mint txn; `#1336` deadline contract in `data`.

### Task 2.2 — `investigations.complete_rootfs_upload` finalize
- **Fits:** ADR-0441 §3 — the explicit commit that writes the durable row Systems reference.
- **Files:** `src/kdive/mcp/tools/lifecycle/investigations/…` (new handler); `artifacts/registration.py`
  (write `owner_kind='investigations'`, `retention_class='rootfs'`); reuse the ADR-0434 §2 HEAD-verify.
- **Do:** run **under the investigation lock**, require OPEN/ACTIVE (serialize with `close_investigation`;
  terminal → `configuration_error`, leave manifest for the reaper); HEAD (`ChecksumMode=ENABLED`); reject
  missing/checksum-less; **assert HEAD checksum == declared** (mismatch → `configuration_error`); write the
  row via **`INSERT … ON CONFLICT DO NOTHING`** against the Task 1.1 partial UNIQUE index on `object_key`,
  **including `encoding`/`uncompressed_size` from the manifest entry**, then **re-SELECT by `object_key` to
  confirm exactly one converged row** and return the declared `checksum_sha256` (the value in hand /
  equivalently the `object_key` token decoded base64url→base64 — the `artifacts` table has **no** checksum
  column); delete the manifest.
- **Acceptance:** spec AC-4 (row incl. encoding, idempotent, concurrent → one row), AC-4c (gzip recoverable),
  **AC-4d reject branch only** (finalize on a terminal investigation rejects, manifest left for the reaper);
  the AC-4d *reclaim* branch (committed-then-reclaimed) is proved in Phase 4/5, since neither the sweep nor
  the close-driven marker exists at the Phase 2+3 boundary.

### Task 2.3 — Remove the System-scoped upload path
- **Fits:** ADR-0441 §3 — replace, not deprecate.
- **Files:** remove `create_system_upload`, `_SYSTEM_UPLOAD`, `_system_accepts_upload` from
  `uploads.py`; remove `_commit_uploaded_rootfs`/`_finalize_provision_ready` rootfs-commit from
  `jobs/handlers/systems.py`; remove `rootfs_upload_window_allowed` from `profiles/provider_policy.py` +
  `providers/local_libvirt/profile_policy.py`; remove the `systems.define` upload-window opening.
- **Second consumer (import-breaking):** `mcp/tools/catalog/artifacts/expected_uploads.py` imports
  `CREATE_SYSTEM_UPLOAD_TOOL` and builds a `'system'` discovery item (`_NEXT_ACTIONS`, `_SYSTEM_CONTRACTS`)
  — deleting the tool symbol **breaks its top-level import** (a hard `ImportError` reddening the app build,
  not a soft test failure). **Re-point** it to an `'investigations'` discovery item for
  `create_investigation_upload` in the same commit (else the new upload path has no discovery surface — a
  phantom-feature regression). Confirm whether `SYSTEM_ARTIFACT_NAMES` (`artifacts/read_model.py`) is
  retired or re-owned.
- **Also (same commit, for green):** delete the old ADR-0434 upload-provision tests and remove the old
  tool's `mcp/exposure.py` + `mcp/schema/tool_index.py` + RBAC-matrix + `_BEHAVIOR_TESTS_BY_TOOL` entries,
  and **register the two new tools** in those same maps — so the guards are green at the Phase 2+3 PR
  boundary. Phase 6 (Task 6.1) does only *doc* regeneration, not registration.
- **Acceptance:** `rg CREATE_SYSTEM_UPLOAD_TOOL` and `rg SYSTEM_ARTIFACT_NAMES` across `src/` show no
  residual references; the app **imports** and guards are green with the new tools registered/discoverable
  and the old gone.
- **Watch:** lands in the **same PR** as Phase 3 (Tasks 3.1/3.3) — the atomic upload-lane swap (see Shape).

---

## Phase 3 — Profile reference + provision resolution

### Task 3.1 — `_UploadRootfs` gains `checksum_sha256`
- **Fits:** ADR-0441 §4.
- **Files:** `src/kdive/profiles/provisioning.py` (`_UploadRootfs.checksum_sha256: str`, required);
  any profile schema doc regenerated.
- **Acceptance:** a profile with `{"kind":"upload","checksum_sha256":"…"}` parses; one missing the field
  is rejected at parse/validation.

### Task 3.2 — define/provision `investigation_id` param + binding invariant
- **Fits:** ADR-0441 §2.
- **Files:** `src/kdive/mcp/tools/lifecycle/systems/provision.py` (`define_system`/`provision_system`
  optional `investigation_id`); `SystemAdmission` (`systems/…`) validates: supplied ⇒ non-terminal
  investigation whose **project equals the System's own (Allocation) project** (reject cross-project;
  closes the `close(force)` deadlock + keeps the SENSITIVE base in one trust boundary); **write-once with
  NULL carve-out** (non-NULL define ⇒ provision must match or omit; NULL define ⇒ first provision may set
  once then immutable — an upload-ref forces non-NULL at define, so only non-upload Systems hit the NULL
  branch; the reclaim gate enumerates referencers by this column); and **upload rootfs ⇒ binding present**
  (`configuration_error` naming the missing binding).
- **Acceptance:** spec AC-3 (upload ref without binding rejected at admission); a bound System persists
  its `investigation_id`.

### Task 3.3 — Resolve + stage within the System's investigation
- **Fits:** ADR-0441 §4/§5.
- **Files:** `providers/local_libvirt/lifecycle/rootfs/materialize.py` +
  `rootfs_upload_fetch.py` (resolve **by content-addressed `object_key`** — transcode ref base64 →
  base64url `<token>`, build `artifact_key("local","investigations",<inv>,"rootfs-"<token>)`, look up
  `owner_kind='investigations' AND owner_id=system.investigation_id AND object_key=<derived>`; **no
  checksum column**; via the short-lived sync connection);
  `providers/local_libvirt/lifecycle/storage.py` (`UPLOADS_DIR` path → `rootfs-uploads/<inv>/<token>.qcow2`).
- **Do:** miss ⇒ `configuration_error` naming the checksum; keep ADR-0434 §2 SHA-256 verify + ADR-0438
  qcow2-magic gate; read `encoding`/`uncompressed_size` **from the resolved row** (not the deleted
  manifest) and strip gzip per ADR-0438; reuse a present verified file (cache hit). Concurrency: **unique
  per-fetcher `<token>.<uuid>.partial`** (correctness — no shared partial) + a **deterministic
  session-scoped `pg_advisory_lock`** keyed via `db.locks._session_lock_key(f"rootfs-fetch:{inv}:{token}")`
  (the **session** keyspace helper, salted apart from `_lock_key`; dedup — NOT `_lock_key`, NOT Python
  `hash()`; NOT `advisory_xact_lock`, which holds a txn open across the download) with a post-acquire `dest`
  re-check. Unlink the own `<token>.<uuid>.partial` in a `finally` on any failure. **Opportunistic
  crash-orphan cleanup:** while holding the lock (which serializes downloads, so no *live* sibling exists),
  glob-unlink any other `<token>.*.partial` — a killed worker's orphan — so it does not persist for the
  investigation's whole life (the reclaim-sweep glob is the backstop, not the only cleanup).
- **Acceptance:** spec AC-1 (one download for two Systems, sequential **and cross-process concurrent**),
  AC-2 (isolation), AC-4b (resolve-by-object_key + transcode), AC-4c (gzip stripped from row encoding).
- **Watch / test harness:** the key must be `_session_lock_key(...)`, not `hash()` (per-process salted). A
  same-process threaded test shares the hash seed and would **pass against the bug**, so the primary guard
  is a cheap **deterministic-key assertion** (the lock key the fetch computes equals
  `_session_lock_key("rootfs-fetch:<inv>:<token>")`, not `hash(...)`) that any unit test can check. The
  full cross-process AC-1 test (two interpreters sharing the per-worker `DATABASE_URL` + `UPLOADS_DIR`/
  `ROOTFS_DIR`, race-synced via a parent-held barrier, asserting one download) is the integration proof;
  spell out that shared-env wiring in the test or it defaults to an ineffective same-process form.

---

## Phase 4 — Reclaim on investigation close + grace

Task 4.1 is decomposed into four independently testable commits (4.1a–4.1d), each with its own failing
test first. **Global watch:** gate on overlay-file **absence**, not `systems.state` — `SystemState.FAILED`
is a terminal sink (no `→torn_down`) excluded from `repair_orphaned_systems`, so a state gate pins a
`failed` System's base forever and defeats the TTL backstop.

### Task 4.1a — Condition-(b) state constant + drift guard
- **Fits:** ADR-0441 §6 (the one place the gate trusts state).
- **Files:** `domain/capacity/state.py` (a named constant beside `SystemState` listing the pre-overlay/
  re-materialize non-terminal states: `defined`, `provisioning`, `reprovisioning`, `restoring`); a
  structural test.
- **Acceptance:** the drift-guard test **reddens** when a new non-terminal `SystemState` is added without
  being classified in/out of the constant.

### Task 4.1b — Referencer enumeration + two-condition liveness gate helper
- **Fits:** ADR-0441 §6.
- **Files:** `reconciler/cleanup/gc.py` (pure helper, unit-tested without the reconciler loop).
- **Do:** for checksum X, `systems WHERE investigation_id=<inv> AND state<>'torn_down'`, parse each
  `provisioning_profile` rootfs ref, keep only `{"kind":"upload","checksum_sha256":X}` (unparseable /
  no-rootfs / catalog / local / different-checksum ⇒ **not** a referencer of X); per referencer stat
  `overlay_path(id)` (`<ROOTFS_DIR>/<id>-overlay.qcow2`) for condition (a) and check the 4.1a constant for
  (b). **Fail-closed:** distinguish a missing overlay under a *present/accessible* `ROOTFS_DIR` (overlay
  gone → reclaimable) from an *absent/inaccessible* `ROOTFS_DIR` (**defer the whole pass** — never read a
  missing root as "all overlays gone"). The reconciler does no host-FS access today, so a mis-deployed
  reconciler must reclaim nothing, not everything.
- **Acceptance:** spec AC-8 (deferred while overlay present; a `failed` referencer whose overlay was
  reclaimed drains), AC-8c (`reprovisioning`/`restoring` defer), AC-8f (unrelated System doesn't pin X),
  AC-8i (**fail-closed** — an inaccessible `ROOTFS_DIR` reclaims nothing).

### Task 4.1c — Per-checksum reclaim (pinned order + fault contract + `.partial` sweep)
- **Fits:** ADR-0441 §6.
- **Files:** `gc.py` (shared per-checksum reclaim helper); a file-unlink port (local host FS).
- **Do:** pinned order object → **unlink** → row (fail-loud-in-txn, **last** — the worklist anchor). One
  fault contract for object-delete + unlink: **404/`ENOENT` = success**, any **real** fault **defers** the
  whole checksum (`drained=False`, row kept) *before* the row delete. Glob-unlink stale `<token>.*.partial`
  before the empty-dir removal.
- **Acceptance:** AC-8e (unlink-fail keeps row + retries), AC-8g (idempotent reconverge; non-404 defers),
  AC-8h (crash-`.partial` swept).

### Task 4.1d — The two sweep entry points + registration + config
- **Fits:** ADR-0441 §6 — mirror ADR-0234's `gc_investigation_artifacts` + `gc_expired_build_artifacts`.
- **Files:** `gc.py` (`gc_investigation_uploaded_rootfs` close-driven + `gc_expired_investigation_rootfs`
  TTL, both calling 4.1b's gate + 4.1c's reclaim); reconciler loop registration; `config/core_settings.py`
  (`KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`; reuse `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`) + `just
  config-docs`.
- **Do:** close-driven selects by **`rootfs_cleanup_pending_at`** (its own marker — NOT `cleanup_pending_at`;
  clears only its own when drained); TTL selects committed `owner_kind='investigations'`,
  `retention_class='rootfs'` objects past retention on a never-closed investigation.
- **Registration guard (concrete semantics):** for local-libvirt the reconciler **is** libvirt-host-local
  (single-host M0/M1 topology; a deploy-role invariant, declared alongside the other host deps). Startup
  checks `ROOTFS_DIR` + `UPLOADS_DIR` are present/accessible: if so, register the sweeps; if not, **skip
  registration and emit a prominent error log + a metric** (the reconciler keeps running its other repairs
  — no availability regression), and reclaim of `owner_kind='investigations'` rootfs does not run on that
  (mis-deployed / future-split) topology. Pin the chosen behavior (skip+alert, not crash) with a test so it
  can't drift. A split reconciler/libvirt-host topology having no rootfs reclaimer is documented as out of
  scope until a remote-reclaim design (the remote lane is deferred, decision 8).
- **Acceptance:** AC-7 (object+row gone past grace), AC-8b (TTL reclaims a never-closed investigation),
  AC-8d (**marker independence** — a drained build artifact nulling `cleanup_pending_at` does not starve
  the rootfs sweep), AC-8i (registration guard fails loudly without host FS), AC-10 (residual committed +
  stale-window object collected), **AC-4d reclaim branch** (finalize-committed-then-close → reclaimed, no
  orphan — the half Phase 2 cannot prove).

### Task 4.2 — Remove teardown + provision-failure rootfs reclaim; re-scope the manifest reaper
- **Fits:** ADR-0441 §5/§6 — reclaim is now sweep-driven; the shared base is no individual provision's
  to delete.
- **Files:** `jobs/handlers/systems.py` (`_delete_uploaded_rootfs_object`/`_row` + teardown calls);
  `providers/local_libvirt/lifecycle/provisioning.py` (`remove_uploaded_rootfs_for_domain` teardown call
  **and** ADR-0435 §1's `uploaded_rootfs_exists`/`staged_pre` provision-failure unlink arm — keep the
  baseline/overlay arms); `reconciler/cleanup/uploads.py` (re-scope the `systems` `{defined, failed}`
  reaper arm to `investigations`).
- **Acceptance:** teardown no longer deletes the base/object/row; a failing provision no longer unlinks
  the shared base (spec AC-9); the ADR-0434 teardown-reclaim tests are replaced by the sweep tests; the
  reaper reaps a stale investigation upload window. Verify the sweep + reaper are the sole reclaimers and
  no leak path is uncovered.
- **Watch:** Tasks 4.1 and 4.2 land in one phase — removing reclaim without the sweep would leak; removing
  the ADR-0435 arm without decision 5's rationale looks like a regression. **And Phase 4 must land with (or
  after) Phase 5** — Phase 5 is the sole producer of `rootfs_cleanup_pending_at`, so removing the teardown
  reclaim here before the close producer exists leaves close+grace inert (only the TTL backstop reclaims).

---

## Phase 5 — Investigation-close coupling

### Task 5.1 — `close_investigation(force)` bound-System coupling
- **Fits:** ADR-0441 §7.
- **Files:** `src/kdive/services/investigations/lifecycle.py` + `mcp/tools/lifecycle/investigations/lifecycle.py`
  (add `force: bool = False`); enumerate `systems WHERE investigation_id=<inv> AND state NOT IN terminal`.
- **Do:** default ⇒ live present → `configuration_error` listing ids, refuse. force ⇒ **all-or-nothing**:
  admin is a **single per-project check** (all bound Systems share the investigation's project — same-project
  invariant — so no per-System variance); matching `systems.teardown`'s `_ADMIN` (no escalation). If admin
  passes, enqueue all teardowns + close + set **both** `cleanup_pending_at` **and** `rootfs_cleanup_pending_at`
  in one txn; an **enqueue error** aborts with **zero** teardowns, investigation unchanged. Any successful
  close sets both markers. Never consider NULL-investigation Systems.
- **Acceptance:** spec AC-5 (block+list), AC-6 (force enqueues teardown + closes; single-project admin
  fails wholesale; **atomic — an enqueue error enqueues zero teardowns**). RBAC: force teardown is
  destructive — **admin** per System project (assert a same-project contributor cannot force-teardown a
  bound System — no escalation).
- **Watch:** `investigations.close` is `_CONTRIBUTOR`, `systems.teardown` is `_ADMIN` (exposure.py). Force
  must not become a contributor-driven teardown; the per-System admin check is the escalation guard.
- **Surface:** update the `investigations.close` wrapper docstring + `Field` for the new `force` param and
  the new refusal contract (wrapper docstring is the agent-facing contract, AGENTS.md).

---

## Phase 6 — Surface, docs, and test sweep (lands last)

### Task 6.1 — Generated-artifact regeneration (tail commit of the Phases 2–5 PR, **not** a separate merge)
- **Why not a later PR:** every code-schema-derived drift guard in `just ci` — `docs-check`,
  `doc-constants-check`, `rbac-matrix-check`, **`cli-verbs-check`** — diffs committed artifacts against a
  fresh generation from the **live** tool schemas, so changing the tool set (Task 2.3) reds all of them
  until regenerated. The regen therefore lands in the **same atomic PR** (its tail commit), never deferred.
  Task 2.3's "guards green" claim scopes only to the *registration* tests (`exposure`/`tool_index`/
  behavior-map), not these generated-doc checks.
- **Commands (all in the PR):** `just docs` (tool reference), `just rbac-matrix` (role→tool matrix),
  `just doc-constants` (tool count), **`just cli-verbs`** (kdivectl verb descriptors — also gated,
  previously omitted).
- **Acceptance:** spec AC-11 (old tool gone, two new tools present with the CONTRIBUTOR gate + the re-homed
  ADR-0439 encoding advertisement); **all four `*-check` gates green** in the same PR.

### Task 6.2 — Agent-facing docs
- **Files:** `mcp/resources/_content/external-build-upload.md`, `toolsets-artifacts.md`, `agent-index.md`,
  `safety-and-rbac.md` — the investigation upload+finalize flow, the `checksum_sha256` profile ref, the
  `close(force=…)` contract. Regenerate any generated tool reference.
- **Acceptance:** `just ci` doc guards (check-mermaid, generated-doc drift) green; the project doc-style
  guard (plain factual prose, no marketing adjectives) satisfied.

### Task 6.3 — Follow-up issues filed
- **Do (not code):** file (a) kernel-build reuse across Systems adopting `owner_kind='investigations'`
  (install-plane reference feature); (b) remote-libvirt (#1433/ADR-0440) investigation-scope parity.
  Link both to #1502. Note #1501's rootfs concern is dissolved by this change (comment on #1501).

### Task 6.4 — Optional live proof
- **Do (manual, not gated):** on the KVM host, provision two Systems in one investigation from one
  uploaded rootfs; assert a single host download and a successful boot of both; then `close(force=True)`
  and confirm the sweep reclaims after grace. Record in the PR per `docs/operating/runbooks/live-testing.md`.

## Verification gaps to watch

- The liveness guard (Task 4.1) is the load-bearing safety property — its test (AC-8) must simulate a
  *live* bound System (not just a row) so a naive "delete if past grace" regresses it.
- Removing the teardown reclaim (Task 4.2) without the sweep (Task 4.1) in place would leak — keep them
  in one commit/phase.
- The binding invariant (Task 3.2) is the only thing preventing an unresolvable upload ref — its negative
  test (AC-3) must fail before the fix.

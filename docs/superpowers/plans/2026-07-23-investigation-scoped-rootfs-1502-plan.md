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

Six ordered phases. Phases 1→4 are the data-model → upload → provision → reclaim spine and are strictly
ordered (each depends on the prior). Phase 5 (close coupling) depends only on Phase 1's column. Phase 6
(surface/docs/tests-sweep) lands last. TDD throughout: write the failing test named in each task's
acceptance criteria first.

Because this is a **re-scope that removes** the System-scoped upload path, the removals and additions
must land together per phase — do not leave both paths live (no dual-format shim; "replace, not
deprecate"). Keep the whole branch green at each commit.

---

## Phase 1 — Schema + domain binding

### Task 1.1 — Migration 0076: `systems.investigation_id`
- **Fits:** ADR-0441 §2 — the System↔Investigation binding the whole reference model resolves against.
- **Files:** `src/kdive/db/schema/0076_systems_investigation_id.sql` (new); regenerate any schema
  snapshot/reference the repo checks in.
- **Do:** `ALTER TABLE systems ADD COLUMN investigation_id uuid REFERENCES investigations (id);` (nullable,
  no default). Add an index on `investigation_id` (the close-coupling and liveness queries filter by it).
- **Acceptance:** `just test` migration round-trip passes; `test_migrate.py` sees 0076 as the new head;
  a NULL-investigation System row inserts unchanged.
- **Rollback:** drop-column (no data loss for NULL rows).
- **Convention:** forward-only `NNNN_*.sql`; monotonic; `test_migrate.py` may hardcode the migration list.

### Task 1.2 — Domain record + repository
- **Fits:** carry the new column into the domain layer.
- **Files:** `src/kdive/domain/lifecycle/records.py` (`System.investigation_id: UUID | None = None`);
  `src/kdive/db/repositories.py` (SYSTEMS read/write of the column).
- **Acceptance:** a System persisted with an `investigation_id` round-trips; existing System tests pass
  with the field defaulting to `None`.

---

## Phase 2 — Investigation-scoped upload window + finalize (replaces System-scoped)

### Task 2.1 — `owner_kind='investigations'` in the upload machinery
- **Fits:** ADR-0441 §3 — reuse `_create_upload`/`upload_manifests` for a new owner.
- **Files:** `src/kdive/artifacts/upload_manifest.py` (`UploadOwnerKind` adds `"investigations"`;
  `INVESTIGATION_UPLOAD_OWNER`); `src/kdive/mcp/tools/catalog/artifacts/uploads.py` (new
  `_INVESTIGATION_UPLOAD` `_UploadOwnerSpec`, `_investigation_project`, `_investigation_accepts_upload`
  → OPEN/ACTIVE); object key/prefix helpers for the content-addressed `rootfs-<token>` name.
- **Acceptance:** `create_investigation_upload` mints a single-PUT presigned URL for a `rootfs`
  declaration on an OPEN investigation; rejects a CLOSED one (`owner_not_accepting_upload`); rejects
  chunked (`chunking_not_supported`, ADR-0436); accepts gzip encoding (ADR-0438/0439).
- **Convention:** CONTRIBUTOR role; audit inside the mint txn; `#1336` deadline contract in `data`.

### Task 2.2 — `investigations.complete_rootfs_upload` finalize
- **Fits:** ADR-0441 §3 — the explicit commit that writes the durable row Systems reference.
- **Files:** `src/kdive/mcp/tools/lifecycle/investigations/…` (new handler); `artifacts/registration.py`
  (write `owner_kind='investigations'`, `retention_class='rootfs'`); reuse the ADR-0434 §2 HEAD-verify.
- **Do:** HEAD the object (`ChecksumMode=ENABLED`); reject missing/checksum-less (`configuration_error`);
  write the write-once row; delete the manifest; return `data.checksum_sha256` (canonical base64).
  Idempotent when the row already exists.
- **Acceptance:** spec AC-4 (row written, manifest deleted, handle returned, idempotent re-call).

### Task 2.3 — Remove the System-scoped upload path
- **Fits:** ADR-0441 §3 — replace, not deprecate.
- **Files:** remove `create_system_upload`, `_SYSTEM_UPLOAD`, `_system_accepts_upload` from
  `uploads.py`; remove `_commit_uploaded_rootfs`/`_finalize_provision_ready` rootfs-commit from
  `jobs/handlers/systems.py`; remove `rootfs_upload_window_allowed` from `profiles/provider_policy.py` +
  `providers/local_libvirt/profile_policy.py`; remove the `systems.define` upload-window opening.
- **Acceptance:** grep shows no residual references; the removed tool is gone from `mcp/exposure.py`,
  `mcp/schema/tool_index.py`, and the RBAC matrix (Phase 6 sweeps the doc/test fallout).
- **Watch:** `SYSTEM_ARTIFACT_NAMES` carried only `rootfs`; confirm no other system-upload consumer.

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
  closes the `close(force)` deadlock + keeps the SENSITIVE base in one trust boundary); **write-once**
  (provision's `investigation_id` must equal define's; reject a change — the reclaim gate enumerates
  referencers by this column); and **upload rootfs ⇒ binding present** (`configuration_error` naming the
  missing binding).
- **Acceptance:** spec AC-3 (upload ref without binding rejected at admission); a bound System persists
  its `investigation_id`.

### Task 3.3 — Resolve + stage within the System's investigation
- **Fits:** ADR-0441 §4/§5.
- **Files:** `providers/local_libvirt/lifecycle/rootfs/materialize.py` +
  `rootfs_upload_fetch.py` (resolve `owner_kind='investigations' AND owner_id=system.investigation_id
  AND checksum_sha256=<ref>` via the short-lived sync connection; no per-System manifest read);
  `providers/local_libvirt/lifecycle/storage.py` (`UPLOADS_DIR` path → `rootfs-uploads/<inv>/<token>.qcow2`).
- **Do:** miss ⇒ `configuration_error` naming the checksum; keep ADR-0434 §2 SHA-256 verify + ADR-0438
  qcow2-magic gate; reuse a present verified file (cache hit).
- **Acceptance:** spec AC-1 (reuse: one download for two Systems) and AC-2 (isolation: cross-investigation
  checksum → resolution miss, no file staged).

---

## Phase 4 — Reclaim on investigation close + grace

### Task 4.1 — Two reclaim sweeps (close-driven + TTL backstop)
- **Fits:** ADR-0441 §6 — mirror ADR-0234's `gc_investigation_artifacts` + `gc_expired_build_artifacts` pair.
- **Files:** `src/kdive/reconciler/cleanup/gc.py` (`gc_investigation_uploaded_rootfs` +
  `gc_expired_investigation_rootfs`, sharing the per-checksum reclaim + liveness helper); reconciler loop
  registration for both; a file-unlink port (local host FS); `config/core_settings.py`
  (`KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS`, reuse `KDIVE_INVESTIGATION_CLEANUP_GRACE_DAYS`) — and the
  config-docs regeneration (`just config-docs`).
- **Do:** close-driven selects investigations past the grace window; TTL backstop selects committed
  `owner_kind='investigations'` `retention_class='rootfs'` objects past the retention TTL on a
  never-closed investigation. Both reclaim **per checksum** on a **two-condition gate**, both required for
  every referencing bound System: (a) its per-System overlay file is **absent** (filesystem probe, not a
  `systems.state` read), **and** (b) it is **not** `defined`/`provisioning` (pre-overlay states that may
  still read/create against the base — the ADR-0435 `provisioning` exclusion; matters for the TTL
  backstop). Reclaim object (best-effort) + row (fail-loud-in-txn) + staged file together. Gate fails →
  skip the checksum (`drained=False`); remove the empty `rootfs-uploads/<inv>/` dir when drained.
- **Acceptance:** spec AC-7 (object+row gone past grace), AC-8 (deferred while an overlay is present;
  **a `failed` referencer whose overlay was reclaimed must drain** — not pinned forever), AC-8b (TTL
  backstop reclaims a never-closed investigation), AC-10 (residual committed object + stale-window object
  collected).
- **Watch:** gate on overlay-file **absence**, not `systems.state`. `SystemState.FAILED` is a terminal
  sink (no `→torn_down`) and is excluded from `repair_orphaned_systems`, so a state gate pins a `failed`
  System's base forever and defeats the TTL backstop. The AC-8 test must include a `failed` referencer and
  assert eventual drainage.

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
- **Watch:** Tasks 4.1 and 4.2 must land in one phase — removing reclaim without the sweep in place
  would leak; removing the ADR-0435 arm without decision 5's rationale would look like a regression.

---

## Phase 5 — Investigation-close coupling

### Task 5.1 — `close_investigation(force)` bound-System coupling
- **Fits:** ADR-0441 §7.
- **Files:** `src/kdive/services/investigations/lifecycle.py` + `mcp/tools/lifecycle/investigations/lifecycle.py`
  (add `force: bool = False`); enumerate `systems WHERE investigation_id=<inv> AND state NOT IN terminal`.
- **Do:** default ⇒ live present → `configuration_error` listing ids, refuse. force ⇒ per bound live
  System require **admin on that System's project** (matching `systems.teardown`'s `_ADMIN` gate — no
  contributor→admin escalation via close; else fail listing it), enqueue teardown, then close + set
  `cleanup_pending_at`. Never consider NULL-investigation Systems.
- **Acceptance:** spec AC-5 (block+list), AC-6 (force enqueues teardown + closes). RBAC: force teardown is
  destructive — **admin** per System project (assert a same-project contributor cannot force-teardown a
  bound System — no escalation).
- **Watch:** `investigations.close` is `_CONTRIBUTOR`, `systems.teardown` is `_ADMIN` (exposure.py). Force
  must not become a contributor-driven teardown; the per-System admin check is the escalation guard.
- **Surface:** update the `investigations.close` wrapper docstring + `Field` for the new `force` param and
  the new refusal contract (wrapper docstring is the agent-facing contract, AGENTS.md).

---

## Phase 6 — Surface, docs, and test sweep (lands last)

### Task 6.1 — MCP surface reconciliation
- **Files:** `mcp/exposure.py`, `mcp/schema/tool_index.py`, RBAC matrix, `_BEHAVIOR_TESTS_BY_TOOL`,
  registrars for the two new tools and the removed one.
- **Acceptance:** spec AC-10 (old tool gone, two new tools present with CONTRIBUTOR gate); exposure/RBAC
  guards green. (Adding/removing an MCP tool trips exposure + behavior-test-map + RBAC-matrix — expect all
  three.)

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

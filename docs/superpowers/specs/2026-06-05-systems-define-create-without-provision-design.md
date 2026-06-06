# systems.define — the create-without-provision producer of `DEFINED` Systems

- **Status:** Draft
- **Date:** 2026-06-05
- **Goal:** Add the missing producer of `SystemState.DEFINED` so the rootfs-upload lane
  (ADR-0048 §5) is reachable end-to-end: an operator creates a System in `DEFINED`, uploads
  a rootfs qcow2 to the System-owned key, then provisions it (`defined → provisioning`), at
  which point the provisioning plane commits the uploaded rootfs.
- **Depends on:** the Provisioning plane (#16,
  [ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) — `systems.provision`/`.get`/
  `.teardown`, the `provision` handler, the per-allocation/per-System locks, the
  `granted → active` flip), external-build ingestion (#110,
  [ADR-0048](../../adr/0048-external-build-artifact-ingestion.md) — the `upload`-kind rootfs,
  `artifacts.create_upload`'s System branch, `_commit_uploaded_rootfs`, the upload reaper),
  the admission precedent ([ADR-0023](../../adr/0023-discovery-allocation-admission.md)).
- **ADR:** [ADR-0025](../../adr/0025-provisioning-plane-libvirt.md) (amended — decision 1's
  "`defined` is never written" is narrowed to "M0 never writes it; #111 adds the producer")
  and [ADR-0048](../../adr/0048-external-build-artifact-ingestion.md) (amended — §5's
  forward-plumbing note becomes a live lane).
- **Issue:** #111.

## 1. Problem

`SystemState.DEFINED` exists as an enum member, the head of the System state machine
(`defined → provisioning`), and a set of *consumers* shipped on #110 as acknowledged
forward-plumbing — each tagged with a `#111` comment:

- `artifacts.create_upload`'s `_owner_accepts_upload` System branch (admits an upload only
  for a `DEFINED` System) and its `system → systems.provision` `next_action`.
- `systems._commit_uploaded_rootfs` (commits the write-once rootfs artifact on
  `provisioning → ready`) and the `DEFINED` entry in `_NON_TERMINAL_SYSTEM` (quota slot).
- the reconciler's `_repair_abandoned_uploads` systems arm (`_UPLOAD_PRE_FINALIZE`).
- `profiles.provisioning._UploadRootfs` (the `upload` rootfs kind).
- `providers/local_libvirt/provisioning.resolve_rootfs_path`'s upload branch.

**Nothing produces a `DEFINED` System.** The only `SYSTEMS.insert` (`systems.provision`)
writes `PROVISIONING` directly — ADR-0025 decision 1 deliberately skipped materializing
`defined`, reserving it for a "create-without-provision path" that M1 never built (M1
shipped reprovision-*in-place*). So the upload lane shipped its consumers ahead of a
producer that does not exist; an `upload`-kind rootfs profile is a guaranteed provision
failure today, which #110 fences at the tool boundary (`validate_rootfs_reference` rejects
`kind:upload` "until #111").

This design adds that producer — `systems.define` — and the `defined → provisioning`
admission path that consumes it, and lifts the #110 fence in the one lane that now has a
real upload window while keeping it in the lanes that do not.

## 2. Scope

In scope:

- `src/kdive/mcp/tools/systems.py` — a new `systems.define` tool and handler-free
  `define_system` function: insert a System in `DEFINED` for a `granted` Allocation, flip
  the Allocation `granted → active`, store the validated profile, audit both transitions.
  Operator-only. Returns a **System envelope** (no job — define does no provider work).
- `src/kdive/mcp/tools/systems.py` — `systems.provision` (`provision_system`) gains the
  `defined → provisioning` **admission** branch: when a `DEFINED` System already exists for
  the allocation, transition it to `provisioning` using its **stored** profile and enqueue
  the `provision` job. The `profile` argument becomes **optional** (the create lane requires
  it; the admit-a-`DEFINED`-System lane ignores it — the stored profile is the system of
  record, ADR-0025 decision 7).
- `src/kdive/providers/local_libvirt/provisioning.py` — split rootfs validation:
  `validate_rootfs_reference` checks only **static well-formedness** (url `sha256` format,
  catalog-name existence) and no longer rejects `upload`; a new **lane** guard rejects
  `upload` only where there is no upload window (the `systems.provision` *create* branch and
  `systems.reprovision`). The worker's `render_domain_xml` path therefore renders an
  `upload` rootfs for an admitted `DEFINED` System.
- ADR-0025 and ADR-0048 amendments (§7 below).
- `tests/mcp/test_create_upload_tool.py`, `tests/reconciler/test_upload_reaper.py` — replace
  the directly-seeded `DEFINED` fixtures with `systems.define`, exercising the producer.
- a new end-to-end **reachability** test: `define → create_upload → provision → handler`
  drives an `upload`-kind System to `ready` with its rootfs committed.
- comment hygiene: the `#111` forward-plumbing comments in the consumers above become
  live-path descriptions.

Out of scope (unchanged):

- Multipart/`> 5 GiB` uploads (#112), the install/boot plane, the rootfs *fetch* for
  `url`/`catalog` (still the next spec's concern — `resolve_rootfs_path` returns a staging
  path; existence of an unfetched image is not this spec's problem).
- Defining a System for `path`/`url`/`catalog` rootfs kinds is *permitted* but pointless
  (no upload step); the motivating and tested case is `upload`.

## 3. The lifecycle, end to end

```
allocations.request ──► Allocation: granted
        │
        ▼  systems.define(allocation_id, profile{rootfs: upload})     [operator]
   System: DEFINED            Allocation: granted ──► active
        │
        ▼  artifacts.create_upload(owner_kind=system, owner_id, [rootfs])   [operator]
   presigned PUT minted; upload manifest persisted (TTL deadline)
        │
        ▼  agent PUTs the qcow2 to the System-owned key
        │
        ▼  systems.provision(allocation_id)                            [operator]
   System: DEFINED ──► PROVISIONING            (Allocation already active — untouched)
   provision job enqueued
        │
        ▼  provision handler  (worker, fake provider in tests)
   render+define domain  ──►  provisioning → ready
   _commit_uploaded_rootfs: HEAD the object, write the write-once artifacts row,
   delete the upload manifest (reaper now exempts the object)
   System: READY
```

If the operator never provisions, the upload manifest's TTL deadline lapses and the
reconciler's `_repair_abandoned_uploads` systems arm prefix-reaps the uncommitted object
(ADR-0048 §6) — unchanged; this spec just makes that owner reachable through `define`
rather than a seeded fixture.

## 4. `systems.define`

Signature: `systems.define(allocation_id: str, profile: dict) -> ToolResponse`. Operator
role on the allocation's project. Body, in one transaction under
`PROJECT → ALLOCATION` advisory locks (the global lock order, ADR-0040 §1):

1. Parse + validate the profile (`ProvisioningProfile.parse` then `validate_profile`). A
   structural failure or unsupported `domain_xml_params` → `configuration_error`. **No**
   lane guard — `define` is the one tool that admits an `upload` rootfs (its purpose); it
   also admits `path`/`url`/`catalog`.
2. Resolve the allocation (probe outside the lock for the project key; re-read under it).
   Missing/foreign → not-found-shaped `configuration_error`. `require_role(OPERATOR)`.
3. **Find-or-return** the existing System for the allocation (one System per Allocation,
   M0). If one exists:
   - `DEFINED` → return its envelope (idempotent re-define).
   - any other state → `configuration_error` with `current_status` (already past `defined`;
     a second System is not minted).
4. If the allocation is not `granted` → `configuration_error` with `current_status`.
5. Enforce the per-project `max_concurrent_systems` quota under the held project lock
   (fail-closed; a `DEFINED` System occupies a slot — it is in `_NON_TERMINAL_SYSTEM`).
   Over quota → `quota_exceeded`.
6. `SYSTEMS.insert(System(state=DEFINED, provisioning_profile=profile.model_dump(by_alias)))`;
   audit `->defined`.
7. `ALLOCATIONS.update_state(granted → active)`; audit `granted->active`.
8. Return `ToolResponse.success(system_id, "defined",
   suggested_next_actions=["artifacts.create_upload", "systems.provision"])`.

Why flip `granted → active` here (not at provision): a `DEFINED` System **exists on the
host slot** the instant its row is written — exactly the condition ADR-0025 decision 2
attaches `active` to ("marks 'a System exists on this host slot' the instant the row
exists, even if provisioning later fails"). Doing it under the allocation lock makes a
concurrent `allocations.release` serialize: either release wins (allocation `released`;
a later `define`/`provision` sees a non-`granted` allocation and refuses) or define wins
(System `DEFINED`, allocation `active`; a later release drives `active → releasing →
released` and the reconciler tears the orphaned System down — it has no domain yet, so the
teardown is trivial). Leaving the allocation `granted` while a System exists is the precise
window ADR-0025 decision 2's rejected alternative warns against.

`systems.define` returns a **System envelope**, not a job handle: define does no provider
work (no domain, no slow libvirt call), so there is nothing to poll. This differs from
`systems.provision`, which returns a job handle because it enqueues provider work.

## 5. `systems.provision` admits a `DEFINED` System

`provision_system` keeps its find-or-create shape and gains a third case. `profile` is now
`dict | None`:

- **No System for the allocation (create lane):** `profile` is **required** (missing →
  `configuration_error`). Reject `upload` here via the lane guard — an upload with no prior
  `define` window can never have a staged object, so fail fast rather than insert a System
  and dead-letter at commit. Otherwise create the System `PROVISIONING`, flip the allocation
  `granted → active`, enqueue. (Unchanged from today except the explicit `upload` rejection
  moves here from `validate_rootfs_reference`.)
- **A `DEFINED` System exists (admit lane):** transition `defined → provisioning` under the
  per-allocation lock, enqueue the `provision` job keyed `"{allocation_id}:provision"` with
  the System id, audit `defined->provisioning`. The **stored** profile is provisioned; any
  passed `profile` is ignored (ADR-0025 decision 7 — the row is the profile's system of
  record; this also avoids re-running the create-lane `upload` rejection on the stored
  upload profile). The allocation is already `active` (flipped at define) — **not** touched.
- **A non-terminal, non-`defined` System exists (retry lane):** re-enqueue the `provision`
  job without a state change (unchanged idempotent-retry behavior).
- **A terminal System exists:** `configuration_error` with `current_status` (unchanged).

The `provision` **handler** is unchanged: it requires the System to be `PROVISIONING` on
entry (now reached from either `defined` or a fresh insert), renders the domain (the
worker's `render_domain_xml → validate_profile → validate_rootfs_reference` now accepts the
`upload` reference), and on `provisioning → ready` runs `_commit_uploaded_rootfs`.

## 6. Splitting static validation from lane admissibility

`validate_rootfs_reference(rootfs)` is called from three places: the `systems.provision`
tool boundary (via `validate_profile`), the `systems.reprovision` tool boundary (via
`validate_profile`), and the worker's `render_domain_xml` (via `validate_profile`). Today it
rejects `upload` everywhere — including the worker, which is why an admitted `DEFINED`
System could not render.

The fix separates two questions:

- **Static well-formedness** (is this reference syntactically resolvable?): `url` `sha256`
  must be `sha256:<64-hex>`; `catalog` name must exist. `path`/`upload` need no static
  check. This is what `render_domain_xml`/`resolve_rootfs_path` require, so it stays in
  `validate_rootfs_reference` and `validate_profile`. `upload` is **well-formed** (no fields
  to check) and is no longer rejected here.
- **Lane admissibility** (does this lane have an upload window for an `upload` rootfs?): only
  `define` does. A small guard — `reject_rootfs_without_upload_window(rootfs)` — raises
  `configuration_error` for `kind:upload` and is called by the `systems.provision` *create*
  branch and by `systems.reprovision` (a `ready` System has no upload window;
  `_owner_accepts_upload` admits an upload only for a `DEFINED` System). It is **not** called
  by `define` or by the worker.

This preserves the #110 fail-fast for the one-step-provision and reprovision lanes (the
behavior `tests/.../test_*` pins) while letting the define-then-provision lane through.

## 7. ADR amendments

- **ADR-0025** decision 1 and its first rejected alternative ("Insert the System as
  `defined`…") are narrowed: `defined` is no longer "reserved for a future path / M0 never
  writes it" but "materialized by `systems.define` (#111) for the create-without-provision /
  rootfs-upload lane; `systems.provision`'s *create* lane still inserts directly at
  `provisioning` (admission-style, decision 1's reasoning holds for the one-step path)." A
  new decision records the `define` producer, the `granted → active`-at-define flip, and the
  `defined → provisioning` admission branch with the stored-profile rule.
- **ADR-0048** §5's forward-plumbing **Note** (the `upload` kind "awaits its producer…
  `validate_rootfs_reference` rejects an `upload` reference") becomes: the producer is
  `systems.define` (#111); the boundary rejection is narrowed to the lanes without an upload
  window; the lane is live end-to-end.

## 8. Testing

TDD, driving the handler/tool functions directly with an injected pool + `RequestContext`
(the repo's prescribed boundary), `migrated_url` for the DB (Docker-gated, not `live_vm`),
a `FakeProvisioning`, and `minio_store` for the object HEAD.

- **`systems.define`**: inserts `DEFINED` for a `granted` allocation and flips it to
  `active` (assert both rows + the two audit rows); re-define is idempotent (same System,
  no second row, allocation stays `active`); a non-`granted` allocation →
  `configuration_error`; an existing non-`DEFINED` System → `configuration_error` (no second
  System); over-quota → `quota_exceeded`; non-operator → authorization error; a foreign /
  missing allocation → not-found-shaped `configuration_error`; an `upload` profile is
  **accepted** (the producer's whole point); a structurally invalid profile →
  `configuration_error`.
- **`systems.provision` admission**: a `DEFINED` System is driven `defined → provisioning`
  with no `profile` argument and the job is enqueued with its id; the create lane still
  rejects an `upload` profile (`configuration_error`); a create call with `profile=None` and
  no existing System → `configuration_error`.
- **Lane split**: `validate_rootfs_reference` accepts `upload` (well-formed) and still
  rejects a malformed `url` sha256 / unknown catalog name; `render_domain_xml` renders an
  `upload` rootfs to its staging path; the create-lane / reprovision guard rejects `upload`.
- **End-to-end reachability** (`tests/integration/` or alongside the systems tests): seed
  resource + `granted` allocation → `systems.define(upload profile)` → `create_upload` →
  stage the object in `minio_store` → `systems.provision(allocation_id)` → run
  `provision_handler` with `FakeProvisioning` → assert System `ready`, exactly one
  systems-owned write-once artifacts row at the rootfs key, upload manifest deleted. This is
  the test the issue asks for; it replaces the seeded-`DEFINED` shortcut with the real lane.
- **Rewritten fixtures**: `test_create_upload_tool.py`'s `DEFINED` seeds and
  `test_upload_reaper.py`'s `DEFINED` seeds call `systems.define` (from a `granted`
  allocation) instead of `SYSTEMS.insert(state=DEFINED)`, so the producer — not a fixture —
  puts the System in `DEFINED`. Gating is unchanged (no `live_vm`, no un-gating).

## 9. Failure modes & edges

- **Re-define / re-provision races:** both serialize on the per-allocation lock; a second
  `define` returns the existing `DEFINED` System, a `provision` after `define` sees the
  `DEFINED` System and admits it once (the `defined → provisioning` edge is one-way; a
  re-issue lands in the retry lane).
- **Release mid-`define`:** serialized on the allocation lock (§4).
- **`upload` profile defined but never uploaded, then provisioned:** the handler's
  `_commit_uploaded_rootfs` HEAD returns `None` → `configuration_error`, the
  `provisioning → ready` transaction rolls back, the System stays `provisioning` for an
  idempotent retry (the existing
  `test_provision_handler_absent_uploaded_rootfs_fails_config_error` already pins this; #111
  makes it reachable through the real producer).
- **`upload` in the create or reprovision lane:** rejected at the boundary
  (`configuration_error`) — fail fast, no System inserted / no profile replaced.
- **Quota:** a `DEFINED` System counts against `max_concurrent_systems`, so define is
  refused at the cap (fail-closed, no quota row → over quota) exactly as provision is.

## 10. Acceptance criteria

1. `systems.define(granted_allocation, profile)` inserts a `DEFINED` System and flips the
   allocation `granted → active`, idempotently, operator-only, quota-enforced.
2. `systems.provision(allocation_id)` (no profile) drives an existing `DEFINED` System
   `defined → provisioning` and enqueues its provision job; the handler then drives it to
   `ready` and commits an `upload` rootfs.
3. The `upload` rootfs kind is rejected only where there is no upload window (one-step
   provision, reprovision), accepted via `define`, and renders in the worker.
4. The end-to-end reachability test passes; the two `DEFINED`-seeding fixtures are rewritten
   to use `systems.define`; the `#111` forward-plumbing comments become live-path comments.
5. ADR-0025 and ADR-0048 describe the real producer.
6. `just ci` is green; no gating weakened.

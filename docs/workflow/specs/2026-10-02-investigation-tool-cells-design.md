# Investigation and artifact MCP tool cells (#3096)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment. Builds on the #2811 carrier
([spec](2026-10-02-core-tool-cells-design.md)) and the #3095 catalog carrier
([spec](2026-10-02-catalog-tool-cells-design.md)).

## Problem

Owner group 3096 in `scripts/coverage_campaign/obligations.toml` holds 13 service tools:
`investigations.{open,get,list,set,link,unlink,close,complete_rootfs_upload}` and
`artifacts.{create_investigation_upload,create_run_upload,fetch_raw,get,list}`. Each tool has every
boundary (authentication, authorization, project-isolation, validation), which gives 65 scenarios
and 260 cells, none bound to a test.

Three facts shape the carrier:

- An investigation can be closed but never deleted, and an unbound Run can be canceled but not
  removed. The default per-project snapshot (ADR-0722 §4) therefore cannot equal itself after a
  cell that opens one.
- `artifacts.list` and `artifacts.get` read redacted System-owned artifacts. Only a worker job on
  a running System writes those (console rotation); no MCP tool does.
- `artifacts.fetch_raw` needs a succeeded Run with a vmlinux.

## Scope

In scope:

1. A live carrier, `tests/integration/test_investigation_tool_cells_live.py::test_investigation_tool_cell`.
   It is parametrized over the 260 cells and framed by `run_tool_cell`, `prove_functional` and
   `prove_rejection`, with no change to `tool_cells.py`.
2. 65 `[implementations]` entries binding group 3096's scenarios to that node. The contract test
   names the node and the tools.
3. A runbook section beside the core and catalog ones.
4. A live run of both configurations on a disposable lab host, re-proving #2811's 56 cells and
   #3095's 152.

Out of scope (operator-approved, 2026-10-03): catalog/config tools (#3095), run/system/job tools
(#3097), allocation/accounting/report tools (#3098), CLI (#3099), and recovery-tool cells plus a
lane with a reachable worker-death authority (#2812). The carrier may call `runs.create`,
`runs.complete_build`, `runs.cancel`, `allocations.request`, `allocations.release` and
`systems.provision` as fixtures. There is no product, `contract.py`, harness or ADR change.

### Live snapshot

Each cell works in a fresh project `cov-<8 hex>` (P). Its snapshot is `project_state(P)` with two
changes:

- The `investigations` and `runs` tables are replaced by the full row text of P's **live**
  investigations (`open`, `active`) and **live** runs (`created`, `running`).
- It adds the row text of the `upload_manifests` and `artifacts` rows owned by those live
  investigations and runs. Neither table has a `project` column.

A closed investigation and a canceled or succeeded Run are history, as #3095's released
allocation is. Cleanup is proven by the cell's owned investigation reaching `closed` and its Runs
reaching a terminal state, so the snapshot after the cell equals the one before it. That is the
narrower snapshot ADR-0722 §4 permits; it drops no live row.

In a rejection cell the target investigation or Run is live throughout the call. Its full row and
its manifests and artifacts are in the snapshot, so a fail-open `set`, `link`, `unlink`, `close`,
upload or finalize changes the snapshot and fails `unchanged-state`.

### Fixtures

Every fixture is created as the cell project's contributor, over `direct`. Each one is undone by
an exit step that also runs when the body fails; a cleanup problem is attached to the body's
error rather than replacing it.

- **Investigation.** `investigations.open` with a title, a description and one external ref. On
  exit, every Run it holds is canceled with `runs.cancel` and the investigation is closed with
  `investigations.close`.
- **Unbound Run.** `runs.create` with `target_kind="local-libvirt"` and build profile
  `{schema_version: 1, arch: x86_64}`. This takes no allocation and flips the investigation to
  `active`.
- **Built Run** (for `fetch_raw`). An unbound Run whose `artifacts.create_run_upload` declares
  two objects:
  - `kernel`: the synthetic bundle `tests.mcp.complete_build_support.valid_combined_kernel_tar()`;
  - `vmlinux`: that bundle's embedded ELF, given a two-entry section-header table with a
    `SHT_NOTE` over its GNU build-id note.

  Both are PUT through the presigned URLs, and then `runs.complete_build(build_id=
  "0123456789abcdef")` runs. The Run ends `succeeded`. Its build is published to the
  investigation, and the investigation's close hands it to the reconciler's build GC.
- **Uploaded bytes.** The cell PUTs 4 KiB of fresh random bytes. Bytes the cell PUT that no
  catalog row has adopted (an unfinalized upload) are purged from the object store on exit, with
  every version. Bytes a finalize or a build adopted belong to the product's reclaim, which the
  close schedules (`rootfs_cleanup_pending_at`, `cleanup_pending_at`). The cell records that
  pending marker instead of deleting product-owned objects.
- **Session System** (for `artifacts.list` and `artifacts.get`). This is one per pytest session,
  created on first use:
  1. Request a 1-vCPU, 1 GiB allocation in the bring-up's funded `KDIVE_PROJECT`, as its operator.
  2. Provision the staged `fedora-kdive-ready-44` with the catalog profile, to `ready`.
  3. Wait until the System holds at least one redacted console part and the set has not changed
     for one minute. Probed on the lab host, a ready guest seals its first part within about a
     minute and then stays quiet.

  A session finalizer releases the allocation and proves teardown with `release_and_verify` (the
  domain and disks are gone and the capacity is returned). Cells read its artifacts as a viewer of
  `KDIVE_PROJECT`; they do not own it. Their snapshot is P's plus the row text of the System's
  artifact rows.

### Functional effects

Independent sources are the evidence database (read-only session), the object store (boto3 with
the stack's credentials), the bytes the cell generated, and the synthetic build fixture.

- `investigations.open`: the envelope id names a row whose project, title, description, external
  refs and `open` state equal the arguments; a viewer of another fresh project gets `not_found`
  from `investigations.get` and does not list it.
- `investigations.get`: for an investigation seeded with a title, a description, two refs and an
  unbound Run, `data` equals the row (project, title, description, refs, state `active`, summary),
  and `data.runs` equals the ids of the `runs` rows bound to it.
- `investigations.list`: as a viewer, with three seeded investigations (one closed):
  - `request.project=P`, and no filter at all, both return exactly P's rows in
    `created_at DESC, id DESC` order, although other projects' investigations exist;
  - `state=closed` returns only the closed one;
  - paging with `limit=1` through `next_cursor` returns the same order.
- `investigations.set`: the row's title and description change; its refs, state, project, summary
  and creation time do not.
- `investigations.link`: the row's refs equal the seeded ref plus the linked one, and a second
  investigation in P is unchanged.
- `investigations.unlink`: of two seeded refs only the selected one disappears, and a second
  investigation is unchanged.
- `investigations.close`: the row is `closed` with the given summary, and `investigations.get`
  returns that summary.
- `investigations.complete_rootfs_upload`: after a minted window and a PUT of known bytes:
  - `data.checksum_sha256` is the base64 SHA-256 of those bytes;
  - the stored object at `data.object_key` has those bytes and lies under the investigation's
    prefix;
  - one `artifacts` row of retention class `rootfs` is owned by the investigation;
  - the upload manifest is gone;
  - after the close, `rootfs_cleanup_pending_at` is set.
- `artifacts.create_investigation_upload`: the returned item's PUT succeeds with exactly
  `required_headers`; the stored bytes equal the generated ones; the `upload_manifests` row is
  owned by the investigation, its prefix is `local/investigations/<id>/`, it contains the key, and
  its manifest declares the bytes' digest.
- `artifacts.create_run_upload`: the same for a `kernel` declaration on an unbound Run, at
  `local/runs/<run>/kernel` and owned by the Run.
- `artifacts.fetch_raw`: for the built Run, `asset=vmlinux` returns a presigned URL whose download
  equals the uploaded vmlinux bytes, and `data.size_bytes` equals their length. After the close the
  Run is `succeeded` and `cleanup_pending_at` is set.
- `artifacts.list`: every page (limit 1) of the session System's listing, in order, equals its
  redacted `artifacts` rows ordered `created_at DESC, id DESC`. The rows are read from the database
  before and after the listing, and the listing is retried once if rotation adds a part between
  the two reads.
- `artifacts.get`: for the System's oldest redacted part, with its stored object read from the store
  (inflated when its metadata says gzip):
  - the object's stored `sensitivity` is `redacted`;
  - a forward window `[0, 512)` and a backward tail window equal those byte ranges, and
    `size_bytes` equals the body length;
  - `find` of a literal taken from the body reports `match_offset` and `match_line` at the body's
    first occurrence;
  - `find` of a term absent from the body reports `match_found` false.

### Rejection cells

| Boundary | Grants and target | Accepted categories |
|---|---|---|
| authentication | a viewer of P; harmless arguments (absent ids, a viewer's `investigations.open`) | HTTP 401 for the foreign signature; the issued-token control is not refused |
| authorization | for the 9 mutating tools, a viewer of P against a setup investigation or Run (the `open` call names P); for `investigations.get`, a member of P with no role; for `artifacts.{list,get}`, a member of `KDIVE_PROJECT` with no role against the session System | `authorization_denied` |
| project-isolation | an operator of another fresh project against the same targets | `not_found` for the investigation reads and edits, `fetch_raw` and `artifacts.get`; `authorization_denied` for `investigations.open`; `configuration_error` for `create_investigation_upload`, `create_run_upload` and `complete_rootfs_upload` |
| validation | a missing required argument, a mistyped field, or an unknown enum value; the token holds the lowest grant that makes the tool visible (contributor of P for the 9 mutating tools, viewer otherwise) | ADR-0722 §3 as amended |

Notes on the table:

- **Non-member `configuration_error`.** For a non-member, the three upload tools return the same
  `configuration_error` they return for an absent owner (`mcp/tools/catalog/artifacts/uploads.py`
  `_create_upload`, `complete_rootfs_upload.py`). The isolation is real and leaks nothing, so
  `configuration_error` is the closed set for those cells. The target is a valid, live owner,
  and that is the only reason left for the call to fail.
- **Filtering list tools.** `investigations.list` (authorization, project-isolation) and
  `artifacts.list` (project-isolation) filter instead of rejecting: the caller gets an empty `ok`
  page (`services/investigations/read.py`, `services/artifacts/listing.py`). ADR-0722 §3 cannot be
  met by these 12 cells. They run through the harness and fail honestly, recording the empty page.
  #3108 decides what isolation evidence a filtering list tool owes. These cells are not coverage.

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on a disposable lab host: one stack, with the two lanes
     run in turn, and KVM available for the session System;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes;
   - the shared stack left as found. That covers other projects' rows, the funded project's
     capacity (the session System is released and its domain and disks proven gone), and the
     object store, which holds no bytes the cell PUT that no product row owns.
3. **Accepted failure classes:**
   - Closed investigations, canceled or succeeded Runs, their audit rows and their unexpired
     upload manifests stay as history. Manifests are reaped at their deadline (24 h). Build and
     rootfs objects adopted by a finalize are reclaimed by the reconciler after the close's grace
     period (one day). The stack wipe after the proof removes all of it.
   - A cell killed mid-run can leave an open investigation, a created Run or the session System.
     The wipe clears them.
   - Console rotation adding a part during an `artifacts.list` read is retried once. A second
     change fails the cell rather than loosening the comparison.
   - Redaction itself (a secret replaced in the stored bytes) is not driven. The cell proves the
     stored object is labelled `redacted` and that the served bytes are those stored bytes. The
     redactor is unit-tested where it is written.
   - Concurrent carriers on one stack are not modelled.
4. **Covered elsewhere:**
   - run, system, job and allocation tools' own cells (#3097, #3098);
   - recovery tools (#2812);
   - the filtering list tools' isolation evidence (#3108).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| bindings | focused-test | `test_coverage_contract.py`: the 260 group-3096 cells bind to the new node, and the unbound set shrinks by exactly them |
| live snapshot and fixtures | task-test-not-applicable | they act only against a live stack's database and object store; proven by the two-lane lab run (`qualify` over the tool cells) |
| live cells | task-test-not-applicable | the cells need a live stack; proven by the same run |
| runbook section | task-test-not-applicable | prose; `just docs-check` covers links and paths |

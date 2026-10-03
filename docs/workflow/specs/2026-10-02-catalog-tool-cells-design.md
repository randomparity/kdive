# Catalog and configuration MCP tool cells (#3095)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment. Builds on the #2811 carrier
([spec](2026-10-02-core-tool-cells-design.md)).

## Problem

Owner group 3095 in `scripts/coverage_campaign/obligations.toml` holds 11 service tools, 38
scenarios and 152 cells, none bound to a test: `images.{delete,describe,kernel_config,list,upload}`,
`shapes.{delete,list,set}` and `resources.{availability,describe,list}`. The #2811 carrier
(`tests/integration/live_stack/tool_cells.py`) proves configuration, both exposures and the four
rejection boundaries, but its functional frame assumes a read-only tool and its rejection frame
assumes the protected state already exists. Four of these tools write catalog state, and two
rejection boundaries need a private image to exist first. Two tables these tools write have no
`project` column, so the default per-project snapshot (ADR-0722 §4) cannot see them:
`system_shapes` (global) and `image_catalog` (keyed by `owner`).

## Scope

In scope:

1. Two extensions to `tool_cells.py`, both backward compatible with the #2811 cells:
   - `prove_functional` records the cell's owned resources: a functional body may return an
     `owned` list, which moves from the `effect` observation into the `cleanup` assertion. The
     before/after snapshot equality stays the cleanup proof.
   - `prove_rejection` takes an optional `setup`: an async context manager factory yielding
     argument overrides. The harness snapshots before `setup`, runs the rejected call inside it
     (the `unchanged-state` comparison brackets only the call), and snapshots again after the
     context exits; `cleanup` requires that last snapshot to equal the first. Without `setup`
     the behaviour is today's.
2. A live carrier, `tests/integration/test_catalog_tool_cells_live.py::test_catalog_tool_cell`,
   parametrized over the 152 cells, with a catalog snapshot: the default per-project snapshot
   plus `image_catalog` rows owned by the cell's project and every `system_shapes` row (count and
   row-text SHA-256, read-only session). It extends the default snapshot rather than replacing it,
   which ADR-0722 §4 leaves to the cell.
3. 38 `[implementations]` entries binding group 3095's scenarios to that node; the contract test
   names the new node and tools.
4. The runbook's tool-cell section gains the catalog carrier and its one precondition.
5. A live run of both configurations on a disposable lab host, with #2811's 56 cells re-proved.

Out of scope (operator-approved, 2026-10-03): investigation/artifact tools (#3096), run/system/job
tools (#3097), allocation/accounting/report tools (#3098), CLI (#3099), recovery-tool cells and a
lane with a reachable worker-death authority (#2812). No `contract.py` or product change.

### Precondition

A public staged-path image declared in `systems.toml` with its build-fs siblings
(`<qcow2>.provenance.json`, `<qcow2>.config`) on disk:
`examples/local-libvirt/build-image.sh fedora-kdive-ready-44`. A cell that needs it and finds none
records `blocked` with that instruction, as `scenario.acquire_image` does.

### Quarantined upload fixture

`images.upload` registers an object that already sits in the object store. The ADR-0048 upload
reassembly writes such an object with metadata `sensitivity=sensitive`,
`retention-class=build` (`src/kdive/artifacts/uploads/reassembly.py`); the server refuses one
without it. The carrier PUTs the precondition image's qcow2 bytes with that metadata to
`uploads/q/<project>/<name>.qcow2` through boto3 (credentials from the stack's `AWS_*`), and
deletes every version of that key and of the registered image's published object when the cell
ends, then lists each key and fails `cleanup` if a version remains. The published object would
otherwise wait for the reconciler's leaked-object sweep. A cleanup failure after a failed body
does not replace the body's error.

### Admission fixtures

Two observations need admission and occupied capacity: `shapes.set` ("use its resource dimensions
in admission") and `resources.availability` ("controlled resource state"). Both use
`allocations.request` and `allocations.release` as fixtures, as the operator, in the project the
bring-up funded (`KDIVE_PROJECT`); those tools' own cells stay with #3098. Admission is
synchronous: a grant or a refusal returns at once, and a release answers `released`. The released
row stays as history in the funded project, outside the cell's snapshot; the fixture proves its
cleanup by the `released` state and the occupancy it gives back.

### Functional effects

Each functional cell uses a fresh project `cov-<8 hex>`. Independent sources: the evidence
database (read-only), `systems.toml` with the build-fs siblings, the test host's
`/proc/meminfo` and CPU count, and the bytes the cell itself uploaded.

| Tool | Effect asserted |
|---|---|
| `images.describe` | identity, format, root device, visibility and capabilities equal the `systems.toml` block; `digest` is the SHA-256 of the qcow2 file; `provenance` equals the sidecar's; the computed `capability_signals` carry the sidecar's makedumpfile and drgn versions and boot-kernel count |
| `images.kernel_config` | the presigned download's SHA-256 and length equal `<qcow2>.config`'s; `default_kernel_version` equals the sidecar's |
| `images.list` | after the cell uploads a private image to project A, A's viewer lists exactly the public rows plus A's rows (database), including the new image; a viewer of a fresh project B lists exactly the public rows |
| `images.upload` | the row is `registered`, `private`, owned by the project, its digest equals the uploaded bytes' SHA-256 and its provenance names the quarantine key; the published object exists; another project's viewer gets `not_found` from `images.describe` |
| `images.delete` | the uploaded row is gone and every other `image_catalog` id is unchanged; `images.describe` answers `not_found` |
| `shapes.list` | names, dimensions and PCIe match equal the `system_shapes` rows, sorted by name |
| `shapes.set` | a `cov-` shape set one vCPU above every visible host's ceiling reads back from the catalog, and admission refuses it naming `vcpus`; re-set to 1 vCPU, 1 GiB, 1 GB it reads back updated and admission grants an allocation sized exactly so; the allocation is released and the shape deleted |
| `shapes.delete` | a `cov-` shape created first is gone and every other row is unchanged |
| `resources.list` | ids, kind, status, arch, vCPUs and memory equal the database rows visible to the caller (global, owned or allow-listed) |
| `resources.describe` | each visible resource's kind, status, pool, cost class, host URI, arch, vCPUs, memory and transports equal its row; the local discovered host's vCPUs and memory equal this host's CPU count and `MemTotal` |
| `resources.availability` | three reads — idle, holding one 1-vCPU allocation, after its release — each equal the database per host (cap, in-use as granted/active/releasing allocations, headroom, schedulability, and `fits` as the shapes whose vCPUs and memory fit a schedulable host with headroom) and in queue depth (`requested` rows); the held host's in-use rises by one and the released read equals the idle one |

### Rejection cells

| Boundary | Tools | Grants and arguments |
|---|---|---|
| authentication | all 11 | viewer of the cell's project; valid arguments that change nothing for the issued-token control (an absent quarantine key, an unknown image id, a shape write the viewer may not make) |
| validation | all but `shapes.list` | a missing required argument or a mistyped `request` field; the token holds the lowest grant that makes the tool visible (operator for `images.{upload,delete}`, `platform_operator` for `shapes.{set,delete}`, viewer otherwise), because `tools.invoke` reports `field_errors` only for a visible tool |
| authorization | `images.{upload,delete}`, `shapes.{set,delete}` | viewer (not operator) of the image's project; `platform_auditor` without `platform_operator`, aimed at a real preset |
| project-isolation | `images.{upload,delete}` | a member of another project only |

The two `images.delete` cells need a private image: `setup` uploads one as the project's operator
and deletes it on exit. Every category accepted is `authorization_denied`.

### Failure model

1. **Actors and deployments:** an operator running the live tier on a disposable lab host (one
   stack, both lanes run in turn); CI runs only the unit tests.
2. **Invariants and assets at stake:** honest per-cell outcomes; the shared stack's catalog is
   left as found (presets, public images, other projects' rows).
3. **Accepted failure classes:** a cell killed mid-run can leave a `cov-` shape, a private image or
   objects behind (the stack is wiped after the proof); concurrent writers to the catalog are not
   modelled (one carrier runs at a time); a remote-libvirt host's facts are compared only with its
   row; a shape with a PCIe match is left out of the `fits` comparison (the seeded presets carry
   none); queue depth is compared with the database but not driven (on the lab stack a second
   request with `on_capacity="queue"` answered `quota_exceeded`, not a queued row); the provider
   capability planes of `resources.describe` are recorded but not compared (no source but the
   provider runtime); released fixture allocations stay as history in the funded project.
4. **Covered elsewhere:** the recovery tools' cells (#2812); the upload ingest that produces a
   quarantined object (the `artifacts.*` upload tools, #3096).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| `prove_functional` owned resources | focused-test | `test_tool_cells.py`: a body returning `owned` records it under `cleanup` and not under `effect` |
| `prove_rejection` setup bracket | focused-test | `test_tool_cells.py`: overrides reach the call; a setup that leaves state fails `cleanup`; a call that changes state under a setup fails `unchanged-state` |
| catalog bindings | focused-test | `test_coverage_contract.py`: the 152 group-3095 cells bind to the new node; unbound set shrinks by exactly them |
| live cells | task-test-not-applicable | the cells need a live stack; proven by the two-lane lab run, `qualify` over 208 tool cells |
| runbook section | task-test-not-applicable | prose; `just docs-check` covers links and paths |

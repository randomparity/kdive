# Qualify the 32 authority HTTP functional tool cells (#3186) — design

Design proposal for issue #3186 (parent #3152, epic #2803), on source baseline `a0d041a85`.

- Frozen scope: [WORK:SCOPE](https://github.com/randomparity/kdive/issues/3186#issuecomment-6318748334),
  token `q3186-d79f717c`. The operator approved the scope and its exclusion owners on 2026-10-16.
- Decision record: [ADR-0788](../../adr/0788-authority-tool-cells-bind-the-serving-authority.md)
  (Accepted with this change).
- Status: approved by the operator on 2026-10-21 (decisions below) and implemented.

## Problem

The run and break-glass HTTP carriers record 32 x86_64 functional cells as `blocked` before any
mutation (`test_run_tool_cells_live.py` `_functional`, `test_breakglass_tool_cells_live.py`
`_blocked`). The cells are 4 tools × `default`/`recovery` × `direct`/`gateway` ×
`local-libvirt`/`remote-libvirt`. The tools are `runs.release_external_boot`,
`ops.force_release`, `ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict`.
Three things have landed since those cells were written:

- the authority System frame, `on_authority_system` (#3184, #3209);
- the fault arms in `authority_faults.py` (#3185, ADR-0787);
- the lane bring-up fixes (#3215, #3296, #3310, #3315).

Two identity gaps also remain (ADR-0788): `ops.force_release` requires no `authority` role, and
a remote cell records the test host's authority instead of the provider host's.

## Scope

In scope, all test harness and coverage contract:

1. Rebind the 32 functional cells to `on_authority_system` and the #3185 arms, called through
   the cell's `HttpCaller` and exposure. Rejection and unsupported cells, and every other tool of
   both carriers, are unchanged.
2. ADR-0788: add `ops.force_release` to `role_overrides` with the `authority` role, and read the
   remote authority's revision in `run_identity` for remote authority cells.
3. Kernel and arm extensions. `start_external_boot_activation`
   (`tests/live_vm/installed_local_authority_support.py`) gains optional `kernel_tree` and
   `evidence_dir`, which it forwards to `build_and_upload_kernel`. The private `_activate` in
   `authority_faults.py` becomes public `activate`, with the same two parameters.
   `held_teardown_release` and `quarantined_orphan` gain `kernel_tree` and pass it to `activate`.
   `held_teardown_release` also gains an independent release observation (below). With
   `kernel_tree=None` the #3185 live module behaves exactly as it does today.
4. A remote revision reader beside `require_installed_authority`
   (`tests/live_vm/installed_remote_authority_support.py`).
5. Unit tests, carrier docstrings, and the runbook sections for the run, break-glass and
   authority-fault cells.

Out of scope, with owners: the WORK:SCOPE exclusions (#3138, #3143, #3144/#3148, #3130/#3131,
#3135, #2818, #2819), and the ordinary remote lane and its carriers. The open in-campaign fixes
are also out of scope: #3321 (PR #3325), #3322 (PR #3324) and #3323. This change consumes them
and does not implement them. There is no production API, schema, migration, provider-family,
architecture or scheduler change.

Surface note for the operator: items 3 and 4 touch two `tests/live_vm/` files
(`installed_local_authority_support.py`, `installed_remote_authority_support.py`), which are
outside the WORK:SCOPE surface's literal paths. Item 2 also changes the shared identity seams
`tests/integration/live_stack/evidence.py` (`run_identity`) and `scenario.py` (`run_cell`), which
every live cell uses; the change applies only to remote-libvirt cells that require `authority`.
All four are test-only and follow from criteria 1 and 4. The operator approved them (D2).

## Design

### Dispatch

The carriers keep `run_tool_cell`, `CellRun`, `HttpCaller`, `prove_rejection`, the contract and
result types, and the provenance seams. Only the functional branch changes. For each functional
cell of the four tools, the carrier picks a body from the table below. It runs that body inside
`on_authority_system(run, base_url, issuer, db_url, project=..., body=...)`. The frame proves
`effect` and `cleanup` and returns them as the record's two assertions. It also does the
following, with no change:

- gates the lane revision;
- records the image and accelerator;
- drains every job of the System;
- closes the ledger's Investigations;
- removes the fixture or tears down through the authority;
- releases the allocation;
- requires the lane inventory and the capacity in use to be unchanged.

| Tool | Local body | Remote body | Caller (all through the cell's exposure) |
|---|---|---|---|
| `runs.release_external_boot` | `_release` (new, run carrier) | `_release` | `contributor` of the frame's project |
| `ops.force_release` | `held_teardown_release` | `held_teardown_release` | `platform_admin` with no role in the project |
| `ops.resolve_recovery_orphan` | `quarantined_orphan` | `REMOTE_ORPHAN_BLOCKED` after the lane gates | `platform_admin` with no role in the project |
| `systems.resolve_external_boot_conflict` | `CONFLICT_BLOCKED` after the lane gates | `CONFLICT_BLOCKED` after the lane gates | none |

A `ToolCall` binds the cell's `HttpCaller` and token:

```python
async def call(tool: str, args: dict[str, object]) -> ToolResponse:
    result = await caller.call(tool, args, token, discover=True)
    assert isinstance(result, ToolResponse), f"{tool} answered {len(result)} envelopes"
    return result
```

It returns error envelopes unchanged, because the held arm must observe a refusal. Under
`gateway`, `discover=True` proves `tools.search` finds the tool before `tools.invoke` calls it.

Every runnable remote functional body first runs one new helper in
`authority_lane.py`, `observe_remote_authority(run, system, db_url)`. It runs
`observe_host(run, remote_host())`, so the record's `host_os` and `host_arch` are the provider
host's. It reads the frame's root image again with `remote_root`, and requires that image to be
`REMOTE_REPRESENTATIVES["fedora"]`; otherwise it stops `blocked` and names the mismatch. It then
records `guest_os` from that representative. The remote frame records only `image_sha256` and
`guest_arch` (`authority_lane.py`, `_on_remote_authority_system`). Without this helper, every
remote record would fail `input-context-mismatch` against its binding. A blocked remote body runs
`observe_host` and the lane gates without a frame.

A blocked body runs `blocked_after_gates`. A blocked cell records `blocked` with its narrower
reason. It is never a pass.

### `runs.release_external_boot` body (`_release`)

1. Load the bound `longterm` fixture with `_fixture()`. It stops `blocked` when the fixture is
   absent. On a remote cell, run `observe_remote_authority` first.
2. List the authority's state:
   - local: `authority_inventory(system_id)` roots and domain facts;
   - remote: `remote_authority_inventory(config, system_id)` volumes and domain.
3. Inside one `try`/`finally` that starts here, call
   `activate(system, kernel_tree=tree, evidence_dir=<scratch>)`. It runs the admin `op` through
   create, upload (no `vmlinux`), install and boot, then drains the activation. A remote
   activation failure stops `blocked` with the job's category and reason. Today that is
   `remote_external_boot_initrd_required` (#3323).
4. Call `runs.release_external_boot` through the cell's `ToolCall` with `{"run_id": ...}`.
   Answer a `system_job_active` refusal the same way the support helper `release_external_boot`
   does: drain the named jobs, then retry within the same bound. Then drain the release job.
5. The `finally` decides from the database, never from what the client saw. A call can raise
   after the server admitted it, and `activate` can raise after `runs.boot` was admitted.
   - If the Run has no activation, it does nothing.
   - If the Run has a release job, it drains that job.
   - If the activation exists and no release job does, it calls the support helper's admin
     release and drains it.

   A failure inside the `finally` is attached to the primary failure as a note, so the record's
   reason still names the first fault. The frame never meets a held activation that this body
   could have released.
6. Effect evidence. Each item is observed outside the tool's own envelope:
   - the release job is `succeeded` (`job_claim`);
   - `assert_root_release_completion` passes for the activate and release jobs. This covers the
     activation `recovered` with cleanup complete, the consumed cleanup receipt, the retired
     root, the terminal journal head and the reservation credit;
   - local: the private cleanup receipt is absent (`receipt_state`). The authority daemon still
     defines the System's domain. The private rootfs root lists exactly what it listed in step 2;
   - remote: the authority pool's volumes containing the System id equal the step-2 set, and the
     daemon still defines the domain.
7. Record the kernel inputs from the upload record and the fixture manifest, as `_cancel` does
   today. Extract the shared lines into one helper used by both.

### `ops.force_release` and `ops.resolve_recovery_orphan` bodies

These reuse `held_teardown_release` and `quarantined_orphan`, with the cell's `ToolCall` in place
of the #3185 module's fixed `direct` platform-admin caller, and with `kernel_tree` set to the
bound `longterm` tree. Every authority cell therefore uploads one kernel, by one mechanism; the
carriers never read `KDIVE_KERNEL_SRC`. Both arms keep their own barrier holds, budget checks,
independent observations and failure cleanup (#3185 spec, Arms A and B).

`held_teardown_release` today proves the final release only from the tool's own envelope
(`released.status == "released"`), and the frame's cleanup would release the allocation itself if
that release had not happened. So the arm gains two observations after the release, both read
outside the tool's envelope:

- `allocations.wait` with `timeout_s=0`, through the frame's own `op`, reads `released`;
- `capacity_in_use(op)` is lower than the value the arm read while the effect was held.

Both go into the `effect`.

The orphan cell proves this: an authority publication of a real quarantine; then an exact-set
`delete` disposition, by the cell's caller, of a receipt that a retry had already removed. The
deletion of a still-present receipt stays unproven (`LOCAL_PRESENT_ORPHAN_BLOCKED`), and the cell
discloses that in its `effect`.

### Identity (ADR-0788)

- `obligations.toml`: the 3112 provider group gains
  `[groups.role_overrides] "ops.force_release" = ["server", "worker", "reconciler", "authority"]`.
- `run_identity(base_url, *, remote_authority=None)`. `run_cell` passes a reader only for a
  `remote-libvirt` cell whose `roles` include `authority`.
  - The reader loads the remote proof config. If the config is unset it returns `None`: the
    remote revision is unknown.
  - Otherwise it reads the remote revision file as the authority account and resolves it in the
    checkout. A failed read or resolve raises.
- For such a cell, `authority` is chosen from the revisions that were read: the remote one, and
  the test host's one when an authority is installed there.
  - If either is not the candidate, the non-candidate one is recorded. A stale authority on
    either host then fails `deployed-revision-mismatch`.
  - Otherwise, if the remote revision was read, it is recorded.
  - Otherwise (remote unknown, test-host authority current or absent) no `authority` is
    recorded, and the cell fails `deployed-role-missing`. The test host's authority is never
    recorded as the remote cell's serving authority.
- Every other cell keeps today's rule unchanged.
- `identity_problems`, `build_record` and `qualify` are unchanged.
- A raised identity read happens before `run_cell`'s `try`, so that cell writes no record, and
  `qualify` reports it not-run with `missing-result`.

### Input bindings

Bindings are unchanged in code.

- Local cells bind `LANE_IMAGES["x86_64"]`. That is the image the fixture frame acquires.
- Remote cells bind `REMOTE_REPRESENTATIVES["fedora"]` on the host that `REMOTE_PROVIDER_SSH`
  names. For this session that variable names the remote authority host. The lane stages that
  representative as its single registered public remote-libvirt image with an inspected root, so
  its catalog digest, the bound volume digest and the authority's private base are one set of
  bytes.
- The release cells bind the `longterm` kernel fields (`--kernel-baseline longterm`).

`observe_remote_authority` (above) supplies the remote record's `host_os`, `host_arch` and
`guest_os`. The frame supplies `image_sha256`, `guest_arch` and the accelerator. Together they
form the full context that `_lane_contexts` binds.

## Cell map

Cell ids are `tool/<tool>/default/<configuration>/<exposure>/<provider>/x86_64/functional`. Each
row covers both configurations and both exposures, so four cells. Owners are unchanged.

| Tool | Provider | Owner | Body / fixture | Gating prerequisites | Qualifying terminal effect |
|---|---|---|---|---|---|
| `runs.release_external_boot` | local | 3119 | `_release`; fixture frame | #3321 (frame teardown after a released activation); `longterm` fixture; D1 | release job `succeeded`; root-release completion; receipt absent; domain kept; private rootfs root back to the pre-activation listing; frame cleanup |
| `runs.release_external_boot` | remote | 3120 | `_release`; remote frame | #3323 (initrd); remote teardown after release (expected from #3321, unproven); representative staged | release job `succeeded`; root-release completion; System-named pool volumes back to the pre-activation set; domain kept; frame cleanup |
| `ops.force_release` | local | 3112 | `held_teardown_release`; barrier `teardown`/`after-provider` | #3321; D1 | refusal `external_boot_system_teardown_required` while held, with the allocation `active`, the head `mutation-started` and the domain gone; then, after `torn_down` and the head `terminal`, the allocation read `released` and capacity lower than while held; frame cleanup |
| `ops.force_release` | remote | 3112 | `held_teardown_release`; remote barrier | #3323; remote teardown after release (expected from #3321, unproven) | as local, on the remote daemon |
| `ops.resolve_recovery_orphan` | local | 3112 | `quarantined_orphan`; barrier `cleanup`/`before-finalize`, abort | #3322 (row-lock privilege); #3321; D1 | one new `quarantined` row naming the observed receipt; release `succeeded` at attempt 2; then that row `deleted` by the cell's job, with every other row unchanged; frame cleanup |
| `ops.resolve_recovery_orphan` | remote | 3112 | blocked after the gates | product gap: the remote authority publishes no quarantine (#3326) | none: stays `blocked` |
| `systems.resolve_external_boot_conflict` | local | 3112 | blocked after the gates | product gaps: resolution cannot restore the recorded source (#3330); teardown from `recovery_conflict` does not complete (#3341) | none: stays `blocked` |
| `systems.resolve_external_boot_conflict` | remote | 3112 | blocked after the gates | same as local (#3330, #3341) | none: stays `blocked` |

The ceiling is 20 qualified and 12 blocked:

- 12 local cells can qualify after #3324 and #3325 merge (D1 accepted);
- 8 remote cells can qualify only after #3323 merges, and only if the remote teardown after a
  released activation works. PR #3325 fixes #3321 in the provider-independent job handlers
  (`src/kdive/jobs/handlers/external_boot/evidence.py` and `lifecycle.py`), so the remote path is
  expected to be covered, but no live run has shown it. If the remote teardown still fails after
  #3325 merges, that failure becomes routing item F3;
- 12 cells stay `blocked` until #3326 (4) and #3327 (8) land. The ceiling rises if they land
  before the live run and add a construction path.

A local cell also depends on #3138 not adopting a durable-binding ownership rule. If #3138 adopts
one, the authority refuses fixture-mode bodies, and the 12 local cells move to `blocked` under
#3144/#3148.

### Operator decisions (2026-10-21)

- **D1. Fixture-mode framing: accepted, with disclosure.** The local System is provisioned through
  the worker and then moved onto the authority's private daemon, because public local authority
  provisioning is blocked by #3144/#3148. The local frame adds `system_provenance`
  (`authority_lane.FIXTURE_PROVENANCE`) to every local record's `effect`: the System's provenance,
  and that every operation under test runs against the real authority. WORK:REVIEW repeats it.
- **D2. Surface: approved.** The two `tests/live_vm/` pass-throughs, `run_identity`, `run_cell`
  and ADR-0788.
- **D3. Sequencing: approved.** Implement now. The live session follows #3324 and #3325. If
  #3323 has not merged by then, the 8 remote cells run and record `blocked` from the observed
  initrd refusal.
- **Owners.** The remote quarantine publication gap is #3326 and the `recovery_conflict` entry gap
  is #3327; the blocked reasons cite them. The 12 cells are wired through the same entry point as
  the runnable ones, so only the product reason (`authority_cells.PRODUCT_BLOCKERS`) holds them.
  F3 exists only if the remote teardown after a released activation still fails once #3325 has
  merged.

### Implementation notes

- The shared framing lives in `tests/integration/live_stack/authority_cells.py`:
  `PRODUCT_BLOCKERS`, `exposed` (the `ToolCall` above), `run_authority_cell` (blocker, then
  fixture, `observe_remote_authority` on remote, then the frame) and the release body
  `released_external_boot` with its database-driven `settle_release`. The carriers only choose
  the caller's grants and the body.
- The local release comparison covers the private rootfs root and the domain. The console root
  is left to the frame, which compares the whole lane inventory after teardown, because console
  rotation may legitimately add entries while the System lives.
- `uploaded_kernel_inputs` (`deep_lifecycle.py`) replaces `_cancel`'s inline kernel fields and
  serves the release cell.

## Failure model

1. **Actors and deployments**
   - The operator runs the carriers on the installed x86_64 lanes:
     - lane-L: the control plane, the local authority and the test runner;
     - lane-R: the remote authority.

     Both run under ADR-0715/ADR-0788 identity, with the ADR-0622/0787 proof barrier enabled.
   - CI runs the unit tests only.
2. **Invariants and assets**
   - Every recorded functional cell of the four tools names the deployed server, worker,
     reconciler and serving authority. A cell whose identity is missing or mismatched cannot
     qualify. Rejection cells keep `server` alone.
   - The frame's lane inventory, quarantine rows other than the cell's own, and the capacity in
     use return to baseline after every cell.
   - No record or artifact carries a host name, address or private path (ADR-0715).
   - The rejection cells and the other tools' cells are unchanged.
3. **Accepted failure classes**
   - A killed run can leave one barrier arm, one framed System, one unreleased external boot,
     or one published quarantine row. The recovery is the runbook's stale-arm restart and
     killed-run cleanup. This change adds two steps to that cleanup: release the Run's external
     boot (admin `runs.release_external_boot`, drained) before `systems.teardown`, and resolve
     any `quarantined` row naming the System with `ops.resolve_recovery_orphan`. Until #3325 and
     #3324 merge, those steps fail in the same way the cells do, and the recovery is the lab
     snapshot rollback in the live plan. The cost is one restart in the unit's start budget.
   - A cell blocked by an open prerequisite records `blocked` and does not qualify. That is the
     honest inventory the issue requires.
   - When the two hosts' revisions differ, the record keeps only the non-candidate one (ADR-0788).
   - A remote identity read that raises (for example an SSH timeout) leaves that cell with no
     record. `qualify` reports it not-run, and a rerun of that cell is the recovery.
4. **Covered elsewhere**
   - Authority teardown after a released activation: #3321 (observed locally; its fix is
     provider-independent, and a remote failure after it merges becomes F3). The resolver
     privilege: #3322. The remote initrd: #3323.
   - Public provisioning: #3144/#3148. Ownership admission: #3138. vmlinux: #3130/#3131/#3135.
   - Native POWER: #2818. Publication: #2819.
   - Remote quarantine publication: #3326. Conflict entry: #3327.

There is no threat model. The change adds no entry point and no trust boundary. The remote
revision read reuses the proof config's existing SSH seam and its fixed command.

## Success

- Each of the 32 cells whose identity read completes records exactly one result per run, under
  the map's bodies. A cell whose identity read raises records none. Every record that qualifies
  has a functional terminal effect observed outside the tool's envelope, and a frame cleanup.
- `qualify` over the session's results reports every one of the 32 cells as qualified, `blocked`,
  `failure` or not-run. The report gives each non-qualified cell its reason.
- The contract tests show `ops.force_release` with `authority` and `ops.force_teardown` without
  it. Every rejection cell keeps `server` alone. The owners and the 136 and 96 cell counts are
  unchanged.

## Validation

Unit tests, all offline with fakes:

- **Contract.** The `force_release` functional roles include `authority`. `force_teardown`,
  rejection roles, owners and counts are unchanged. The matrix digest moves.
- **Negative qualification** (`tests/scripts/test_coverage_evidence.py`):
  - a `force_release` functional record without `authority` fails `deployed-role-missing`;
  - a remote authority record whose `authority` is not the candidate fails
    `deployed-revision-mismatch`;
  - an absent record for one of the 32 cells reports `missing-result` and not-run.
- **Identity.**
  - Remote equal to local is recorded.
  - A stale local authority with a current remote one records the stale one.
  - An unset config with a current local authority records no `authority`.
  - An unset config with a stale local authority records the stale one.
  - An unreadable remote revision raises.
  - A non-authority or local cell passes no reader.
- **Dispatch.** Each of the 8 tool and provider pairs selects the body in the table. The caller's
  grants are `contributor` for the release and `platform_admin` with no project role for the ops
  tools. A `ToolCall` returns error envelopes unchanged under both exposures. The blocked bodies
  raise their named reasons only after the lane gates, which a fake gate that records its call
  order proves.
- **Remote context.** For each remote functional body, the `Context` built from a fake run equals
  `_lane_contexts`' binding for that cell. A frame root image other than the representative stops
  `blocked`.
- **Release body.** It fails when the release job did not succeed, when a private root entry is
  left over, or when the receipt is still present. The `finally` follows the database. It does
  nothing with no activation. It drains the cell's job when the call was admitted and then
  raised. It admin-releases when activation succeeded and no release job exists.
- **Force-release arm.** A fake that answers `released` while the allocation stays `active` fails.
  So does a fake whose capacity does not drop.
- Each new negative test is checked red under a controlled fault, which is then reverted.

Guardrails: `just lint`, `just type`, and the focused pytest. A live session runs only after the
operator approves a separate plan (below).

## Live plan shape (for approval; nothing is granted)

- **Hosts.** lane-L and lane-R as in `.agent/campaigns/2d21598e2016-3296-live-evidence/RECIPE.md`:
  one lab control-plane guest that also runs the local authority, and one lab Fedora 44 remote
  authority guest. Take snapshots before the run and roll both back after it. Stay clear of the
  nightly backup lock window.
- **Lane preparation** (about 2.5 h):
  - Install the candidate on both lanes, with the barrier enabled.
  - Use the fixture-mode inventory.
  - Stage the remote fedora representative as the authority root image. This takes an existing
    build or one `image.yml` rebuild of about 50 min.
  - Build the `longterm` fixture on lane-L. This is one kernel build of about 45 min, and it needs
    approval.
  - Generate the bindings with `REMOTE_PROVIDER_SSH` naming lane-R.
- **Runs.** Run the default configuration first, then restart the stack with
  `KDIVE_WORKER_DEATH_VERIFIER=docker` for the recovery configuration. Each configuration runs the
  16 functional cells, selected by `-k`, one scenario at a time. Estimates per configuration:
  - 6 local runnable cells at about 15 min each;
  - 4 remote runnable cells at about 20 min each;
  - 6 blocked cells at about 2 min each.

  That is about 3 h per configuration and about 6 h for both.
- **Close.** Assemble, qualify, read back, roll back and shred the scratch PKI, in about 0.5 h.
  The total is about 9 h. It can be split into one session per configuration, with the lanes
  held between them.

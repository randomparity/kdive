# Qualify the 32 authority HTTP functional tool cells (#3186) — design

Design proposal for issue #3186 (parent #3152, epic #2803), on source baseline `a0d041a85`.

- Frozen scope: [WORK:SCOPE](https://github.com/randomparity/kdive/issues/3186#issuecomment-6318748334),
  token `q3186-d79f717c`. The operator approved the scope and its exclusion owners on 2026-10-16.
- Decision record: [ADR-0788](../../adr/0788-authority-tool-cells-bind-the-serving-authority.md)
  (Proposed).
- Status: awaiting the operator's design decision. Nothing is implemented.

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
3. Two small pass-through extensions. `start_external_boot_activation`
   (`tests/live_vm/installed_local_authority_support.py`) gains optional `kernel_tree` and
   `evidence_dir`, which it forwards to `build_and_upload_kernel`. The private `_activate` in
   `authority_faults.py` becomes public `activate`, with the same two parameters.
4. A remote revision reader beside `require_installed_authority`
   (`tests/live_vm/installed_remote_authority_support.py`).
5. Unit tests, carrier docstrings, and the runbook sections for the run, break-glass and
   authority-fault cells.

Out of scope, with owners: the WORK:SCOPE exclusions (#3138, #3143, #3144/#3148, #3130/#3131,
#3135, #2818, #2819), and the ordinary remote lane and its carriers. The open in-campaign fixes
are also out of scope: #3321 (PR #3325), #3322 (PR #3324) and #3323. This change consumes them
and does not implement them. There is no production API, schema, migration, provider-family,
architecture or scheduler change.

Surface note for the operator: items 3 and 4 touch `tests/live_vm/`, which is outside the
WORK:SCOPE surface's literal paths. Both are test-only and follow from criteria 1 and 4. They need
the operator's approval at this gate.

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

A blocked body runs `blocked_after_gates`. For a remote cell it also runs `observe_host` first,
so the blocked record carries the provider host. A blocked cell records `blocked` with its
narrower reason. It is never a pass.

### `runs.release_external_boot` body (`_release`)

1. Load the bound `longterm` fixture with `_fixture()`. It stops `blocked` when the fixture is
   absent. On a remote cell, run `observe_host(run, remote_host())`.
2. List the authority's state:
   - local: `authority_inventory(system_id)` roots and domain facts;
   - remote: `remote_authority_inventory(config, system_id)` volumes and domain.
3. Call `activate(system, kernel_tree=tree, evidence_dir=<scratch>)`. It runs the admin `op`
   through create, upload (no `vmlinux`), install and boot, then drains the activation. A remote
   activation failure stops `blocked` with the job's category and reason. Today that is
   `remote_external_boot_initrd_required` (#3323).
4. In a `try`/`finally`, call `runs.release_external_boot` through the cell's `ToolCall` with
   `{"run_id": ...}`. Answer a `system_job_active` refusal the same way the support helper
   `release_external_boot` does: drain the named jobs, then retry within the same bound. Then
   drain the release job. The `finally` calls the support helper's admin release only when the
   tool call was never admitted, so the frame never meets a held activation.
5. Effect evidence. Each item is observed outside the tool's own envelope:
   - the release job is `succeeded` (`job_claim`);
   - `assert_root_release_completion` passes for the activate and release jobs. This covers the
     activation `recovered` with cleanup complete, the consumed cleanup receipt, the retired
     root, the terminal journal head and the reservation credit;
   - local: the private cleanup receipt is absent (`receipt_state`). The authority daemon still
     defines the System's domain. The two private roots list exactly what they listed in step 2;
   - remote: the authority pool's volumes containing the System id equal the step-2 set, and the
     daemon still defines the domain.
6. Record the kernel inputs from the upload record and the fixture manifest, as `_cancel` does
   today. Extract the shared lines into one helper used by both.

### `ops.force_release` and `ops.resolve_recovery_orphan` bodies

These reuse `held_teardown_release` and `quarantined_orphan` unchanged, with the cell's `ToolCall`
in place of the #3185 module's fixed `direct` platform-admin caller. Both arms keep their own
barrier holds, budget checks, independent observations and failure cleanup (#3185 spec, Arms A
and B). The carrier exports `KDIVE_KERNEL_SRC` to the same `longterm` tree, so every authority
cell uploads one kernel.

The orphan cell proves this: an authority publication of a real quarantine; then an exact-set
`delete` disposition, by the cell's caller, of a receipt that a retry had already removed. The
deletion of a still-present receipt stays unproven (`LOCAL_PRESENT_ORPHAN_BLOCKED`), and the cell
discloses that in its `effect`.

### Identity (ADR-0788)

- `obligations.toml`: the 3112 provider group gains
  `[groups.role_overrides] "ops.force_release" = ["server", "worker", "reconciler", "authority"]`.
- `run_identity(base_url, *, remote_authority=None)`. `run_cell` passes a reader only for a
  `remote-libvirt` cell whose `roles` include `authority`.
  - The reader loads the remote proof config. If the config is unset it returns `None`, so the
    revision is not recorded.
  - Otherwise it reads the remote revision file as the authority account and resolves it in the
    checkout. A failed read or resolve raises.
  - A test-host authority revision, when present, is also read. The recorded `authority` is the
    non-candidate revision when the two differ, and the shared value otherwise.
- `identity_problems`, `build_record` and `qualify` are unchanged. A missing role fails
  `deployed-role-missing`; a stale one fails `deployed-revision-mismatch`.

### Input bindings

Bindings are unchanged in code.

- Local cells bind `LANE_IMAGES["x86_64"]`. That is the image the fixture frame acquires.
- Remote cells bind `REMOTE_REPRESENTATIVES["fedora"]` on the host that `REMOTE_PROVIDER_SSH`
  names. For this session that variable names the remote authority host. The lane stages that
  representative as its single registered public remote-libvirt image with an inspected root, so
  its catalog digest, the bound volume digest and the authority's private base are one set of
  bytes.
- The release cells bind the `longterm` kernel fields (`--kernel-baseline longterm`).

The remote body adds a guard: when the frame's root image is not that representative, it stops
`blocked` and names the mismatch. This keeps a wrong image from reaching `input-context-mismatch`
at `qualify`.

## Cell map

Cell ids are `tool/<tool>/default/<configuration>/<exposure>/<provider>/x86_64/functional`. Each
row covers both configurations and both exposures, so four cells. Owners are unchanged.

| Tool | Provider | Owner | Body / fixture | Gating prerequisites | Qualifying terminal effect |
|---|---|---|---|---|---|
| `runs.release_external_boot` | local | 3119 | `_release`; fixture frame | #3321 (frame teardown after a released activation); `longterm` fixture; D1 | release job `succeeded`; root-release completion; receipt absent; domain kept; private roots back to the pre-activation listing; frame cleanup |
| `runs.release_external_boot` | remote | 3120 | `_release`; remote frame | #3323 (initrd); remote teardown after release (unproven; #3321 or a sibling); representative staged | release job `succeeded`; root-release completion; System-named pool volumes back to the pre-activation set; domain kept; frame cleanup |
| `ops.force_release` | local | 3112 | `held_teardown_release`; barrier `teardown`/`after-provider` | #3321; D1 | refusal `external_boot_system_teardown_required` while held, with the allocation `active`, the head `mutation-started` and the domain gone; then `released` after `torn_down` and the head `terminal`; frame cleanup |
| `ops.force_release` | remote | 3112 | `held_teardown_release`; remote barrier | #3323; remote teardown after release | as local, on the remote daemon |
| `ops.resolve_recovery_orphan` | local | 3112 | `quarantined_orphan`; barrier `cleanup`/`before-finalize`, abort | #3322 (row-lock privilege); #3321; D1 | one new `quarantined` row naming the observed receipt; release `succeeded` at attempt 2; then that row `deleted` by the cell's job, with every other row unchanged; frame cleanup |
| `ops.resolve_recovery_orphan` | remote | 3112 | blocked after the gates | product gap: the remote authority publishes no quarantine (no owner yet, F1) | none: stays `blocked` |
| `systems.resolve_external_boot_conflict` | local | 3112 | blocked after the gates | product gap: no production path enters `recovery_conflict` (no owner yet, F2) | none: stays `blocked` |
| `systems.resolve_external_boot_conflict` | remote | 3112 | blocked after the gates | same as local (F2) | none: stays `blocked` |

The ceiling is 20 qualified and 12 blocked:

- 12 local cells can qualify after #3324 and #3325 merge, if D1 is accepted;
- 8 remote cells can qualify only after #3323 merges, and only if the remote teardown after a
  released activation works;
- 12 cells stay `blocked` until product changes outside this scope give them owners.

A local cell also depends on #3138 not adopting a durable-binding ownership rule. If #3138 adopts
one, the authority refuses fixture-mode bodies, and the 12 local cells move to `blocked` under
#3144/#3148.

### Operator decisions requested

- **D1. Fixture-mode framing.** The local System is provisioned through the worker and then moved
  onto the authority's private daemon, because public local authority provisioning is blocked by
  #3144/#3148. Provisioning is not the tool under test, and the tool's effect is observed on the
  real authority. Recommendation: accept fixture-mode framing for the 12 local cells, with the
  disclosure carried in every record's `effect`. Otherwise those 12 cells stay `blocked` under
  #3144/#3148.
- **D2. Surface.** Approve the two `tests/live_vm/` pass-throughs above.
- **D3. Sequencing.** Recommendation: implement now; run the live session after #3324 and #3325
  merge; include the remote cells if #3323 has merged by then. If #3323 has not merged, the 8
  remote cells run and record `blocked` from the observed initrd refusal.
- **F1 and F2 (routing, not filed here).** The remote quarantine publication gap and the missing
  `recovery_conflict` entry need owners. The 12 cells otherwise carry reasons that name no issue.

## Failure model

1. **Actors and deployments**
   - The operator runs the carriers on the installed x86_64 lanes:
     - lane-L: the control plane, the local authority and the test runner;
     - lane-R: the remote authority.

     Both run under ADR-0715/ADR-0788 identity, with the ADR-0622/0787 proof barrier enabled.
   - CI runs the unit tests only.
2. **Invariants and assets**
   - Every recorded cell names the deployed server, worker, reconciler and serving authority.
     A cell whose identity is missing or mismatched cannot qualify.
   - The frame's lane inventory, quarantine rows other than the cell's own, and the capacity in
     use return to baseline after every cell.
   - No record or artifact carries a host name, address or private path (ADR-0715).
   - The rejection cells and the other tools' cells are unchanged.
3. **Accepted failure classes**
   - A killed run leaves one barrier arm or one framed System. The recovery is the runbook's
     stale-arm restart and killed-run cleanup. The cost is one restart in the unit's start budget.
   - A cell blocked by an open prerequisite records `blocked` and does not qualify. That is the
     honest inventory the issue requires.
   - When the two hosts' revisions differ, the record keeps only the non-candidate one (ADR-0788).
4. **Covered elsewhere**
   - Authority teardown after a released activation: #3321. The resolver privilege: #3322.
     The remote initrd: #3323.
   - Public provisioning: #3144/#3148. Ownership admission: #3138. vmlinux: #3130/#3131/#3135.
   - Native POWER: #2818. Publication: #2819.
   - Remote quarantine and conflict entry: F1/F2, routed by the campaign root.

There is no threat model. The change adds no entry point and no trust boundary. The remote
revision read reuses the proof config's existing SSH seam and its fixed command.

## Success

- The 32 cells in the map record one result each per run, under the map's bodies. Every record
  that qualifies has a functional terminal effect observed outside the tool's envelope, and a
  frame cleanup.
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
  - An unset config records no `authority`.
  - An unreadable remote revision raises.
  - A non-authority or local cell passes no reader.
- **Dispatch.** Each of the 8 tool and provider pairs selects the body in the table. The caller's
  grants are `contributor` for the release and `platform_admin` with no project role for the ops
  tools. A `ToolCall` returns error envelopes unchanged under both exposures. The blocked bodies
  raise their named reasons only after the lane gates.
- **Release body.** It fails when the release job did not succeed, when a private root entry is
  left over, or when the receipt is still present. A tool call that is never admitted triggers the
  admin release in the `finally`.
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

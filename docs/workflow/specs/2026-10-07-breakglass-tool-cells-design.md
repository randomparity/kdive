# The x86_64 break-glass provider tool cells (#3112)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment; evidence identity per
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md). This design reuses
the local provider-lane frame of #3062 ([spec](2026-10-06-system-tool-cells-design.md)) and the
remote lane of #3080 ([spec](2026-10-06-remote-system-tool-cells-design.md)).

## Problem

Group 3112 holds `ops.force_release`, `ops.force_teardown`, `ops.resolve_recovery_orphan` and
`systems.resolve_external_boot_conflict`. It has 136 x86_64 cells on `local-libvirt` and
`remote-libvirt`, 32 functional and 104 rejection, and no carrier binds them. The group declares
`authority = true`, so the deployed `authority` role is required for every functional cell. The
demo-up lane installs no provider authority (`deploy/ansible/playbooks/local-libvirt-host.yml`
runs no authority role), and a remote authority tuple would route every remote install and boot
through external boot (#3120). In that state, `run_cell` records `missing:authority` for all 32.

The four tools do not all need the authority:

- `ops.force_release` releases any Allocation through `release_with_backstops`. Only a System
  with external-boot history makes the release wait for its teardown (`_require_system_teardown`).
- `ops.force_teardown` enqueues the same `TEARDOWN` job as `systems.teardown`. It takes the
  authority-marked path only for a System with external-boot activation history.
- `ops.resolve_recovery_orphan` needs quarantined recovery objects of an external-boot
  activation, and `systems.resolve_external_boot_conflict` needs an activation in
  `recovery_conflict`. No harness in `tests/` constructs either state, and no live test calls
  either tool.

On 2026-10-07 the operator chose option A (recorded in the issue's `WORK:SCOPE`): split the
flags per tool. `ops.force_release` and `ops.force_teardown` drop the authority, and their
functional cells are proven live. The two resolve tools keep it, and their functional cells
stop `blocked`.

## Scope

1. **Contract flags** in `scripts/coverage_campaign/obligations.toml`. Group 3112 becomes two
   provider groups, both owned by 3112, and each tool keeps its observation string:
   - `ops.force_release` and `ops.force_teardown`, with no `authority`;
   - `ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict`, with
     `authority = true`.

   `contract.py` is unchanged. Its ppc64le route still sends these tools' ppc64le cells to
   #2818, which take the same flags. The 136 x86_64 cells stay with owner 3112. `[implementations]`
   binds the 34 scenarios (17 local, 17 remote) to the new carrier node.
2. **Release seam** in the frame. `cleanup.release_and_verify` releases the Allocation through
   a `release` callable, which defaults to `allocations.release`. `scenario.on_catalog_system`
   and its `_cleanup`, `remote_lifecycle.on_remote_system` and `remote_cleanup`, and the
   `tool_cells.LaneFrame` protocol pass an optional `release` through. This mirrors their
   existing `provision` seam ("a cell whose tool under test is the provision passes its own").
   Every existing caller keeps the default.
3. **Carrier** `tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell`.
   It is parametrized over the native local-libvirt cells and the x86_64 remote-libvirt cells of
   the four tools, as in the System carrier, and framed by `run_tool_cell`.
4. **Runbooks:** one section in `docs/operating/runbooks/live-testing.md` (local) and one in
   `remote-live-stack.md` (remote), each recording the live run.

No product source, ADR or migration change is needed.

### Functional cells

Every functional call is made by a platform admin who holds no role in the System's project: a
token with `platform_roles = ("platform_admin",)` and one fresh `cov-<hex>` project of its own.
The call goes through the cell's exposure, with `reason = "coverage #3112"`.

- **`ops.force_release`.** The lane frame provisions the lane image in a fresh project and
  observes the guest over SSH. The cell then passes a `release` that calls `ops.force_release` on
  the frame's Allocation and requires the status `released`. The frame's
  `release_and_verify` then proves the rest with that release. Capacity in use was above its
  pre-allocation value while the Allocation was held. After the release, `allocations.wait`
  reports `released`, the System reaches `torn_down`, the provider libvirt no longer defines the
  domain, every owned disk is absent on the provider host, and the summed `in_use` is back to
  its pre-allocation value. After that cleanup returns, the cell proves `effect`: the call's
  answer, a second `allocations.wait` still reporting `released`, and exactly one
  `platform_audit_log` row for the admin subject with `tool = ops.force_release` and
  `scope = <project>:<allocation_id>`.
  *Reading of the observation.* For an ordinary System, the release is terminal when the call
  returns, and the provider teardown follows. The cell therefore proves that the release stays
  terminal while the provider effects quiesce: domain, disks and capacity are reclaimed, and the
  Allocation is still `released` afterwards. The authority-path ordering, where a System with
  external-boot history is torn down before the release, is not observed here. It belongs to the
  follow-up candidate below.
- **`ops.force_teardown`.** Inside `on_lane_system`, the cell first reads the provider's set of
  `kdive-` domains and the lane's staged base: the local staged qcow2 file, or the remote base
  volume in the instance's pool. It then calls `ops.force_teardown` on the frame's System. Unless
  the answer is already `torn_down`, it drains the returned job with the frame's project
  operator, since the job's authorizing project is the System's project. It then proves:
  - the System is `torn_down`;
  - the domain is no longer defined;
  - every owned disk is absent;
  - the `kdive-` domain set equals the earlier set minus this domain, so unrelated domains stay;
  - the staged base still exists, so unrelated storage stays;
  - one `platform_audit_log` row records `ops.force_teardown` for this admin and the System's
    scope.

  The frame then releases the Allocation and proves cleanup as for `systems.teardown`.
- **`ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict`.** These stop
  `blocked` (`missing-prerequisite`) before any stack mutation. The reason names the missing
  installed authority, the missing authority-lane System frame, and the missing construction of
  a quarantined orphan or a recovery conflict. A remote cell first probes the provider host, so
  its record carries that host. These cells are never `success`.

### Rejection cells

Every rejection cell targets the stack's `lane_target` for its provider: a torn-down System and
its released Allocation in project T. The snapshot is `project_state(T)`.

| Tool | Gate | authentication / authorization grants | validation | project-isolation |
|---|---|---|---|---|
| `ops.force_release` | `platform_admin` | `platform_operator` of a fresh project | `allocation_id: 7`, as admin | none in the contract |
| `ops.force_teardown` | `platform_admin` | `platform_operator` of a fresh project | `system_id: 7`, as admin | none |
| `ops.resolve_recovery_orphan` | `platform_admin` | `platform_operator` of a fresh project | `system_id: 7`, as admin | none |
| `systems.resolve_external_boot_conflict` | `admin` of T | `viewer` / `contributor` of T | `system_id: 7`, as admin of T | operator of a fresh project; `not_found`, identical to an absent System (`absent_twin`) |

The well-typed arguments are:

- `reason: "coverage #3112"` for the two force tools;
- `object_identities: ["cov-orphan"]` and `disposition: "delete"` for the orphan tool;
- `operation: "restore-recorded-source"` and `observed_identity: "sha256:" + 64 zeros` for the
  conflict tool.

Every handler checks the role before it resolves the object: the platform role in `breakglass.py`
and `recovery_requests.py`, and the conflict tool's `require_role` right after it resolves the
System. Each refused call therefore writes nothing to T. A platform denial adds a
`platform_audit_log` row, which has no project column and is excluded from the snapshot.

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on the disposable lab: a control-plane guest with one
     demo-up stack per configuration (`recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`), and
     a separate remote provider host prepared as in #3120, with no provider authority anywhere;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes: the 16 resolve functional cells are `blocked`, never covered;
   - a contract whose flags match option A, with routing unchanged;
   - existing carriers unchanged: the `release` seam defaults to today's `allocations.release`;
   - the hosts left as found: each functional cell's System, domain, disks and capacity are
     reclaimed and proven per cell; force teardown leaves other domains and the staged base.
3. **Accepted failure classes:**
   - A cell killed mid-run can leave a System or Allocation behind. The post-run wipe clears it,
     as for the earlier carriers.
   - A failed lane target fails every rejection cell of its stack and provider. It is not
     retried.
   - Another `kdive-` domain created or removed on the provider during a force-teardown cell
     fails that cell. No other allocator runs on the lab stack.
   - The admin's `platform_audit_log` rows and the targets' history rows stay. They are audit
     history, and the wipe clears them.
4. **Covered elsewhere:**
   - the authority lane frame plus an orphan/conflict construction harness (follow-up candidate);
   - ppc64le cells (#2818);
   - mutating operator tools (#3110) and build-use recovery (#3111);
   - the capability boundary (#2814).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| per-tool flags and owners | focused-test | `tests/scripts/test_coverage_contract.py`: x86_64 cells of the four tools stay 3112 (136), ppc64le stay 2818; functional roles carry `authority` only for the two resolve tools |
| carrier bindings | focused-test | same file: the four tools' cells bind the carrier node |
| release seam | focused-test | `tests/integration/live_stack/test_cleanup.py`: a supplied `release` replaces `allocations.release`, and the proof still runs |
| carrier bodies | task-test-not-applicable | they need a live stack and provider host; their evidence is the lab run below |
| live run | lab run | both configurations on both lanes; `qualify` reports 120 of 136 qualified and the 16 resolve functional cells blocked |

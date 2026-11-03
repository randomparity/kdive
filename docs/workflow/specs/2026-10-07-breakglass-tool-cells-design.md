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
flags per tool. `ops.force_release` and `ops.force_teardown` drop the authority. The two resolve
tools keep it, and their functional cells stop `blocked`. The scope audit then found that the
`ops.force_release` observation ("terminal release only after protected provider effects are
quiescent") describes an ordering that only an authority-owned System with external-boot history
has: an ordinary release is terminal when the call returns, and the teardown follows. At the
second checkpoint, the operator chose option (b): its 8 functional cells stop `blocked` as well.
Both decisions are recorded in the issue's re-frozen `WORK:SCOPE`.

## Scope

1. **Contract flags** in `scripts/coverage_campaign/obligations.toml`. Group 3112 becomes two
   provider groups, both owned by 3112, and each tool keeps its observation string:
   - `ops.force_release` and `ops.force_teardown`, with no `authority`;
   - `ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict`, with
     `authority = true`.

   `contract.py` is unchanged. Its ppc64le route still sends these tools' ppc64le cells to
   #2818, which take the same flags. The 136 x86_64 cells stay with owner 3112. `[implementations]`
   binds the 34 scenarios (17 local, 17 remote) to the new carrier node.
2. **Carrier** `tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell`.
   It is parametrized over the native local-libvirt cells and the x86_64 remote-libvirt cells of
   the four tools, as in the System carrier, and framed by `run_tool_cell`. The shared frame
   (`cleanup.py`, `scenario.py`, `remote_lifecycle.py`, `tool_cells.py`) is reused unchanged.
3. **Runbook:** one section in `docs/operating/runbooks/live-testing.md` covering both lanes,
   linking the remote lane's set-up in `remote-live-stack.md` §8, and recording the live run.

No product source, ADR or migration change is needed.

Each record's identity comes from the inherited frame. `run_cell` records the candidate and the
deployed roles. The bindings and `observe_guest` record the guest and image. `observe_host`
records the provider host of a remote cell. The four tools declare no kernel inputs, so no
kernel identity is bound.

### Functional cells

A platform admin who holds no role in the System's project makes the `ops.force_teardown` call:
a token with `platform_roles = ("platform_admin",)` and one fresh `cov-<hex>` project of its own.
The call goes through the cell's exposure, with `reason = "coverage #3112"`.

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
  a quarantined orphan or a recovery conflict.
- **`ops.force_release`.** This stops `blocked` before any stack mutation as well. The reason
  states that its ordering exists only for an authority-owned System with external-boot history,
  and that no lane frames one.
- A blocked remote cell first probes the provider host, so its record carries that host. Blocked
  cells are never `success`.

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
     a separate remote provider host prepared as in #3120, with no provider authority anywhere,
     that serves only this run while it lasts (no other stack's remote cells run concurrently);
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes: the 24 functional cells of the resolve tools and of
     `ops.force_release` are `blocked`, never covered;
   - a contract whose flags match option A, with routing unchanged;
   - existing carriers unchanged: the shared frame files are not edited;
   - the hosts left as found: each force-teardown cell's System, domain, disks and capacity are
     reclaimed and proven per cell, and its other domains and the staged base remain.
3. **Accepted failure classes:**
   - A cell killed mid-run can leave a System or Allocation behind. The post-run wipe clears the
     stack and the local worker's libvirt. It does not reach the remote provider host: the
     post-run provider check lists any leftover `kdive-` domain or overlay volume there, and the
     runbook's manual step removes it (`virsh destroy`/`undefine`, `vol-delete`).
   - A failed lane target fails every rejection cell of its stack and provider. It is not
     retried.
   - Another `kdive-` domain created or removed on the provider during a force-teardown cell
     fails that cell. No other allocator runs on the lab stack.
   - The admin's `platform_audit_log` rows and the targets' history rows stay. They are audit
     history, and the wipe clears them.
4. **Covered elsewhere:**
   - the authority lane frame plus an orphan/conflict construction harness, which also owns the
     `ops.force_release` ordering (follow-up candidate);
   - ppc64le cells (#2818);
   - mutating operator tools (#3110) and build-use recovery (#3111);
   - the capability boundary (#2814).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| per-tool flags and owners | focused-test | `tests/scripts/test_coverage_contract.py`: x86_64 cells of the four tools stay 3112 (136), ppc64le stay 2818; functional roles carry `authority` only for the two resolve tools |
| carrier bindings | focused-test | same file: the four tools' cells bind the carrier node |
| record identity | task-test-not-applicable | inherited unchanged from `run_cell`, the bindings, `observe_guest` and `observe_host`, which their own tests cover; this change adds no identity field |
| carrier bodies | task-test-not-applicable | they need a live stack and provider host; their evidence is the lab run below |
| live run | lab run | both configurations on both lanes; `qualify` reports 112 of 136 qualified (8 `success`, 104 `rejection`) and the 24 blocked functional cells |

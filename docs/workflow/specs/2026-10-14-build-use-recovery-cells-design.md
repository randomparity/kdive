# Build-use recovery tool cells (#3111)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and the 2026-10-14 amendment this change adds. Builds on the #2812 carrier
([spec](2026-10-06-operator-tool-cells-design.md)) and reuses the shared tool-cell harness unchanged.

## Problem

Owner 3111 in `scripts/coverage_campaign/obligations.toml` holds `ops.build_uses_list` and
`ops.recover_build_use`. They are registered only in the `recovery` configuration, so the contract
gives them 16 cells: 4 functional and 12 rejection (`authentication`, `authorization`,
`validation`), each in `direct` and `gateway`. None is bound to a test.

The issue assumed that recovery needs a Docker-incarnation worker whose death the server's
verifier can reach. The source says otherwise:

- `KDIVE_WORKER_DEATH_VERIFIER` only decides whether the two tools are registered
  (`src/kdive/mcp/assembly/tool_registration.py`, `assembly.worker_death_verifier`). No caller
  invokes `verify_dead`.
- `ops.recover_build_use` succeeds only when the holder named on the use row has a durable
  `worker_incarnations` row with `state = 'terminated'`. The check is in
  `recover_investigation_build_use`, `src/kdive/db/schema/0108_worker_fence_runtime_paths.sql`.
- On the demo-up lane, the systemd lifecycle witness writes that row. When a worker unit's
  invocation dies, `scripts/live-stack/worker-lifecycle.sh status` publishes its termination
  (`_status_slot`, `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`).

So the demo-up `recovery` lane (`KDIVE_WORKER_DEATH_VERIFIER=docker`) can prove both tools against
a real worker, as the 2026-10-14 root direction for #3111 says. The worker there is a systemd unit
with a `local` incarnation, not a Docker container. ADR-0722 §2 and its Consequences say a recovery
call on this lane "cannot prove a worker dead". That is wrong, and the amendment corrects it.

## Scope

1. **R5** (operator decision 2026-10-13): `ops.recover_build_use`'s role override becomes
   `["server", "worker"]`, which drops `authority`.
2. **Bindings**: `[implementations]` binds the 8 scenarios of the two tools to
   `tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell`.
3. **Carrier**: the operator carrier adds both tools to `TOOLS`. Their rejection cells use the
   carrier's existing rejection path. Their functional cells call a new helper,
   `tests/integration/live_stack/build_use_recovery.py`, which strands a real build-use pin as
   described below.
4. **ADR-0722 amendment** (2026-10-14). It records where the termination evidence comes from and
   which lane the recovery tools' functional cells run on.
5. A runbook section in `docs/operating/runbooks/live-testing.md`, and `test_coverage_contract.py`
   updates for the override and the bindings.

There is no product, schema, fixture or harness-frame change.

### A stranded pin from a real worker

A build-use pin exists only while an install job runs. `acquire_build_use` commits it before the
provider install, and `release_build_use` removes it afterwards
(`src/kdive/jobs/handlers/runs/install.py`, `_run_install_step`). A worker that dies between the
two leaves the pin behind. The helper produces that state as follows:

1. **System and build.** The lane frame (`lane_for(cell).frame`, the local catalog frame) provisions
   the lane image in a fresh project `cov-<hex>`. It runs under its own `CellRun`, so its guest and
   image observations do not enter the service cell's context. The service cell binds only host
   and `accelerator: none`. The helper opens an investigation, creates a bound Run, and uploads the
   verified `longterm` fixture with `with_vmlinux=True` through `build_and_upload_kernel`. With
   that debuginfo reference, the install injects `lib/modules` through libguestfs, which keeps the
   pin held for seconds. Without `KDIVE_FIXTURE_ROOT`, or with an unverified fixture, the
   cell stops `blocked` before it touches the stack.
2. **Freeze with the pin held.** The helper calls `runs.install`, then polls
   `investigation_build_uses` for the investigation's row every 20 ms, for up to 10 minutes. The
   holder is the worker that wrote that row; its `worker_incarnations.authority_binding.unit` names
   its systemd unit. The helper then freezes the whole unit cgroup with
   `sudo -n systemctl freeze <unit>`. The unit's main process may be a gate in front of the
   worker, so freezing only that process would not be enough.
3. **Prove the freeze point.** The worker must be frozen outside its release transaction, or a
   recovery call would wait on that transaction's lock. The helper proves this two ways:
   - The pin row still exists.
   - `pg_try_advisory_lock(hashtextextended('kdive:worker-incarnation:' || holder, 1803))`
     succeeds. That is the transaction lock acquire, release and recovery all take. The helper
     releases it immediately.

   A pin that disappeared, or a lock held by the frozen worker, means the race was lost. The
   helper then thaws the unit (`systemctl thaw`), lets the install finish, cleans up, and stops the cell
   `blocked`. A lost race never becomes evidence.
4. **Kill and witness.** The cell kills the unit with
   `sudo -n systemctl kill --signal=SIGKILL <unit>`, then waits up to 30 s for the unit's
   `ActiveState` to read `failed` or `inactive`. The unit has `Restart=no`, so it stays dead.
   `worker-lifecycle.sh status` then publishes the incarnation's termination. The helper
   requires the holder's `worker_incarnations` row to read `state = 'terminated'` with an outcome.
5. **Restore.** This step runs whatever the body did:
   - A frozen holder that was never killed is thawed. It finishes the install and releases its pin
     itself. Its job is never canceled: `release_investigation_build_use` refuses a release once
     the job is not `running`, so a cancel would strand the pin under a live holder.
   - A killed holder's install job is canceled through `jobs.cancel` first, so no restarted worker
     reclaims it. Then the fleet is replaced: the helper counts the started slots in `status`
     before the freeze, runs `worker-lifecycle.sh start <that count>`, and requires every slot to
     come back started.
   - The helper waits up to 5 minutes for the install job to be terminal and for no pin to remain
     for the investigation. If the install succeeded, its staged kernel joins the frame's owned
     paths. The helper then closes the investigation and returns to the frame, which proves
     teardown, release and disk absence.

All observations of the stack come from the evidence database, read through the harness login in
read-only sessions, and from the lifecycle response. The tools themselves are called through the
cell's exposure with a fresh `platform_operator` token that also holds `viewer` on the project.
Recovery requires that viewer grant, and the list returns only the caller's granted projects.

### Functional effects

- `ops.build_uses_list`: the listing is called twice.
  - **Active.** While the holder is frozen, its incarnation row is `active`, and the listing must
    equal the database's pin rows for the project: use id, investigation, generation, job,
    attempt, holder and `created_at`, oldest first. The stranded pin must be among them.
  - **Orphaned.** The listing is called again after the kill and the witnessed termination. It
    must again equal the database rows, with the same pin, whose holder is now `terminated`.

  The cell then recovers the orphan through `ops.recover_build_use`, which is cleanup and not this
  cell's effect, so the restore finds no pin.
- `ops.recover_build_use`: the tool is called twice.
  - **Alive.** While the holder is frozen and `active`, the call with the exact use id and holder
    returns `status=error` and `error_category=configuration_error`. The pin row is unchanged, and
    no recovery row exists for it.
  - **Dead.** After the kill and the witnessed termination, the same call returns
    `status=recovered` with `data.holder` equal to the holder. The pin row is gone. Exactly one
    `investigation_build_use_recoveries` row exists for the use id, and its job, attempt,
    generation, holder, `authority_kind = 'local'` and termination outcome equal the witnessed
    incarnation row. Every other pin in the database, read before and after, is unchanged.

### Rejection cells

These use the carrier's existing path and `project_state` snapshot:

| Boundary | Grants | Arguments |
|---|---|---|
| authentication | viewer of P | `{}` for the list; for recovery, a random use id, holder `cov-holder` and reason `cov` |
| authorization | `platform_auditor` (no operator role) | same as authentication |
| validation | `platform_operator` (makes the tool visible to `tools.invoke`) | `{"limit": "many"}`; `{"use_id": "not-a-uuid", "holder": "x", "reason": "x"}` |

A recovery call with a random use id finds no pin, so the authentication control call changes
nothing. It may write a platform-audit row, which the snapshot excludes (ADR-0722 §4).

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on a disposable local lab host. The stack is started with
     `KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh`, the workers are the
     installed `kdive-live-worker@N` units, and the operator has passwordless `sudo -n`
     (`systemctl kill`, as the resource-limit carriers already require);
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest outcomes. A lost freeze race, a missing fixture or a missing `sudo` grant stops the
     cell `blocked`, never `success`;
   - the shared stack restored: the full worker fleet started, no build-use pin left, and the
     cell's System torn down and its capacity released;
   - other projects' pins are never touched.
3. **Accepted failure classes:**
   - Recovery, audit and lifecycle rows the cells cause stay as history.
     `investigation_build_use_recoveries` carries the project column, but only the functional
     cells write it, and they use the frame's cleanup proof, not the snapshot.
   - The fleet restart replaces every worker slot, not only the killed one. The cells run
     serially on a dedicated lane, so no other job is in flight.
   - The killed install job is reclaimed after its lease expires. A later attempt may finish the
     install on the cell's System. The restore waits for that before teardown.
   - Docker- and Kubernetes-incarnation workers are not exercised. Recovery reads only the durable
     termination row, whatever the authority kind. Each lifecycle witness's own tests cover how
     that row is written.
4. **Covered elsewhere:**
   - durable finalization and cross-replica guarantees (#2681);
   - failure carriers (#3117, #2816);
   - the Kubernetes lane, which is out of scope by the approved exclusion set of 2026-10-07.

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| R5 override and bindings | focused-test | `test_coverage_contract.py`: `ops.recover_build_use` functional roles are `("server", "worker")`; the 16 cells bind to the operator node; the unbound set shrinks by exactly them |
| freeze-point and restore logic | focused-test | `tests/integration/live_stack/test_build_use_recovery.py` with fake `sudo`, lifecycle and database seams: a lost race stops `blocked` and thaws; the restore runs after a body failure; restore restarts the observed slot count |
| live cells | task-test-not-applicable | proven by the `recovery`-lane lab run (`qualify` over the 16 cells) |
| ADR amendment, runbook | task-test-not-applicable | prose; `just records` and `just docs-check` |

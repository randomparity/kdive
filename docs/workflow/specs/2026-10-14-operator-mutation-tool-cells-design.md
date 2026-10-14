# Mutating operator MCP tool cells (#3110)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its amendments, [ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) for deployed role
identity, and [ADR-0769](../../adr/0769-live-harness-login-role.md) for the
harness login's seeding writes. Builds on the #2811 harness
([spec](2026-10-02-core-tool-cells-design.md)), the read-only operator carrier
([spec](2026-10-06-operator-tool-cells-design.md)) and the authority-owned System frame
([spec](2026-10-13-authority-system-frame-design.md)), all reused unchanged.

## Problem and authority

Owner 3110 in `scripts/coverage_campaign/obligations.toml` holds the fourteen mutating operator
service tools: `images.extend`, `images.prune_expired`, `inventory.clear_override`,
`ops.reconcile_now`, `ops.reconcile_systems`, `ops.set_cost_class_coeff`,
`ops.set_host_capacity`, `ops.set_queue_paused`, `resources.deregister`, `resources.drain`,
`resources.register`, `resources.renew`, `resources.set_scheduling` and
`resources.set_status`. Their 216 cells are unbound: 56 functional (each tool × `default`,
`recovery` × `direct`, `gateway`) and 160 rejection (authentication and authorization for every
tool, validation for the twelve that take arguments; none has a project-isolation boundary).
The role overrides stay as they are: `inventory.clear_override` needs server and reconciler,
`ops.set_queue_paused` server and worker, `ops.reconcile_now` and `resources.drain` server,
worker, reconciler and authority.

The frozen charter is the issue's `WORK:SCOPE` with token `q3110-75f32755` (campaign
73d4edacff-8cf13f64-8877-4610-beaf-496193501043). It reuses the operator's 2026-10-07 approval of
the exclusions: the read-only operator tools (#2812), the worker-death tools (#3111) and native
POWER (#2818). Per the operator decisions of 2026-10-13/14, the authority cells run on the
installed local-authority lane; no remote authority cell exists in this group. No product
source, public contract, ADR or migration changes.

## Contract changes

`[implementations]` binds the 54 scenarios of the fourteen tools (14 functional, 14
authentication, 14 authorization, 12 validation) to
`tests/integration/test_operator_mutation_tool_cells_live.py::test_operator_mutation_tool_cell`.
A scenario id names no configuration, so each binding covers both configurations. No group,
owner, role or routing changes; the matrix digest moves only through the bindings.

## Approach and alternatives

Chosen: one carrier on the merged tool-cell frame (`run_tool_cell`, `prove_functional`,
`prove_rejection`), like #2812's. Each functional body seeds the state its tool needs through the
public API or the harness login's ADR-0769 seeding writes (`INSERT`/`UPDATE` on `resources`,
`INSERT` on `allocations`, the metering rows), observes the effect in a source the server did not
produce, and restores what it changed, checking the restore. The private-image upload helpers of
the catalog carrier move to `tests/integration/live_stack/private_images.py` so both carriers
share them; the catalog carrier's behaviour does not change.

Rejected:

- Mutate the lane's own host for every resource tool. A failed restore would break every later
  cell on the stack; a cell-owned runtime host confines the change to that cell.
- Compose the fault-inject provider for synthetic hosts. The lanes do not compose it, and
  composing it adds a discovered resource and diagnostics checks that change other carriers'
  expectations on the same stack.
- Direct SQL restores of state the harness login cannot write (`image_catalog`,
  `inventory_overrides`, `cost_class_coefficients`, `ops_control`). Widening the ADR-0769 grant
  set is a deployment change outside this issue; every restore here goes through a tool.
- Run the authority cells' bodies outside the authority frame and record the installed
  authority's revision alone. The operator's 2026-10-08 checkpoint forbids treating
  ordinary-process proof as authority evidence.

## Shared fixtures

- **Projects.** `P` is a fresh `cov-<8 hex>` project whose `project_state` snapshot every cell
  checks, as in #2812. A cell that places allocations does so in a separate fresh workload
  project `W`, metered with `seed_metering`; `W`'s allocation and ledger rows are history and are
  named under `owned`. An authority frame runs in its own fresh project.
- **Cell-owned runtime host `R`.** `resources.register` as `platform_admin`: kind
  `local-libvirt`, name `cov-<8 hex>`, `owner_project` the cell's `W` (or `P` when nothing is
  placed), cost class the lane host's, `vcpus` 2, `memory_mb` 2048, cap 2. Its `host_uri` is
  `qemu+tcp://<server host>:<server port>/system`, the server's own listener, which satisfies the
  register TCP preflight. Local-libvirt discovery and reaping read the configured
  `KDIVE_LIBVIRT_URI`, never a row's `host_uri`, and no System is ever provisioned on `R`;
  `owner_project` confines placement on it to `W`. `R` is deregistered at the end of the cell: a
  host that never held an allocation is deleted; one that did is cordoned with its lease cleared,
  because retained allocation rows keep a foreign key to it.
- **Placement probe.** `allocations.request` in `W` with `resource = {mode: id, resource_id}`,
  `vcpus` 1, `memory_gb` 1, `disk_gb` 1. A granted probe is released through
  `allocations.release` and checked `released`. A probe on a cordoned host must find no placement
  candidate: admission answers `configuration_error` and writes no allocation row in `W`. A
  capacity, budget or quota denial (`allocation_denied`, `quota_exceeded`) fails that check.
- **Platform snapshot.** `prove_functional` and `prove_rejection` take a snapshot that adds to
  `project_state(P)` the platform rows a cell must leave as found: every declared or discovered
  host's scheduling, health and cap, every override entry, `ops_control.queue_paused`, every
  coefficient except the cell-owned class (whose restore the cell checks itself), and the runtime
  hosts and private images `P` owns.
- **Tokens.** The tool under test is called through the cell's exposure with the functional
  grants below. Setup, placement and restore calls use separate `direct` tokens with their own
  subjects.

## Functional effects

Grants: `platform_operator` for `ops.set_cost_class_coeff`, `ops.set_host_capacity`,
`ops.set_queue_paused`, `ops.reconcile_now`, `resources.set_status` and
`resources.set_scheduling`; `platform_admin` for `images.extend`, `images.prune_expired`,
`inventory.clear_override`, `ops.reconcile_systems` and the three registration tools; both for
`resources.drain`, whose `force_release` mode needs admin. Platform roles do not nest.

- **`resources.register`**: registers `R` (owner `P`). The row for the returned id has kind
  `local-libvirt`, the requested name, `managed_by = runtime`, `owner_project = P`, status
  `available`, not cordoned, the requested `host_uri` and cost class, capabilities exactly
  `{vcpus, memory_mb, concurrent_allocation_cap}` as requested, and a lease in the future.
  Restore: deregister, row deleted.
- **`resources.deregister`**: two hosts `R1`, `R2` (owner `W`); `R2` holds a granted placement.
  Deregistering `R2` without `force` answers `conflict` and leaves `R2` and its allocation
  unchanged. Deregistering `R1` answers `deregistered` with disposition `deleted`; `R1`'s row is
  gone and `R2`'s is unchanged. Restore: release the allocation, deregister `R2` (cordoned).
- **`resources.renew`**: `R` (owner `P`); after one second, the renewed `lease_expires_at` equals
  the returned value and is later than before, and every other column is unchanged. Restore:
  deregister, row deleted.
- **`resources.set_status`**: `R` (owner `P`), cordoned first through `resources.set_scheduling`.
  `degraded`, then `offline`, then `available` each read back as the row's status while
  `cordoned` stays true. Restore: deregister, row deleted.
- **`resources.set_scheduling`**: `R` (owner `W`). `cordoned` sets the flag and leaves status
  `available`; the placement probe finds no candidate. `schedulable` clears the flag with status
  unchanged; the probe is granted on `R`. Restore: release, deregister (cordoned).
- **`ops.set_host_capacity`**: `R` (owner `W`, cap 2) holds one granted placement `A1`. Cap 1
  reads back in the row; the probe is denied `allocation_denied` with `reason = at_capacity`, cap
  1 and one in use, while `A1` stays granted. Cap 2 reads back and the probe is granted. Restore:
  release both, deregister (cordoned).
- **`ops.set_cost_class_coeff`**: the cell-owned class `cov-tool-cells`. `R` (owner `W`) carries
  it. Coefficient 1 reads back from `cost_class_coefficients`; a granted placement's `reserved`
  ledger row carries the class and amount `r1`. Coefficient 2 reads back; the next placement's
  `reserved` row is `2·r1` within one `KCU_QUANTUM`. Restore: the class's value before the cell,
  read back; release both placements, deregister (cordoned). The class row itself persists (see
  Failure model).
- **`ops.set_queue_paused`**: requires `queue_paused` false at start, else the cell stops
  `blocked`. Pause reads back true. A setup-token `ops.diagnostics` call then reports the four
  local worker-vantage checks with the worker-unavailable detail, and the `diagnostics_worker_check` job it
  enqueued is still `queued` with no worker after the server's 15 s wait. Resume reads back false;
  that job is then claimed (worker set) and `succeeded` within 120 s, and a second diagnostics
  call reports none of the four unavailable. A `finally` resumes the queue whatever failed.
- **`inventory.clear_override`**: the declared config host `D`, the first `[[fault_inject]]` or
  `[[remote_libvirt]]` entry of the exported `KDIVE_SYSTEMS_TOML` whose row is config-owned, holds
  the file's cap and has no override entry; none stops the cell `blocked`. A setup
  `ops.set_host_capacity` to the file cap + 1 writes a `detached` entry, and the cap still reads
  file cap + 1 after one reconcile interval. The clear answers `cleared` and the entry is gone;
  within three reconcile intervals the reconciler restores the file cap. A `finally` clears a
  remaining entry.
- **`ops.reconcile_systems`**: the harness inserts a config-owned `fault-inject` row named
  `cov-<8 hex>-departed`, which the file does not declare, and `R` (owner `P`) exists. The pass
  answers with the departed name in `pruned_names`; its row is gone, `R` is unchanged, every
  declared `[[fault_inject]]`/`[[remote_libvirt]]` name and every declared `[[image]]` has a
  config-owned row (an image whose source cannot be realized stays `defined`), and every declared
  cost class has its file coefficient. The loop enforces that end state too, so only the prune is
  attributed to the call; the rest compares the resulting catalog with the file.
  Restore: deregister `R`, row deleted.
- **`images.extend`**: a private image `I` owned by `P`, uploaded with a 600 s lifetime. Extending
  by 3600 s answers `extended`; the row's `expires_at` equals the returned value, is later than
  before and lies within the call's bracketed `now + 3600 s` (or the configured ceiling). Name,
  owner, visibility, state, digest and object key are unchanged, and the object's stored versions
  are the same. Restore: `images.delete`, object versions purged, both checked.
- **`images.prune_expired`**: a live local System booted from the public lane image, framed by
  `on_catalog_system` on an inner run. Three private images are uploaded, each in its own fresh
  project: `S` named like the lane image with a 1 s lifetime, `B` with the default lifetime, and
  `X` with a 1 s lifetime. The retention guard matches a private image by provider and name
  against every non-terminal System's catalog rootfs (`image_referenced_by_live_system`), so the
  live System references `S`. Just before the call the expired private images are exactly `X`
  and `S`; the prune answers `pruned` 1; `X`'s row and object versions are gone; `S` (expired,
  referenced) and `B` (not expired) remain. Restore: delete `S` and `B`, purge their objects,
  check both; the frame tears the System down.
- **`ops.reconcile_now`** (authority): inside `on_authority_system` on an inner run. A fresh
  unmetered project `X` gets `R` (owner `X`) and a harness-inserted `granted` allocation `E` whose
  lease ended a minute ago. The pass answers `ok`; `E` is `expired`; the expiry audit row lies
  inside the call. The authority-owned System stays `ready`, its domain stays active on the
  authority daemon, and its allocation and every other non-terminal allocation keep their state.
  Restore: deregister `R` (cordoned); the frame removes the System.
- **`resources.drain`** (authority): inside `on_authority_system` on an inner run. `L` is the lane
  host carrying the authority-owned System. `passive` answers `cordoned` listing exactly the
  database's live allocations on `L`, the System's among them; `L` reads cordoned with status
  unchanged; the System stays `ready` and active on the authority daemon and its allocation keeps
  its state; a placement probe on `L` finds no candidate. `resources.set_scheduling` restores `L`
  (in a `finally`). Then `force_release` on `R` (owner `W`, one granted placement) releases that
  placement (`released` 1, `skipped` 0, `failed` 0), cordons `R`, and leaves the System's
  allocation unchanged. Restore: deregister `R` (cordoned).

Both authority bodies prove `effect` and `cleanup` on the outer service cell from their own
observations plus the inner frame's records, so the outer record's context stays the service
host's. Without `LOCAL_AUTHORITY_LANE_CONFIG` the frame stops `blocked`, and a stack without an
installed authority records `missing:authority`, which fails the cell; neither counts as proof.

### Periodic-reconciler races

The reconciler loop runs the same expiry and image-prune repairs every 30 s, and the inventory
prune on each pass. Each race-prone cell seeds immediately before its call and attributes the
repair to the call: `E`'s expiry audit row and the pass's `expired_allocations` count both fall
inside the call; the departed name appears in `pruned_names`; `X` and the referenced `S` are the
only expired private images just before the call, and the answer's `pruned` count is exactly 1. When the loop got there first, the cell reseeds and tries
again, at most three times, then fails. The loop's repair is never counted.

## Rejection cells

| Boundary | Grants and arguments | Accepted outcome |
|---|---|---|
| authentication | a viewer of `P`, arguments naming an absent id or a fresh name | HTTP 401 for the foreign signature; the issued token's call is not 401 (it is denied by role, so nothing changes) |
| authorization | `platform_auditor` for the operator tools, `platform_operator` for the admin tools; a real target where one exists (`R` from a setup for the host tools, a private image of `P` from a setup for `images.extend`, the cell-owned class), otherwise an absent identity the role check refuses first | `authorization_denied` |
| validation | the functional grants, one mistyped argument per tool (a list for a string, a string for an integer or boolean, an unknown `kind`) | ADR-0722 §3 as amended |

A setup's own rows go before `cleanup` is proven, per `prove_rejection`.

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on the exclusive installed local-authority lab lane (x86_64),
     with two bring-ups sharing one evidence root: `default`, and `recovery` with
     `KDIVE_WORKER_DEATH_VERIFIER=docker`;
   - CI, which runs only the contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes; a cell whose prerequisite is absent stops `blocked` and never
     records success;
   - the lane left as found: the lane host schedulable, the queue running, the declared host at
     its file cap with no override, the cell-owned class at its prior value, no cell image or
     object, and the authority daemon's inventory unchanged.
3. **Accepted failure classes:**
   - History the snapshots exclude: audit, platform-audit and tool-invocation rows; `W`'s and
     `X`'s allocation and ledger rows; the finished diagnostics jobs.
   - Residue with no API delete path: the `cov-tool-cells` cost class row (reconcile never deletes
     a coefficient) and one cordoned, lease-cleared runtime row per cell that placed an
     allocation. Both are inert: no file declares the class, and a cordoned host takes no
     placement.
   - The authority release fence is not exercised. Fixture-mode Systems carry no authority
     binding in the database, because public authority-owned provisioning is blocked by #3144
     and #3148; `force_release` therefore drains a plain host, never the authority System's.
   - Concurrent carriers on one stack are not modelled: the queue pause, the lane-host cordon and
     the declared host's cap are stack-wide while a cell runs.
   - A killed run can leave the queue paused, the lane host cordoned or a `detached` entry; the
     runbook names the restore calls.
4. **Covered elsewhere:** the read-only operator tools (#2812), the worker-death tools (#3111),
   native POWER (#2818), the authority-binding fence (#3144, #3148, #3186).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| bindings | focused-test | `test_coverage_contract.py`: the 216 cells bind to the new node and the unbound set shrinks by exactly them |
| shared helper move | focused-test | the catalog carrier still imports and type-checks; `just type`, `just lint` |
| live cells | task-test-not-applicable | they act only against a live stack; proven by the two-configuration run on the authority lane (`qualify` over the tool cells) |
| runbook section | task-test-not-applicable | prose; `just docs-check` covers links and paths |

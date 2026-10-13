# Authority-owned System frame for HTTP tool cells (#3184) — design

Issue: #3184 (child of #3152). Plan: `docs/workflow/plans/2026-10-13-authority-system-frame.md`.
The remote-authority observer, remote lane, cleanup and invariance are #3209.

## Problem

The tool-cell frames (`on_lane_system`, `lane_target` in `tests/integration/live_stack/tool_cells.py`)
provision on the worker's libvirt or the ordinary remote daemon, observe the guest over SSH and
prove cleanup there. An authority-owned System lives on the authority's private session daemon
(`/run/kdive/provider-authority/libvirt/libvirt-sock`) with artifacts under
`/var/lib/kdive/provider-authority/{rootfs,console}`. Those frames cannot see it, and the
cells that need such a System (#3186) have no frame.

## Scope

One new module, `tests/integration/live_stack/authority_lane.py`, adds
`on_authority_system(run, base_url, issuer, db_url, *, project, body)`, the third System frame.
It is selected explicitly by the carrier, never derived from `cell.roles`: obligation groups mark
`authority` for reasons other than the frame (D1-A removes one).

**Why a sibling and not a `Lane`.** `on_lane_system` observes the guest by authorizing an SSH key
and the `LaneFrame` body receives only `(op, system_id, owned)`. The fixture System's domain is
not on the worker daemon that would serve the key job (and an authority-bound System refuses
`systems.authorize_ssh_key` before its first activation, `ordinary_mutation_is_fenced`). The
authority frame observes through the private daemon instead and hands its body the allocation,
per-System authority config and ledger, so it cannot fit those two contracts unchanged.

### Lane configuration

`KDIVE_LIVE_STACK_LOCAL_AUTHORITY_CONFIG` names an owner-only mode-0400/0600 JSON file, read with
the existing `load_config` metadata checks (moved into a shared reader). Closed fields:
`installed_revision` (40 hex), `authority_service` (the literal
`kdive-external-boot-authority.service`), `system_mode` (`fixture` | `public`). An unset variable
stops the cell `blocked`.

### Selection

- `cell.provider == "remote-libvirt"`: `blocked`, naming #3209, before any read or mutation.
- `system_mode == "public"`: `blocked` before any mutation, naming #3144 (authority provision
  answers `provider-conflict` after defining the domain and fences the allocation from release)
  and #3148 (the stored machine identity never matches). Attempting it strands the allocation,
  which this frame could not clean up. Operator decision 2026-10-13.
- `system_mode == "fixture"`: the runnable path below. It requires the lane's plain
  `local-libvirt` resource to carry no `authority_instance`: such a route would turn the frame's
  `systems.provision` into the blocked public path. After the allocation and before the
  provision, the frame reads the allocated resource's name and, when
  `local_authority_instance_for_resource` names an instance, releases the allocation and stops
  `blocked` naming the precondition.

### Fixture-mode lifecycle

1. **Gates, before any mutation:** authority unit active; `sudo -n cat` of the installed revision
   equals `installed_revision`; `require_deployed_revision` (server and every running fixed
   worker slot); `require_installed_authority_routes`; `require_evidence_read_access(db_url)`.
   Then the **before** inventory (below).
2. **Public creation:** an `admin` token for `project` (teardown needs it), `seed_metering`, `acquire_image` of the lane
   image (`LANE_IMAGES[arch]`), `allocations.request` (kind `local-libvirt`), and
   `systems.provision` to `ready` with a local rootfs pinned to that image's registered digest at
   the fixed path `/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2` (crashkernel `256M`).
   Admission's pin check proves the staged bytes. The frame then requires the System's
   `system_root_provenance` row to carry that digest and architecture.
3. **Private move:** a per-System `NativeAuthorityConfig` (fresh `kdive-2151-<sha12>-<nonce8>`
   prefix, `fixture_mode="create"`) and a `ResourceLedger`. `provision_authority_fixture` runs
   on a scratch ledger; its five reported names must equal the five deterministic names of the
   runbook's fixture-cleanup list, and are then recorded. If the create raises, the frame records
   those five deterministic names instead, so cleanup removes whatever a partial create left
   (`--remove` treats an absent name as success).
4. **Confinement and observation:** `require_authority_artifact_confinement`; then
   `provision_authority_fixture` in `verify-existing` mode (exact domain name, UUID, metadata,
   private overlay, console and baseline kernel, owners and modes); then the observer reads the
   domain as active and its `type`.
5. **Body:** `body(AuthoritySystem)` returns the `effect` observation; the frame proves `effect`
   with the exposure. `AuthoritySystem` carries `op`, `project`, `allocation_id`, `system_id`,
   `config` and `ledger` (a body records its own Investigations/Runs there and closes them).
6. **Cleanup**, in the installed carrier's documented order (#2966 refuses
   `allocations.release` while a System with external-boot history is not torn down):
   a. drain every non-terminal job whose payload names the System or one of its Runs
      (`payload->>'system_id'`, or `payload->>'run_id'` of a Run on the System), re-querying until
      none is open, `jobs.wait` to any terminal state within one bounded deadline;
   b. close each ledger Investigation still open;
   c. `remove_authority_fixture` (domain first);
   d. `systems.teardown` and wait for `torn_down`;
   e. `release_and_verify` against the worker daemon (Allocation `released`, worker domain and
      disks absent, capacity back);
   f. the **after** inventory must equal **before**.
   Steps run in order and stop at the first failure: a drain that times out leaves the fixture
   in place rather than removing a domain under a running job, and a fixture that did not go
   away blocks teardown. With no System (provision admission refused), only the release, the
   capacity check and f run.

**Inventory** (one fixed root-run program): names of every domain on the authority daemon, the
entry names of the two private artifact roots, and the worker daemon's `kdive-` domains. The
journal root is excluded: a lane is an audit record that outlives fixtures by design.

**Outcome.** Success records `effect` and `cleanup` (owned names, inventory digest). A failed
creation or body, including `KeyboardInterrupt` and task cancellation, still runs cleanup and
appends a `cleanup-attempt` artifact (exception type names only), then re-raises; if that cleanup
also fails, an `ExceptionGroup` carries the primary error first. A cleanup failure after a passing
body fails the scenario.

## Failure model

1. Actors and deployments: a campaign operator on the dedicated installed local-authority lane
   (#3183), with passwordless `sudo -n`, running one authority scenario at a time.
2. Invariants and assets: unrelated domains and artifacts on the authority and worker daemons;
   the authority's private artifacts stay unwritable by workers and the operator; capacity is
   released; evidence never names hosts, paths beyond the fixed roots, or credentials.
3. Accepted failure classes:
   - A concurrent scenario on the same lane changes the inventory: the frame fails (safe
     direction); the runbook requires exclusivity.
   - A killed process (SIGKILL) skips cleanup: the runbook's exact-name fixture removal, then
     `systems.teardown` and `allocations.release`, cover it.
   - A concurrent writer to the fixed worker rootfs between admission and the move changes the
     copied bytes undetected (the provenance row keeps the admitted digest): accepted under the
     runbook's lane exclusivity.
   - The fixture move depends on install routing through the server binding, which #3138 may
     change: the frame then fails loud at the body, not silently.
4. Covered elsewhere: remote authority frame #3209; public provisioning defects #3144/#3148;
   fault construction #3185; cell rebinding #3186; ownership admission #3138.

### Threat model

- Boundaries added: two fixed programs run under `sudo -n` (the inventory/observer, as root
  with the authority venv's Python); widened: none. Existing boundaries reused unchanged:
  the fixture script, the confinement probes, the route preflight.
- Actors: the local operator (trusted to run the lane); worker identities (untrusted for
  private artifacts, probed by confinement).
- Controls: program text is a module constant; its only argument is a `UUID` re-validated
  by `UUID()` in the program; output is bounded JSON, parsed strictly, sizes capped; no shell.
- Out of scope: the authority's own isolation guarantees (ADR-0741 limitation stands).

## Success

1. Remote cells and `public` mode stop `blocked` before any mutation with the named owners.
2. In `fixture` mode, a gate failure creates nothing.
3. A passing body proves `effect` and `cleanup`; the inventory is unchanged.
4. A failing body and a failing cleanup each fail the scenario as stated in Outcome.
5. A live run on the installed lane proves 3 and the body-failure half of 4.

## Validation

- Unit (`tests/integration/live_stack/test_authority_lane.py`): selection stops, the routed
  resource stop, gate order before mutation, cleanup order (teardown before release), partial
  fixture create, cleanup on body failure and on cancellation, cleanup failure, `ExceptionGroup`,
  inventory drift, the drain query naming Run jobs, config rejection; boundaries mocked
  (subprocess, HTTP client, DB).
- Live (`tests/integration/test_authority_lane_frame_live.py`, `live_stack`): a success body and
  a raising body on the installed local-authority lane.

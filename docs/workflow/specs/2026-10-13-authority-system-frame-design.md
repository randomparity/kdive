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
Completion criterion 1 was amended to this explicit carrier selection by operator decision
(2026-10-13); the ordinary `Lane`/`LaneFrame`/`on_lane_system` frames, boundary assertions and
evidence owners stay unchanged, and the `tool_cells.py` docstring names the third frame.

**Why a sibling and not a `Lane`.** `on_lane_system` observes the guest by authorizing an SSH key
and the `LaneFrame` body receives only `(op, system_id, owned)`. The fixture System's domain is
not on the worker daemon that would serve the key job (and an authority-bound System refuses
`systems.authorize_ssh_key` before its first activation, `ordinary_mutation_is_fenced`). The
authority frame observes through the private daemon instead and hands its body the allocation,
per-System authority config and ledger, so it cannot fit those two contracts unchanged.

### Lane configuration

`LOCAL_AUTHORITY_LANE_CONFIG` names an owner-only mode-0400/0600 JSON file, read with
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
entry names of the two private artifact roots, and every domain the worker daemon defines. The
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

- Boundaries added: one fixed program run under `sudo -n` (the inventory/observer, as root
  with the authority venv's Python in isolated mode) and a `sudo -n cat` of the installed
  revision file; widened: none. Existing boundaries reused unchanged:
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

## Addendum: remote authority branch (#3209)

Issue: #3209 (child of #3152), as narrowed by its "Scope correction" (operator decision,
2026-10-13). Plan: `docs/workflow/plans/2026-10-13-remote-authority-system-frame.md`. This
addendum replaces the remote `blocked` stop above; everything else in this spec is unchanged.

### Remote scope

`on_authority_system` sends a `remote-libvirt` cell to a remote branch in the same module. It
reuses the frame's job drain, Investigation closing, root-provenance check, failure grouping and
`AuthoritySystem` body contract. `tool_cells.py`, `Lane`, `LaneFrame` and `lane_for` stay
unchanged. No fixture path exists remotely, so the branch provisions through public HTTP
provisioning on the dedicated authority-routed remote resource. A failure of that first real
construction is a finding to record as a narrower blocker, not to work around.

**Configuration.** `KDIVE_LIVE_VM_REMOTE_AUTHORITY_CONFIG` names the existing closed
`RemoteAuthorityProofConfig`; its fields are unchanged. The frame replaces the file's
`ownership_prefix` with one minted per invocation (`kdive-2151-<revision12>-<hex8>`) and labels
the body's `ResourceLedger` with it. `KDIVE_SYSTEMS_TOML` must name the lane's inventory, as for the
local branch.

**Observer.** `remote_authority_inventory(config, system_id=None)` in
`tests/live_vm/installed_remote_authority_support.py` runs one fixed program over the existing
bounded SSH seam: `sudo -n -u kdive-provider-authority
/opt/kdive-provider-authority/.venv/bin/python -I -c <program> [<uuid>]`, as the authority account
and never root (that account owns the interpreter tree). It reads the storage pool name from the installed authority environment
(`/etc/kdive/provider-authority.env`, `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`, default `default`, the
authority's own default), opens the private session socket
`/run/kdive/provider-authority/libvirt/libvirt-sock`, refreshes the pool and reports the host
architecture, the daemon's domain names, the pool's volume names and, for the named System,
whether its domain runs and its domain type. `require_installed_authority(config)` reads the unit
state and, as the same account, `/opt/kdive-provider-authority/revision` over the same seam.

**Selection, before any mutation** (each a `blocked` stop):

- the remote config variable is unset, or `KDIVE_SYSTEMS_TOML` does not name a file;
- `resource_name` is not declared, or carries no authority route for `authority_instance`;
- the control plane's catalog does not hold exactly one registered public `remote-libvirt` image
  for the cell's architecture with a digest and a `root_spec`, or the database does not hold the
  resource. Admission's own root checks (authority, source kind, identity, architecture) still
  run after allocation; a row that fails them is failure class 3.4, not a stop;
- the remote host's architecture is not the cell's.

**Lifecycle.**

1. Gates: remote unit active and its installed revision equal to `installed_revision`;
   `require_deployed_revision` for the server and every running fixed worker slot;
   `require_evidence_read_access`. Then the **before** inventory: remote domains and pool volumes.
2. An `admin` token, `seed_metering`, `allocations.request` by resource id, `systems.provision`
   of a `disk-image` profile whose `base_image_source` is the selected catalog image, and a wait
   for `ready`. `require_root_provenance` must match that image's digest and architecture.
3. The observer must report the System's domain running on the private daemon; its type gives
   `accelerator`. The body then runs as in the local branch.
4. Cleanup, in order, stopping at the first failure: drain the System's and its Runs' jobs; close
   open ledger Investigations; `systems.teardown` to `torn_down` (through the authority); the
   observer must report no domain for the System and no pool volume naming its id; release the
   allocation and require the capacity in use unchanged; the **after** inventory must equal
   **before**. With no System, only the release and the inventory check run.

Outcome and failure grouping are those of the local branch. Evidence records the image digest,
guest architecture, accelerator, owned names (allocation, System id, domain name), drained job
states and the inventory digest; it never records the SSH target, pool name or host paths.

### Remote failure model

1. Actors and deployments: a campaign operator running one authority scenario at a time from the
   control-plane host of the installed lanes (#3183), with batch-mode SSH to the dedicated remote
   authority host and passwordless `sudo -n -u kdive-provider-authority` there.
2. Invariants and assets: the remote authority's other domains and pool volumes; released
   capacity; the private daemon and pool stay unreachable to workers (the frame only reads them,
   as the authority account, through the operator's own access); evidence free of host identities.
3. Accepted failure classes:
   - A concurrent scenario on the remote authority changes the inventory: the frame fails (safe
     direction); the runbook requires exclusivity.
   - A SIGKILL skips cleanup: the runbook's remote cleanup (teardown, release, manual removal on
     the private daemon) covers it.
   - Pool refresh during an authority operation can fail the read: the frame fails loud.
   - A failed public provision (for example a fenced allocation) fails cleanup and leaves state
     for the runbook: recorded as a narrower blocker, as the scope requires.
4. Covered elsewhere: local branch (#3184); fault construction (#3185); qualification (#3186);
   local provisioning defects (#3144/#3148); ownership admission (#3138).

### Remote threat model

- Boundary added: one fixed program run as the authority account on the remote authority host
  over SSH. Widened:
  the bounded SSH seam gains a larger output bound for this read (1 MiB stdout, 120 s).
- Actors: the operator (trusted to run the lane); the authority account and every process sharing
  its UID (its session daemon and the guests' QEMU) — untrusted for privilege, so nothing runs as
  root on their files; worker identities (untrusted, not on this path).
- Controls: the program is a module constant; its only argument is a `UUID` validated on both
  sides; the pool name is validated against the authority's own pattern before use; `shlex.join`
  quotes the remote command; output is bounded and parsed strictly into closed shapes; errors
  surface as type names in evidence.
- Controls on privilege: the program, its interpreter and the revision read run as the authority
  account through `sudo -n -u`, so a replaced interpreter or module gains nothing; the account
  already reads the environment file (group-readable), its own socket and the revision file. Its
  answers are evidence, not a security decision: a compromised account can lie to the frame,
  which then fails or passes a lane it already controls.
- Out of scope: the remote authority's own isolation guarantees; SSH host-key and account
  management (operator-preconfigured, batch mode only); the local lane's root-run inventory
  (merged #3184 code, not changed here).

### Remote success and validation

1. A remote cell with no config, an unrouted resource, no catalog image carrying a digest and a
   `root_spec`, or a foreign host architecture stops `blocked` with no mutation.
2. A passing body proves `effect` and `cleanup`; the remote inventory is unchanged.
3. A failing body, a remaining domain or volume, and inventory drift each fail the scenario.
4. Two invocations mint different prefixes; the config file's prefix is never used.

Unit and live validation are listed per contract in the plan's Verification entries.

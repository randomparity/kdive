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
and the `LaneFrame` body receives only `(op, system_id, owned)`. An authority System refuses
ordinary mutation, including `systems.authorize_ssh_key`, before its first activation
(`ordinary_mutation_is_fenced`); a fixture System's domain is not on the worker daemon that would
serve the key job. The authority frame observes through the private daemon instead and hands its
body the authority identifiers it needs, so it cannot fit those two contracts unchanged.

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
- `system_mode == "fixture"`: the runnable path below.

### Fixture-mode lifecycle

1. **Gates, before any mutation:** authority unit active; `sudo -n cat` of the installed revision
   equals `installed_revision`; `require_deployed_revision` (server and every running fixed
   worker slot); `require_installed_authority_routes`; `require_evidence_read_access(db_url)`.
   Then the **before** inventory (below).
2. **Public creation:** operator token for `project`, `seed_metering`, `acquire_image` of the lane
   image (`LANE_IMAGES[arch]`), `allocations.request` (kind `local-libvirt`), and
   `systems.provision` to `ready` with a local rootfs pinned to that image's registered digest at
   the fixed path `/var/lib/kdive/rootfs/live-vm-provisioned-rootfs.qcow2` (crashkernel `256M`).
   Admission's pin check proves the staged bytes. The frame then requires the System's
   `system_root_provenance` row to carry that digest and architecture.
3. **Private move:** a per-System `NativeAuthorityConfig` (fresh `kdive-2151-<sha12>-<nonce8>`
   prefix, `fixture_mode="create"`) and a `ResourceLedger`;
   `provision_authority_fixture` records the five fixture names.
4. **Confinement and observation:** `require_authority_artifact_confinement`; then
   `provision_authority_fixture` in `verify-existing` mode (exact domain name, UUID, metadata,
   private overlay, console and baseline kernel, owners and modes); then the observer reads the
   domain as active and its `type`, and the worker daemon no longer defines it.
5. **Body:** `body(AuthoritySystem)` returns the `effect` observation; the frame proves `effect`
   with the exposure. `AuthoritySystem` carries `op`, `project`, `allocation_id`, `system_id`,
   `config` and `ledger` (a body records its own Investigations/Runs there and closes them).
6. **Cleanup, always:** drain every non-terminal job whose `payload->>'system_id'` is the System
   (`jobs.wait` to any terminal state, bounded); `remove_authority_fixture` (domain first);
   the observer proves the five names absent; `release_and_verify` against the worker daemon
   (System `torn_down`, Allocation `released`, worker domain absent, capacity back); then the
   **after** inventory must equal **before**.

**Inventory** (one fixed root-run program): names of every domain on the authority daemon, the
entry names of the two private artifact roots, and the worker daemon's `kdive-` domains. The
journal root is excluded: a lane is an audit record that outlives fixtures by design.

**Outcome.** Success records `effect` and `cleanup` (owned names, inventory digest). A body
failure still runs cleanup; a cleanup failure after a passing body fails the scenario; both
failing raise an `ExceptionGroup` with the body's error first. Every failed path appends a
`cleanup-attempt` artifact with exception type names only.

## Failure model

1. Actors and deployments: a campaign operator on the dedicated installed local-authority lane
   (#3183), with passwordless `sudo -n`, running one authority scenario at a time.
2. Invariants and assets: unrelated domains and artifacts on the authority and worker daemons;
   the authority's private artifacts stay unwritable by workers and the operator; capacity is
   released; evidence never names hosts, paths beyond the fixed roots, or credentials.
3. Accepted failure classes:
   - A concurrent scenario on the same lane changes the inventory: the frame fails (safe
     direction); the runbook requires exclusivity.
   - An interrupted process (SIGKILL) skips cleanup: the runbook's exact-name manual removal
     covers it.
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

- Unit (`tests/integration/live_stack/test_authority_lane.py`): selection stops, gate order
  before mutation, cleanup on body failure, cleanup failure, `ExceptionGroup`, inventory drift,
  config rejection; boundaries mocked (subprocess, HTTP client, DB).
- Live (`tests/integration/test_authority_lane_frame_live.py`, `live_stack`): a success body and
  a raising body on the installed local-authority lane.

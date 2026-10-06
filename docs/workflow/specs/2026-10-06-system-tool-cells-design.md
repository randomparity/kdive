# Provider-lane tool cells and the x86_64 local System tool cells (#3062)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment; evidence identity per
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md). Builds on the #2811
tool-cell harness ([spec](2026-10-02-core-tool-cells-design.md)) and the #2809 local catalog frame
(`tests/integration/live_stack/scenario.py`, `on_catalog_system`).

## Problem

Every tool-cell carrier so far proves `service` cells. `bindings()` in
`tests/integration/live_stack/tool_cells.py` binds only those, so a `provider` cell has no expected
`Context` and `qualify` reports `input-binding-missing`. A provider cell also needs a guest: its
contract cell names a guest architecture and a `kvm` accelerator, and `qualify` requires the
record to carry the guest OS, guest architecture and accelerator it bound. No frame provides a
System for a provider tool to act on, prove the effect against, and reclaim.

The lifecycle group (`owner = 3062`) holds eleven tools. The operator split it on 2026-10-06:
#3062 keeps the frame and the six `systems.*` tools, and the five run and image tools move to
#3119. The six tools' x86_64 local-libvirt cells are 24 functional and 96 rejection cells
(two configurations, two exposures; authentication, authorization, project-isolation and
validation boundaries).

## Scope

1. **Ownership split** in `obligations.toml`: the six `systems.*` tools form their own provider
   group (owner 3062). `images.publish`, `runs.boot`, `runs.cancel`, `runs.install` and
   `runs.release_external_boot` form a provider group owned by 3119 that keeps `kernel = true`
   and `authority = true`. In `contract.py` `_tool_cells`, owner 3119 follows owner 3062's
   routes: `ppc64le` cells go to #2818 and `remote-libvirt` cells to #3080, so #3080 still owns
   all 216 remote x86_64 cells.
2. **Contract flags of the six tools** (operator decision, 2026-10-06, recorded in the issue's
   `WORK:SCOPE`). The new group drops `kernel` and `authority`: the six
   tools take and produce no kernel (provision boots the catalog image's own kernel through
   `direct-kernel`, ADR-0272), and on a Resource with no authority binding none of them routes
   through the provider authority (ADR-0623). Their functional cells then require `server`,
   `worker` and `reconciler` and no kernel input. The #3119 group keeps both flags.
3. **Frame** in `tests/integration/live_stack/tool_cells.py`, provider-generic where it costs
   nothing:
   - `bindings()` also binds every bound `local-libvirt` tool cell whose guest architecture is the
     host's: host identity, the lane image's catalog `guest_os` and `guest_arch`, the cell's
     accelerator and the staged image's SHA-256 (`image_smoke.staged_image`, null when unstaged).
     `LANE_IMAGES` names one catalog image per architecture: `fedora-kdive-ready-44` for x86_64.
     Service cells bind as before. Cells of another provider or architecture stay unbound.
     A bound cell that declares kernel inputs gets the lane's kernel identity: the CLI's optional
     `--kernel-baseline NAME` binds the verified `$KDIVE_FIXTURE_ROOT/NAME` fixture through
     `deep_lifecycle.bound_kernel`, and without it those fields stay null, which `qualify` reports
     as `required-input-missing`. No cell of this change declares kernel inputs; #3119's do.
   - A cell that declares the `authority` role keeps it: `run_cell` already records
     `missing:authority` and fails the parameter, and `qualify` reports `deployed-role-missing`.
     A unit test pins that for a provider cell.
   - `run_tool_cell` skips, without a record, a provider cell whose host architecture is not this
     host's.
   - `on_lane_system`: the functional frame. It wraps `on_catalog_system` (fresh funded project,
     image acquisition, allocation, provision to `ready`, accelerator from the domain XML, owned
     disks, and the `cleanup` proof by `release_and_verify`), then authorizes a frame key through
     the operator client, probes the guest over SSH, checks it against the catalog entry, and
     records `guest_os` and `guest_arch`. The body proves `effect`. `on_catalog_system` gains one
     optional argument, `provision`, so the `systems.provision` cell provisions through the cell's
     exposure; every other caller is unchanged.
   - `lane_target`: the rejection target, read once per stack like the configuration. It
     provisions one System from the lane image in a fresh funded project, observes the same
     guest identity, accelerator and image digest, tears it down and releases the allocation
     with the same cleanup proof, and waits until that project's snapshot is stable. Every role
     check of the six tools runs before any state check (`ssh_access.py`, `admin.py`,
     `services/systems/admission.py`), so a torn-down System and a released Allocation are valid
     rejection targets that no background work changes. Each rejection cell records the target's
     observed context.
4. **Carrier** `tests/integration/test_system_tool_cells_live.py::test_system_tool_cell`,
   parametrized over the six tools' native local-libvirt cells. `[implementations]` binds the
   30 scenarios (6 functional, 6 × 4 rejection; the ppc64le local cells share these scenarios and
   are never parametrized on an x86_64 host).
5. A runbook section and a live run of both lanes on the disposable lab host.

No product source, ADR or migration change.

### Functional effects

Each functional cell runs in its own fresh project P, framed by `on_lane_system`. The tool under
test is called through the cell's exposure with the tool's gate as grants: contributor of P for
`provision`, `authorize_ssh_key` and `reprovision`; viewer of P for `ssh_info` and
`check_ssh_reachable`; admin of P for `teardown`. Setup and observation calls (allocation,
`jobs.wait`, `systems.get`) use the frame's operator client. Independent sources are the worker's
libvirt domain and disk files, the guest itself over SSH, and `resources.availability`.

- `systems.provision`: the cell's call provisions the System. The frame waits for `ready`, the
  guest answers SSH as root and matches the catalog entry. The domain XML's vCPU count and memory
  equal the allocation's 2 vCPU and 2 GiB, and summed `in_use` capacity is above its value before
  the allocation.
- `systems.ssh_info`: the returned port equals the domain XML's loopback `hostfwd` port. SSH to
  the returned host and port authenticates with the frame key, and the guest's DMI
  `product_uuid` equals the libvirt domain UUID, so the coordinates reach this System's guest.
- `systems.authorize_ssh_key`: before the call, the frame key (the unrelated key) is in root's
  `authorized_keys`. The cell authorizes a second fresh key and drains the job. The second key
  authenticates, the frame key still authenticates, and `authorized_keys` after the call is the
  set before it plus exactly the new key.
- `systems.check_ssh_reachable`: the drained job's verdict is `reachable`. Independently, a TCP
  connection to the endpoint reads an `SSH-` banner and `systemctl is-active sshd` in the guest
  reports `active`.
- `systems.reprovision`: the cell writes a marker file in the guest, then reprovisions with the
  same catalog profile and waits for `ready`. After a fresh frame key is authorized (the new
  install has no prior key), the guest answers with a new `boot_id` and no marker. Each owned disk
  either no longer exists or is a different file (a new inode), and the new domain's disks join
  the owned set the cleanup proves absent.
- `systems.teardown`: the cell's call tears the System down; the cell drains the job and waits for
  `torn_down`. The worker's libvirt no longer defines the domain and every owned disk is absent.
  The frame's cleanup then releases the allocation and proves capacity returned to its value
  before the allocation.

### Rejection cells

The target project T is `lane_target`'s; the snapshot is `project_state(T)`. Arguments name the
target System (`system_id`, plus a fresh public key or the lane catalog profile where the tool
needs one) or, for `systems.provision`, the target's Allocation with the lane catalog profile.

| Boundary | Grants | Accepted |
|---|---|---|
| authentication | viewer of T; every issued-token control call is then refused by role or readiness without a write | HTTP 401 for the foreign signature; the issued token is not refused |
| authorization | member of T without a role for `ssh_info` and `check_ssh_reachable`; viewer of T for the contributor tools; contributor of T for `teardown` | `authorization_denied` |
| project-isolation | operator of a fresh project | `not_found` for the SSH tools, `configuration_error` for the others; in both cases indistinguishable from the answer for an absent id (`absent_twin`) |
| validation | the functional grants on T, with a mistyped `system_id` or `allocation_id` | ADR-0722 §3 as amended |

The `gateway` validation cells of `systems.provision` and `systems.reprovision` fail under the
amendment's rule (their binding failures are re-enveloped without `field_errors`), four cells in
all. They are recorded as failing, not covered, until a decision names their evidence.

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier on a disposable KVM lab host, one stack per lane (`default`,
     and `recovery` started with `KDIVE_WORKER_DEATH_VERIFIER=docker`), local-libvirt only;
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes; a contract whose ownership matches the operator's split;
   - the host left as found: every System the cells create is torn down, its domain undefined,
     its disks removed and its capacity returned, proven per cell.
3. **Accepted failure classes:**
   - A cell killed mid-run can leave a System and its allocation; `demo-down.sh --wipe --yes`
     clears them, as for the earlier carriers.
   - Torn-down Systems, released allocations, their ledger rows and audit rows stay as history.
   - The four `gateway` validation cells of `provision`/`reprovision` fail (above).
   - A failed `lane_target` fails every rejection cell of the lane; it is not retried.
   - Concurrent carriers on one stack are not modelled; capacity comparisons assume this carrier
     is the only allocator.
4. **Covered elsewhere:** run/image tools (#3119), remote cells (#3080), ppc64le cells (#2818),
   the capability boundary (#2814), operator force and recovery tools (#3110-#3112), CLI
   translation (#3099), service-lane run/system/job tools (#3097), filtering-list isolation
   (#3108).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| ownership split, flags and routing | focused-test | `test_coverage_contract.py`: owner sets of 3062 and 3119, 3080 still 216, ppc64le to 2818, the six tools' roles and inputs |
| bindings of provider cells | focused-test | `test_tool_cells.py`: native local cells bound with guest, accelerator and image digest; foreign-arch and remote cells unbound; service cells unchanged |
| kernel inputs and authority role | focused-test | `test_tool_cells.py`: a bound cell declaring kernel inputs gets the supplied kernel fields, one that does not gets none; a provider cell declaring `authority` fails its parameter on a stack without that role |
| foreign-arch skip | focused-test | `test_tool_cells.py`: `run_tool_cell` skips a ppc64le cell on x86_64 before any stack read |
| `on_catalog_system` provision override | focused-test | existing frame tests stay green; the override is exercised live |
| carrier bindings | focused-test | `test_coverage_contract.py`: the 30 scenarios bind to the new node |
| live cells | task-test-not-applicable | act only against a live stack; proven by the two-lane lab run and `qualify` |
| runbook | task-test-not-applicable | prose; `just docs-check` |

# Remote power, snapshot and control tool cells on x86_64 (#3116)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
with its 2026-10-13 amendment (capability cells follow the support verdict) and
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) (evidence identity).
Builds on the local drivers of #2815 ([spec](2026-10-08-local-power-snapshot-proof-design.md)) and
#3115 ([spec](2026-10-14-local-control-tool-cells-design.md)) and the remote provider lane of
#3080 ([spec](2026-10-06-remote-system-tool-cells-design.md)).

## Problem

The coverage contract routes 160 x86_64 `remote-libvirt` cells of eight tools to #3116:
`control.power`, `control.capture_traffic`, `control.diagnostic_sysrq`, `control.watch_for_crash`,
`systems.snapshot`, `systems.restore`, `systems.list_snapshots` and `systems.delete_snapshot`.
That is 32 functional and 128 rejection cells (8 tools × `default`/`recovery` × `direct`/`gateway`
× {functional, authentication, authorization, project-isolation, validation}). Remote-libvirt
supports every gated capability, so there is no capability cell (the issue's 188 predates the
amendment). None is bound.

The local drivers already prove these tools, but they observe the worker's own libvirt and host
filesystem, so they cannot run on a remote System:

- `power_snapshots.domain_state`, `checkpoints` and `domain_defined` open `worker_libvirt_uri()`;
- `power_snapshots.domain_kernel` hashes the domain's direct `<os><kernel>` file on this host,
  while a remote install is in-guest (disk-image boot, kernel in the guest's `/boot`);
- `control_cells.capture_filters` reads QMP through the worker's libvirt;
- the capture spool joins `owned` as the local `pcap_dir(system)`, while the remote capturer writes
  a per-job volume `kdive-pcap-<system>-<job>.pcap` in the provider's storage pool
  (`providers/remote_libvirt/lifecycle/traffic_capture.py`, ADR-0432);
- the synthetic flow goes to `10.0.2.2`, the user-network gateway. A remote domain's capture taps
  the libvirt-network NIC (`discover_netdev_id`: the first aliased `<interface>`, `hostnet0`); its
  user-mode SSH NIC is `restrict=on` and is not captured (`lifecycle/xml.py`, ADR-0291/0721);
- the control carrier's rejection target, `capture_cells.capture_target`, provisions with
  local-libvirt crash options, opens a drgn-live session and keeps only lane keys, so it records
  no provider host; it is local by construction.

## Scope

1. **Provider lane** (`tests/integration/live_stack/tool_cells.py`). `Lane` gains three fields
   filled by `_local_lane` and `_remote_lane`:
   - `connect() -> libvirt.virConnect`: the provider's libvirt for a native read. Local:
     `worker_connect` (`libvirt.open(worker_libvirt_uri())`). Remote: `observer(host.dest)`, the
     operator's `qemu+ssh` observer that `remote_xml` and `remote_volume_absent` already use.
   - `spool(system_id, job_id) -> str`: the provider-host path a capture job writes. Local:
     `pcap_dir(system)`. Remote: `remote_pcap_path(dest, pool, system, job)`, the pool's target
     path joined with `pcap_volume_name` (`remote_lifecycle.py`).
   - `installed_kernel`: the deep-body kernel observer. Local: `domain_kernel`, moved unchanged
     from `power_snapshots.py`. Remote: `guest_boot_kernel(family=fedora)`, as #2810 and #3120 do.
2. **Power and snapshots** (`power_snapshots.py`). `domain_state`, `checkpoints` and
   `domain_defined` take a `connect`; `Session` passes `guest.lane.connect`; `functional` boots with
   `guest.lane.installed_kernel`; `protected_state`/`snapshot_of` take the lane's `connect`.
   `domain_defined` keeps `worker_connect` as its default for `capture_cells`. The scenario bodies,
   oracles, rejection arguments and target (`lane_target`, per provider) are unchanged.
3. **Control** (`control_cells.py`, `capture_cells.py`). `capture_cells.boot` observes the kernel
   with `guest.lane.installed_kernel` (the local value is the same function). In `control_cells`:
   - `capture_filters` takes a `connect`; the capture body passes `guest.lane.connect`;
   - the capture spool joins `owned` as `guest.lane.spool(system, job)` once the call returns the
     job id (locally the same directory as before);
   - the flow's destination is `10.0.2.2` on the local lane and, on the remote lane, the guest's
     IPv4 default-route gateway read in the guest (`ip -4 route show default`), validated as an
     IPv4 address; an empty or malformed answer fails the cell;
   - `run_target(run, base_url, issuer, db_url, fixture)` is the remote rejection target: one
     torn-down lane System per stack and provider whose Run booted the fixture (`boot`), no
     crash options, no debug session; it keeps the provider-host keys with the lane keys, and its
     protected state is `capture_target`'s (project rows, jobs naming the System or Run, domain
     definedness) read through the lane's `connect`. A failed preparation is replayed, not
     retried, as in `lane_target` and `capture_target`.
4. **Carriers.** `test_power_snapshot_tool_cells_live.py` and `test_control_tool_cells_live.py`
   also parametrize the x86_64 remote cells (`REMOTE_LANE_FAMILIES`, as the System carrier does);
   the control carrier aims remote rejections at `run_target`, local ones at `capture_target`
   unchanged. Each loads the fixture for the cell's guest architecture.
5. **Bindings.** `[implementations]` binds the 40 `tool/remote-libvirt/<tool>/<variant>/<kind>`
   scenarios: 25 to the power carrier, 15 to the control carrier. The ppc64le remote cells share
   these ids (owner #2818) and are never parametrized. The existing
   `tool_cells bindings --remote --kernel-baseline longterm` writes their contexts unchanged.
6. **Runbook** section in `docs/operating/runbooks/remote-live-stack.md` and one live run of both
   configurations against a separate x86_64 provider host (operator-granted).

No product source, ADR, migration, `contract.py` or matrix change. Functional effects and
rejection boundaries are #2815's and #3115's, observed through the remote lane, and the cleanup
is `remote_cleanup` (domain undefined, owned volumes, external snapshot overlays and pcap volumes
absent on the provider host, no new `kdive-*` domain, capacity returned). A remote disk-only
revert, a memory snapshot of a domain carrying the user-mode NIC, or a pcap fetch-back failure is
recorded as a failing cell and reported as a product finding, never fixed or waived here.

Considered and rejected:

- *Branch on `lane.provider` inside each helper* instead of lane fields: the provider host's
  destination and pool live only in `_remote_lane`, so each helper would re-probe the host.
- *Make `capture_target` lane-aware for remote control rejections*: it would run a remote drgn-live
  session (#3113's unproven area) and local crash options to gate cells that need neither, and
  would change a target #2814/#3115 already proved.
- *Reuse `lane_target`*: `control.capture_traffic` resolves a Run, which `lane_target` lacks;
  adding one changes the protected state of every carrier sharing it.
- *Send the remote flow to `10.0.2.2`*: the user-mode NIC is not the captured netdev and refuses
  guest-initiated traffic (`restrict=on`).
- *Move the local control rejections to `run_target`*: local cells are #3115's, proven; no gain.

## Failure model

1. **Actors and deployments:**
   - an operator running `live_stack` from a control plane whose stack runs the committed
     candidate (`demo-up.sh`; `recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`), against one
     separate disposable x86_64 `remote-libvirt` provider host used by this lane alone;
   - CI and `just test`, which run only the unit and contract tests.
2. **Invariants and assets at stake:**
   - no cell counts without its observed effect (restored state, power state, pcap flow, console
     SysRq output, crash-signature verdict) and its provider-host cleanup;
   - the provider host is left as found: every System a completing cell creates is torn down,
     its domain undefined, its overlays, snapshot overlays and pcap volumes gone;
   - the provider destination and pool never enter evidence (ADR-0715);
   - local cells keep their behaviour (moved observer, same values).
3. **Accepted failure classes:**
   - a killed cell can leave a System, allocation, domain, volume or pcap on the provider host;
     `demo-down.sh --wipe --yes` plus the runbook's provider check clear them;
   - a failed or blocked `lane_target`/`run_target` fails or blocks every rejection cell of that
     provider lane, not retried;
   - torn-down Systems, terminal Runs, the stored Run-owned pcap and audit rows remain as history;
   - a cell blocked before the provider host is observed records the control-plane host;
   - concurrent allocators on the stack or the provider host are not modelled.
4. **Covered elsewhere:** local cells (#2815, #3115); ppc64le and POWER qualification (#2818);
   remote capture and debug cells (#3114, #3113); the capability boundary (#2814).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| lane fields | focused-test | `test_tool_cells.py`: local lane reads the worker libvirt, `pcap_dir`, `domain_kernel`; remote lane uses the observer, the pool path and `guest_boot_kernel(fedora)` |
| remote pcap path | focused-test | `test_remote_lifecycle.py`: pool target joined with the job's volume name; a pool without a target path fails |
| native reads through `connect` | focused-test | `test_power_snapshots.py`: state, checkpoints and definedness read the given connection and close it |
| kernel observer from the lane | focused-test | `test_power_snapshots.py`, `test_control_cells.py`: `functional`/`boot` hand `deep_body` the lane's observer |
| remote flow gateway | focused-test | `test_control_cells.py`: IPv4 gateway accepted; empty or non-IPv4 refused |
| run target | focused-test | `test_control_cells.py`: one preparation per stack and provider; a failure replayed with its artifacts; host keys kept, kernel keys dropped |
| remote protected state | focused-test | `test_control_cells.py`: jobs of System and Run plus definedness through the lane |
| carrier bindings | focused-test | `test_coverage_contract.py`: the 200 remote power and 120 remote control cells bind their carriers |
| live cells | task-test-not-applicable | act only against a live stack and provider host; proven by the lab run and `qualify` |
| runbook | task-test-not-applicable | prose with no executable consumer; `just docs-check` |

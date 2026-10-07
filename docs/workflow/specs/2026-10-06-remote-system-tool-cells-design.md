# Remote provider-lane tool cells and the x86_64 remote System tool cells (#3080)

Decision records: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md)
and its 2026-10-02 amendment; evidence identity per
[ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md). Builds on the
#3062 provider-lane frame ([spec](2026-10-06-system-tool-cells-design.md)) and the #2810 remote
deep-lifecycle frame (`tests/integration/live_stack/remote_lifecycle.py`, `on_remote_system`).

## Problem

The #3062 frame binds and runs only `local-libvirt` provider cells: `bindings()` leaves every
`remote-libvirt` tool cell unbound, `on_lane_system` and `lane_target` frame only the local lane
image through `on_catalog_system`, and the System carrier reads domain XML and disk absence from
the worker's own libvirt. The coverage contract routes 216 x86_64 remote cells of the lifecycle
groups to #3080. The operator narrowed #3080 on 2026-10-06 to the six `systems.*` tools (120
cells: 24 functional, 96 rejection) and moved the 96 remote cells of group 3119 to #3120.

## Scope

1. **Owner routing** in `scripts/coverage_campaign/contract.py` `_tool_cells`: x86_64
   `remote-libvirt` cells of group 3062 go to 3080, of group 3119 to 3120. `ppc64le` cells of
   both still go to 2818. `obligations.toml` is unchanged apart from `[implementations]`.
2. **Lane** in `tests/integration/live_stack/tool_cells.py`: one frozen `Lane` value says how a
   provider cell's System is framed, profiled and observed on its provider:
   - `entry`: the guest identity the probe must match (`distro`, `version`, `arch`);
   - `profile(ref)`: the provisioning profile (`catalog_profile` locally; `remote_profile` with
     the staged base volume remotely);
   - `xml(system_id)` and `absent(path)`: the domain XML and owned-disk absence, read from the
     provider's own libvirt (the worker's locally; `qemu+ssh` to the provider host remotely,
     through the operator's `REMOTE_PROVIDER_SSH` observer);
   - `frame`: `on_catalog_system` bound to the lane image, or `on_remote_system` bound to the
     remote lane family.

   `lane_for(cell)` builds it. The remote lane image is the `fedora` representative
   (`fedora-kdive-remote-base-43`, `REMOTE_LANE_FAMILIES = {"x86_64": "fedora"}`). An unset or
   unreachable observer, a foreign architecture, a missing `[[remote_libvirt]]` instance, or an
   unstaged base volume stops the cell `blocked`, as #2810's cells do.
3. **Frame**: `on_lane_system`, `observe_guest`, `Guest` and `lane_target` take their lane from
   `lane_for`. `Guest` carries the `Lane` in place of the catalog name and entry. `lane_target`
   memoizes one target per stack and provider, records the target's own artifacts (the remote
   provider-host identity among them) beside its cleanup proof, and carries the lane profile for
   rejection arguments. `on_remote_system` gains the optional `provision` argument
   `on_catalog_system` already has, so the remote `systems.provision` cell provisions through
   its exposure. `run_tool_cell` skips by control-plane architecture only for non-remote cells:
   a remote cell's host is the provider host, which its frame observes.
4. **Bindings**: `bindings()` takes an optional `RemoteHost`. With it, each bound x86_64 remote
   cell whose guest architecture is the provider host's gets the provider host's OS and
   architecture, `fedora:43` / `x86_64`, the cell's accelerator, the staged base volume's
   SHA-256 (null when unstaged) and the declared kernel fields. The CLI gains `--remote`, which
   observes the provider host first and exits 2 naming the blocker. Service and local bindings
   are unchanged.
5. **Carrier**: `tests/integration/test_system_tool_cells_live.py::test_system_tool_cell` also
   parametrizes the six tools' x86_64 remote cells (ids contain `remote-libvirt`, so `-k`
   selects a lane). Its bodies read domain XML, definedness and disk absence through
   `guest.lane`, compare `ssh_info`'s host and port with the domain's `hostfwd` address and port
   (the loopback address locally, `ssh_addr` remotely), and take rejection profiles from the
   target. `[implementations]` binds the 30 `tool/remote-libvirt/systems.*` scenarios; the
   ppc64le remote cells share them and are never parametrized.
6. A runbook section in `docs/operating/runbooks/remote-live-stack.md` and one live run of both
   configurations against a separate x86_64 provider host.

No product source, ADR or migration change. The functional effects and rejection boundaries are
#3062's ([spec](2026-10-06-system-tool-cells-design.md#functional-effects)) with the provider's
own observers: the remote cleanup additionally proves the provider host's `kdive-*` domain set
unchanged (`remote_cleanup`).

## Failure model

1. **Actors and deployments:**
   - an operator running the live tier from a control plane with the stack at the candidate,
     against one separate disposable x86_64 `remote-libvirt` provider host used by this lane
     alone, one stack per configuration (`recovery` with `KDIVE_WORKER_DEATH_VERIFIER=docker`);
   - CI, which runs only the unit and contract tests.
2. **Invariants and assets at stake:**
   - honest per-cell outcomes; ownership matching the operator's split;
   - provider host left as found: every System a completing cell creates is torn down, its
     domain undefined, its volumes gone, no new `kdive-*` domain, capacity returned;
   - the provider destination never enters evidence (ADR-0715).
3. **Accepted failure classes:**
   - a killed cell can leave a System, allocation, domain or overlay on the provider;
     `demo-down.sh --wipe --yes` plus the runbook's provider check clear them;
   - torn-down Systems, released allocations and audit rows remain as history;
   - a failed or blocked `lane_target` fails or blocks every rejection cell of that provider
     lane, not retried;
   - a cell blocked before the provider host is observed records the control-plane host, so
     `qualify` adds context-mismatch reasons (as for #2810);
   - concurrent allocators on the stack or the provider host are not modelled.
4. **Covered elsewhere:** remote run/image cells (#3120), local cells (#3062, #3119), ppc64le
   (#2818), capability boundary (#2814), remote host defects (#3081, #3082, #3083, #3087, #3093,
   #3101).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| owner routing | focused-test | `test_coverage_contract.py`: 3080 owns 120, 3120 owns 96, remote ppc64le 2818 |
| carrier bindings | focused-test | `test_coverage_contract.py`: the 30 remote scenarios bind the System node |
| remote bindings | focused-test | `test_tool_cells.py`: provider-host, guest, accelerator, digest; local/service unchanged; no host, no remote cells |
| lane selection | focused-test | `test_tool_cells.py`: `lane_for` picks local/remote; remote blocked without observer or staged volume |
| remote arch skip | focused-test | `test_tool_cells.py`: a remote cell is not skipped by control-plane architecture |
| target memo per provider | focused-test | `test_tool_cells.py`: one preparation per stack and provider |
| `on_remote_system` provision override | task-test-not-applicable | the default is the call it replaces, and the frame acts only through a live stack client and provider host; the override is exercised by the remote `systems.provision` cells |
| live cells | task-test-not-applicable | act only against a live stack and provider host; proven by the lab run and `qualify` |
| runbook | task-test-not-applicable | prose; `just docs-check` |

# Remote debug tool cells on x86_64 (#3113)

Issue: [#3113](https://github.com/randomparity/kdive/issues/3113), part of epic #2803.
Scope record: `WORK:SCOPE` token `q3113-9f2c61ab`. No ADR (slot 0791 stays unused): every
choice below follows an accepted record or a merged sibling carrier.

## Problem

`build_contract()` gives #3113 the 440 x86_64 `remote-libvirt` cells of the 21 `debug.*` tools:
88 functional (22 tool variants — `debug.start_session` has `gdbstub` and `drgn-live` — times
`default`/`recovery` times `direct`/`gateway`) and 352 rejection (authentication, authorization,
project-isolation, validation; no capability cell, ADR-0722 2026-10-13). None has a node:
`tests/scripts/test_coverage_contract.py::test_debug_owners_and_carrier_follow_the_approved_split`
asserts that today. #2813's local carrier (`tests/integration/live_stack/debug_cells.py`, run
by `tests/integration/test_debug_tool_cells_live.py`) proves the same tools on local-libvirt.
It cannot run a remote cell as it stands:

- `gdbstub_profile` writes `provider["local-libvirt"]`, which a remote profile lacks.
- `Session.variant` and the live test read the variant at `scenario_id.split("/")[2]`, which is
  the tool name in a provider-qualified remote id (`tool/remote-libvirt/<tool>/<variant>/...`).
- The rejection target is cached per stack, not per stack and provider, and keeps only lane
  keys, so a remote rejection record would carry the control plane as its host.
- Protected state reads `domain_defined` on the worker's libvirt, where a remote domain never
  exists, so an unchanged-state proof would observe nothing.
- Nothing proves the gdb transport gone after a cell (criterion 2).

## Design

Extend the local carrier onto the remote lane; add no second carrier. The remote lane and its
provider-host observers are #3080's and #3116's (`Lane.xml`, `Lane.connect`,
`Lane.installed_kernel`, `on_remote_system`, `remote_cleanup`). `capture_cells.boot` takes the
lane's `installed_kernel` after #3116, so the fixture boots and is verified in the guest's own
`/boot` on the provider. Nothing depends on #3114: the carrier keeps its own variant reader and
host keys, which can fold into #3114's `capture_cells` helpers once both land.

1. **Provisioning.** `gdbstub_profile` returns a `remote-libvirt` profile unchanged: remote
   provisions a gdbstub on every System and resolves `CaptureMethod.GDBSTUB`, so the install
   appends `nokaslr` (`RemoteLibvirtProfilePolicy`, ADR-0083). The ground truth (`read_truth`)
   then compares the guest's `vfs_read` with the fixture `System.map` exactly as locally.
2. **Variant.** `Session.variant` and the live test use a new `variant_of(cell)`, the
   second-to-last scenario segment, which names the variant in a local or a remote id.
3. **drgn-live.** The four `debug.start_session/drgn-live` remote functional cells run.
   drgn-live on a remote System runs `kdive-drgn` in the guest, which never receives the Run's
   DWARF vmlinux (#3124, blocked by #3121). The start envelope then carries #3124's own
   symptom, a `missing_debuginfo` warning (ADR-0322/0335 runtime probe). On the remote lane
   alone, `start_body` stops that cell `blocked`, naming #3124 and the warning's `reason`, after
   the session was recorded for ending. Without the warning the cell runs the local assertions,
   so a closed #3124 turns it into a success and any other defect fails it.
4. **Transport observer** (new, in `debug_cells.py`). A remote functional cell reads the
   domain's `-gdb tcp:<addr>:<port>` argument from `Lane.xml` and counts ESTABLISHED TCP
   sockets on that 4-tuple at both ends: on the provider host,
   `ss -Htn state established '( sport = :<port> )'` over the operator SSH
   (`remote_lifecycle.host_ssh`, today's private `_host_ssh` made public); on the control plane,
   `ss -Htn state established dst <addr>:<port>` locally. The gdb subprocess lives in the MCP
   server process (`debug.start_session` is synchronous, no job), which the demo-up lane runs as
   a host process on the control plane (`stack-services.sh`); the evidence key is `server`. After the body and `end_all`, both counts must settle to 0
   within ten one-second polls and every `debug_sessions` row of the Run must read `detached`.
   Then a calibration session (operator client) must show `{provider: 1, server: 1}` while
   attached and `{0, 0}` after its end, with every row `detached` again. Calibrating after the
   body keeps the body's own session rows exact (`list_body` compares them). The cell's effect
   carries `transport: {after, attached, sessions_open: 0}`. A server not on this host fails the
   calibration rather than passing vacuously. The calibration runs inside the body's
   `try/finally`, so a failed check still ends its session.
   Remote drgn-live holds no socket of its own: it reaches the guest through the guest agent
   (`remote_libvirt/lifecycle/connect.py`), so for its cells the transport proof is the Run's
   session rows reading `detached`.
5. **Rejection target per provider.** `_TARGETS` keys on `(base_url, provider)`. On the remote
   lane the target keeps the provider host's `host_os`/`host_arch` beside its lane keys.
   `snapshot_of(db_url, target, connect)` reads `domain_defined(system_id, connect)` through the
   lane's `connect`. The remote target proves its own session's transport the same way (`{1, 1}`
   while attached, `{0, 0}` after the end) and its summary artifact, which every remote rejection
   record carries, holds those counts: criterion 2 for a rejection cell is the target's proof,
   since a refused call opens no session.
6. **Bindings.** 110 `tool/remote-libvirt/debug.*` scenario ids in `obligations.toml` name
   `tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell`. The live test's
   cells add the remote cells of `REMOTE_LANE_FAMILIES` architectures. `tool_cells bindings
   --remote --kernel-baseline longterm` binds them unchanged.
7. **Runbook** section in `docs/operating/runbooks/remote-live-stack.md`.

Domain, volume and capacity cleanup on the provider host is the frame's `remote_cleanup`,
unchanged. Candidate, deployed-role, kernel and host identity are recorded by `run_cell`, the
deep boot and `observe_host`, unchanged. A failure observed live is recorded as a failing cell
and reported, never fixed or waived here.

Expected per configuration while #3124 is open: 44 functional (42 `success`, 2 `blocked`
drgn-live) and 176 `rejection` cells; the other configuration's 220 are skipped. `qualify` then
qualifies 436 of 440 and lists the 4 drgn-live cells as `missing-prerequisite`; it exits non-zero
by design. Their records carry the provider host, guest and kernel, because the block comes after
the boot.

## Failure model

1. **Actors and deployments**
   - An operator runs `-m live_stack -k remote-libvirt` on a demo-up control plane whose server
     and workers run as host processes there. A separate x86_64 provider host is reached over the operator's SSH.
   - CI runs only the unit tests and the contract test.
2. **Invariants and assets at stake**
   - No cell reports `success` without its observation, its transport proof and the provider
     cleanup. No blocked or failed cell qualifies.
   - Evidence records carry no host name, address or SSH destination (ADR-0715).
   - Local cells keep their behaviour: the changes branch on `provider` alone.
3. **Accepted failure classes**
   - TIME-WAIT sockets after detach are not counted; only ESTABLISHED ones are. A closed
     connection is not a live transport.
   - Ambient guest reads may take the `vfs_read` breakpoint, as on the local lane (#2813).
4. **Covered elsewhere**
   - drgn-live in remote guests: #3124 (blocked by #3121).
   - ppc64le cells: #2818. Remote pcap and SysRq read-back defects: #3116's findings (these
     cells use neither).
   - Authority-provisioning defects: #3144 and #3148.

## Validation

- Unit tests (`tests/integration/live_stack/test_debug_cells.py`): remote `gdbstub_profile`
  identity; `variant_of` on both providers' ids; the symptom-gated drgn-live block; the `-gdb` argument parser; the
  transport check against scripted socket counts (calibration not 1 fails, leftover sockets
  fail, an open session row fails, a failed calibration still ends its session); the target's
  transport proof; the per-provider target cache; remote rejection pairs.
- Contract test: the 440 remote cells carry `_DEBUG_NODE`.
- Live: both configurations on the remote pair under an operator grant, then `qualify`.

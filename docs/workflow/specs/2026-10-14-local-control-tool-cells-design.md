# Local traffic, SysRq and crash-watch tool cells (#3115)

## Problem

Main routes 60 x86_64 local-libvirt cells of `control.capture_traffic`,
`control.diagnostic_sysrq` and `control.watch_for_crash` to #3115: 12 functional and 48 rejection.
All three capabilities are supported, so ADR-0722's 2026-10-13 amendment emits no capability
cell. No carrier runs them.

## Scope

`tests/integration/live_stack/control_cells.py` holds the scenarios; the carrier
`tests/integration/test_control_tool_cells_live.py` frames each cell with `run_tool_cell`, bound
by 15 `[implementations]` rows. Functional cells reuse `on_lane_system` and `capture_cells.boot`
(the `longterm` fixture); only the tool under test uses the cell's exposure.

- **capture_traffic**: the guest sends UDP datagrams carrying a fresh token to the user-network
  gateway `10.0.2.2:9` every 0.5 s until stopped. A 15 s capture drains; `artifacts.fetch_raw`
  downloads its pcap, which must parse as Ethernet libpcap with at least five IPv4/UDP port-9
  records whose payload is the token. QMP `qom-list /objects` then shows no `kdive-dump-`
  filter, and `pcap_dir(system)` joins `owned`, so cleanup proves the host spool gone.
- **diagnostic_sysrq**: set `kernel.sysrq=1`, count `sysrq: Show Memory` in guest `dmesg`, run
  `show_memory`. Its artifact (`artifacts.get` `find`) holds that header, then `Mem-Info:`, and
  `dmesg` holds one more header.
- **watch_for_crash**: a 20 s watch ends `not_fired` with `20 <= elapsed_s <= 30`. A 180 s
  watch then fires while the guest writes `<2>Kernel panic - not syncing: kdive-cov-<token>` to
  `/dev/kmsg` every 2 s: `signature` is `Kernel panic`, `matched` holds the token. The writer is
  killed; no queued or running watch job remains. A written signature (not a real panic)
  repeats past the pickup baseline and keeps the System `ready` for cleanup.

Rejections aim at `capture_cells.capture_target` (a torn-down, Run-bound System) and its
protected state. Gate contributor; isolation answers `configuration_error` for the System tools,
`not_found` for the Run-resolved `capture_traffic`; validation passes the integer `7` as id.

### Failure model

1. Actors and deployments: an operator running the `live_stack` carrier on a disposable x86_64
   KVM host via `demo-up.sh`; `just test` running the unit oracles.
2. Invariants and assets: no cell counts without its observed effect; owned domain, disks, pcap
   spool and capacity are released; the rejection target's protected state is unchanged.
3. Accepted failure classes: a guest lacking `/dev/udp` or `sysctl` fails the cell (the Fedora
   44 lane image has both); product defects such as an unreadable pcap stay failing, reported.
4. Covered elsewhere: power and snapshot #2815; remote #3116; ppc64le #2818; fixture kernel
   gaps #3219; host_dump spool #3220.

## Success

Each of the 60 cells records `success`/`rejection` evidence with candidate, role and kernel
identity, or an honest `blocked`/`failure` with its cause. The binding test pins the 120 local
cells (ppc64le cells share the arch-free ids) to the carrier.

## Validation

- pcap flow parser, watch and SysRq oracles, rejection args: focused-test,
  `test_control_cells.py`; another port, a truncated record, a late quiet run, a missing token
  or a header without `Mem-Info:` fails. Red by breaking the port match.
- carrier binding: focused-test, `tests/scripts/test_coverage_contract.py`.
- live effect: focused-test, `uv run python -m pytest -m live_stack
  tests/integration/test_control_tool_cells_live.py` on a provisioned guest.
- runbook section: task-test-not-applicable, operator prose with no executable consumer.

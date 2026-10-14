# Local traffic, SysRq and crash-watch tool cells (#3115)

## Problem

Main routes 60 x86_64 local cells of `control.capture_traffic`, `control.diagnostic_sysrq` and
`control.watch_for_crash` to #3115 (12 functional, 48 rejection; supported capabilities carry no
capability cell under ADR-0722's 2026-10-13 amendment). No carrier runs them.

## Scope

`live_stack/control_cells.py` holds the scenarios; the carrier `test_control_tool_cells_live.py`
frames each cell with `run_tool_cell`, bound by 15 `[implementations]` rows. Functional cells
reuse `on_lane_system` and `capture_cells.boot` (`longterm` fixture); only the tool under test
uses the cell's exposure.

- **capture_traffic**: the guest sends UDP datagrams carrying a fresh token to the user-network
  gateway `10.0.2.2:9` every 0.5 s until stopped. A 15 s capture drains; `artifacts.fetch_raw`
  downloads its pcap, which must parse as Ethernet libpcap with at least five IPv4/UDP port-9
  records whose payload is the token. QMP `qom-list /objects` over `worker_libvirt_uri()`
  then shows no `kdive-dump-` filter; `pcap_dir(system)` joins `owned`, so cleanup proves the
  spool gone. The stored pcap is Run-owned and retained, like the capture cells' cores.
- **diagnostic_sysrq**: set `kernel.sysrq=1`, count `sysrq: Show Memory` in guest `dmesg`, run
  `show_memory`. Its artifact (`artifacts.get` `find`) holds that header, then `Mem-Info:`, and
  `dmesg` holds one more header.
- **watch_for_crash**: a 20 s watch ends `not_fired` with `20 <= elapsed_s <= 30`. A 180 s
  watch then fires while the guest writes `<2>Kernel panic - not syncing: kdive-cov-<token>` to
  `/dev/kmsg` every 2 s: `signature` is `Kernel panic`, `matched` holds the token. The writer is
  killed; no queued or running watch remains. A written signature (not a real panic)
  outlasts the pickup baseline and keeps the System `ready`.

Rejections aim at `capture_cells.capture_target` (a torn-down, Run-bound System) and its
protected state. Gate contributor; isolation answers `configuration_error` for the System tools,
`not_found` for the Run-resolved `capture_traffic`; validation passes the integer `7` as id.
Every call carries required non-id arguments (`command: show_memory`) so only the boundary rejects.

### Failure model

1. Actors and deployments: an operator running the carrier on a disposable x86_64 KVM host
   via `demo-up.sh`; `just test` running the unit oracles.
2. Invariants and assets: no cell counts without its observed effect; owned domain, disks, pcap
   spool and capacity are released; the rejection target's protected state is unchanged.
3. Accepted failure classes: product defects (an unreadable pcap) stay failing, reported.
4. Covered elsewhere: power/snapshot #2815; remote #3116; ppc64le #2818; fixture gaps #3219.

## Success

Each of the 60 cells records `success`/`rejection` evidence with candidate and role identity
(kernel identity on the 12 functional), or an honest `blocked`/`failure`. The binding test pins
the 120 local cells (ppc64le share the arch-free ids) to the carrier.

## Validation

- pcap parser, watch and SysRq oracles, rejection args: focused-test, `test_control_cells.py`;
  another port, a truncated record, a late quiet run, a missing token, a header without
  `Mem-Info:` or a dropped `command` fails. Red by breaking the port match.
- carrier binding: focused-test, `tests/scripts/test_coverage_contract.py`.
- live effect: focused-test, `uv run python -m pytest -m live_stack
  tests/integration/test_control_tool_cells_live.py` once per configuration via `demo-up.sh`
  (recovery: `KDIVE_WORKER_DEATH_VERIFIER=docker`), deployed head = committed candidate.
- runbook section: task-test-not-applicable, prose with no executable consumer.

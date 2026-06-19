# Reserve a dedicated gdbstub ACL-probe port so the diagnostic never halts a live System (#602)

- **Issue:** #602 — `gdbstub_acl` diagnostic halts a live System (TCP-connect to its gdbstub leaves the guest paused)
- **ADR:** [ADR-0184](../adr/0184-gdbstub-acl-reserve-probe-port.md)
- **Status:** Accepted

## Problem

The `gdbstub_acl` worker-vantage diagnostic (`ops.diagnostics` / `doctor`) opens a bare TCP
connection to the **lowest** port of the remote host's configured gdbstub range
(`src/kdive/diagnostics/gdbstub_acl.py` `_lowest_port` → `gdb_port_min`, e.g. `47000`). QEMU
stops the guest vCPUs the instant a gdb client completes a TCP attach, and the probe never speaks
the RSP detach/continue before closing the socket — so the guest is left **paused**.

Remote-libvirt assigns gdbstub ports **lowest-first**
(`providers/remote_libvirt/lifecycle/gdb.py::allocate_gdb_port`, called from
`lifecycle/provisioning.py::_define_and_start` with `port_min=config.gdb_port_min`), so the
**first** System allocated on a host sits on exactly the port the probe targets. The collision is
deterministic: run diagnostics against a host with one live remote System and that System pauses,
and the next agent op fails with a misleading `transport_failure` ("domain is not running"). This
was reproduced live on D2 (ub24-big, 2026-06-19) during the #587 re-verification.

The probe's own design intent (ADR-0164) is "a policy check with **no live listener**": a fast
`ECONNREFUSED` proves the SYN reached the host TCP stack (ACL admits) and a connect timeout proves
a firewall DROP (blocked). The bug is that the port it probes is no longer listener-free once a
System is running on it.

## Goal / success criteria

1. Running `ops.diagnostics` against a host with a live remote System on the lowest gdbstub port
   does **not** pause that System. Encoded structurally: the port the provisioner assigns to a
   System is **never** the port the probe targets.
2. The `gdbstub_acl` three-state ACL contract (ADR-0164) is unchanged: SYN-reach → `pass`,
   connect timeout → `fail` (with the firewall fix string), any other error / unset host →
   `error`.
3. The firewall ACL the probe exercises is the operator-configured range; the operator-facing
   `pass`/`fail` messages still name the full range to open.

Falsifiable check: tests asserting (a) on a fresh host the provisioner allocates `gdb_port_min + 1`
(not `gdb_port_min`); and (b) even when a stale/foreign domain already records `gdb_port_min`, the
provisioner still never assigns it — the reserved port is excluded by the allocation floor, not by
the "already taken" set. Both fail on the current code (which allocates from `gdb_port_min`) and
pass after the fix, pinning the invariant across the fresh-host and reuse-own-recorded-port paths.

## Approach

Reserve the lowest port of the operator-configured gdbstub range as a dedicated ACL-probe port,
never handed to a System. This is the clean form of the issue's recommended option 1, and it needs
no change to the probe's logic (the probe already targets the lowest port, which is now guaranteed
listener-free) — only the System-allocation floor moves up.

- `RemoteLibvirtConfig` gains two derived, documented properties over the existing
  `gdb_port_min`/`gdb_port_max`:
  - `acl_probe_port -> int` = `gdb_port_min` — the reserved probe port (never assigned to a
    System).
  - `assignable_gdb_port_min -> int` = `gdb_port_min + 1` — the floor System allocation uses.
- `lifecycle/provisioning.py::_define_and_start` passes `port_min=config.assignable_gdb_port_min`
  to `allocate_gdb_port` (was `config.gdb_port_min`). `allocate_gdb_port` itself is unchanged: it
  stays a pure "lowest free in `[port_min, port_max]`" helper; the reservation is policy applied at
  the call site via the config property.
- `_parse_gdbstub_range` validation tightens: the range must span **at least two** ports
  (`gdb_port_max > gdb_port_min`) — one reserved probe port plus at least one assignable System
  port. A single-port range now fails fast with a `CONFIGURATION_ERROR` whose message names the
  reservation and the minimum, e.g. "gdbstub_range must span at least 2 ports (the lowest is
  reserved for the ACL probe)".

  **Compatibility:** this is a behavior change for a deployment that today declares a one-port range
  (e.g. `gdbstub_range = "47000:47000"`). `_parse_gdbstub_range` runs in
  `remote_config_from_inventory` (per-op, fail-closed), not in the `is_remote_libvirt_configured`
  opt-in gate, so such a deployment keeps starting but begins failing every remote op with the
  `CONFIGURATION_ERROR` above until the operator widens the range. No in-repo `systems.toml` example
  or fixture uses a single-port range (verified during implementation); the default is 47000–47099.
- The probe (`diagnostics/gdbstub_acl.py`), the check (`diagnostics/checks.py::GdbstubAclCheck`),
  and the contribution (`providers/remote_libvirt/diagnostics/contribution.py`) keep their current
  signatures and the `port_range` string for the operator message. Only their docstrings change to
  state that the probed lowest port is the reserved ACL-probe port.
- Discovery's advertised `gdbstub_port_min`/`gdbstub_port_max` capabilities keep reporting the full
  operator-configured range (the firewall ACL range), unchanged.

## Non-goals

- No RSP detach/continue in the probe (issue option 2): heavier, requires the probe to recognize a
  real gdbstub, and still halts the guest for a window. Rejected in the ADR.
- No new config field or `systems.toml` schema change: the reserved port is derived from the
  existing range, so no migration and no operator re-declaration.
- No teardown/migration of a System that an **older** build already placed on `gdb_port_min`. Such
  a pre-fix System remains probe-vulnerable until it is torn down; new provisions never land there.
  Out of scope (noted in ADR consequences).

## Risks

- The reservation is enforced at the single `allocate_gdb_port` call site. A future second caller
  that passes `config.gdb_port_min` would reintroduce the bug; mitigated by routing callers through
  `config.assignable_gdb_port_min` and a test that pins the provisioner's fresh-host port to the
  assignable floor.
- Reserving one port reduces per-host System capacity by one (default range 47000–47099: 100 → 99
  assignable). Acceptable and documented; the 1-port-range validation makes the trade-off explicit.

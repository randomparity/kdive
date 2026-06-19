# ADR 0184 — Reserve the lowest gdbstub port as a dedicated ACL-probe port

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers

## Context

The `gdbstub_acl` worker-vantage diagnostic (ADR-0164, ADR-0091 §2) validates that the remote
host's firewall/ACL admits the configured gdbstub port range. It does so by opening a bare TCP
connection to the **lowest** port of the range (`diagnostics/gdbstub_acl.py` `_lowest_port` →
`config.gdb_port_min`): a connect or fast `ECONNREFUSED` means the SYN reached the host TCP stack
(`pass`), a connect timeout means the firewall dropped it (`fail`), any other error is
indeterminate (`error`). The check is deliberately a *policy* check "with no live listener" — it
needs no running guest because the ACL is a range rule.

Remote-libvirt assigns each System a gdbstub port **lowest-first**
(`lifecycle/gdb.py::allocate_gdb_port`, called from `_define_and_start` with
`port_min=config.gdb_port_min`). So the first System provisioned on a host occupies exactly the
port the probe targets. QEMU halts the guest vCPUs the moment a gdb client completes a TCP attach,
and the probe closes the socket without an RSP detach/continue — leaving the guest **paused**.
Running diagnostics against a host with a live System therefore silently wedges that System, and
the next agent op fails with a misleading `transport_failure` ("domain is not running"). Confirmed
live on D2 (ub24-big) on 2026-06-19 during the #587 re-verification (#602).

## Decision

Reserve the lowest port of the operator-configured gdbstub range as a dedicated ACL-probe port that
is never assigned to a System.

- `RemoteLibvirtConfig` exposes two derived properties over the existing `gdb_port_min` /
  `gdb_port_max`:
  - `acl_probe_port` = `gdb_port_min` — the reserved probe port (no System ever binds it, so it is
    always listener-free).
  - `assignable_gdb_port_min` = `gdb_port_min + 1` — the floor used for System allocation.
- `_define_and_start` allocates from `config.assignable_gdb_port_min`; `allocate_gdb_port` stays a
  pure "lowest free in `[port_min, port_max]`" helper, so the reservation is policy at the call
  site, not in the allocator.
- `_parse_gdbstub_range` requires the range to span at least two ports
  (`gdb_port_max > gdb_port_min`) — one reserved probe port plus at least one assignable System
  port — failing closed with a `CONFIGURATION_ERROR` that names the reservation.

The probe keeps targeting the lowest port; that port is now guaranteed to have no live gdbstub, so
the probe behaves exactly as ADR-0164 designed (refuse → `pass`, timeout → `fail`) and never
attaches to a running guest. The firewall ACL is a range rule, so probing the reserved port still
exercises it. The probe / check / contribution signatures and the operator-facing range messages
are unchanged.

## Consequences

- Running `ops.diagnostics` against a host with a live remote System no longer pauses it: the probe
  targets `gdb_port_min`, which the provisioner never assigns.
- The `gdbstub_acl` three-state contract (ADR-0164) and the firewall-fix message (which names the
  full configured range) are preserved.
- Per-host System capacity drops by one (default 47000–47099: 100 → 99 assignable). The trade-off
  is explicit via the ≥2-port validation; a 1-port range is now rejected at config-resolution time.
- A System that a **pre-fix** build already placed on `gdb_port_min` stays probe-vulnerable until
  it is torn down; this fix changes only future allocations and adds no running-domain migration.
- The reservation is enforced at the one `allocate_gdb_port` call site. A test pins the
  provisioner's fresh-host port to `assignable_gdb_port_min` so a regression that allocated the
  probe port again is caught.

## Considered & rejected

- **Speak RSP to detach after connecting (issue option 2).** The probe would, on a successful
  connect, send a gdb-RSP detach/continue so the guest resumes. Rejected: it requires the probe to
  recognize a real gdbstub and implement RSP, still halts the guest for a window (a concurrent op
  can still hit the paused domain), and turns a reachability probe into a protocol client. The
  reservation removes the halt entirely.
- **Probe the highest port instead of the lowest.** With lowest-first allocation the top port is
  assigned last, so a collision needs a full range of live Systems. Rejected: "least likely" is not
  "never" — a busy host still wedges a System, failing acceptance criterion 1. Reserving a port is a
  guarantee, not a probability.
- **Add a separate `systems.toml` probe-port field.** Rejected as scope creep: it adds operator
  surface and a schema change for a value that derives cleanly from the existing range.
- **Skip ports currently held by running Systems (read the domain-XML registry in the probe).**
  Rejected: it couples the provider-agnostic probe to libvirt domain enumeration, races against a
  System starting up between the registry read and the connect, and still has no listener-free port
  to fall back to when the whole range is busy. The static reservation is simpler and race-free.

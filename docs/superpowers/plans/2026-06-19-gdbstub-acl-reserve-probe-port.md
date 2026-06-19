# Implementation plan — Reserve a gdbstub ACL-probe port (#602)

- **Spec:** [docs/specs/2026-06-19-gdbstub-acl-reserve-probe-port.md](../../specs/2026-06-19-gdbstub-acl-reserve-probe-port.md)
- **ADR:** [ADR-0184](../../adr/0184-gdbstub-acl-reserve-probe-port.md)
- **Branch:** `feat/gdbstub-acl-reserve-probe-port-602`

## Context

The `gdbstub_acl` worker-vantage diagnostic TCP-connects to the lowest gdbstub port
(`config.gdb_port_min`), which the lowest-first allocator hands to the first System — so the probe
halts a live guest. Fix: reserve `gdb_port_min` as a probe-only port and start System allocation at
`gdb_port_min + 1`. The probe keeps targeting the lowest (now listener-free) port; the firewall ACL
(a range rule) is still exercised; the three-state contract is unchanged.

The work is tightly coupled (config property + its sole consumer + validation + tests), so it is
implemented directly in this session as one TDD pass, not fanned out to subagents.

## Guardrails (run before every commit)

- `just lint` — `ruff check` + `ruff format --check`
- `just type` — `ty check` over src + tests
- Focused tests: `uv run python -m pytest tests/providers/remote_libvirt/test_config.py tests/providers/remote_libvirt/lifecycle/test_provisioning.py tests/diagnostics/test_gdbstub_acl_probe.py tests/diagnostics/test_provider_checks.py -q`
- Before push: full `just ci`.

## Task 1 — Config: reserved-probe-port properties + ≥2-port validation

**File:** `src/kdive/providers/remote_libvirt/config.py` (+ `tests/providers/remote_libvirt/test_config.py`)

TDD order:

1. **Failing tests first** in `test_config.py`:
   - `acl_probe_port == gdb_port_min` and `assignable_gdb_port_min == gdb_port_min + 1` on a
     `RemoteLibvirtConfig` built with the default range.
   - A `[[remote_libvirt]]` instance with `gdbstub_range = "47000:47000"` (single port) raises
     `CategorizedError` / `CONFIGURATION_ERROR` from `remote_config_from_inventory`, and the message
     names the reservation (assert on a stable substring, e.g. `"at least 2 ports"` and
     `"reserved for the ACL probe"`). Mirror the existing inverted-range test at
     `test_config.py:242`.
   - A two-port range (`"47000:47001"`) still resolves (boundary: exactly one assignable port).
2. **Implementation:**
   - Add two read-only properties to `RemoteLibvirtConfig`:
     `acl_probe_port` → `self.gdb_port_min`; `assignable_gdb_port_min` → `self.gdb_port_min + 1`.
     Docstring each: the probe port is reserved and never assigned to a System; the assignable floor
     is what provisioning uses.
   - In `_parse_gdbstub_range`, after the inversion check, reject `high <= low` with a
     `CONFIGURATION_ERROR`: "gdbstub_range must span at least 2 ports (the lowest is reserved for
     the ACL probe)". Update the function docstring's Raises clause.

**Acceptance:** the new tests pass; the existing valid/inverted/out-of-range tests still pass; `ty`
clean (properties return `int`).

## Task 2 — Provisioning: allocate from the assignable floor

**File:** `src/kdive/providers/remote_libvirt/lifecycle/provisioning.py` (+ `tests/providers/remote_libvirt/lifecycle/test_provisioning.py`)

TDD order:

1. **Update / add tests** in `test_provisioning.py`:
   - `test_provision_defines_starts_and_waits_for_agent`: expected port `47000 → 47001`
     (line ~770).
   - `test_provision_start_failure_advances_to_next_port`: expected ports `47000/47001 → 47001/47002`
     (lines ~839-841).
   - `test_provision_skips_ports_recorded_by_other_domains`: set the foreign domain at `47001`
     (an assignable port, not the reserved one) and expect the new System at `47002`, preserving the
     test's "skip a port another domain holds" intent (lines ~790-806).
   - **New regression test** (spec criterion b): a foreign domain records the reserved port
     `47000`; assert the new System is still allocated `47001` — i.e. the reserved port is excluded
     by the floor, not merely because it is "taken".
   - Confirm `test_provision_retry_reuses_own_recorded_port` (own at 47001) is unaffected.
2. **Implementation:** in `_define_and_start`, change the `allocate_gdb_port(...)` call to pass
   `port_min=config.assignable_gdb_port_min` (was `config.gdb_port_min`). `port_max` stays
   `config.gdb_port_max`. Do **not** change `allocate_gdb_port` itself — it stays a pure
   lowest-free-in-range helper; reservation is policy at the call site.

**Acceptance:** updated + new tests pass; the bounded start-failure advance and reuse-own-port paths
still behave; no other caller of `allocate_gdb_port` passes `config.gdb_port_min`
(`rg "allocate_gdb_port\(|gdb_port_min" src/` to confirm).

## Task 3 — Docstrings: state the reservation at the probe / check / allocator

**Files:** `src/kdive/diagnostics/gdbstub_acl.py`, `src/kdive/diagnostics/checks.py`
(`GdbstubAclCheck`), `src/kdive/providers/remote_libvirt/lifecycle/gdb.py` (`allocate_gdb_port`).

No logic change. Update docstrings so the implicit coupling is explicit:

- `gdbstub_acl.py` module docstring + `_lowest_port`/`probe`: the lowest port of the range is the
  reserved ACL-probe port (`RemoteLibvirtConfig.acl_probe_port`), guaranteed to have no live System
  gdbstub, so the connect never pauses a guest.
- `GdbstubAclCheck` docstring: note the probed port is the reserved probe port; the `port_range` in
  the operator message is still the full configured firewall range.
- `allocate_gdb_port` docstring: callers pass `config.assignable_gdb_port_min` (not
  `gdb_port_min`); `gdb_port_min` is reserved for the ACL probe (ADR-0184).

**Acceptance:** `just lint` / `just type` clean; existing probe tests
(`test_gdbstub_acl_probe.py`, `test_provider_checks.py`) still pass unchanged — they assert the
probe targets port 47000, which is now the reserved port (still correct).

## Verification (whole-branch, step 5 / 7)

- Full `just ci`.
- Re-grep for stray `config.gdb_port_min` allocation uses.
- Confirm discovery's advertised `gdbstub_port_min/max` still report the full configured range
  (unchanged; advisory firewall range).

## Rollback / cleanup

Pure code+docs change, no migration, no schema, no new config field. Revert is a straight `git
revert` of the branch. No running-domain migration is performed (a pre-fix System on `gdb_port_min`
is out of scope per the spec non-goals).

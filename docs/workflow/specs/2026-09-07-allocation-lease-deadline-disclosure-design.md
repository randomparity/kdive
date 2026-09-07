# Allocation lease deadline disclosure (#2306)

## Problem

`allocations.request` grants a lease and returns `data = {project, resource_id}` — no deadline, no
reference clock (`allocations/request.py:189-202`). `allocations.renew` extends a lease and returns
`{project}` (`allocations/lifecycle.py:89-100`). `allocations.wait` / `allocations.list` emit
`lease_expiry` with no clock to measure it against (`allocations/common.py:68-87`). All three build
`data` ad hoc rather than through the disclosure already shipped for `build_ref` retention
(`runs/create.py:165-172`, `runs/common.py:417-421`), so AGENTS.md's "State a limit's full
contract" — unit, reference clock, scope, consequence, recovery action — goes unmet on the grant.

## Scope

One unit inside `src/kdive/mcp/tools/lifecycle/allocations/`:

- `common.py` gains `reference_clock(conn)` (`SELECT clock_timestamp()`, ISO-8601) and
  `lease_deadline_data(alloc, server_time)` — `{}` when `alloc.lease_expiry is None`, else
  `{"lease_expiry": iso(...), "server_time": ...}`, raising `ValueError` when a lease arrives
  without a clock. One enforcement point for all three sites.
- `common.py`: `allocation_next_actions(GRANTED)` gains `allocations.renew`, unconditionally.
- `request.py`, `lifecycle.py`, `common.py`/`view.py` each spread `lease_deadline_data` into their
  success `data`, taking the clock from the connection the handler already holds.
- `registrar.py`: the `window` `Field` description plus the `allocations.request` and
  `allocations.renew` docstrings state the five-part contract.

Operator-approved non-goals: any near-expiry threshold gating the renew breadcrumb; a literal
default-hours figure in the `window` text, because the bound is env-overridable.

## Success

1. A granted `allocations.request` envelope carries absolute `data.lease_expiry` + `server_time`.
2. A queued (`requested`) envelope carries neither.
3. `allocations.wait` and `allocations.list` carry `server_time` beside a non-null `lease_expiry`.
4. A granted allocation's `suggested_next_actions` names `allocations.renew`, still role-filtered.
5. An `allocations.renew` success envelope carries the extended `lease_expiry` and `server_time`.
6. `just ci` green with regenerated agent-facing doc artifacts.

## Validation

Green for every focused-test entry below: `uv run python -m pytest
tests/mcp/lifecycle/test_allocations_tools.py tests/mcp/lifecycle/test_allocations_renew.py -q`.

- **Grant and queued envelope keys (1, 2)** — focused-test, in `test_allocations_tools.py`:
  `test_grant_discloses_lease_deadline`, `test_queued_request_discloses_no_lease_deadline`.
  Red: `server_time` absent from the grant's `data`.
- **Read-path clock (3)** — focused-test, same file: `test_wait_discloses_lease_deadline_clock`,
  `test_list_discloses_lease_deadline_clock`. Red: no `server_time` key beside `lease_expiry`.
- **Renew breadcrumb (4)** — focused-test: the existing exact-list assertions at
  `test_allocations_tools.py:288` and `:310` go red until `allocations.renew` is added.
- **Renew envelope (5)** — focused-test:
  `test_allocations_renew.py::test_renew_discloses_extended_lease_deadline`. Red: `data` holds
  only `project`.
- **Clock-required invariant** — focused-test:
  `test_allocations_tools.py::test_lease_deadline_data_requires_server_time` asserts `ValueError`
  for a lease without a clock. Red: no raise.
- **`window` / docstring contract text (6)** — task-test-not-applicable. The changed surface is
  prose in one `Field` description and two wrapper docstrings; its only machine-checkable consumer
  is the committed generated reference, which `just docs-check` diffs byte-for-byte after
  `just docs`. A task-specific test could only snapshot wording.

# Allocation lease deadline disclosure (#2306)

## Problem

No `allocations.*` site discloses a lease deadline with a reference clock: `request.py:189-202`
returns `{project, resource_id}`, `lifecycle.py:89-100` `{project}`, `common.py:68-87` a clockless
`lease_expiry`, and `allocation_next_actions` never names `allocations.renew`. Each builds `data`
ad hoc rather than reusing the shipped `build_ref` disclosure (`runs/create.py:165-172`), leaving
AGENTS.md's "State a limit's full contract" unmet.

## Scope

- `common.py` gains `lease_reference_clock(conn)` (`SELECT clock_timestamp()` rendered
  `.astimezone(UTC).isoformat()`, since psycopg renders a `timestamptz` in the session timezone —
  `artifacts/uploads.py:254-261` settled this for #1336) and `lease_deadline_data(alloc,
  server_time)`: `{}` for a lease-less allocation, else both keys normalized the same way, raising
  `ValueError` for a lease with no clock. One enforcement point.
- `allocation_next_actions` names `allocations.renew` for the leaseholding states `granted` and
  `active` (`renew.py:69` refuses only terminal states), unconditionally.
- Each handler reads the clock inside its existing `pool.connection()` block, gated on a lease (so
  a queued poll adds no query and a list page reads once), then threads it to its sync response
  builder. `envelope_for_allocation` gains `server_time: str | None = None`, mirroring
  `envelope_for_run`, and spreads the helper on its success and `FAILED` branches, where
  `_allocation_recovery`'s `lease_expiry` is re-asserted so the invariant covers the read path.
- `registrar.py`: the `window` `Field` description and a new paragraph in the `allocations.request`
  and `.renew` docstrings state the contract; first lines stay byte-identical for `cli-verbs`.

Non-goals (operator-approved): a near-expiry threshold gating the breadcrumb; a literal default
figure in `window`, the bound being env-overridable. Out of surface: `lease_reference_clock` is a
third copy of this read (`runs/create.py:165`, `build_catalog.py:103`); consolidating is follow-up.

## Success

1. A granted `allocations.request` envelope carries `data.lease_expiry` and `server_time`, both
   ending `+00:00`; a queued (`requested`) one carries neither.
2. `allocations.wait` / `allocations.list` carry `server_time` beside any non-null `lease_expiry`,
   on the success and `FAILED` branches alike.
3. A leaseholding (`granted` or `active`) allocation's `suggested_next_actions` names
   `allocations.renew`, still role-filtered.
4. An `allocations.renew` success envelope carries the extended `lease_expiry` and `server_time`.
5. The `window` description and both docstrings name the unit, the `server_time` clock, the
   per-allocation scope, reconciler reclamation as the consequence, and `allocations.renew` as the
   recovery, with no literal default figure. `just ci` green, generated artifacts committed.

## Validation

Green for all: `uv run python -m pytest tests/mcp/lifecycle tests/mcp/core/test_tool_docs.py -q`.

- **Envelope keys, UTC offset, `FAILED` branch, breadcrumb (1-3)** — focused-test in
  `test_allocations_tools.py`: `test_grant_discloses_lease_deadline` (red: no `server_time`; also
  asserts `+00:00`), `test_queued_request_discloses_no_lease_deadline`,
  `test_read_paths_disclose_lease_clock` (wait, list, and a `FAILED` leaseholder), and the
  existing exact-list assertions at `:288`/`:310`, red until `allocations.renew` is added.
- **Renew envelope (4) and the clock-required invariant** — focused-test:
  `test_allocations_renew.py::test_renew_discloses_extended_lease_deadline` (red: `data` holds only
  `project`) and `test_allocations_tools.py::test_lease_deadline_data_requires_server_time` (red:
  no `ValueError`).
- **Contract text (5)** — focused-test `test_allocation_tools_state_the_lease_deadline_contract`
  in `tests/mcp/core/test_tool_docs.py`, shaped like `:616`, asserting `server_time`,
  `lease_expiry`, and `allocations.renew` in the `window` description and both tool descriptions.

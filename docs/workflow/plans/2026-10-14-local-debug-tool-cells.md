# Local GDB debug tool cells — implementation plan

Spec: `docs/workflow/specs/2026-10-14-local-debug-tool-cells-design.md`. Issue #2813.
Branch `feat/debug-tool-cells-2813`, base `main`.

Guardrails per task: `just test-verbose <paths>`, `just lint`, `just type`; `just records`
for the ADR task. Live proof (task 6) runs on a lab x86_64 host with the committed
candidate deployed.

## Task 1 — ADR-0756 amendment and the filtered set

- Append the dated amendment to `docs/adr/0756-filtered-list-boundary-evidence.md`
  (`## Decision`).
- `scripts/coverage_campaign/contract.py`: `FILTERED_LIST_BOUNDARIES` gains the ten
  pairs.
- `tests/scripts/test_coverage_contract.py`: the set equals the thirteen pairs; each of
  the ten pairs' cells carries the denied-visibility observation.

Verify: `just test-verbose tests/scripts/test_coverage_contract.py`, `just records`.

## Task 2 — group 2813 routing

- `contract.py` `_tool_cells`: group 2813's ppc64le cells → #2818, remote-libvirt cells →
  #3113; extend the routing comment.
- Contract test: ppc64le cells #2818's, remote cells #3113's and unbound, 440 x86_64
  local cells #2813's.

## Task 3 — the `debug.list_sessions` empty page

- `tests/integration/live_stack/tool_cells.py` `_empty_page`: `debug.list_sessions` →
  object id `debug_sessions`, actions `["debug.get_session"]`. Any other unnamed tool
  keeps failing closed (raise for a tool with no page rather than defaulting to the
  artifacts shape).
- Unit test in `tests/integration/live_stack/test_tool_cells.py`.

## Task 4 — the debug scenario module

`tests/integration/live_stack/debug_cells.py`, following `capture_cells.py`:

- `TOOLS`, `GATES` (contributor; viewer for `get_session`/`list_sessions`), `_BELOW`.
- `gdbstub_profile` / `provision_gdbstub`: adds `debug.gdbstub = true` only.
- Ground truth: `Truth` (`/proc/version`, kallsyms addresses and successors for
  `vfs_read`, `vfs_write`, `linux_banner`, `jiffies_64`, the `[loop]` text symbols, the
  `loop` `.text` base); `parse_kallsyms`, `successor`, `in_function`. Checked against the
  fixture `System.map` for `vfs_read`.
- `Session`: the booted fixture System, the operator client, the exposed call (gate
  role, minted per call, `discover=True`), `start`/`end` (end always in `finally`),
  `stop_in_vfs_read` (operator breakpoint, resume task + banner), `halted` (no banner
  reply within 5 s).
- One body per tool (22 with both `start_session` variants) per the spec table; the
  three known-gap bodies end with a failing assertion naming the gap.
- Rejection: `DebugTarget` and `debug_target` (cached, replayed on failure);
  `rejection(tool, boundary, target, variant)` with each tool's fixed arguments, the
  `list_sessions` filtered control, and the absent-id twin; `snapshot_of` reusing
  `capture_cells.protected_state`.
- Unit tests in `tests/integration/live_stack/test_debug_cells.py`: provision options,
  kallsyms parsing, `in_function`, rejection arguments/categories/grants per tool and
  boundary, filtered control wiring, the target's lane-only context.

## Task 5 — the carrier and bindings

- `tests/integration/test_debug_tool_cells_live.py` (`live_stack`), one parameter per
  native local cell of the 21 tools, `run_tool_cell`; blocked when the fixture is absent;
  `blocked` on a non-x86_64 lane.
- `obligations.toml` `[implementations]`: the 440 x86_64 scenario ids (shared with the
  ppc64le ones) → `tests/integration/test_debug_tool_cells_live.py::test_debug_tool_cell`.
- Contract test: all 880 local cells name the node; no remote cell does.

Verify: `just test-verbose tests/integration/live_stack/test_debug_cells.py
tests/integration/live_stack/test_tool_cells.py tests/scripts/test_coverage_contract.py`,
`just lint`, `just type`.

## Task 6 — runbook and live proof

- `docs/operating/runbooks/live-testing.md`: the debug carrier section (prerequisites:
  server `gdb`, the `longterm` fixture, `--kernel-baseline longterm` bindings; run
  command; expected runtime; the three known-gap failures) and the last run's results.
- On the lab guest: deploy the committed candidate, bring the stack up with
  `examples/local-libvirt/demo-up.sh`, write bindings, run both configurations, record results, tear
  down.

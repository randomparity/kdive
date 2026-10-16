# Build-use recovery tool cells — implementation plan (#3111)

Spec: [2026-10-14-build-use-recovery-cells-design.md](../specs/2026-10-14-build-use-recovery-cells-design.md).
Goal: bind and prove the 16 `recovery`-configuration cells of `ops.build_uses_list` and
`ops.recover_build_use` against a real stranded pin.

Expected implementation size: 750–850 changed lines (M) — the helper module and its unit tests
(~480), the carrier bodies (~140), the contract mapping and test (~25) and the docs (~110).

Global Constraints: Python 3.14 and the repository's existing dependencies only; no product,
schema or fixture change; public text names operator decisions by date and no lab identifiers.

Guardrails per task: `just lint`, `just type`, and `just test-verbose <paths>`. Before the push,
`just records` and `just docs-check`, then the full `just ci`.

## Task 1 — R5 and bindings

- Test first, in `tests/scripts/test_coverage_contract.py`:
  - the functional roles of `ops.recover_build_use` are `("server", "worker")`;
  - the 16 cells of `_OPERATOR_SPLIT[3111]` bind to `_OPERATOR_NODE`;
  - the `bound` set gains `*_OPERATOR_SPLIT[3111]`.
- Change `scripts/coverage_campaign/obligations.toml`:
  - set the override to `["server", "worker"]`;
  - add the 8 `tool/ops.{build_uses_list,recover_build_use}/default/{authentication,authorization,functional,validation}`
    bindings beside the operator block.
- Verify: `just test-verbose tests/scripts/test_coverage_contract.py`.

## Task 2 — stranded-pin helper

- New module `tests/integration/live_stack/build_use_recovery.py`:
  - `Seams` dataclass: `sudo(argv) -> CompletedProcess`, `lifecycle(argv) -> LifecycleResponse`,
    with defaults that run the real commands;
  - `stranded_pin(...)` async context manager: system, build, install, freeze, freeze-point
    proof, and restore in `finally`;
  - `kill_and_witness(pin)`.
- Unit tests in `tests/integration/live_stack/test_build_use_recovery.py`, using fake seams and a
  fake database read:
  - a vanished pin, or a held advisory lock, stops `blocked` and thaws;
  - the restore runs when the body raises;
  - the restore restarts the observed slot count only after a kill;
  - a frozen, unkilled worker is thawed.
- Verify: `just test-verbose tests/integration/live_stack/test_build_use_recovery.py`.

## Task 3 — carrier

- `tests/integration/test_operator_tool_cells_live.py`:
  - add both tools to `TOOLS`;
  - dispatch their functional cells to the helper-backed bodies (list: active and orphaned
    comparisons, then recovery as cleanup; recover: alive refusal, then dead success with the
    exact recovery row and every other pin unchanged);
  - extend `_valid` and `_invalid` for the rejection rows in the spec.
- Verify: `uv run python -m pytest --collect-only -q -m live_stack
  tests/integration/test_operator_tool_cells_live.py` reports 140 tests (124 + 16), then
  `just type`.

## Task 4 — records and docs

- Add a dated amendment to ADR-0722 (2026-10-14). It records that termination evidence comes from
  the lifecycle witness's durable incarnation row and that the verifier only gates registration.
  It also names the demo-up `recovery` lane as the recovery tools' functional lane.
- Add a runbook section to `docs/operating/runbooks/live-testing.md`: the `sudo -n` prerequisite,
  the fixture, the 16 cells, what each functional cell observes, and the restore.
- Verify: `just records`, `just docs-check`.

## Task 5 — live proof

- On an assigned local lab guest, bring up the `recovery` lane with
  `KDIVE_WORKER_DEATH_VERIFIER=docker examples/local-libvirt/demo-up.sh` at the committed
  candidate, and confirm the deployed server, worker and reconciler revisions match it.
- Run the 16 cells with `-k build_use` under the runbook environment, assemble the evidence, and
  run `qualify` over the bindings.
- Expect 4 `success` and 12 `rejection` results, and afterwards the started slot count, no pin,
  and no frozen unit. Record the run in the runbook section.

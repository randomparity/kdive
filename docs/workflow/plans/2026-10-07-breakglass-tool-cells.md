# Plan: the x86_64 break-glass provider tool cells (#3112)

**Goal.** Bind and prove the 136 x86_64 cells of `ops.force_release`, `ops.force_teardown`,
`ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict` on both libvirt lanes.
See the [spec](../specs/2026-10-07-breakglass-tool-cells-design.md).

**Architecture.** `obligations.toml` splits group 3112 on the `authority` flag (operator
option A) and binds the 34 scenarios to a new carrier. The carrier reuses the shared frame
unchanged. It proves `ops.force_teardown` on real Systems. It stops the functional cells of
`ops.force_release` (operator checkpoint 2, option (b)) and of the two resolve tools `blocked`,
and it runs every rejection cell against the stack's lane target.

**Tech stack.** Python 3.14, pytest, the repository's `live_stack` harness, and `uv`.

Expected implementation size: 250–330 changed lines (L). Derived from the file map below:
carrier ~190, `obligations.toml` ~45, contract test ~20, and runbook ~50.

## Global Constraints

- No product source, ADR or migration change. No new dependency. No new `KDIVE_`-prefixed
  variable.
- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` is strict over the whole tree (`just type`).
- Prose rule: no "critical", "robust", "comprehensive" or "elegant"; write "Milestone", never
  "Sprint".
- Observations and the `contract.py` routing stay unchanged. Edits to `obligations.toml` stay
  additive, because campaign 6460ad12693e edits it too. The shared frame files (`cleanup.py`,
  `scenario.py`, `remote_lifecycle.py`, `tool_cells.py`) are not edited (operator checkpoint 2).
- Guardrails: `just lint`, `just type` and `just test-changed`; `just docs-check` for the
  runbooks; `just records` needs `git fetch origin main` first.
- Commits follow Conventional Commits, with an imperative subject of 72 characters or fewer.
- The host is x86_64; the targets are x86_64 and ppc64le. The carrier collects remote cells only
  for guest architectures in `REMOTE_LANE_FAMILIES` (x86_64).
- No lab host name or address goes in any committed file, record or PR text.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | one 3112 group with `authority = true`; no 3112 rows | two 3112 groups (the force tools without authority, the resolve tools with it); 34 implementation rows |
| `tests/scripts/test_coverage_contract.py` | 3112 cells unbound | flags and bindings asserted |
| `tests/integration/test_breakglass_tool_cells_live.py` | — | new carrier |
| `docs/operating/runbooks/live-testing.md` | — | one new section after the run cells section, covering both lanes |

## Task 1: Split the flags and bind the scenarios

**Verification.**
- Contract: the force tools carry no `authority` role and the resolve tools carry it. Mode:
  focused-test, `tests/scripts/test_coverage_contract.py::test_operator_tools_follow_the_approved_split`.
  - Red: the new role assertion fails, because every functional cell of the four tools carries
    `authority`.
  - Green: `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.
- Contract: the four tools' cells bind the carrier node. Mode: focused-test, same file,
  `test_pending_cells_have_owned_assertions_but_no_invented_nodes`.
  - Red: `{None}` is not `{_BREAKGLASS_NODE}`.
  - Green: the same command as above.

Steps:

1. In `tests/scripts/test_coverage_contract.py`, add the node constant after `_RUN_NODE`:

   ```python
   _BREAKGLASS_NODE = (
       "tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell"
   )
   ```

2. At the end of `test_operator_tools_follow_the_approved_split`, add:

   ```python
       assert len([c for c in cells if c.owner == 3112]) == 136
       # Operator option A (#3112, 2026-10-07): only the resolve tools need the authority.
       base = ("server", "worker", "reconciler")
       roles = {(c.operation, c.roles) for c in glass if c.kind == "functional"}
       assert roles == {
           ("ops.force_release", base),
           ("ops.force_teardown", base),
           ("ops.resolve_recovery_orphan", (*base, "authority")),
           ("systems.resolve_external_boot_conflict", (*base, "authority")),
       }
   ```

3. In `test_pending_cells_have_owned_assertions_but_no_invented_nodes`, add after the
   `remote_runs` assertion:

   ```python
       glass = [c for c in contract.cells if c.operation in _OPERATOR_SPLIT[3112]]
       assert len(glass) == 272 and {c.node_id for c in glass} == {_BREAKGLASS_NODE}
   ```

   Add `*_OPERATOR_SPLIT[3112],` to its `bound` set. Run the focused command and expect both new
   assertions to fail.

4. In `obligations.toml`, replace the 3112 group with:

   ```toml
   [[groups]]
   owner = 3112
   execution = "provider"
   [groups.tools]
   "ops.force_release" = "Observe terminal release only after protected provider effects are quiescent."
   "ops.force_teardown" = "Verify forced teardown removes owned domain/storage and preserves unrelated resources."

   [[groups]]
   owner = 3112
   execution = "provider"
   authority = true
   [groups.tools]
   "ops.resolve_recovery_orphan" = "Wait for repair and verify only the exact quarantined object set is resolved."
   "systems.resolve_external_boot_conflict" = "Wait for conflict resolution and verify authoritative provider and database state agree."
   ```

5. Append 34 rows at the end of `[implementations]`. For each tool, the local rows are
   `"tool/<tool>/default/<kind>"` and the remote rows are
   `"tool/remote-libvirt/<tool>/default/<kind>"`, each set to
   `"tests/integration/test_breakglass_tool_cells_live.py::test_breakglass_tool_cell"`. The
   kinds are `authentication`, `authorization`, `functional` and `validation` for all four
   tools, plus `project-isolation` for `systems.resolve_external_boot_conflict`. Order: the 17
   local rows, then the 17 remote rows, each sorted by tool and then by kind.
6. `build_contract()` validates that a bound node is an existing test function
   (`_validate_node`), so Task 1 commits after Task 2's carrier, never before it. Run the focused
   command and expect it to pass once Task 2 exists.

## Task 2: The carrier

**Interfaces.** It consumes `on_lane_system`, `lane_target`, `prove_rejection`, `run_tool_cell`, `tool_cells`, `HttpCaller`,
`Grants`, `Rejection`, `project_state`, `boundary_of` and `one` from `tool_cells`;
`observe_host`, `observer`, `remote_host`, `remote_kdive_domains`, `staged_base_volume` and
`REMOTE_REPRESENTATIVES` from `remote_lifecycle`; `staged_image` from `image_smoke`; and
`drain_job`, `await_system_state` and `worker_libvirt_uri` from
`spine`. All of them exist on `main` with the signatures used.

**Verification.**
- Contract: the carrier collects exactly the native and x86_64 remote cells of the four tools.
  Mode: focused-test.
  `uv run python -m pytest tests/integration/test_breakglass_tool_cells_live.py --collect-only -q`
  must list 136 parameters on an x86_64 host. With no stack, they skip (`live_stack`).
- Carrier bodies: task-test-not-applicable. They need a live stack and a provider host, and
  Task 3's lab run is their evidence.

Steps:

1. Write `tests/integration/test_breakglass_tool_cells_live.py` with these units:
   - `_admin()`: `Grants(f"{p}-admin", (p,), {}, ("platform_admin",))` for a fresh `cov-<hex>`
     project `p`.
   - `_platform_operator()`: the same, but with `platform_operator`.
   - `_grants(project, role)` and `_stranger()`, as in the System carrier.
   - `_call(caller, tool, args, token)`: `one(await caller.call(tool, args, token, discover=True))`.
   - `_audit_rows(db_url, principal, tool, scope)`: a read-only
     `SELECT count(*) FROM platform_audit_log WHERE principal = %s AND tool = %s AND scope = %s`.
   - `_inventory(cell)`: through `libvirt.open(worker_libvirt_uri())` locally or
     `observer(remote_host().dest)` remotely, returns `remote_kdive_domains(conn)` and whether
     the lane's staged base still exists.
     - Locally, the base is `staged_image(LANE_IMAGES[platform.machine()]) is not None`.
     - Remotely, `storageVolLookupByName(staged_base_volume(image))` in the refreshed
       `remote_host().pool`, where `VIR_ERR_NO_STORAGE_VOL` means absent.
   - `_defined(xml, system_id)`, as in the System carrier.
   - `_force_teardown(run, caller, db_url, guest)`, the `on_lane_system` body, as the spec's
     functional section states: the call through the exposure with the admin token and
     `reason = "coverage #3112"`, the proofs listed there, and one audit row with scope
     `<project>:<system_id>`.
   - `_blocked(run)`: remotely, `observe_host(run, remote_host())` first; then
     `ScenarioStop(BLOCKED, reason)`. The reason names the tool: for `ops.force_release`, its
     ordering needs an authority-owned System with external-boot history, which no lane frames;
     for the resolve tools, the missing installed authority (remotely, the
     `provider_authority_host` role and `[[remote_libvirt]]` authority tuple, which would route
     every remote install and boot through external boot), the missing authority-lane System
     frame, and the missing orphan or conflict construction.
   - `_args(tool, system_id, allocation_id)` and `_rejection(tool, boundary, target)`, per the
     spec's rejection table.
   - `_scenario` dispatches the functional kinds and otherwise runs `prove_rejection` with
     `partial(project_state, db_url, target.project)`.
   - `_cells()` applies the System carrier's filter, and `test_breakglass_tool_cell(cell)` calls
     `run_tool_cell(cell, _scenario)`.
2. Run `just lint`, `just type` and Task 1's focused contract command (green). Run the collect
   command (136). Commit in this order, the carrier first because it collects through
   `tool_cells(TOOLS)` and needs no binding: `test(live): carry the x86_64 break-glass provider
   tool cells`, then Task 1 as `test(coverage): split break-glass flags and bind the cells`.

## Task 3: The runbook and the lab run

**Verification.**
- Runbook section: task-test-not-applicable, because they are human procedure. `just docs-check`
  is the guardrail.
- Lab run: deploy the committed head on the control-plane guest. Run `demo-up.sh` in the default
  configuration and then, after `demo-down.sh --wipe --yes`, in the recovery configuration
  (`KDIVE_WORKER_DEATH_VERIFIER=docker`). In each configuration, run the carrier with
  `-k local-libvirt` and with `-k remote-libvirt`. Then `evidence assemble` and `qualify`.
  Expected: 112 qualified (8 `success`, 104 `rejection`) and 24 functional cells `blocked`
  (8 `ops.force_release` and 16 resolve), so `qualify` exits non-zero by design. The provider host serves only this run while
  it lasts. Wipe afterwards; `virsh list --all` shows no `kdive-` domain on either host. On the
  provider host, any leftover `kdive-` domain is removed with `virsh destroy` and `undefine`, and
  its overlay volume with `virsh vol-delete`, because the wipe does not reach that host.

Steps:

1. In `live-testing.md`, add `#### Break-glass provider tool cells (#3112)` after the run cells
   section. It covers:
   - what the force-teardown cells do, and why the other functional cells are blocked;
   - a summary of the rejection table;
   - the bindings command (`--remote`, no `--kernel-baseline`) and both lanes' commands
     (`-k local-libvirt` and `-k remote-libvirt`), with the remote set-up linked to
     `remote-live-stack.md` §8;
   - that the provider serves only this run;
   - the manual removal of provider leftovers after an interrupted run.
2. Run the lab run, then write the "Last run" paragraphs with the candidate SHA, the counts and
   the cleanup checks. Keep host names and addresses out.
3. Run `just docs-check`, then commit: `docs(runbook): add and record the break-glass tool
   cells`.

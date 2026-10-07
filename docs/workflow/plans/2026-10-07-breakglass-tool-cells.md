# Plan: the x86_64 break-glass provider tool cells (#3112)

**Goal.** Bind and prove the 136 x86_64 cells of `ops.force_release`, `ops.force_teardown`,
`ops.resolve_recovery_orphan` and `systems.resolve_external_boot_conflict` on both libvirt lanes.
See the [spec](../specs/2026-10-07-breakglass-tool-cells-design.md).

**Architecture.** `obligations.toml` splits group 3112 on the `authority` flag (operator
option A) and binds the 34 scenarios to a new carrier. The frame gains an optional `release`
seam, next to its `provision` seam, so a cell can make the frame's release its tool under test.
The carrier proves the two force tools on real Systems. It stops the two resolve functional
cells `blocked`, and runs every rejection cell against the stack's lane target.

**Tech stack.** Python 3.14, pytest, the repository's `live_stack` harness, and `uv`.

Expected implementation size: 330–420 changed lines (L). Derived from the file map below:
carrier ~230, frame seam ~25, seam test ~20, `obligations.toml` ~45, contract test ~20, and
runbooks ~60.

## Global Constraints

- No product source, ADR or migration change. No new dependency. No new `KDIVE_`-prefixed
  variable.
- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` is strict over the whole tree (`just type`).
- Prose rule: no "critical", "robust", "comprehensive" or "elegant"; write "Milestone", never
  "Sprint".
- Observations and the `contract.py` routing stay unchanged. Edits to `obligations.toml`,
  `tool_cells.py` and the frame stay additive, because campaign 6460ad12693e edits them too.
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
| `tests/integration/live_stack/cleanup.py` | `release_and_verify` calls `allocations.release` itself | adds `Release`, `release_allocation`, and a `release` parameter |
| `tests/integration/live_stack/scenario.py` | `on_catalog_system`, `_cleanup` | add a `release` keyword passed through to the cleanup |
| `tests/integration/live_stack/remote_lifecycle.py` | `on_remote_system`, `remote_cleanup` | add a `release` keyword passed through to the cleanup |
| `tests/integration/live_stack/tool_cells.py` | `LaneFrame` protocol | adds a `release` keyword |
| `tests/integration/live_stack/test_cleanup.py` | — | a supplied release replaces `allocations.release` |
| `tests/integration/test_breakglass_tool_cells_live.py` | — | new carrier |
| `docs/operating/runbooks/live-testing.md` | — | new section after the run cells section |
| `docs/operating/runbooks/remote-live-stack.md` | — | new §10 |

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
   (`_validate_node`), so Task 1 commits after Task 3's carrier, never before it. Run the focused
   command and expect it to pass once Task 3 exists.

## Task 2: The release seam

**Interfaces.** Later tasks rely on:
`cleanup.Release = Callable[[LiveStackClient, str], Awaitable[None]]`,
`cleanup.release_allocation(client, allocation_id) -> None`, and a keyword `release: Release`
on `release_and_verify`, `scenario.on_catalog_system`, `remote_lifecycle.on_remote_system`,
`remote_lifecycle.remote_cleanup` and `tool_cells.LaneFrame.__call__`.

**Verification.**
- Contract: a supplied `release` replaces `allocations.release`, and the proof still verifies
  teardown, domain, disks and capacity. Mode: focused-test,
  `tests/integration/live_stack/test_cleanup.py::test_supplied_release_replaces_allocations_release`.
  - Red: `TypeError: unexpected keyword argument 'release'`.
  - Green: `uv run python -m pytest tests/integration/live_stack/test_cleanup.py -q`.
- Contract: the existing frames keep today's release. Mode: focused-test, the existing
  `test_cleanup.py` cases, which assert `allocations.release` is called. The green command is
  the same.

Steps:

1. Add the test:

   ```python
   def test_supplied_release_replaces_allocations_release() -> None:
       client = _Client(1, 0)
       released: list[str] = []

       async def release(_client: LiveStackClient, allocation_id: str) -> None:
           released.append(allocation_id)

       result = asyncio.run(
           release_and_verify(
               cast(LiveStackClient, client),
               allocation_id="a",
               system_id="s",
               domain="kdive-s",
               disks=[],
               in_use_before=0,
               connect=lambda: _Conn(False),
               release=release,
               deadline_s=1.0,
               poll_s=0.0,
           )
       )
       assert released == ["a"] and "allocations.release" not in client.calls
       assert result["allocation"] == "released" and result["in_use"] == [0, 1, 0]
   ```

2. In `cleanup.py`, import `Awaitable` beside `Callable` and add, before `release_and_verify`:

   ```python
   Release = Callable[[LiveStackClient, str], Awaitable[None]]


   async def release_allocation(client: LiveStackClient, allocation_id: str) -> None:
       """``allocations.release`` as the frame's project operator: the frames' default release."""
       ok(await scalar(client, "allocations.release", allocation_id=allocation_id), "release")
   ```

   Then add `release: Release = release_allocation,` after `absent` in `release_and_verify`'s
   parameters, and replace its `ok(await scalar(client, "allocations.release", ...), "release")`
   line with `await release(client, allocation_id)`. Extend the docstring: "``release`` performs
   the release; a cell whose tool under test is the release passes its own."
3. In `scenario.py`, give `on_catalog_system` the keyword `release: Release = release_allocation`
   after `provision`, and give `_cleanup` the trailing keyword
   `release: Release = release_allocation`, passed as `release=release` to `release_and_verify`.
   In `on_catalog_system`, pass `release=release` only to the direct `_cleanup` call that proves
   `cleanup`. The `cleanup_attempt` partial keeps the default `release_allocation`: the failure
   path never re-runs a cell's tool under test. Import `Release` and `release_allocation` from
   `cleanup`.
4. In `remote_lifecycle.py`, apply the same change: `remote_cleanup(..., before, release=...)`
   passes it to `release_and_verify`, and `on_remote_system(..., provision, release=...)` passes
   it only to the direct `remote_cleanup` call. The `cleanup_attempt` partial keeps the default.
5. In `tool_cells.py`, add `release: Release = release_allocation,` to `LaneFrame.__call__`
   after `provision`, and import both names from `cleanup`.
6. Run the focused command (green), then `just type` (green), then commit:
   `test(live-stack): let a cell supply the frame's release`.

## Task 3: The carrier

**Interfaces.** It consumes `lane_for`, `Lane.frame` (with `release`), `on_lane_system`,
`lane_target`, `observe_guest`, `prove_rejection`, `run_tool_cell`, `tool_cells`, `HttpCaller`,
`Grants`, `Rejection`, `project_state`, `boundary_of` and `one` from `tool_cells`;
`observe_host`, `observer`, `remote_host`, `remote_kdive_domains`, `staged_base_volume` and
`REMOTE_REPRESENTATIVES` from `remote_lifecycle`; `staged_image` from `image_smoke`; and
`drain_job`, `await_system_state`, `scalar`, `mint_role_token` and `worker_libvirt_uri` from
`spine`. All of them exist on `main` with the signatures used.

**Verification.**
- Contract: the carrier collects exactly the native and x86_64 remote cells of the four tools.
  Mode: focused-test.
  `uv run python -m pytest tests/integration/test_breakglass_tool_cells_live.py --collect-only -q`
  must list 136 parameters on an x86_64 host. With no stack, they skip (`live_stack`).
- Carrier bodies: task-test-not-applicable. They need a live stack and a provider host, and
  Task 4's lab run is their evidence.

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
   - `_force_teardown(run, caller, db_url, guest)` and `_force_release(run, caller, base_url,
     issuer, db_url)`, as the spec's functional section states: the call through the exposure
     with the admin token and `reason = "coverage #3112"`, the proofs listed there, and one
     audit row with scope `<project>:<object id>`. `_force_release` calls `lane.frame` with
     `provision=provision_catalog` and a `release` that makes the call and requires `released`.
     After the frame returns, it re-reads `allocations.wait` (`timeout_s=0`) with a fresh
     project-operator token, requires `released`, and proves `effect`.
   - `_blocked(run)`: remotely, `observe_host(run, remote_host())` and then
     `ScenarioStop(BLOCKED, remote reason)`; locally, `ScenarioStop(BLOCKED, local reason)`. The
     spec gives the reasons.
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

## Task 4: Runbooks and the lab run

**Verification.**
- Runbook sections: task-test-not-applicable, because they are human procedure. `just docs-check`
  is the guardrail.
- Lab run: deploy the committed head on the control-plane guest. Run `demo-up.sh` in the default
  configuration and then, after `demo-down.sh --wipe --yes`, in the recovery configuration
  (`KDIVE_WORKER_DEATH_VERIFIER=docker`). In each configuration, run the carrier with
  `-k local-libvirt` and with `-k remote-libvirt`. Then `evidence assemble` and `qualify`.
  Expected: 120 qualified (16 `success`, 104 `rejection`) and the 16 resolve functional cells
  `blocked`, so `qualify` exits non-zero by design. The provider host serves only this run while
  it lasts. Wipe afterwards; `virsh list --all` shows no `kdive-` domain on either host. On the
  provider host, any leftover `kdive-` domain is removed with `virsh destroy` and `undefine`, and
  its overlay volume with `virsh vol-delete`, because the wipe does not reach that host.

Steps:

1. In `live-testing.md`, add `#### Break-glass provider tool cells (#3112)` after the run cells
   section. It covers what each functional cell does, the blocked resolve cells, the rejection
   table summary and the bindings command (no `--kernel-baseline`), with `-k local-libvirt`.
   In `remote-live-stack.md`, add `## 10. Remote break-glass tool cells (#3112)`, which runs the
   same carrier with `-k remote-libvirt` on §8's lane. It states that the provider serves only
   this run, and gives the manual removal of provider leftovers after an interrupted run.
2. Run the lab run, then write the "Last run" paragraphs with the candidate SHA, the counts and
   the cleanup checks. Keep host names and addresses out.
3. Run `just docs-check`, then commit: `docs(runbook): add and record the break-glass tool
   cells`.

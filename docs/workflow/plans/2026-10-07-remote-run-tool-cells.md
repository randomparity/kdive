# Plan: the x86_64 remote run and image tool cells (#3120)

**Goal.** Prove the 96 x86_64 remote-libvirt cells of `runs.install`, `runs.boot`,
`runs.cancel`, `runs.release_external_boot` and `images.publish` with the existing run carrier.
[Spec](../specs/2026-10-07-remote-run-tool-cells-design.md).

**Architecture.** `[implementations]` binds the 24 remote scenarios to the run carrier node. The
carrier `tests/integration/test_run_tool_cells_live.py` also collects the remote cells. It takes
every provider name from the cell, observes the remote installed kernel with #2810's
`guest_boot_kernel`, and stops the remote release and publish functional cells `blocked`. The
frame (`tool_cells.py`, `remote_lifecycle.py`, `deep_lifecycle.py`) is reused unchanged.

**Tech stack.** Python 3.14, pytest, the repository's `live_stack` harness, `uv`.

Expected implementation size: 150–200 changed lines (L) — the file map below: carrier ~70,
`obligations.toml` 24, contract test ~10, runbook ~70.

## Global Constraints

- No product source, ADR or migration change; no new dependency; no new `KDIVE_`-prefixed
  variable.
- Ruff line length 100, lint `E,F,I,UP,B,SIM`; `ty` strict over the whole tree (`just type`).
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; "Milestone", never "Sprint".
- Owners, flags and `contract.py` unchanged. Edits to `obligations.toml` stay additive: another
  campaign edits it.
- Guardrails: `just lint`, `just type`, `just test-changed`; `just docs-check` for the runbook;
  `just records` needs `git fetch origin main` first.
- Commit format: Conventional Commits, imperative subject of 72 characters or fewer.
- Host x86_64; targets x86_64 and ppc64le. The carrier collects remote cells only for guest
  architectures in `REMOTE_LANE_FAMILIES` (x86_64).
- No lab host name or address in any committed file, record or PR text.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | 24 local run/image rows | plus 24 `tool/remote-libvirt/<tool>/default/<kind>` rows after the remote System rows |
| `tests/scripts/test_coverage_contract.py` | remote run/image cells unbound | remote run/image cells (192) bound to the run carrier node |
| `tests/integration/test_run_tool_cells_live.py` | local cells only; `local-libvirt` hard-coded | remote cells collected; provider from the cell; remote installed-kernel observer; remote release and publish functional cells blocked |
| `docs/operating/runbooks/remote-live-stack.md` | §8 remote System cells | §9 remote run and image cells with the lab result |
| `docs/operating/runbooks/live-testing.md` | #3119 command runs the whole carrier | `-k local-libvirt` on that command |

## Task 1 — Bind the remote scenarios

**Verification.**
- Contract: the 24 remote run/image scenarios bind the run carrier node. Mode: focused-test.
  `tests/scripts/test_coverage_contract.py::test_pending_cells_have_owned_assertions_but_no_invented_nodes`.
  Red after step 1: the new assertion `len(remote_runs) == 192 and {node} == {_RUN_NODE}` fails
  with `{None}`. Green after step 2:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`.

Steps:

1. In `test_pending_cells_have_owned_assertions_but_no_invented_nodes`, replace

   ```python
       assert {c.node_id for c in runs if c.provider == "remote-libvirt"} == {None}
   ```

   with

   ```python
       remote_runs = [c for c in runs if c.provider == "remote-libvirt"]
       assert len(remote_runs) == 192 and {c.node_id for c in remote_runs} == {_RUN_NODE}
   ```

   and delete the now-wrong exclusion below it:

   ```python
       remote_runs = {c.id for c in runs if c.provider == "remote-libvirt"}
       unbound = [c for c in contract.cells if c.operation not in bound or c.id in remote_runs]
   ```

   becomes

   ```python
       unbound = [c for c in contract.cells if c.operation not in bound]
   ```

   Run the focused command: expect one failure on the new assertion.
2. In `scripts/coverage_campaign/obligations.toml` `[implementations]`, after the last
   `tool/remote-libvirt/systems.teardown/...` row, add one row per tool and kind, value
   `"tests/integration/test_run_tool_cells_live.py::test_run_tool_cell"`: tools
   `images.publish`, `runs.boot`, `runs.cancel`, `runs.install`, `runs.release_external_boot`;
   kinds `authentication`, `authorization`, `functional`, `project-isolation`, `validation`,
   except `images.publish`, which has no `project-isolation` scenario (24 rows). Key form:
   `"tool/remote-libvirt/runs.boot/default/authentication"`. Run the focused command: green.
3. `just lint`, then commit `test(coverage): bind the remote run and image tool cells`.

## Task 2 — The remote lane in the run carrier

**Verification.**
- Contract: the carrier collects the 96 x86_64 remote cells. Mode: focused-test (structural).
  Red before the change: `uv run python -m pytest --collect-only -q -m live_stack -k remote-libvirt
  tests/integration/test_run_tool_cells_live.py` reports `96 deselected` / no remote ids. Green:
  the same command lists 96 `remote-libvirt` ids.
- Contract: provider-qualified target Run, publish arguments and snapshot. Mode:
  task-test-not-applicable — the code acts only against a live stack and provider host; the lab
  run's remote rejection cells and `qualify` prove it.
- Contract: remote installed-kernel observer and blocked remote release/publish cells. Mode:
  task-test-not-applicable — `guest_boot_kernel` is #2810's unchanged function and the blocks are
  a `ScenarioStop` before any stack call; the lab records prove both.

Edits in `tests/integration/test_run_tool_cells_live.py`:

1. Imports: add `REMOTE_LANE_FAMILIES` to the `tool_cells` import list; add
   `REMOTE_REPRESENTATIVES`, `guest_boot_kernel`, `observe_host` and `remote_host` from
   `tests.integration.live_stack.remote_lifecycle`.
2. Constants: rename `_PROVIDER` to `_LOCAL` everywhere; add `_REMOTE = "remote-libvirt"` and

   ```python
   _NO_REMOTE_AUTHORITY = (
       "runs.release_external_boot needs a remote provider authority (provider_authority_host "
       "and the [[remote_libvirt]] authority tuple) and an authority-lane System frame; no "
       "runbook provisions one, and an authority would route every remote install and boot "
       "through external boot"
   )
   _NO_REMOTE_PUBLISH = (
       "images.publish builds catalog images for local-libvirt only: the IMAGE_BUILD handler "
       "answers remote-libvirt with configuration_error (not implemented); remote base images "
       "are staged with deploy/ansible/playbooks/image.yml"
   )
   ```
3. `_create_run(op, investigation, system_id, provider: str = _LOCAL)`: the unbound branch sends
   `{"target_kind": provider}`.
4. `_step`: pass `installed_kernel=guest_boot_kernel if guest.lane.provider == _REMOTE else
   partial(_domain_kernel, guest.lane.xml)`.
5. `_build_jobs(db_url, provider, name)` keys `f"image_build:{provider}:{name}"`; the local
   `_publish` calls it with `_LOCAL`.
6. `_functional`: before the local branches,

   ```python
       remote = run.cell.provider == _REMOTE
       blocked = {"runs.release_external_boot": _NO_AUTHORITY}
       if remote:
           blocked = {"runs.release_external_boot": _NO_REMOTE_AUTHORITY, PUBLISH_TOOL: _NO_REMOTE_PUBLISH}
       if tool in blocked:
           if remote:
               # A read-only probe, so the blocked record carries the provider host like the rest.
               observe_host(run, remote_host())
           raise ScenarioStop(Outcome.BLOCKED, blocked[tool])
       if tool == PUBLISH_TOOL:
           await _publish(run, caller, base_url, issuer, db_url)
           return
   ```
7. `_RUN_TARGETS: dict[tuple[str, str], _RunTarget | Exception]`, keyed
   `(base_url, run.cell.provider)`; `_unbound_run(base_url, issuer, db_url, project, provider)`
   passes `provider` to `_create_run`.
8. `_publish_rejection(boundary, provider, name)` and `_publish_state(db_url, provider, name)` use
   `provider` in their arguments, the catalog-row query and `_build_jobs`.
9. `_scenario`: the publish rejection name is

   ```python
   def _rejected_image(cell: Cell) -> str:
       """The image name a publish rejection aims at: the published image, or the remote lane's."""
       if cell.provider == _REMOTE:
           return REMOTE_REPRESENTATIVES[REMOTE_LANE_FAMILIES[str(cell.guest_arch)]].name
       return PUBLISHED_IMAGES[(platform.machine(), cell.exposure)]
   ```

   called after `lane_target`, which blocks a guest architecture without a remote lane first.
10. `_cells()`:

    ```python
    def _cells() -> list[Cell]:
        host = platform.machine()
        return [
            c
            for c in tool_cells(TOOLS)
            if (c.provider == _LOCAL and c.guest_arch == host)
            or (c.provider == _REMOTE and c.guest_arch in REMOTE_LANE_FAMILIES)
        ]
    ```
11. Module docstring: name the remote cells and `remote-live-stack.md`.
12. Run the collection command (96 remote ids), `just lint`, `just type`; commit
    `test(live): carry the x86_64 remote run and image tool cells`.

## Task 3 — Runbook and lab run

**Verification.**
- Contract: runbook procedure and recorded run. Mode: task-test-not-applicable — prose; `just
  docs-check`.

Steps:

1. Add `## 9. Remote run and image tool cells (#3120)` to
   `docs/operating/runbooks/remote-live-stack.md`: the cells, what differs from #3119 (in-guest
   install observer, blocked release and publish with reasons), prerequisites (§8's plus the §7
   `DOCKER-USER` rule if guests cannot reach the object store, the `longterm` fixture), the
   command block (`bindings --remote --kernel-baseline longterm`, `-k remote-libvirt` per
   configuration on a wiped stack, assemble, qualify), the expected tally (per configuration 6
   `success`, 38 `rejection`, 4 `blocked`), and the provider check, with the §7 leftover-allocation
   release before the wipe.
1. In `docs/operating/runbooks/live-testing.md` §"Run and image tool cells (#3119)", append
   `-k local-libvirt` to `uv run python -m pytest -m live_stack tests/integration/test_run_tool_cells_live.py`
   and say the carrier also holds the remote cells of `remote-live-stack.md` §9.
2. Lab run on the disposable kdive-servers lab (control plane and separate provider host), the
   stack at the committed candidate in both configurations. After the default stack's carrier
   run, one manual `images.publish` call for `remote-libvirt` with a throwaway name (no image of
   that name exists, so a build that did run could overwrite nothing), recording the job's
   terminal state, error category, message and `details.provider`.
3. Record the run (candidate, hosts by OS only, outcomes, lab-only workarounds) in §9; `just
   docs-check`; commit `docs(runbook): record the remote run and image tool-cell lab run`.

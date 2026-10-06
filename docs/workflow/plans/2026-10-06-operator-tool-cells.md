# Read-only operator tool cells (#2812) — implementation plan

Goal: split owner 2812's contract groups as the operator approved, and bind and prove the 124
cells of the eight read-only operator tools on the #2811 tool-cell carrier, in both
configurations and both exposures.

Architecture: an `obligations.toml` regrouping plus a one-condition owner route in
`contract.py`, one new live carrier `tests/integration/test_operator_tool_cells_live.py`
parametrized over the eight tools' cells, 31 `[implementations]` entries, contract tests, and a
runbook section. Design: [spec](../specs/2026-10-06-operator-tool-cells-design.md); decision
record ADR-0722. The harness `tests/integration/live_stack/tool_cells.py` is reused unchanged.

Tech stack: Python 3.14, pytest, fastmcp client (`kdive.mcp.dev_harness`), psycopg 3 — all
existing dependencies; nothing is added.

Expected implementation size: 700–800 changed lines (M) — the carrier (~570), the regrouping
(~45 moved lines plus 31 bindings), contract tests (~45) and the runbook (~35). The range is
above the M band because the carrier, like the #3095 and #3096 carriers, is one functional body
per tool; the scope did not change.

## Global Constraints

- No product source change, no harness change, no new dependency, no ADR, no migration.
- ADR-0722 governs exposure, configuration proof, rejection rules and the protected-state
  snapshot. The existing carriers keep their behaviour; their tests stay green unchanged.
- Keep `obligations.toml` edits additive around the other groups: campaign 6460ad12693e may
  land #3097/#3098 bindings in `[implementations]` concurrently.
- Line length 100; `just lint`, `just type` and `just test-changed` green before each commit;
  `git fetch origin main && just records` before pushing.
- Live proof only on the disposable lab host, both lanes, at a committed deployed head.
- Public text names no host, address, user or lab identifier.

## File map

| File | Today | After |
|---|---|---|
| `scripts/coverage_campaign/obligations.toml` | 2812 owns 24 service + 4 provider tools | 2812 keeps 8; new groups 3110 (14) and 3111 (2); provider group owner 3112; + 31 bindings (criteria 3, 5) |
| `scripts/coverage_campaign/contract.py` | ppc64le route for owner 3062 only | owners 3062 and 3112 (criterion 4) |
| `tests/scripts/test_coverage_contract.py` | split and binding assertions for 2811/3095-3098 | + the 2812 split, the routing, and the new node (criteria 3-4) |
| `tests/integration/test_operator_tool_cells_live.py` | — | the eight-tool carrier (criteria 1, 2) |
| `docs/operating/runbooks/live-testing.md` | core, catalog, investigation sections | + operator section (criterion 5) |

No ownership transition in code; no path becomes obsolete.

## Task 1 — ownership split and routing

Files: modify `scripts/coverage_campaign/obligations.toml`, `scripts/coverage_campaign/contract.py`,
`tests/scripts/test_coverage_contract.py`.

Interfaces: produces the owner sets Task 2's bindings and tests rely on. Consumes
`build_contract`, `load_mapping` (existing, `scripts/coverage_campaign/contract.py`).

Verification:

- Contract: owner split and overrides. `Mode: focused-test` —
  `test_operator_tools_follow_the_approved_split` in `tests/scripts/test_coverage_contract.py`;
  red: `owned` differs (2812 still owns 28 tools); green:
  `uv run python -m pytest tests/scripts/test_coverage_contract.py -q` passes.
- Contract: ppc64le routing. `Mode: focused-test` — same test asserts every 3112-group cell on
  `ppc64le` is owned by 2818; red before the `contract.py` change.

Steps:

1. Add to `tests/scripts/test_coverage_contract.py`, after `test_core_tools_follow_the_approved_split`:

   ```python
   _OPERATOR_TOOLS = {
       "audit.query",
       "inventory.list",
       "ops.diagnostics",
       "ops.export_cost_classes",
       "ops.export_systems_toml",
       "ops.jobs_list",
       "ops.tool_trail",
       "secrets.list",
   }
   _OPERATOR_SPLIT = {
       3110: {
           "images.extend",
           "images.prune_expired",
           "inventory.clear_override",
           *(f"ops.{n}" for n in ("reconcile_now", "reconcile_systems", "set_cost_class_coeff")),
           *(f"ops.{n}" for n in ("set_host_capacity", "set_queue_paused")),
           *(f"resources.{n}" for n in ("deregister", "drain", "register", "renew")),
           *(f"resources.{n}" for n in ("set_scheduling", "set_status")),
       },
       3111: {"ops.build_uses_list", "ops.recover_build_use"},
       3112: {
           "ops.force_release",
           "ops.force_teardown",
           "ops.resolve_recovery_orphan",
           "systems.resolve_external_boot_conflict",
       },
   }


   def test_operator_tools_follow_the_approved_split(inventory: Inventory) -> None:
       cells = build_contract(inventory=inventory).cells
       split = {*_OPERATOR_SPLIT[3110], *_OPERATOR_SPLIT[3111], *_OPERATOR_SPLIT[3112]}
       ops = [c for c in cells if c.operation in _OPERATOR_TOOLS | split]
       owned = {o: {c.operation for c in ops if c.owner == o} for o in (2812, *_OPERATOR_SPLIT)}
       assert owned == {2812: _OPERATOR_TOOLS, **_OPERATOR_SPLIT}
       assert len([c for c in cells if c.owner == 2812]) == 124
       glass = [c for c in cells if c.operation in _OPERATOR_SPLIT[3112]]
       assert {c.owner for c in glass if c.guest_arch == "ppc64le"} == {2818}
       assert {c.owner for c in glass if c.guest_arch == "x86_64"} == {3112}
       overrides = {g.owner: set(g.role_overrides) for g in load_mapping().groups}
       assert overrides[3110] == {
           "inventory.clear_override",
           "ops.reconcile_now",
           "ops.set_queue_paused",
           "resources.drain",
       }
       assert overrides[3111] == {"ops.recover_build_use"}
   ```

   Break-glass ppc64le cells belong to 2818, under no key of `owned`; the `glass` assertions
   cover them.
2. Run `uv run python -m pytest tests/scripts/test_coverage_contract.py -q -k operator`; expect
   one failure on `owned`.
3. In `obligations.toml`, move the 14 `#3110` tool lines (names in step 1) out of the
   `owner = 2812` service group, verbatim, into a new `[[groups]]` with `owner = 3110` and
   `execution = "service"` placed after the 2812 service group, and move the four matching
   `[groups.role_overrides]` lines with them. Move `ops.build_uses_list` and
   `ops.recover_build_use` into a new `owner = 3111`, `execution = "service"` group with the
   `"ops.recover_build_use" = ["server", "worker", "authority"]` override. Change the provider
   group's `owner = 2812` to `owner = 3112`. Leave the 2812 group with exactly the eight
   tools and no `[groups.role_overrides]` table.
4. In `contract.py` `_tool_cells`, change `if group.owner == 3062 and arch == "ppc64le":` to
   `if group.owner in (3062, 3112) and arch == "ppc64le":` and add the comment
   `# Break-glass ppc64le cells wait for the native POWER lane (#2818), like lifecycle's.`
5. Run the step-2 command; expect it to pass. Run
   `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`; expect all pass.
6. `just lint`, `just type`; commit `test(coverage): split operator tool ownership into
   #3110-#3112`.

## Task 2 — operator carrier and bindings

Files: create `tests/integration/test_operator_tool_cells_live.py`; modify `obligations.toml`
`[implementations]`, `tests/scripts/test_coverage_contract.py`.

Interfaces consumed (existing in `tests/integration/live_stack/tool_cells.py`): `Grants(subject,
projects, roles={}, platform_roles=())`, `HttpCaller(exposure, base_url, issuer)` with
`token(grants) -> str` and `async call(tool, args, token, *, discover=False)`, `one(result) ->
ToolResponse`, `Rejection(args, grants)`, `prove_functional(run, caller, grants, body,
snapshot)` (a body's returned `owned` list is recorded), `prove_rejection(run, caller, boundary,
rejection, snapshot)`, `project_state(db_url, project)`, `boundary_of(cell)`, `tool_cells(tools)`,
`run_tool_cell(cell, scenario)`, `Functional`, `Exposure`, `Boundary`; `ScenarioStop`,
`CellRun` (`tests/integration/live_stack/scenario.py`). Product names read by the carrier:
`SECRET_REF_ID` (`kdive.diagnostics.checks`), `WORKER_UNAVAILABLE_DETAIL`
(`kdive.diagnostics.contracts`), the four `*_ID` constants in
`kdive.diagnostics.contributions.multiarch_gdb`, `args_digest` (`kdive.security.audit`),
`read_secret_file(root, ref)` (`kdive.security.secrets.secrets`), `INVENTORY_WRITEBACK` and
`SECRETS_ROOT` (`kdive.config.core_settings`), `CLI_CLIENT_ID` (`kdive.config.cli_settings`),
`load_inventory_optional`, `systems_toml_path`. Each was confirmed at `2aca4de60`.

Verification:

- Contract: bindings. `Mode: focused-test` — `test_pending_cells_have_owned_assertions_but_no_invented_nodes`
  gains `_OPERATOR_NODE` and `_OPERATOR_TOOLS` (124 cells, one node) and adds the eight tools to
  `bound`; red before the bindings (`node_id` is `None`); green with the step-6 command.
- Contract: the carrier's live behaviour. `Mode: task-test-not-applicable` — every assertion is
  against a live server, worker and evidence database; there is nothing a unit test could
  observe that is not the live tool itself. Proven by the two-lane lab run (Task 3).

Carrier structure (one module, ADR-0722 frame; the spec's *Functional effects* and *Rejection
cells* are the normative assertions each function implements):

| Function | Contract |
|---|---|
| `_rows(db_url, query, params)` | read-only `psycopg` session, `dict_row` rows |
| `_functional_grants(tool, project)` | `audit.query`: admin of P + `platform_auditor`; `inventory.list`, `ops.tool_trail`: `platform_auditor`; else `platform_operator` |
| `_pages(caller, tool, request, token)` | every item, `limit=1` per page via `next_cursor`; `discover` on the first page; fails past 200 pages |
| `_settled(observe, read)` | `read()`, `observe()`, `read()`; returns when both reads agree, else retries, failing after 3 |
| `_audit` | three viewer denials (`audit.query` project form: P twice, P2 once) → `audit_log` rows by subject; project form equals P's rows, all-projects form equals all three, as `ts` datetimes and `""` for NULL |
| `_granted(caller, db_url)` | `allocations.request` 1 vCPU / 1 GiB / 1 GB in `KDIVE_PROJECT` as contributor; must be `granted`; released on exit and checked `released`; released best-effort if the body raised |
| `_inventory` | project and `resource_id` filtered listings (limit 200) equal the allocation and System rows of `allocations` / `systems ⋈ allocations ⋈ resources` in `created_at DESC, id DESC`, holding the granted allocation; `limit=1` gives `allocation_count == 1` and `truncated` iff either stream has more than one row; returns `owned=[allocation id]` |
| `_diagnose(caller, token)` | default run; any `provider == "remote-libvirt"` item stops the cell `blocked` |
| `_diagnostics` | checks equal `{secret_ref, multiarch_gdb, pseries_fadump, guest_arch_accel, depmod_toolchain}`; `secret_ref` pass; no worker check carries `WORKER_UNAVAILABLE_DETAIL`; `has_failure`/`has_error` match statuses; `git rev-parse HEAD` starts with `service_version.commit`; `with_egress=true` returns one `error` item `diagnostics` whose detail contains "could not be assembled" |
| `_export_cost` | `tomllib` parse of `data.toml` `cost_class` equals `cost_class_coefficients` rows (`name`, `str(coeff)`) by name |
| `_export_systems` | `KDIVE_INVENTORY_WRITEBACK` unset; same cost-class equality; declared `local_libvirt` and `image` names ⊆ exported; `persist=true` answers `configuration_error` containing "writeback is disabled" |
| `_jobs` | `_diagnose` first; the newest `diagnostics_worker_check` job is the known job; `_settled` listing (limit 200): `depth_*` equal `GROUP BY state`, rows equal `jobs` rows (`id`, kind, state, `authorizing->>'project'`, attempt as text, worker or `""`); known job present under a project the token does not hold; `_settled` paging of the known job's state equals the database order |
| `_trail` | unique viewer token calls `projects.list`, `session.whoami`, `inventory.list` (denied); `tool_invocation` rows of its session are `(inventory.list, denied), (session.whoami, ok), (projects.list, ok)` with subject, `operator-cli`, the CLI client id and `args_digest({})`; paged trail equals them |
| `_configured_secrets()` / `_secrets` | secret settings' values: a value resolving under `KDIVE_SECRETS_ROOT` is a reference and its content a secret, else the value is a secret; labels sorted, unique, ⊆ `{<process-global>, <scoped>} ∪ refs`; no secret in `model_dump_json()` |
| `_valid` / `_invalid` | `audit.query` `{"request": {"scope": "project", "project": P}}` / `{"request": {"scope": "galaxy"}}`; `ops.diagnostics` `{}` / `{"with_egress": "maybe"}`; `ops.export_systems_toml` `{}` / `{"persist": "maybe"}`; others `{}` / `{"request": {"limit": "many"}}` |
| `_rejection_grants` | validation: functional grants; project-isolation: admin of a fresh project; authorization: `platform_auditor` for operator-gated tools, else viewer of P; authentication: viewer of P |
| `_scenario` / `test_operator_tool_cell` | the #3095 pattern: fresh P, `project_state` snapshot, `prove_functional` with `partial(body, db_url=db_url)` or `prove_rejection`; parametrized over `tool_cells(TOOLS)` |

Steps:

1. In `tests/scripts/test_coverage_contract.py`, add
   `_OPERATOR_NODE = "tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell"`
   beside `_INVESTIGATION_NODE`; in the pending-cells test add
   `operator = [c for c in contract.cells if c.operation in _OPERATOR_TOOLS]`,
   `assert len(operator) == 124 and {c.node_id for c in operator} == {_OPERATOR_NODE}`, and
   `*_OPERATOR_TOOLS` in `bound`. Run
   `uv run python -m pytest tests/scripts/test_coverage_contract.py -q -k pending`; expect the
   new assertion to fail.
2. Create the carrier as the table above specifies, with a module docstring naming #2812,
   ADR-0722 and the runbook, and `pytestmark = pytest.mark.live_stack`.
3. Generate the bindings and append them, sorted, at the end of `[implementations]`:

   ```bash
   uv run python - <<'EOF'
   from tests.integration.live_stack.tool_cells import tool_cells
   from tests.integration.test_operator_tool_cells_live import TOOLS
   node = "tests/integration/test_operator_tool_cells_live.py::test_operator_tool_cell"
   for s in sorted({c.scenario_id for c in tool_cells(TOOLS)}):
       print(f'"{s}" = "{node}"')
   EOF
   ```

   Expect 31 lines.
4. `uv run python -m pytest tests/scripts/test_coverage_contract.py -q`; expect all pass.
5. `uv run python -m pytest tests/integration/test_operator_tool_cells_live.py -q`; expect 124
   skipped (no stack), no collection error.
6. `just lint`, `just type`, `just test-changed`; commit `test(live): prove the read-only
   operator tool cells over HTTP`.

## Task 3 — runbook and live proof

Files: modify `docs/operating/runbooks/live-testing.md`.

Verification: `Mode: task-test-not-applicable` — prose; `just docs-check` covers links and paths.

Steps:

1. Add `#### Read-only operator tool cells (#2812)` after the #3096 section: the carrier node, the
   124 cells, that it runs on the same lanes, bindings and assembly, the catalog carrier's
   environment (sourced `env.sh`, `KDIVE_DATABASE_URL`, `KDIVE_SYSTEMS_TOML`), the one
   `KDIVE_PROJECT` allocation it takes and releases, the history rows it leaves, that
   `KDIVE_INVENTORY_WRITEBACK` must stay unset, and a `Last run` paragraph filled from step 3.
2. `just docs-check`; expect exit 0. Commit `docs(runbook): run the read-only operator tool cells`.
3. On the lab guest at the committed head: regenerate `inputs.json` with the `tool_cells
   bindings` command; bring up the `default` lane, run the carrier with
   `KDIVE_DATABASE_URL="$KDIVE_MIGRATION_DATABASE_URL"`; bring up the
   `KDIVE_WORKER_DEATH_VERIFIER=docker` lane, run it again; `evidence assemble`, then `qualify`.
   Expect 62 cells recorded per lane and the 124 rows of these tools qualified; a failing or
   blocked cell is recorded as observed, never retried into a pass.
4. Wipe the lab stack.

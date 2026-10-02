# Core MCP tool cells: carrier harness and six tools (#2811)

Decision record: [ADR-0722](../../adr/0722-tool-cell-exposure-configuration-and-rejection-evidence.md).
Builds on [ADR-0715](../../adr/0715-live-evidence-identity-and-staged-image-binding.md) and the
ADR-0686 coverage contract.

## Problem

Owner group 2811 in `scripts/coverage_campaign/obligations.toml` holds 52 service tools, 227
scenarios and 908 cells, and none of them is bound to a test. The live-stack evidence seam
(`tests/integration/live_stack/evidence.py`, `scenario.py`) records native cells only: it has no
notion of a server configuration, an exposure, or a rejection boundary. The operator split the
issue on 2026-10-02: this change is the carrier, and the other 46 tools move to #3095-#3098.

## Scope

In scope:

1. Move 46 tools out of owner group 2811 into four new `service` groups:
   - #3095: `images.{delete,describe,kernel_config,list,upload}`, `shapes.{delete,list,set}`,
     `resources.{availability,describe,list}`;
   - #3096: the eight `investigations.*` tools and the five `artifacts.*` tools;
   - #3097: `runs.{bind,complete_build,create,get,list,set}`, `systems.{get,list}`,
     `jobs.{cancel,list,wait}`, with the `jobs.cancel` and `jobs.wait` role overrides;
   - #3098: the five `allocations.*` tools, the five `accounting.*` tools and
     `reports.generate`, with the `allocations.release` and `allocations.wait` role overrides.
   Group 2811 keeps `session.whoami`, `projects.list`, `tools.search`, `tools.invoke`,
   `fixtures.validate` and `systems.profile_examples`. No `contract.py` change is needed: an
   owner is a group attribute.
2. A new harness module, `tests/integration/live_stack/tool_cells.py`, implementing ADR-0722:
   configuration proof, the `direct` and `gateway` callers, the rejection harness, the
   per-project protected-state snapshot, the tool-cell frame around `run_cell`, and a
   `bindings` command that writes the expected `Context` of every bound service tool cell.
   `run_cell` (`scenario.py`) gains a `proves` outcome so a completed rejection cell records
   `rejection`, which `qualify` requires of a rejection cell; existing callers keep `success`.
3. A live carrier, `tests/integration/test_core_tool_cells_live.py::test_core_tool_cell`,
   parametrized over every cell of the six tools, and 14 `[implementations]` entries binding
   their scenarios to it.
4. The live-testing runbook section and a live run of both configurations.

Out of scope (operator-approved): the other 46 tools' cells (#3095-#3098), CLI translation
(#3099), production-utilization reporting (#2720, #2725), provider-dependent tool cells (#2812,
#3062, #3080). `examples/local-libvirt/demo-up.sh` needs no change: the server inherits
`KDIVE_WORKER_DEATH_VERIFIER` from the caller's environment (`scripts/live-stack/lib.sh`,
`restart_host_processes`), so the recovery lane is `KDIVE_WORKER_DEATH_VERIFIER=local
examples/local-libvirt/demo-up.sh`.

### Cells

The contract assigns the six tools 14 scenarios, each with four cells
(`default`/`recovery` × `direct`/`gateway`): 56 cells. Every tool has `functional` and
`authentication`; `tools.search` and `tools.invoke` also have `validation`. None has
`authorization` or `project-isolation`, because all six are public tools with no scopes. The
harness still implements those two boundaries for #3095-#3098; this change proves them with unit
tests only.

### Functional effects

Each functional cell mints a token with distinctive claims for a fresh project `cov-<8 hex>`
(two projects, a role on one, a `platform_auditor` platform role) and compares the result with a
source the server did not produce. `tools.search` is the exception: its schemas are compared with
the same server's operator-direct catalog (cross-surface consistency), anchored by the
`tools.invoke` signature the test knows (`name` required, `arguments` optional):

| Tool | Effect compared |
|---|---|
| `session.whoami` | `principal`, `client_id`, `projects`, `roles`, `platform_roles` against the token's own decoded claims |
| `projects.list` | the `{project, role}` items, `principal` and `platform_roles` against the decoded claims |
| `tools.search` | names mode returns `session.whoami` and `tools.invoke` with an `input_schema` equal to the operator-direct catalog's `inputSchema`, and `tools.invoke`'s has properties `{name, arguments}` and requires `name`; query `"granted projects roles"` returns `projects.list` among its matches |
| `tools.invoke` | the inner `projects.list` envelope (object id `projects`) matches the decoded claims |
| `fixtures.validate` | `path` and `profiles` equal the test-side `load_fixture_catalog(fixture_catalog_path_from_env())` |
| `systems.profile_examples` | every example parses as `ProvisioningProfile`; the local example passes `validate_profile_for_provider`; its catalog rootfs name is the first public local-libvirt image in the test-side `systems.toml` |

`direct` calls `tools.invoke` with `name="projects.list"`; `gateway` reaches the same through
`tools.invoke(name="tools.invoke", ...)`. The `cleanup` assertion of a functional cell compares
the project snapshot before and after the cell (the six tools create nothing durable).

### Rejection probes

- `authentication`: forged-signature token (ADR-0722 §3), HTTP 401.
- `validation`: `tools.search` with `limit=0`; `tools.invoke` with `arguments={}` and no `name`.

## Success

1. `python -m scripts.coverage_campaign check` passes; group 2811 owns exactly the six tools and
   56 cells; #3095, #3096, #3097 and #3098 own 11, 13, 11 and 11 tools; the four role overrides
   are preserved on the same tools.
2. Every one of the 14 scenarios is bound to `test_core_tool_cell`, and no other tool scenario is
   bound by this change.
3. A run whose unclipped operator catalog proves `default` writes records for exactly the 28 `default` cells and
   skips the 28 `recovery` cells; the reverse for `recovery`; a partial catalog fails the run
   without writing records.
4. Each functional record carries `effect` and `cleanup`; each rejection record carries its
   boundary, `unchanged-state` and `cleanup`; each record carries the configuration proof as an
   extra artifact.
5. `qualify` over the assembled results of both live lanes, with bindings from the `bindings`
   command, reports every one of the 56 cells qualified, at a stack whose server, worker and
   reconciler report the candidate.

## Failure model

1. **Actors and deployments**
   - a developer or CI operator running the `live_stack` tier on the demo-up lane, x86_64;
   - the ordinary `just test` suite, which runs the harness unit tests only.
2. **Invariants and assets at stake**
   - coverage evidence must never claim a configuration, exposure or effect that was not observed;
   - the harness must not leave durable rows behind on the shared lab stack.
3. **Accepted failure classes**
   - writes to rows outside the cell's project go unobserved by the snapshot: the six tools are
     token or file projections, and ADR-0722 lets later carriers narrow or widen the snapshot;
   - the configuration proof reads the catalog once per process; a server restarted mid-run in the
     other configuration is not detected (the runbook forbids restarts during a run, as for the
     image smoke);
   - `authorization` and `project-isolation` paths are proven by unit tests only here, because
     none of the six tools has those boundaries.
4. **Covered elsewhere**
   - deployed-revision mismatch and dirty checkout: ADR-0715 / `identity_problems`;
   - cells of the other 46 tools: #3095-#3098; CLI translation: #3099.

### Threat model

The change adds test code only. Boundaries it touches:

- **Server HTTP authentication** (existing, exercised not widened): a forged token is sent to the
  real server. Control: the server's JWT verifier. The forged key is generated per run in memory
  and never written.
- **Lab database** (existing): the snapshot reads with the DSN the carrier already uses
  (`KDIVE_DATABASE_URL`). Table names come from `pg_catalog` and are quoted with
  `psycopg.sql.Identifier`; the project value is a bound parameter. Read-only queries.
- **Evidence artifacts** (existing): observations hold tool names, digests, counts and synthetic
  claim values only, no tokens. Tokens are never written to an artifact.

Out of scope: hardening the server against forged tokens beyond observing the 401; secrets in the
lab DSN, which the operator supplies as for every live carrier.

## Validation

- Owner split: `tests/scripts/test_coverage_contract.py` asserts the per-owner tool sets, the
  2811 cell count and the preserved role overrides (red before the TOML edit).
- Harness: `tests/integration/live_stack/test_tool_cells.py` covers configuration
  classification, rejection classification per boundary and exposure, the rejection harness's
  unchanged-state failure, `forge` keeping the `kid`, the bindings contexts, and that
  `run_cell(..., proves=rejection)` writes outcome `rejection`, with fake callers.
- Bindings: the contract test asserts the 14 scenarios bind `test_core_tool_cell` and that
  `_validate_node` accepts it.
- Live: both lanes on a disposable lab host; `qualify` output recorded in the runbook.

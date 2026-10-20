# CLI translation cells for the core service tools (#3099) — design

Decision record: [ADR-0795](../../adr/0795-cli-translation-exposure.md), extending ADR-0722.

## Problem

The coverage contract has no CLI cells (`contract.py`, `_tool_cells`, exposures `direct` and
`gateway`), so CLI translation of the core service tools is not accounted for. The five core
carriers already prove each operation's effect and rejections over HTTP; the CLI layer must reuse
those scenarios rather than add a second experiment per operation.

## Scope

In: the contract's `cli` exposure and its validators (`contract.py`, `obligations.toml`); a
`CliCaller` in the shared harness `tests/integration/live_stack/tool_cells.py`; the caller
selection in each core carrier (`test_core_tool_cells_live.py`, `test_catalog_tool_cells_live.py`,
`test_investigation_tool_cells_live.py`, `live_stack/run_system_job_cells.py`,
`live_stack/allocation_accounting_cells.py`) plus a `direct` caller for owned-Run cleanup; unit
tests; the runbook's exposure sentence.
Out (owners): missing operation experiments → #3097/#3098; provider experiments → their scenario
owners; POWER execution → #2818; operator tools (no `cli` group). No production code changes.

Cell set: every `direct` cell of the 52 tools in the groups owned by 2811, 3095, 3096, 3097 and
3098 gains a `cli` twin owned by 3099 with the same scenario id, node, kind, configuration,
observation, assertions and roles. Per tool and configuration that is 1 functional cell plus its
rejection boundaries (2, 3 or 5 cells); totals 2811: 28, 3095: 76, 3096: 130, 3097: 110, 3098: 110
= 454. Bindings: the existing `bindings()` already writes a service context (host OS/arch,
accelerator `none`) for every bound service cell, so `cli` cells bind with no change.

Scenario reuse: a `cli` cell runs its carrier's existing scenario with a `CliCaller`, so every call
the scenario makes through the cell's caller (fixture, paging and read-back calls included) goes
through `kdivectl`, and the cell's outcome covers those verbs too. The one exception is
`run_system_job_cells._end_runs`, which cancels owned Runs: it gets a `direct` HTTP caller, so a
translation gap cannot leave owned Runs behind.

Translation (`cli_argv(tool, args)`): the tool's `GeneratedVerb` gives the command path. An
`unwrap_request` verb takes the fields of `args["request"]`. Each non-`None` field becomes a
positional when named in `positionals`, `--<param>-json <json>` when in `json_params`, or its
flag's form: a `store_true` flag alone for `True` and nothing for `False`; `bool_optional`
`--x`/`--no-x`; `append` repeated; otherwise, and for any non-boolean value of a boolean flag,
`--name <text>`, so the real parser judges the value. A field the verb has no option for raises
`CliTranslationGap`: the cell fails as untranslatable, whatever its boundary. `accounting.usage`
(`target` → `--project` or `--investigation-id`) and `accounting.report` (`projects`
comma-joined, `window` → `--since`/`--until`) have explicit inverses because their handlers
reshape the payload. A `confirm_destructive` verb gets `--yes`; every call ends with `--json`.

Exactness: except for those two verbs, when the real `kdivectl` parser accepts the command line,
`assemble_generated_payload` on the parsed namespace must equal `args` (minus `None` fields and
`False` `store_true` fields), or the call raises `CliTranslationGap` naming the keys. The 13 other
handler overrides read exactly their descriptor's fields, so the same check covers them.

Result: a printed JSON envelope → `ToolResponse` (a `{"result": [...]}` body → a list), after
asserting the exit code is ADR-0089's code for its `error_category` (0 when none). Exit 2 with no
envelope → `CliUsageError` carrying the option names argparse cites. Exit 3 with the generic
seam's "is not positively classified" refusal → `CliTierRefusal`. Exit 1 with an `error: ` line →
`LiveStackToolError` carrying it. Anything else fails with the first stdout line. Messages carry
verb paths and option names, never argument values.

Rejections: ADR-0795 decision 3. `validation` accepts `CliUsageError` (a declared option's value
refused, a required option missing, or a token no option takes, as for a non-boolean value of
a boolean flag; the evidence records that class and the refused option names), a `configuration_error` envelope, or a tool error naming validation. `authorization` and
`project-isolation` read a `CliTierRefusal` as `authorization_denied`: the server hid the tool
from the caller's catalog, and no call was made. An ADR-0756 filtered list still needs the empty
page, so a tier refusal there fails the cell. `authentication` runs the same command line with
`forge(token)`, then the issued token; a forged run that exits 2 is a translation gap. Evidence
records exposure `cli`.

Predicted translation gaps (static, from the parser and the carriers' arguments): `kdivectl`
refuses scalar values for `--extend-json`, `--window-json` and `--limit-kcu-json`
(`allocations.renew`, `allocations.request`, `accounting.set_budget`); `jobs list` has no filter
options; `systems list` has only `--state`; `allocations list` has only `--project`, while the
carriers page with `limit`, `cursor` and `state`; `images list` has only `--scope` and
`resources list` only `--kind`, while the catalog carrier sends `limit` (validation) and `cursor`
(paging). Cells whose calls reach these verbs are expected
to fail as translation gaps; the live report compares against this list and the campaign root
routes the product defects. This changes no other owner's cells.

## Failure model

1. Actors and deployments: a campaign operator running the `live_stack` suite on an exclusive
   x86_64 demo-up lane; CI unit tests with no stack.
2. Invariants and assets at stake: the contract digest and cell set read by qualification; the
   shared harness the other carriers use (`direct`/`gateway` behaviour must not change); owned
   live state, which each scenario's existing cleanup proof covers.
3. Accepted failure classes: a dropped field in the two reshaping accounting verbs surfacing only
   through the scenario's effect check (rejection cells have none), because their inverses are
   hand-written; `None` treated as absent.
4. Covered elsewhere: operator/provider CLI coverage (a later decision, ADR-0795); POWER (#2818);
   product CLI defects a cell exposes (recorded failing, routed by the campaign root).

## Validation

- Contract: unit tests in `tests/scripts/test_coverage_contract.py` (twin equality, counts,
  owners, validators, the ADR-0756 pair test).
- Caller: unit tests in `tests/integration/live_stack/test_tool_cells.py` with a fake runner for
  translation, gaps, exactness, exit table, tier refusal and result shapes, plus one real
  `python -m kdive.cli` subprocess against an unreachable URL.
- Carriers: `just type` and `just lint` over the six call sites.
- Live: the `cli` cells of the five carriers on a granted lane, both configurations.

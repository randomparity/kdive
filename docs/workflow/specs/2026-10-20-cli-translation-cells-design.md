# CLI translation cells for the core service tools (#3099) — design

Decision record: [ADR-0795](../../adr/0795-cli-translation-exposure.md), extending ADR-0722.

## Problem

The coverage contract has no CLI cells (`contract.py`, `_tool_cells`, exposures `direct` and
`gateway`), so CLI translation of the core service tools is not accounted for. The five core
carriers already prove each operation's effect and rejections over HTTP; the CLI layer must reuse
those scenarios rather than add a second experiment per operation.

## Scope

In: the contract's `cli` exposure and its validators (`contract.py`, `obligations.toml`); a
`CliCaller` in the shared harness `tests/integration/live_stack/tool_cells.py`; a one-line caller
selection in each core carrier (`test_core_tool_cells_live.py`, `test_catalog_tool_cells_live.py`,
`test_investigation_tool_cells_live.py`, `live_stack/run_system_job_cells.py`,
`live_stack/allocation_accounting_cells.py`); unit tests; the runbook's exposure sentence.
Out (owners): missing operation experiments → #3097/#3098; provider experiments → their scenario
owners; POWER execution → #2818; operator tools (no `cli` group). No production code changes.

Cell set: every `direct` cell of the 52 tools in the groups owned by 2811, 3095, 3096, 3097 and
3098 gains a `cli` twin owned by 3099 with the same scenario id, node, kind, configuration,
observation, assertions and roles. Per tool and configuration that is 1 functional cell plus its
rejection boundaries (2, 3 or 5 cells); totals 2811: 28, 3095: 76, 3096: 130, 3097: 110, 3098: 110
= 454. Bindings: the existing `bindings()` already writes a service context (host OS/arch,
accelerator `none`) for every bound service cell, so `cli` cells bind with no change.

Translation (`cli_argv(tool, args)`): the tool's `GeneratedVerb` gives the command path. An
`unwrap_request` verb takes the fields of `args["request"]`. Each field becomes, in order: a
positional when named in `positionals`; `--<param>-json <json>` when in `json_params`; the
flag's own form (`store_true` flag alone when `True`, `bool_optional` `--x`/`--no-x`, `append`
repeated, else `--name <text>`); and an unknown field `--<field-with-dashes> <text>`, which
argparse refuses. `None` fields are omitted. `accounting.usage` (`target` → `--project` or
`--investigation-id`) and `accounting.report` (`projects` comma-joined, `window` → `--since`
/`--until`) have explicit inverses because their handlers reshape the payload. Destructive verbs
with `confirm_destructive` get `--yes`; every call ends with `--json`.

Exactness: for a verb without a handler override, when the real `kdivectl` parser accepts the
command line, `assemble_generated_payload` on the parsed namespace must equal `args` (minus `None`
fields and `False` `store_true` fields); otherwise the call fails naming the differing keys. A
handler override is proven by the scenario's own effect observation instead.

Result: printed JSON envelope → `ToolResponse` (a `{"result": [...]}` body → a list), after
asserting the exit code is ADR-0089's code for its `error_category` (0 when none). No envelope and
exit 2 → `CliUsageError` (a `LiveStackToolError`). Exit 1 with an `error: ` line → a
`LiveStackToolError` carrying it. Anything else fails the call. Messages name only the verb path,
never argument values.

Rejections: ADR-0795 decision 3. `rejected_by_validation("cli", …)` accepts `CliUsageError`, a
`configuration_error` envelope, or a tool error naming validation. Authentication runs the same
command line with `forge(token)` then the issued token. The evidence `exposure` field records
`cli`.

## Failure model

1. Actors and deployments: a campaign operator running the `live_stack` suite on an exclusive
   x86_64 demo-up lane; CI unit tests with no stack.
2. Invariants and assets at stake: the contract digest and cell set read by qualification; the
   shared harness the other carriers use (`direct`/`gateway` behaviour must not change); owned
   live state, which each scenario's existing cleanup proof covers.
3. Accepted failure classes: a `cli` validation cell passing on an argparse refusal of a field
   the verb does not know — that refusal is the CLI's real behaviour for that command line; an
   override verb's dropped field surfacing only through the scenario's effect check, because no
   pure forward function exists for it; `None` treated as absent.
4. Covered elsewhere: operator/provider CLI coverage (a later decision, ADR-0795); POWER (#2818);
   product CLI defects a cell exposes (recorded failing, routed by the campaign root).

## Validation

- Contract: unit tests in `tests/scripts/test_coverage_contract.py` (twin equality, counts,
  owners, validators).
- Caller: unit tests in `tests/integration/live_stack/test_tool_cells.py` with a fake runner for
  translation, exactness, exit table and result shapes, plus one real `python -m kdive.cli`
  subprocess against an unreachable URL.
- Carriers: `just type` and `just lint` over the five call sites.
- Live: the `cli` cells of the five carriers on a granted lane, both configurations.

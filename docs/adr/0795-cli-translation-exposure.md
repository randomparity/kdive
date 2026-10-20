# 0795 — A `cli` exposure accounts for kdivectl translation of the core service tools

## Status

Accepted (2026-10-20)

## Context

ADR-0722 gives every registered tool two exposures, `direct` and `gateway`, and binds one pytest
node per scenario across them. #2811 also asks for CLI translation checks that reuse the shared
operation contracts. Nothing in the contract accounts for them:
`scripts/coverage_campaign/contract.py` generates only `direct` and `gateway` cells, and
`tests/integration/test_kdivectl_generated_lifecycle.py` drives four verbs without evidence
records. The core service tools are the five groups owned by #2811, #3095, #3096, #3097 and #3098
in `obligations.toml`: 52 tools, all with a generated `kdivectl` verb, 15 of them through a
handler override (`HANDLER_OVERRIDES`).

## Decision

1. **Exposure.** A mapping group may set `cli = true`. Its tools gain a third exposure, `cli`,
   beside `direct` and `gateway`, with every kind and configuration the `direct` exposure has. A
   `cli` cell keeps the `direct` twin's scenario id, so it binds to the same pytest node and runs
   the same scenario and observation. #3099 owns every `cli` cell. The five core service groups
   set it; no provider group may (the validator refuses it, and refuses a `cli` tool with no
   generated verb).
2. **Caller.** A `cli` cell's caller translates each tool call of the cell's scenario into the
   tool's generated `kdivectl` command line and runs `python -m kdive.cli … --json` as a
   subprocess with a `kdivectl` token (the `direct` profile). It returns the envelope the command
   printed. It fails the cell when the command line cannot carry the call's arguments (a field
   with no option, or a parsed payload that differs from the call), or when the exit code is not
   ADR-0089's code for the envelope's error category. Owned-state cleanup may use a `direct`
   caller instead, so a translation gap cannot strand owned state.
3. **Rejections.** `authentication`: a forged token must exit 1 with the fixed
   `error: authentication failed` diagnostic, and the issued token must not. `validation`:
   exit 2 with no envelope (a declared option's value refused before any call), a
   `configuration_error` envelope, or a tool error naming a validation error, as on `direct`.
   `authorization` and `project-isolation` keep ADR-0722's envelope rules, read from the printed
   envelope; the generic seam's exit-3 refusal of a tool the caller's catalog hides counts as
   `authorization_denied`, because the server hid it and no call was made. ADR-0756 filtered lists
   still need the empty page.

The matrix digest moves because the cell set does; the contract version stays 1.

## Consequences

- The contract grows by 454 required cells (5,571 to 6,025). A core carrier run executes half
  again as many cells (908 to 1,362); each VM-backed core scenario provisions once more per
  configuration.
- A verb that cannot express an operation's arguments, or that maps a category to the wrong exit,
  fails its `cli` cell instead of being skipped. Six verbs are predicted to (see the spec).
- Operator and provider tools stay without `cli` cells; extending them needs a later decision.

## Considered & rejected

- **Standalone CLI tests outside the contract.** judgment: they are not accounted for in the
  result matrix, which is what #3099 exists to fix.
- **Functional, authentication and validation cells only (302 cells).** judgment: leaves the
  exit-3 denial translation, including the generic seam's catalog refusal, unproven on any live
  verb. Offered to the operator as the cheaper option.
- **`cli` cells in the `default` configuration only (227 cells).** judgment: the census registers
  each core tool in both configurations, and ADR-0722 proves every exposure in both.
- **A separate `cli` scenario id and node.** judgment: a second experiment per operation is the
  duplication #2811 rules out.
- **`kdivectl tool call` with JSON arguments.** judgment: it bypasses the generated verbs, their
  options and handler overrides, which are the translation #2811 asks to prove.

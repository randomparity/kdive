# ADR 0423 — Generic dispatch handler for generated `kdivectl` verbs (#1450)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** kdive maintainers
- **Issue:** [#1450](https://github.com/randomparity/kdive/issues/1450) (epic #1442).
- **Implements:** [ADR-0421](0421-schema-generated-kdivectl-verbs.md) — the generated-verb
  surface ADR-0421 decided is wired into the parser by #1448 and given its `--<param>-json`
  escapes by #1449; this ADR lands the runtime that ADR-0421 decisions 4 and 6 describe (tier
  ceremony, envelope rendering) for those verbs.
- **Builds on (does not supersede):** [ADR-0089](0089-operator-cli-mcp-client.md) (the CLI is a
  pure MCP client), [ADR-0107](0107-cli-mutating-tool-call-opt-in.md) (its decision 4 — tier from
  the *live* annotations, not a committed artifact — governs here).

## Context

`#1448` merged the generated-verb surface into the parser and left one deliberate seam,
`dispatch.invoke_generated_verb`, as a stub: it invoked every generated verb with an empty
`{}` payload and forwarded only the passthrough's top-level tier opt-in flags — flags a
generated verb does not even carry. So a generated verb could reach its tool but never with
arguments, and a mutating or destructive generated verb failed closed (the passthrough refuses
a non-read-only tool at the read-only default).

Two half-built dispatch paths already exist (issue #1450 evidence): the curated mutation verbs
(`commands/mutations.py`) do preflight → call → render → exit-code with *typed arguments*, and
the `tool call` passthrough (`dispatch.py:_tool_call`) does tier-resolution → preflight →
confirm → call → exit-code with a *raw JSON string*. A generated verb needs the union: the
passthrough's live-annotation tier resolution and confirmation, driven by the curated path's
typed argparse arguments, rendered through the shared `render_envelope` (ADR-0421 §6).

## Decision

We will implement `invoke_generated_verb` as one generic handler covering every generated verb:

1. **Payload from the namespace.** Strip the `registry.GENERATED_ARG_PREFIX` (`genarg_`) dest
   prefix off each scalar/append flag value, fold in each `--<param>-json` value (already
   validated to a JSON container at parse time, #1449), and — for an `unwrap_request` verb —
   re-wrap the whole body under a single `request` key (no key when nothing was given), exactly
   as the curated read verbs do by hand. An unset `store_true` flag and any absent flag are
   omitted so the server default holds.
2. **Tier from the live annotations.** Classify the verb's tool from the *live* `list_tools()`
   annotations (`passthrough.classify_tool`), never the committed artifact, so a stale artifact
   built against a different server build cannot downgrade a tool's tier (ADR-0107 decision 4).
   An unclassifiable (`UNKNOWN`) tool is fail-closed and unreachable.
3. **Ceremony from that live tier.** A generated *mutating* verb needs no opt-in flag — naming
   the verb is the acknowledgement (ADR-0421 decision 4). A mutating or destructive verb runs
   the fail-closed token-`exp` preflight (`ensure_token_valid`). A destructive verb additionally
   requires the typed-`yes` confirmation, dischargeable non-interactively by `--yes` — added to
   the parser only for verbs the committed artifact marks destructive.
4. **Render and exit.** Render the response envelope through `render_envelope` (a table by
   default, the whole envelope on `--json`, ADR-0421 §6) and derive the exit code from the
   envelope (`exit_code_for_envelope`, the 0–6 contract).

## Consequences

- Every generated verb is now fully usable: typed arguments reach the tool, mutating verbs work
  without a ceremony flag, destructive verbs confirm, and exit codes match the envelope. This
  unblocks #1453 (live proof).
- The `--yes` flag's *presence* on a verb is decided by the committed `destructive` bit (a parse
  time fact) while the *ceremony* is decided by the live tier (a call-time fact). The two can
  disagree only for a stale artifact, and both mismatch directions are safe: a live downgrade
  makes an existing `--yes` a harmless no-op; a live upgrade to destructive with no `--yes` flag
  still refuses on a non-TTY (fail-closed) rather than dispatching unconfirmed.
- `invoke_generated_verb` no longer routes through `_tool_call`; the two paths stay separate
  because the passthrough gates on `--allow-*` opt-in flags a generated verb does not carry.
- No schema change, no migration; CLI-only.

## Alternatives considered

- **Keep routing generated verbs through `_tool_call`.** Rejected: `_tool_call` gates on the
  `--allow-mutating`/`--allow-destructive` opt-ins, which generated verbs deliberately do not
  carry (naming the verb is the acknowledgement). Reusing it would force every generated
  mutating verb to demand a redundant flag, contradicting ADR-0421 decision 4.
- **Resolve the tier from the committed `GeneratedVerb.destructive`/`read_only` fields.**
  Rejected: a committed artifact built against a different server build could downgrade a tool's
  tier and silently skip confirmation. ADR-0107 decision 4 makes the live annotations
  authoritative; the committed bit is used only for the parser's `--yes` shape, where a
  mismatch stays fail-closed.
- **Send unset `store_true` booleans as explicit `False`.** Rejected: argparse cannot
  distinguish "unset" from an explicit `False` for a `store_true` flag, so sending `False`
  would override a server-side default of `True`. Omitting the key lets the server default hold.

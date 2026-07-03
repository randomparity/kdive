# ADR-0307: classify a resolver symbol miss as `symbol_not_found`, not `debug_attach_failure` (#1013)

- Status: Accepted
- Date: 2026-07-03
- Builds on [ADR-0248](0248-gdbstub-symbol-resolution.md) (`resolve_symbol` via
  `-data-evaluate-expression &<name>`), [ADR-0001](0001-greenfield-rewrite.md) (the closed
  `ErrorCategory` taxonomy and `CategorizedError`), [ADR-0019](0019-tool-response-envelope.md)
  (the `ToolResponse` envelope), and [ADR-0118](0118-wait-on-resource-mechanisms.md) (retryability as
  a pure function of the category). Tail of the black-box diagnosability epic #1018 (BLACK_BOX_REVIEW.md
  Finding 4); sibling of #1008–1012.

## Context

`debug.resolve_symbol` on a symbol gdb cannot resolve to an address — a `static` function inlined /
optimized away into its caller, or an addressless enum/macro constant — returns
`error_category:"debug_attach_failure"`, `retryable:true`. Both fields are wrong:

- The **attach is fine.** Resolving another symbol on the *same* live session succeeds immediately
  afterward. The debug transport is not the failing thing; the requested name simply has no address.
- **Retrying is pointless.** The symbol will not appear on a bare re-invocation, yet an agent
  trusting `retryable:true` retries the doomed call.

Root cause (verified against the issue):
- `resolve_symbol` runs `-data-evaluate-expression &<name>` (`gdbmi.py`), which for an unknown /
  optimized-away symbol makes gdb answer `^error,"No symbol \"<name>\" in current context."`, and for
  an addressless enumerator `^error,"...address of value not located in memory."`.
- `execute_mi_command` maps **every** MI `^error` generically to `DEBUG_ATTACH_FAILURE` — it is the
  shared write path for every op, so it cannot know a resolver miss from a real transport fault.
- `DEBUG_ATTACH_FAILURE` is hardwired `retryable:true` in `_RETRYABLE_BY_CATEGORY`.
- No `symbol_not_found` category exists in the closed `ErrorCategory` enum.

## Decision

Add a new closed-taxonomy category `SYMBOL_NOT_FOUND = "symbol_not_found"`, classified
`retryable:false`, and narrow the resolver-specific `^error` to it — **only** inside
`resolve_symbol`, never in the shared `execute_mi_command`.

- **Reclassify in `resolve_symbol`, not the shared path.** A new private `_evaluate_symbol` runs the
  `&<name>` evaluation and catches the `DEBUG_ATTACH_FAILURE` `execute_mi_command` raises. If the
  (already-redacted) gdb `msg` matches `_SYMBOL_NOT_FOUND_RE` — the two resolver-miss phrasings —
  it re-raises `SYMBOL_NOT_FOUND`; any other gdb error passes through unchanged. This mirrors the
  existing `_stack_command` reclassification pattern (a running-target error → `inferior_running`),
  which already narrows a generic attach failure to a precise per-op code from the same
  `details["payload"]["msg"]`.

- **Anchor the regex to the two per-symbol phrasings, precisely.** `No symbol ... in current context`
  and `...address of value not located in memory`. Anchoring to `in current context` (not a bare
  `No symbol`) is deliberate: gdb's `No symbol table is loaded` means the **debuginfo never loaded** —
  a genuine attach-level fault that must stay `DEBUG_ATTACH_FAILURE`, not be masked as a per-symbol
  miss.

- **`retryable:false`.** Added to `_RETRYABLE_BY_CATEGORY`; the retryable table's exhaustiveness test
  over `ErrorCategory` forces a deliberate classification for the new category.

- **Actionable hint.** The `CategorizedError.details` carry `code:"symbol_not_found"`, the redacted
  `name`, and a `hint`: *"symbol may be inlined or optimized away; try disassembling its caller."*
  These surface in the failure envelope's `data` via the existing `safe_error_details` projection —
  no new plumbing.

- **`bad_symbol_value` is untouched.** The defensive path for a `^done` reply whose value has no
  parseable hex address stays `DEBUG_ATTACH_FAILURE`: that is a malformed/unexpected gdb reply, not a
  clean resolver miss. Only the two gdb `^error` phrasings above are narrowed.

## Consequences

- Resolving an inlined / optimized-away symbol returns `error_category:"symbol_not_found"`,
  `retryable:false`, with `data.hint`. An agent stops retrying and pivots to disassembling the caller.
- Resolving a present symbol still succeeds; a genuine attach fault (including `No symbol table is
  loaded`) still returns `debug_attach_failure`, `retryable:true`.
- `SYMBOL_NOT_FOUND` is a new stable wire string in the closed taxonomy; `docs/guide/errors.md` gains
  its row.
- The addressless-enumerator miss (`&<enum constant>`) now also classifies as `symbol_not_found`,
  matching its true meaning ("no address for this name").

## Rejected alternatives

- **Classify in `execute_mi_command`.** It is shared by every op; a `No symbol` string there would
  mis-narrow set-breakpoint / evaluate failures that are legitimately attach-level, and couples the
  generic write path to resolver semantics. The per-op reclassification is the established pattern.
- **Reuse `not_found`.** `not_found` is the object-lookup miss (a by-id row that does not exist,
  ADR-0097) and carries seam-suppressed detail (`"not found"`), which would hide the actionable hint.
  A symbol miss is a distinct, diagnostic (non-suppressed) condition.
- **Reuse `configuration_error`.** The name *is* a valid bare C identifier (already gated); the input
  is well-formed, so it is not a caller-input error — it is a resolution outcome.
- **Also narrow `bad_symbol_value`.** A `^done` with an unparseable value is an unexpected gdb reply,
  not a clean miss; leaving it attach-level preserves the "something is wrong with the transport"
  signal for a genuinely anomalous response.

# Spec: `artifacts.get` token-safe window ceiling (#835)

ADR: [ADR-0257](../../adr/0257-artifacts-get-token-safe-window-ceiling.md)

## Problem

ADR-0247 added `byte_offset`/`max_bytes` windowing to `artifacts.get` and fixed the
*default* call: with no `max_bytes`, the handler returns a 16 KiB window
(`ARTIFACT_GET_WINDOW_DEFAULT_BYTES`), safely under a typical MCP tool-result token
budget. The residual, filed as #835, is the *explicit* path:

```
effective_max = min(max(max_bytes, 1), inline_cap)   # reads.py:281
inline_cap    = KDIVE_ARTIFACT_INLINE_MAX_BYTES      # default 64 KiB
```

A caller passing `max_bytes: 65536` (or any value ≥ 64 KiB) clamps only to the
64 KiB inline cap. 64 KiB of redacted log text is ≈16k–22k tokens after
JSON-escaping and UTF-8 `errors="replace"` expansion, which can cross the MCP
client's tool-result token ceiling (~25k tokens) and error with "result exceeds
maximum allowed tokens" — the very fallback-to-file symptom ADR-0247 set out to
remove, reachable again whenever the caller names an explicit large `max_bytes`.

Root cause: the server bounds the window in **bytes** while the client bounds the
result in **tokens** — a units mismatch across a trust boundary. The byte cap that
governs the window (`KDIVE_ARTIFACT_INLINE_MAX_BYTES`) is an *operator-tunable
payload preference*, not a *token-safety invariant*; nothing guarantees a single
window stays under the client ceiling regardless of what the caller or operator
asks for.

## Goal

Guarantee that any single `artifacts.get` window — default or explicit `max_bytes`,
at any configured `KDIVE_ARTIFACT_INLINE_MAX_BYTES` — stays comfortably under the
MCP client's tool-result token ceiling, while `next_offset` paging still reaches
the whole object and `refs.download_uri` still serves it in full. No new tool, no
weakening of any authorization/redaction gate, no behavior change for callers
already within the safe window (the 16 KiB default path is unaffected).

## Decision summary (see ADR-0257)

Add one hard module constant in `reads.py`:

```
ARTIFACT_GET_WINDOW_MAX_BYTES = 24 * 1024   # token-safe per-window ceiling
```

and add it as a third term to the window clamp:

```
effective_max = min(max(max_bytes, 1), inline_cap, ARTIFACT_GET_WINDOW_MAX_BYTES)
```

The ceiling is a *non-configurable* upper bound: unlike `inline_cap`, an operator
cannot raise it, so the token-safety property holds regardless of
`KDIVE_ARTIFACT_INLINE_MAX_BYTES` or the caller's `max_bytes`. `inline_cap` remains
as the existing configurable knob that can only lower the window further
(`min` of the three); its lowered-cap behavior is unchanged.

### Why 24 KiB

The client ceiling is ~25k tokens. Worst-case expansion for arbitrary redacted log
bytes is ≈0.33 tokens/byte (ADR-0247's "64 KiB ≈ 16k–22k tokens", i.e. up to
≈22000/65536 ≈ 0.336). A 24 KiB window is therefore ≤ 24576 × 0.336 ≈ 8.3k tokens
worst case — about one third of the ceiling, leaving ample room for the rest of the
envelope (the presigned `refs.download_uri` is itself several hundred tokens) and
any other content already in the caller's tool-result. 24 KiB is also > the 16 KiB
default, so the default window is untouched.

## Behavior

- Default call (no `max_bytes`): 16 KiB window — unchanged (16 KiB < 24 KiB).
- Explicit `max_bytes ≤ 24 KiB`: honored exactly — unchanged.
- Explicit `max_bytes > 24 KiB` (incl. 64 KiB / 65536): clamped to 24 KiB. Because
  the object is larger than the window, `content_truncated="true"` and
  `next_offset` are set, so the caller sees the clamp and pages — not a silent
  truncation.
- Lowered `KDIVE_ARTIFACT_INLINE_MAX_BYTES` (e.g. 8 KiB): still wins via `min`
  (8 KiB < 24 KiB) — unchanged.
- Every authorization/redaction/degradation gate, the 1 MiB fetch ceiling, the
  `content_omitted` large-object branch, and `refs.download_uri` minting are all
  unchanged.

## Success criteria

1. `max_bytes=65536` on a >24 KiB redacted artifact returns a window of exactly
   24 KiB, `content_truncated="true"`, `next_offset="24576"`.
2. Paging from that `next_offset` continues to reach the rest of the object, and
   concatenating windows reproduces the source bytes.
3. The default (no `max_bytes`) call still returns a 16 KiB window.
4. An explicit `max_bytes` below the ceiling (e.g. 8000) is honored exactly.
5. Lowering `KDIVE_ARTIFACT_INLINE_MAX_BYTES` below 24 KiB still bounds the window
   to the lowered cap (the existing ADR-0247 criterion still holds).
6. The `max_bytes` parameter description and the `artifacts.get` docstring state
   the token-safe ceiling, and the generated tool reference
   (`docs/guide/reference/artifacts.md`) matches a fresh generation.

## Out of scope

- A configurable token-budget setting (rejected — see ADR-0257; same reasoning as
  ADR-0247's rejected configurable-default-window knob).
- A `console.tail` / `artifacts.grep` tool: `artifacts.search_text` + byte paging
  already cover targeted retrieval (issue notes this is optional).
- Changing `KDIVE_ARTIFACT_INLINE_MAX_BYTES`'s default or removing it.

# ADR-0257: `artifacts.get` token-safe window ceiling (#835)

- Status: Accepted
- Date: 2026-06-26

## Context

ADR-0247 added `byte_offset`/`max_bytes` windowing to `artifacts.get` and sized the
*default* window (`ARTIFACT_GET_WINDOW_DEFAULT_BYTES = 16 KiB`) to the MCP
tool-result token budget, removing the whole-object overflow on a ~64 KiB boot
console. The residual #835 reported is the *explicit* path: the per-call window
clamp is

```
effective_max = min(max(max_bytes, 1), inline_cap)
inline_cap    = KDIVE_ARTIFACT_INLINE_MAX_BYTES   # default 64 KiB
```

(`src/kdive/mcp/tools/catalog/artifacts/reads.py:281`). A caller passing
`max_bytes: 65536` clamps only to the 64 KiB inline cap. 64 KiB of redacted log
text is ≈16k–22k tokens after JSON-escaping and UTF-8 `errors="replace"`
expansion, which can exceed the MCP client's ~25k-token tool-result ceiling and
error with the client-side "result exceeds maximum allowed tokens" — forcing the
file/`jq` fallback ADR-0247 set out to remove.

The root cause is a units mismatch across a trust boundary: the server bounds the
window in **bytes**, the client bounds the result in **tokens**. The byte cap that
governs the window (`KDIVE_ARTIFACT_INLINE_MAX_BYTES`) is an operator-tunable
*payload preference*, not a *token-safety invariant* — nothing guarantees a single
window stays under the client ceiling regardless of what the caller's `max_bytes`
or the operator's config asks for.

The redaction/authorization invariants (CLAUDE.md "Secrets by reference + mandatory
redaction", the ADR-0140/0247 gate, the 1 MiB fetch ceiling, the `content_omitted`
large-object branch, and the always-minted `refs.download_uri`) stand and are
untouched by this change.

## Decision

Add one hard module constant in `reads.py` and add it as a third term to the
existing window clamp:

```python
# Token-safe per-window ceiling. The MCP client bounds a tool-result in tokens
# (~25k); the server bounds the window in bytes. 24 KiB of redacted log text is
# <= ~8.3k tokens worst case (ADR-0247: 64 KiB ~ 16k-22k tokens, i.e. <= ~0.336
# tokens/byte), about a third of the client ceiling -- room for the rest of the
# envelope. Unlike KDIVE_ARTIFACT_INLINE_MAX_BYTES this is not operator-tunable,
# so the token-safety bound holds regardless of caller max_bytes or config.
ARTIFACT_GET_WINDOW_MAX_BYTES = 24 * 1024

effective_max = min(max(max_bytes, 1), inline_cap, ARTIFACT_GET_WINDOW_MAX_BYTES)
```

`inline_cap` (`KDIVE_ARTIFACT_INLINE_MAX_BYTES`) stays as the configurable knob
that can only *lower* the window further (it is one term of the `min`); its
lowered-cap behavior (ADR-0247's clamp-to-lowered-cap criterion) is unchanged. The
new constant is the non-defeatable upper bound.

The over-ceiling clamp is not a silent-success trap, for the same reason ADR-0247's
over-`inline_cap` clamp is not: a window shorter than the object sets
`content_truncated="true"` and `next_offset`, so a caller that asked for more than
24 KiB sees the clamp in the response and pages the rest. `refs.download_uri`
remains authoritative for the whole object in one shot.

The `max_bytes` parameter description (`registrar.py`) and the `artifacts.get`
docstring/handler docstring are updated to state the 24 KiB token-safe ceiling
alongside the existing `KDIVE_ARTIFACT_INLINE_MAX_BYTES` mention; the generated
tool reference (`docs/guide/reference/artifacts.md`) is regenerated.

### Why 24 KiB

The client ceiling is ~25k tokens. The estimate rests on one assumption: the
REDACTED artifacts `artifacts.get` serves inline are line-oriented text (console,
redacted dmesg, build-log); SENSITIVE binaries (vmcore/vmlinux) are never inlined
(ADR-0140/0243). Text keeps JSON escaping near 1:1, so worst-case expansion is
≈0.336 tokens/byte (ADR-0247's "64 KiB ≈ 16k–22k tokens"). A 24 KiB window is
≤ 24576 × 0.336 ≈ 8.3k tokens — about one third of the ceiling, leaving ample
headroom for the rest of the envelope (the presigned `download_uri` alone is
several hundred tokens) and other content already in the caller's context. 24 KiB
is also greater than the 16 KiB default, so the default window path is untouched.
(A future REDACTED artifact kind carrying non-text bytes would JSON-escape at up to
6 chars/byte and require re-deriving this ceiling — none exists today.)

## Consequences

- An explicit `max_bytes` of 64 KiB (or any value above 24 KiB) now returns a
  24 KiB window with `next_offset`, instead of a 64 KiB window that could overflow
  the client token ceiling. The caller pages or follows `download_uri`.
- The default (no-`max_bytes`) call is unchanged: 16 KiB < 24 KiB.
- A caller that explicitly requests ≤ 24 KiB is unchanged.
- An operator who lowers `KDIVE_ARTIFACT_INLINE_MAX_BYTES` below 24 KiB is
  unchanged (the lowered cap still wins via `min`); one who raises it above 24 KiB
  no longer widens the per-call window past the token-safe ceiling. Its help text is
  corrected to say so (it previously described the pre-ADR-0247 omit-threshold
  semantics, stale since windowing landed), and the config reference is regenerated.
- The change is contained to `reads.py` (constant + one `min` term + docstring) and
  `registrar.py` (param description + docstring), plus the regenerated tool
  reference and tests. No schema, migration, RBAC, tool-surface, or config-setting
  change. The MCP `data` output is generic free-form (`runs.get`-style), so no
  committed output snapshot changes.

## Considered & rejected

- **A configurable token-budget setting** (`KDIVE_ARTIFACT_GET_WINDOW_MAX_BYTES`).
  Rejected for the same reasons ADR-0247 rejected a configurable default-window
  knob: a module constant avoids a speculative knob and a second
  config-reference regeneration, and the per-call `max_bytes` already lets a caller
  pick a smaller window. Crucially, the value's *purpose* is a safety invariant the
  operator should not be able to defeat — exposing it as a setting would reintroduce
  the "operator raises it and overflows the client" failure this ADR closes.
- **Lowering `KDIVE_ARTIFACT_INLINE_MAX_BYTES`'s default from 64 KiB to 24 KiB.**
  Achieves the default-path numbers but is operator-defeatable (raise it back) and
  conflates the operator's payload preference with the token-safety invariant. The
  issue explicitly requires the bound to hold *regardless of requested max_bytes*,
  which a tunable cap cannot promise.
- **Hard `ge`/`le` schema bound on `max_bytes`.** Same rejection as ADR-0247:
  `artifacts.get` is off the `BindingErrorMiddleware` allowlist, so a schema bound
  would leak a raw pydantic `ValidationError` instead of the uniform envelope, and
  adding a middleware entry is out of this change's file scope. Handler clamping
  keeps the change in `reads.py`/`registrar.py` and signals the clamp through
  `content_truncated`/`next_offset`.
- **A tail (last-N-KB) default window** (the issue's optional suggestion). Rejected
  as separable scope: it changes the default *position*, not the token-safety
  bound this issue is about, and would alter the established head-then-page contract
  ADR-0247 set. The token-safety ceiling is orthogonal and shippable on its own.
- **A dedicated `console.tail` / `artifacts.grep` tool.** Out of scope per the
  issue: `artifacts.search_text` (ADR-0225) + byte paging already cover targeted
  retrieval.

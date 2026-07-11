# Spec: `artifacts.get` byte windowing (#803, BB-P4 D3)

ADR: [ADR-0247](../../adr/0247-artifacts-get-byte-windowing.md)

## Problem

`artifacts.get` takes only `artifact_id`. On success it returns the whole redacted
object inline in `data.content` when the object is at or under
`KDIVE_ARTIFACT_INLINE_MAX_BYTES` (default 64 KiB), and omits content
(`content_omitted="artifact_too_large"`) above it
(`src/kdive/mcp/tools/catalog/artifacts/reads.py:237-277`).

The most common redacted artifact — a successful-boot console — lands right at
64 KiB. 64 KiB of text is ~16k–20k tokens, which overflows a typical MCP
tool-result token budget. The caller has no way to ask for a smaller slice: the
inline cap is a fixed server byte limit, not a per-call control. The black-box
review (`BLACK_BOX_REVIEW.md`, 2026-06-25, D3) confirmed the symptom: the
successful-boot console spilled to a file while the smaller crash console
returned fine.

Mitigations already present: `refs.download_uri` is always minted regardless of
size, and `data.size_bytes` is returned in both branches. `artifacts.search_text`
(ADR-0225) gives bounded grep but needs a pattern and cannot page sequentially.

## Goal

Let a caller fetch a bounded byte window of a redacted text artifact inline,
without exceeding the tool-result token budget, and page through the rest — while
keeping `refs.download_uri` available for the full object and preserving every
existing authorization/redaction gate.

## Decision summary (see ADR-0247)

Add two optional parameters to `artifacts.get`:

- `byte_offset: int = 0` — the start byte of the window.
- `max_bytes: int = 16384` — the maximum window length; default sized to the token
  budget (16 KiB ≈ 4k–5k tokens).

Both are declared as plain `int` (no hard `ge`/`le` schema bound) and normalized
in the handler, **not** rejected at arg-binding. `artifacts.get` is not on the
`BindingErrorMiddleware` allowlist, so a hard schema bound would leak a raw
pydantic `ValidationError` rather than the uniform envelope (the leak ADR-0225's
middleware prevents for `search_text`); editing that middleware is out of this
change's scope. The parameter descriptions state the effective bounds in prose for
discoverability.

The handler clamps `byte_offset` to `max(byte_offset, 0)` and computes
`effective_max = min(max(max_bytes, 1), KDIVE_ARTIFACT_INLINE_MAX_BYTES)`, then
fetches the object once (bounded by a 1 MiB fetch ceiling, matching
`search_text`'s `_MAX_SEARCHABLE_ARTIFACT_BYTES`) and slices
`data[byte_offset : byte_offset + effective_max]` **before** the UTF-8 decode,
decoding the slice with `errors="replace"`. The `min(..., inline_cap)` term means
an operator who lowers the configured inline cap is never overrun. Over-cap
clamping is not a silent-success trap: `content_truncated`/`next_offset` signal
the clamp so the caller can page.

Returned `data` on the windowed-content branch:

- `size_bytes` — the full object size (unchanged).
- `content` — the decoded window.
- `content_truncated` — `"true"` when bytes remain after the window
  (`byte_offset + len(window) < size_bytes`), else `"false"`.
- `next_offset` — `str(byte_offset + len(window))`, present **only** when
  `content_truncated` is `"true"`; the `byte_offset` to resume paging.

Objects larger than the 1 MiB fetch ceiling keep the existing
`content_omitted="artifact_too_large"` + `refs.download_uri` behavior (no
fetch). `byte_offset` at or past the object end yields an empty `content`,
`content_truncated="false"`, no `next_offset` (clean paging termination).

The store seam, the `head`-before-`get` redaction gate, the post-fetch
`fetched.sensitivity is REDACTED` recheck, the etag stale-handle check, and the
best-effort store-outage degradation (`content_unavailable`) are all unchanged.

## Acceptance criteria

1. With no new arguments, `artifacts.get` on a 64 KiB-plus console returns at most
   16 KiB inline (the default window), `content_truncated="true"`, and a
   `next_offset` that advances paging — the default read path no longer overflows.
2. A caller can pass `byte_offset`/`max_bytes` to fetch any window of a redacted
   text artifact up to the fetch ceiling, and page to completion: repeatedly
   calling with the returned `next_offset` yields each successive window and the
   final window has `content_truncated="false"` and no `next_offset`.
3. `byte_offset` at or past `size_bytes` returns empty `content`,
   `content_truncated="false"`, no `next_offset`, status `available`.
4. A `max_bytes` window whose start/end splits a multi-byte UTF-8 sequence decodes
   without error (replacement characters at the split), never raising.
5. Out-of-range inputs are normalized in the handler, never rejected at
   arg-binding (no raw `ValidationError` escapes): a negative `byte_offset` clamps
   to 0 (reads from the start); a `max_bytes <= 0` floors to a 1-byte window.
6. `max_bytes` is clamped to the configured `KDIVE_ARTIFACT_INLINE_MAX_BYTES`
   (`effective_max = min(max(max_bytes, 1), configured_cap)`): a request above the
   configured cap, or above the default when the operator lowered the cap, returns
   at most the cap with `content_truncated="true"` + `next_offset`
   (direct-handler test with a lowered cap).
7. An object above the 1 MiB fetch ceiling returns
   `content_omitted="artifact_too_large"` + `refs.download_uri` even when
   `byte_offset`/`max_bytes` are set — windowing is unavailable above the ceiling
   (the download URI is the path for larger objects).
8. `refs.download_uri` remains present for every in-ceiling and over-ceiling
   redacted object (existing behavior).
9. Every existing gate is preserved: sensitive/quarantined/cross-project ids stay
   not-found-shaped; a drifted `head`/`fetched` sensitivity is rejected before the
   bytes reach the response; viewer role is still required; a store outage still
   degrades to `content_unavailable` with the metadata envelope intact.
10. The `artifacts.get` schema advertises `byte_offset`/`max_bytes` (with the
    effective bounds stated in their descriptions) and the generated tool
    reference (`docs/guide/reference/artifacts.md`) is regenerated to match.

## Edge cases enumerated

- `byte_offset=0, max_bytes` ≥ object size, object ≤ ceiling → whole object inline,
  `content_truncated="false"`, no `next_offset`.
- Object exactly at the fetch ceiling (1 MiB) → windowed (fetched + sliced).
- Object one byte over the fetch ceiling → `content_omitted="artifact_too_large"`,
  `download_uri` present, never fetched.
- `byte_offset` mid-object, `max_bytes` reaching exactly the last byte →
  `content_truncated="false"`, no `next_offset`.
- `byte_offset` mid-object, `max_bytes` short of the end → `content_truncated="true"`,
  `next_offset = byte_offset + max_bytes`.
- Empty (zero-byte) object → empty `content`, `content_truncated="false"`.
- Multi-byte UTF-8 boundary split at either window edge → `errors="replace"`.
- A multi-byte sequence split *across* two paged windows is lossy: it decodes to a
  replacement char at the end of window N and the start of window N+1, so
  concatenating decoded windows is a best-effort text view, not byte-exact.
  `download_uri` is authoritative for exact bytes (consistent with ADR-0140's
  `errors="replace"` text view). Byte offsets/`next_offset` stay byte-exact, so
  paging never skips or repeats bytes.

## Out of scope

- `size_bytes` on `artifacts.list` items: would require a per-item object-store
  `head` (N round-trips) or a new DB column; size is already discoverable via
  `artifacts.get` (one call) and `artifacts.search_text`. Deferred (ADR-0247
  "Considered & rejected").
- Ranged store reads (`get_range`): the seam exists but discards the object's
  sensitivity metadata, so using it would drop the post-fetch redaction recheck.
  Preserving that gate within the owned file scope means a full bounded fetch +
  in-process slice. The 1 MiB ceiling bounds the cost; larger objects use
  `download_uri`. Deferred (ADR-0247 "Considered & rejected").
- Line-based windowing (`line_offset`/`line_limit`): byte windowing is simpler,
  exact, and sufficient for the token-budget symptom. Deferred.

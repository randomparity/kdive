# ADR-0247: `artifacts.get` byte windowing (#803)

- Status: Accepted
- Date: 2026-06-25

## Context

`artifacts.get` (ADR-0140) returns a redacted artifact's bytes inline in
`data.content` when the object size is at or under
`KDIVE_ARTIFACT_INLINE_MAX_BYTES` (default 64 KiB), and omits the content
(`content_omitted="artifact_too_large"`, `download_uri` only) above it
(`src/kdive/mcp/tools/catalog/artifacts/reads.py:237-277`). It takes only
`artifact_id`; there is no per-call range or length control.

The most common redacted artifact, a successful-boot console, is ~64 KiB — right
at the cap. 64 KiB of text is ~16k–20k tokens, which overflows a typical MCP
tool-result token budget, so the whole-object inline response is unusable for the
artifact it most needs to serve. The Part 4 black-box review
(`BLACK_BOX_REVIEW.md`, 2026-06-25, D3) confirmed it: the boot console spilled to
a file while the smaller crash console returned fine. `download_uri` (always
minted) and `artifacts.search_text` (ADR-0225, bounded grep — needs a pattern,
cannot page) are partial workarounds; the primary `get` cannot be windowed.

The redaction invariant (CLAUDE.md "Secrets by reference + mandatory redaction")
and the ADR-0140 gate stand: only `REDACTED` artifacts are served, the object's
own metadata sensitivity is confirmed via `head` before the URI is minted, and
the fetched bytes' sensitivity is re-verified before they reach the response.

## Decision

Add two optional parameters to `artifacts.get`, advertised in the schema:

- `byte_offset: int = 0` (`ge=0`) — the window's start byte.
- `max_bytes: int = 16384` (`ge=1`, `le=65536`) — the window's maximum length.
  The default (16 KiB ≈ 4k–5k tokens) is sized to the token budget, not the
  64 KiB byte cap; the schema maximum equals the inline-cap default (64 KiB).

The handler keeps the existing `head`-then-`get_artifact` structure and adds an
in-process slice:

1. `head` for size + object-metadata sensitivity. Gate unchanged: a non-`REDACTED`
   `head.sensitivity` is not-found-shaped, no URI minted, no fetch.
2. Mint `refs.download_uri` (unchanged, every size).
3. If `head.size_bytes` exceeds a 1 MiB **fetch ceiling**
   (`_MAX_WINDOWED_FETCH_BYTES`, equal to `search_text`'s
   `_MAX_SEARCHABLE_ARTIFACT_BYTES`): omit content
   (`content_omitted="artifact_too_large"`) — existing behavior, now at the 1 MiB
   ceiling rather than the 64 KiB inline cap, so 64 KiB–1 MiB objects become
   windowable instead of omitted.
4. Otherwise fetch the whole object via `get_artifact(key, head.etag)` (etag
   stale-handle check unchanged), re-verify `fetched.sensitivity is REDACTED`
   (unchanged), then slice `data[byte_offset : byte_offset + effective_max]`
   **before** the UTF-8 decode, where
   `effective_max = min(max_bytes, KDIVE_ARTIFACT_INLINE_MAX_BYTES)`. Decode the
   slice with `errors="replace"`.

Returned `data` on the windowed branch:

- `size_bytes` — full object size.
- `content` — the decoded window.
- `content_truncated` — `"true"` iff `byte_offset + len(window) < size_bytes`.
- `next_offset` — `str(byte_offset + len(window))`, present **only** when
  truncated; the offset to resume paging.

`byte_offset` at or past the object end yields an empty window,
`content_truncated="false"`, no `next_offset` — clean paging termination.
`effective_max` clamps to the configured inline cap so lowering
`KDIVE_ARTIFACT_INLINE_MAX_BYTES` is honored even though the schema maximum is the
static default.

## Consequences

- The default read path on a 64 KiB-plus console returns 16 KiB inline with a
  `next_offset`, no longer overflowing the token budget; the caller pages the rest
  or follows `download_uri`.
- Byte windowing is exact and content-agnostic; a multi-byte UTF-8 sequence split
  at either window edge becomes a replacement character (the bytes are
  authoritative via `download_uri`, matching ADR-0140's `errors="replace"` text
  view).
- A windowed call fetches the whole object (up to 1 MiB) into memory to slice; for
  the text artifacts this serves (console, redacted dmesg, build-log) that is
  bounded and matches `search_text`'s existing fetch profile. Paging a near-1 MiB
  object in 16 KiB windows re-fetches it per window — acceptable for these sizes;
  genuinely large objects use `download_uri`.
- Every authorization/redaction/degradation gate is unchanged. The change to the
  MCP surface is additive (two optional parameters with defaults that reproduce a
  bounded subset of the prior whole-object response), and `runs.get`-style
  generic envelope output schema (`data` free-form) means no committed output
  snapshot changes; the generated tool reference is regenerated.

## Considered & rejected

- **Ranged store read (`get_range`).** The store seam already has
  `get_range(key, start, length)` (`store/objectstore.py:255`), which would avoid
  pulling the whole object into memory. Rejected: it returns only bytes, discarding
  the object's sensitivity metadata, so it cannot perform the post-fetch
  `fetched.sensitivity is REDACTED` recheck this change preserves. Carrying
  sensitivity on a ranged read means extending the store seam, outside this
  change's file scope; the 1 MiB ceiling bounds the full-fetch cost and the
  download URI covers larger objects.
- **`size_bytes` on `artifacts.list` items.** Useful for discovering size before a
  `get`, but the size lives in the object store, not the `artifacts` row, so each
  item would need a `head` (N round-trips) or a new DB column + migration.
  Rejected as scope creep: size is already discoverable with one `artifacts.get`
  (or `search_text`'s oversize reason), and the windowing fix does not depend on
  it.
- **Line-based windowing (`line_offset`/`line_limit`).** A natural fit for text,
  but it needs a full decode + line index and complicates the "page by returned
  cursor" contract for partial trailing lines. Rejected: byte windowing is exact,
  simpler, and sufficient for the token-budget symptom; `download_uri` is
  authoritative for exact content.
- **A configurable default-window setting.** Rejected: a module constant avoids a
  speculative knob and a second config-reference regeneration; the per-call
  `max_bytes` already lets a caller pick its window, and the configured inline cap
  still bounds the maximum.
- **Raising/removing the inline cap instead of windowing.** Rejected: the cap is a
  server payload ceiling; the defect is the lack of a *caller* control, not the
  cap value. Windowing decouples "how much the caller asked for" from "how much
  the server will inline".

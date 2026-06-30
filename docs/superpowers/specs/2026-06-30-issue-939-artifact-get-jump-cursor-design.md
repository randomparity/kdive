# Filtered jump-cursor `artifacts.get`, retiring `artifacts.search_text`

- **Issue:** #939 — `artifacts.search_text` is literal and line-oriented; weak for multi-line crash signatures
- **ADR:** [ADR-0283](../../adr/0283-artifact-get-jump-cursor.md)
- **Status:** Draft (pending review)
- **Date:** 2026-06-30

## Problem

`artifacts.search_text` matches literal `|`-OR terms on a per-line, case-sensitive
basis (`security/artifacts/artifact_search.py`). Multi-line evidence — KASAN reports,
oops/panic stack traces — cannot be matched as a single signature, and the agent-facing
`pattern` description says only "grep-style", implying a regex/multi-line capability the
tool does not have (#939). The tool is therefore both under-powered and mis-advertised.

Two facts reframe the fix:

1. **Only one redacted artifact class is large enough for search to matter.** Build logs
   are tail-capped at 16 KiB, console parts at 64 KiB, and `vmcore`/`vmlinux` are
   `SENSITIVE` and never served inline. Only **redacted dmesg** approaches the 1 MiB
   ceiling (`KDIVE_DMESG_CAP_BYTES`, default 1 MiB). For everything else the agent
   already reads the whole artifact in one to three `artifacts.get` windows and searches
   it with its own full-power tooling. Server-side search exists for exactly one reason:
   **token economics on a ~1 MiB log** (paging it whole is ~43 round-trips / ~250k
   tokens; returning only the relevant region is bounded).

2. **The agent is the better matcher** — regex, multi-line, semantic — but only over text
   it holds. A server-side literal line matcher can never out-reason it. So the server
   tool's job is to *locate* a region in a haystack too big to deliver wholesale, after
   which the agent reads that region and reasons over it.

`artifacts.search_text` and `artifacts.get` address the **same** ≤1 MiB size class (both
cap there), so they overlap. They also do not compose: `search_text` returns *line
numbers* while `get` takes *byte offsets*, so "locate then read" cannot chain.

## Decision

Fold the locate job into `artifacts.get` as a **byte-offset jump cursor**, and **delete
`artifacts.search_text`**. `artifacts.get` gains two optional parameters; with `find`
absent it behaves exactly as today (full backward compatibility for the plain windowed
read path).

| parameter | type | default | meaning |
| --- | --- | --- | --- |
| `find` | `str?` | absent | literal term(s) to jump to; `\|`-OR via the existing `parse_literal_terms` lane (e.g. `"BUG: KASAN\|Call Trace"`). Absent → plain windowed read. |
| `direction` | `"forward" \| "backward"` | `forward` | which way the cursor moves through the artifact. |

This resolves #939 without building a multi-line matcher: the agent issues
`find="BUG: KASAN"` (a single anchor line) and the returned window already spans the whole
multi-line KASAN block, so multi-line reasoning happens agent-side where it is strongest.
The mis-advertised `pattern` description is **deleted with the tool**, so there is no stale
description left to correct.

## Detailed design

### Cursor and window mechanics

One **direction-relative** cursor, always reported as `data.next_offset`, always "pass this
back as `byte_offset` to continue in the same `direction`". Matching runs over the **entire**
decoded body (`artifacts.get` already fetches the whole object, ≤1 MiB, into memory); the
match is located against all of it, and only the **returned context window** is clipped to
the existing 24 KiB token ceiling.

- **forward** (`find` present): locate the first match at byte position `>= byte_offset`
  (default `0`); the window is **anchored at the matched line and extends forward** to the
  effective cap. `next_offset` = the byte just past the matched line, so the next call
  resumes searching forward, after this match.
- **backward** (`find` present): locate the last match at byte position `<= byte_offset`
  (**default `byte_offset` = end-of-artifact**); the window is **anchored at the matched
  line and extends backward**. `next_offset` = the byte just before the matched line.
- **no match in `direction`** → `data.match_found = false`, no `content`, no `next_offset`.
  The agent stops cleanly.
- The cursor **strictly advances** (forward: strictly increases; backward: strictly
  decreases), reusing the existing forward-progress guard, so paging can neither loop on a
  non-advancing cursor nor re-emit a boundary match.
- On a hit, `data.match_offset` (exact byte) and `data.match_line` (1-based) are returned so
  the agent can re-window precisely or correlate a hit; a match whose context exceeds the
  24 KiB cap clips, and `match_offset` lets the agent re-window exactly.

`direction` is orthogonal to `find`. `direction="backward"` with **no** `find`, defaulting
to end-of-artifact, is the tail-read + page-up path — the dominant kernel-crash triage
motion — at zero extra parameters: the window is the last `effective_max` bytes and
`next_offset` is its start, so a follow-up call pages upward.

### Byte-space matching (Unicode and line-orientation)

`artifacts.get` windows on **raw (decompressed) bytes** and decodes each window for display
with `errors="replace"`; `byte_offset`/`next_offset` are byte offsets. To keep that cursor
currency exact, matching operates in **byte space**:

- **Match on raw UTF-8 bytes** — each `find` term is encoded to UTF-8 and matched as
  `bytes in bytes`. UTF-8 is self-synchronizing, so a valid encoded term matches only at
  real character boundaries in valid content, and `match_offset` is a true byte offset with
  no char-to-byte mapping over a 1 MiB body.
- **Line boundaries are computed on `\n` only**, not Python `str.splitlines()` (which also
  breaks on U+2028/U+2029/`\x0b`/`\x0c`/`\x85`… bytes that legitimately appear in kernel
  logs). `\n`-splitting is the correct line model for log data and is used only to land
  `next_offset` and context cleanly; the design never *requires* line orientation — where a
  match has no nearby `\n`, `next_offset` falls back to `match_offset ± len(term)`.
- **Decoding is display-only**, per-window, `errors="replace"` (unchanged). The matched line
  is whole because the window is anchored at its start; only a window's far seam may render a
  `U+FFFD`, exactly as plain paging does today.
- **No Unicode normalization** (NFC/NFD): a non-ASCII term must match the artifact's exact
  byte encoding. Kernel crash signatures are ASCII, so unaffected — the `Field` text states
  this rather than implying normalization.

Artifacts are **not** guaranteed line-oriented by type, only by producer convention (dmesg,
build log, console snapshot are whole text; **console parts are byte-chunked** and may begin
or end mid-line). Byte-windowing degrades gracefully here where the old line-context model
would not: a pathological no-newline / single-long-line artifact still locates `match_offset`
and returns a byte window. For a single **console part**, a `find` term (≤256 chars) is far
smaller than the 4 KiB inter-part seam overlap, so any single term appears whole in at least
one part; whole multi-line reasoning across a part seam is the per-part read's inherent limit
and is why Run-scoped console reading is steered at the reassembled `console-<run_id>`
snapshot, not individual parts.

### Response shape

On a plain windowed read (`find` absent), `data` is unchanged: `size_bytes`, `content`,
`content_truncated`, and `next_offset` when truncated; `content_omitted`/`content_unavailable`
on the existing degraded paths. With `find` present, `data` adds `match_found` (bool), and on
a hit `match_offset` (int) and `match_line` (int); `content`/`next_offset` carry the match
window and the direction-relative continuation cursor as above.

### Removal of `artifacts.search_text` (MCP tool layer only)

Deleted: the `artifacts.search_text` registrar tool and `Field` text, the
`artifacts_search_text` handler, the `_artifacts_search_text` function, the
`ArtifactSearchRequest` model, and the `ArtifactReadHandlers` dataclass (used only to bind
the search-store seam for the tool).

**Preserved: the entire `security/artifacts/artifact_search.py` module** —
`search_text()`, `SearchResult`, `SearchMatch`, `parse_literal_terms`,
`ArtifactSearchInputError`, and the bound constants. `jobs/handlers/runs/boot_evidence.py`
calls `search_text()` for expected-boot-failure detection (`expected_crash_matched_line`,
`generic_panic_matches`), so the line-oriented matcher is a live dependency, not dead code.
The jump cursor is a **new** byte-space matcher on the `artifacts.get` path; it reuses
`parse_literal_terms` to validate `find` but does not reuse the line-oriented `search_text()`.
The two matchers serve different consumers (boot-evidence preset matching vs. agent log
navigation) with different needs, which is why they no longer share one tool.

Updated references: `runs.get` `data.console_access` text (drops the `search_text` mention),
`vmcore_view` `suggested_next_actions`, every `suggested_next_actions` list carrying
`search_text`, and generated tool docs (`just docs`). This is a breaking change to the agent
tool surface, acceptable pre-first-release.

## Error handling

- Empty / malformed / oversized `find` (via `parse_literal_terms`) → `configuration_error`,
  `data.reason = "bad_search_input"` (the existing search error envelope).
- Artifact `> 1 MiB` and `SENSITIVE`/non-redacted gates are unchanged from `artifacts.get`.
- `direction` outside the literal set is rejected by the typed parameter before the handler.
- Store outages degrade exactly as `artifacts.get` does today (`content_unavailable`).

## Testing

Drive the handler directly (repo convention): forward first-hit; backward first-hit from the
default end; full paging enumerates matches in order then `match_found=false`; backward + no
`find` from end equals a tail read and pages upward; **forward + no `find` is byte-identical
to today** (regression); `|`-OR jumps to the nearest of several terms; a gzip artifact matches
on plaintext with plaintext offsets; `> 1 MiB` and `SENSITIVE` gates unchanged; empty/malformed
`find` → `configuration_error` `reason=bad_search_input`; a match whose context exceeds the
24 KiB cap clips with a correct `match_offset`; cursor strict-advance at both artifact ends; a
match at offset 0 (empty `before`) and at EOF with no trailing newline. Migrate the existing
`search_text` suite onto the filtered `artifacts.get`.

## Rollback

The change is additive parameters on `artifacts.get` plus a tool deletion; there is no schema,
migration, RBAC, or persisted-state change. Rollback is reverting the branch. No data migration
is involved.

## Considered and rejected

- **Add multi-line / sequence / preset matching to `search_text`** (the issue's original
  direction): deepens investment in a server-side matcher the agent outclasses, and keeps the
  two overlapping tools. The jump cursor delivers the multi-line *outcome* (agent reads the
  block) without a multi-line matcher.
- **Keep both tools; add the filter to `get` and leave `search_text`:** two overlapping tools,
  continued agent confusion over which to reach for, and still owes #939's docs-honesty fix on
  `search_text`. "Replace, don't deprecate."
- **Multi-window return (several match windows per call), like `search_text` today:** re-imports
  `search_text`'s complex return shape into `get`. The jump cursor returns one match per call;
  triage is overwhelmingly "find the crash, read around it" (a handful of calls), not "enumerate
  all hits", so the simpler one-window model wins. The cost (N round-trips to enumerate N
  scattered matches) is accepted.
- **Regex / multi-line matching server-side:** rejected by ADR-0064's anti-ReDoS stance —
  redacted artifact content is partly guest-influenced. Matching stays literal.
- **Match in decoded-string space:** would require a char-to-byte map over a 1 MiB body and
  inherits `str.splitlines()` over-splitting. Byte-space matching is exact and simpler.
- **Loglevel / timestamp-range filters:** fragile under redaction and only the dmesg class could
  use them, which the substring + direction cursor already covers. YAGNI.

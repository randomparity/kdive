# ADR-0283: filtered jump-cursor `artifacts.get`, retiring `artifacts.search_text` (#939)

- Status: Accepted
- Date: 2026-06-30
- Supersedes the redacted-artifact-search half of [ADR-0064](0064-expected-boot-failures-artifact-search.md) and all of [ADR-0225](0225-search-text-bounded-context-schema.md)
- Builds on [ADR-0247](0247-artifacts-get-byte-windowing.md) (byte windowing) and [ADR-0257](0257-artifacts-get-token-safe-window-ceiling.md) (24 KiB token-safe ceiling)
- Spec: [2026-06-30-issue-939-artifact-get-jump-cursor-design.md](../superpowers/specs/2026-06-30-issue-939-artifact-get-jump-cursor-design.md)

## Context

`artifacts.search_text` (ADR-0064/0225) matches literal `|`-OR terms per-line and
case-sensitive over a redacted artifact. Multi-line evidence — KASAN reports, oops/panic
stack traces — cannot be matched as one signature, and the agent-facing `pattern`
description says only "grep-style", implying a regex/multi-line capability the tool does not
have (#939). The tool is both under-powered for the stated use and mis-advertised.

Two facts reframe the fix:

1. **Only redacted dmesg is large enough for server-side search to matter.** Build logs are
   tail-capped at 16 KiB, console parts at 64 KiB, `vmcore`/`vmlinux` are `SENSITIVE` and
   never inlined; only dmesg approaches the 1 MiB ceiling (`KDIVE_DMESG_CAP_BYTES`). For
   everything else the agent reads the whole artifact in one to three `artifacts.get`
   windows and searches it with its own tooling. The sole justification for a server-side
   filter is **token economics on a ~1 MiB log** (paging it whole is ~43 round-trips).
2. **The agent is the stronger matcher** — regex, multi-line, semantic — but only over text
   it holds. The server tool's real job is to *locate* a region in a haystack too large to
   deliver wholesale; the agent then reads that region and reasons over it.

`artifacts.search_text` and `artifacts.get` cover the **same** ≤1 MiB size class (both cap
there), so they overlap; and they do not compose, because `search_text` returns *line
numbers* while `get` takes *byte offsets*.

## Decision

Fold the locate job into `artifacts.get` as a byte-offset **jump cursor** and **delete
`artifacts.search_text`**. `artifacts.get` gains two optional parameters; with `find` absent
it is byte-identical to today's plain windowed read.

### 1. Two new parameters

- **`find: str?`** — literal term(s) to jump to, `|`-OR via the existing
  `parse_literal_terms` lane (preserved; also used by boot-evidence). Absent → plain
  windowed read, unchanged.
- **`direction: "forward" | "backward"` = `forward`** — which way the cursor moves.

### 2. Direction-relative jump cursor

Matching runs over the **entire** decoded body (`artifacts.get` already fetches the whole
object, ≤1 MiB, into memory); only the returned context window is clipped to the ADR-0257
24 KiB ceiling. The single cursor `data.next_offset` always means "pass back as `byte_offset`
to continue in the same `direction`".

- **forward**: first match at byte `>= byte_offset` (default `0`); window anchored at the
  matched line, extending forward; `next_offset` = just past the matched line.
- **backward**: last match at byte `<= byte_offset`; window anchored at the matched line,
  extending backward; `next_offset` = just before it. `byte_offset` defaults to the
  direction's natural edge: `forward` keeps the existing start (`0`/negative = from-start),
  and `backward` treats an omitted/`0`/negative `byte_offset` as **end-of-artifact** (a strict
  backward search from byte 0 is degenerate, so the default is repurposed losslessly); a
  positive value bounds the backward search.
- **no match in `direction`** → `data.match_found = false`, no `content`, no `next_offset`.
- The cursor strictly advances (reusing the existing forward-progress guard), so paging
  cannot loop or re-emit a boundary match. A hit also returns `data.match_offset` (byte) and
  `data.match_line` (1-based) for precise re-windowing/correlation.

`direction` is orthogonal to `find`: `direction="backward"` with no `find`, defaulting to
end-of-artifact, is the tail-read + page-up path (the dominant crash-triage motion) at zero
extra parameters.

### 3. Byte-space matching

`artifacts.get` windows on raw (decompressed) bytes with byte offsets, so matching stays in
byte space to keep the cursor exact: `find` terms are UTF-8-encoded and matched as
`bytes in bytes` (UTF-8 self-synchronization keeps matches on character boundaries); line
boundaries are computed on `\n` only — not `str.splitlines()`, which over-splits on
U+2028/U+2029/`\x0b`/`\x0c`/`\x85` bytes that appear in kernel logs — and are used only to
land `next_offset`/context (never required: a match with no nearby `\n` advances the cursor
by `match_offset ± len(term)`). Decoding stays display-only (`errors="replace"`). No Unicode
normalization: a non-ASCII term must match the artifact's exact encoding; the `Field` text
states this. ASCII crash signatures are unaffected.

### 4. Retire `artifacts.search_text` (MCP tool layer only)

Delete the registrar tool, the `artifacts_search_text`/`_artifacts_search_text` handlers,
the `ArtifactSearchRequest` model, and the `ArtifactReadHandlers` dataclass. **Keep the whole
`artifact_search.py` module** — `search_text()`, `SearchResult`, `SearchMatch`,
`parse_literal_terms`, `ArtifactSearchInputError`, the bound constants — because
`boot_evidence.py` calls `search_text()` for expected-boot-failure detection. The jump cursor
is a separate byte-space matcher; it reuses only `parse_literal_terms` for `find` validation.
Drop the `search_text` mention from `runs.get` `data.console_access`, `vmcore_view`
`suggested_next_actions`, and every `suggested_next_actions` carrying it; regenerate the
committed tool reference (`just docs`). No schema, migration, RBAC, or config change.

## Consequences

- #939's multi-line ask is met without a multi-line matcher: the agent issues
  `find="BUG: KASAN"` and the returned window spans the whole block; multi-line reasoning
  happens agent-side.
- #939's mis-advertised `pattern` description is removed with the tool — no stale text to
  correct; the new `find`/`direction` `Field` text states the literal/`|`-OR/byte-space/
  no-normalization contract directly.
- One artifact-read surface instead of two overlapping ones; locate→read now composes on a
  single byte-offset currency.
- **Breaking change to the agent tool surface** (`artifacts.search_text` removed). Acceptable
  pre-first-release; agents migrate to `artifacts.get` with `find`.
- The jump cursor returns **one match per call**. Enumerating N scattered matches costs N
  round-trips, versus `search_text`'s up to 50 windows in one call. Each call is stateless and
  re-fetches the whole artifact (≤ 1 MiB) from the object store and re-scans it, so N matches
  cost N bounded fetches + scans (no cross-call caching). Accepted: triage is overwhelmingly
  "find the crash, read around it", not "enumerate all hits", and the design bounds the *token*
  cost of a large log regardless of fetch count.
- **`find` on an artifact larger than 1 MiB rejects, it does not silently miss.** A plain
  `.get` over the 1 MiB windowed-fetch ceiling omits inline content and returns only a download
  URI; `find` cannot search bytes never fetched, so it returns `configuration_error`
  `reason=artifact_too_large` (preserving `search_text`'s rejection) rather than
  `match_found=false`, so "could not search" is never read as "no such crash". Redacted dmesg is
  capped at exactly 1 MiB, so the motivating class stays searchable.
- **A match on a line longer than the 24 KiB window cap** returns a `match_offset`-anchored
  window (not line-anchored) so the returned content always contains the matched bytes.
- A single **console part** is byte-chunked (mid-line edges); a `find` term (≤256 chars) is
  far smaller than the 4 KiB inter-part overlap, so any single term appears whole in at least
  one part. Whole multi-line reasoning across a part seam remains the per-part read's limit;
  Run-scoped console reading is steered at the reassembled `console-<run_id>` snapshot.
- No persisted state changes; rollback is reverting the branch.

## Considered & rejected

- **Add multi-line / sequence / preset matching to `search_text`** (the issue's original
  direction): deepens a server-side matcher the agent outclasses and keeps two overlapping
  tools. The jump cursor delivers the multi-line outcome without a multi-line matcher.
- **Keep both tools (filter on `get`, `search_text` retained):** overlap, agent confusion,
  and still owes the docs-honesty fix on `search_text`. "Replace, don't deprecate."
- **Multi-window return (several match windows per call):** re-imports `search_text`'s return
  shape into `get`. One-window jump matches the dominant triage pattern; the N-round-trip
  enumeration cost is accepted.
- **Regex / multi-line server-side matching:** rejected by ADR-0064's anti-ReDoS stance —
  redacted content is partly guest-influenced. Matching stays literal.
- **Match in decoded-string space:** needs a char-to-byte map over a 1 MiB body and inherits
  `str.splitlines()` over-splitting. Byte-space matching is exact and simpler.
- **Loglevel / timestamp-range block filters:** fragile under redaction and usable only by the
  dmesg class the substring + direction cursor already covers. YAGNI.

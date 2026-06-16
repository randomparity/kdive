# Spec: End-to-end artifact retrieval through `artifacts.get` (#485)

ADR: [ADR-0140](../adr/0140-artifacts-get-content-retrieval.md)

## Problem

`artifacts.get` returns only `refs={"object": key}` — no content, no download URI
(`src/kdive/mcp/tools/catalog/artifacts/reads.py`). A caller cannot retrieve the
full bytes of a redacted artifact (console log, redacted dmesg) through the MCP
surface. `artifacts.search_text` reads the full object but returns only bounded
match windows. The store already exposes `get_artifact` (full bytes) and
`presign_get` (time-boxed GET URL); `get` exposes neither.

## Goal

A caller retrieves a redacted artifact's full content end-to-end:
- inline, within a documented size bound, for small artifacts; **and**
- via a presigned download URI for any size.

The existing redaction guarantee is preserved: only `redacted` artifacts are
returned, and content passes the same `sensitivity is REDACTED` gate
`search_text` applies.

## Behavior

`artifacts.get(artifact_id)` — authorization, sensitivity, project, and
quarantined/sensitive-is-not-found behavior are unchanged. On success the response
gains:

1. **`refs["object"]`** — unchanged (the object key).
2. **`refs["download_uri"]`** — a presigned GET URL for the object key, expiry
   `KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS` (default 900). Present whenever the store
   is reachable.
3. **`data["size_bytes"]`** — the object size from `head`.
4. **`data["content"]`** — the UTF-8-decoded (`errors="replace"`) object bytes, a
   best-effort text view; present iff `size_bytes <= KDIVE_ARTIFACT_INLINE_MAX_BYTES`
   (default 64 KiB). `download_uri` is authoritative for the exact bytes.
5. **`data["content_truncated"]`** — `"false"` (the inline body is never a clip;
   oversized omits `content` entirely).

### Size handling

The inline cap is **deliberately distinct** from the `search_text` fetch/scan
bound. `search_text` is willing to *fetch* up to `_MAX_SEARCHABLE_ARTIFACT_BYTES`
(1 MiB) but caps what it *returns* at `MAX_MATCHES_JSON_CHARS` (64 KiB). Inlining a
full 1 MiB object into one `ToolResponse.data` value would be far larger than any
other read tool returns and a heavy token payload for an LLM consumer. So
`artifacts.get` inlines only up to `KDIVE_ARTIFACT_INLINE_MAX_BYTES` (default 64 KiB,
matching `search_text`'s return scale) and routes anything larger to the URI:

- `size_bytes <= KDIVE_ARTIFACT_INLINE_MAX_BYTES`: fetch via
  `get_artifact(key, head.etag)`, re-verify `fetched.sensitivity is REDACTED`
  (reject to a not-found-shaped `configuration_error` otherwise), and return the
  decoded bytes in `data["content"]`.
- `size_bytes > KDIVE_ARTIFACT_INLINE_MAX_BYTES`: omit `content`; set
  `data["content_omitted"] = "artifact_too_large"`; `get_artifact` is never called.
  The presigned `download_uri` is the retrieval path for any size.

### Best-effort degradation

The metadata envelope (`available`, `refs.object`) is the contract `artifacts.get`
already honors and must not regress. The content/URI enrichment is best-effort:

- store factory raises (`CONFIGURATION_ERROR` — S3 unconfigured): return the
  metadata envelope with `data["content_unavailable"] = "store_unconfigured"`,
  no `download_uri`, no `content`.
- `head`/`get_artifact`/`presign_get` raises `CategorizedError`: return the
  metadata envelope with `data["content_unavailable"] = "store_error"`, no
  `download_uri`, no `content`.

A store outage never turns a successful `get` into a tool failure.

## Security

- Only `redacted` artifacts reach this path (`_authorized_redacted_artifact` +
  the `sensitivity is REDACTED` re-check). A `redacted` object stores redactor
  output (`_extract_redacted` at capture, `providers/local_libvirt/retrieve.py`),
  so its bytes are already redacted.
- `presign_get` is called with the authorized `redacted` key only; the URL cannot
  address the `sensitive` sibling object. It is a bearer capability bounded by the
  configured TTL. The URI is minted **only after `head().sensitivity is REDACTED`**,
  so the URI path enforces the same object-metadata redaction gate as the inline
  path for every size (including oversized artifacts whose body is never fetched);
  a DB-row/object sensitivity drift is not-found-shaped on both paths.
- No new redaction pass is run on fetched bytes — the persisted object is already
  the redactor's output; the gate is the sensitivity re-check, identical to
  `search_text`.
- `data["content"]` is a best-effort UTF-8 text view (`errors="replace"`). Redacted
  artifacts today are text (console / redacted dmesg); for exact bytes (or a future
  non-text redacted artifact) `refs["download_uri"]` is authoritative. `content` is
  never trusted as a byte-faithful copy.

## Out of scope

- Streaming/chunked inline bodies for large artifacts (the URI is the bulk path).
- Range reads, conditional GETs, or content negotiation.
- Changing the advertised output schema (stays flat `{"type":"object"}`, ADR-0113).

## Tests

Handler-level, injected store seam (mirrors the `search_text` tests):

- redacted ≤`KDIVE_ARTIFACT_INLINE_MAX_BYTES`: `content` present and equals the
  decoded object, `size_bytes` set, `download_uri` present.
- redacted >`KDIVE_ARTIFACT_INLINE_MAX_BYTES` (but small object): `content` absent,
  `content_omitted: artifact_too_large`, `download_uri` present, `get_artifact`
  never called (`store.got is False`).
- fetched object `sensitivity != REDACTED` at a redacted row's key: not-found-shaped
  `configuration_error` (the post-fetch redaction gate).
- `head().sensitivity != REDACTED` at a redacted row's key (object/row drift): not-found-shaped
  `configuration_error`, URI never minted, body never fetched — for both an
  inline-eligible and an oversized object (the pre-URI redaction gate).
- store factory raises: metadata envelope + `content_unavailable: store_unconfigured`,
  no URI.
- `head`/`presign_get` raises: metadata envelope + `content_unavailable: store_error`.
- sensitive / quarantined / cross-project / malformed-uuid id: unchanged
  not-found-shaped behavior (no content, no URI).
- viewer-role gate unchanged.
- `KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS` is passed to `presign_get` as `expires_in`.

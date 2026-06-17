# ADR-0140: End-to-end artifact retrieval through `artifacts.get` (#485)

- Status: Accepted
- Date: 2026-06-16

## Context

`artifacts.get` returns only metadata — `refs={"object": key}` — with no `data`
and no download URI (`mcp/tools/catalog/artifacts/reads.py`). There is no MCP
surface that returns the full bytes of a redacted artifact (e.g. a console log)
end-to-end. This is asymmetric and surprising: `artifacts.search_text` already
reads the full object via `store.get_artifact(key, head.etag)` (bounded by
`_MAX_SEARCHABLE_ARTIFACT_BYTES = 1 MiB`) but returns only bounded match windows.
The store layer already has both capabilities, neither exposed by `get`:
`get_artifact(key, etag)` (full bytes) and `presign_get(key, expires_in)` (a
time-boxed presigned GET URL). Found during black-box MCP evaluation (call #31, D5).

The redaction invariant (CLAUDE.md "Secrets by reference + mandatory redaction"):
all guest/console/gdb output passes the redactor before persistence, and only
`redacted` artifacts are returned by the read surface. A `redacted` artifact's
object **already stores redacted bytes** — the capture path writes a separate
`sensitive` object and a `redacted` derivative (`_extract_redacted`,
`providers/local_libvirt/retrieve.py`), so the bytes at a `redacted` key are the
post-redactor output, not the raw core.

## Decision

Extend `artifacts.get` to return the redacted artifact's content two ways, both
preserving the existing authorization + sensitivity gate (`redacted`-only,
viewer role, project-scoped, quarantined/sensitive ids are not-found-shaped):

1. **Size-bounded inline content** in `data["content"]`. Follow the `search_text`
   structure (`head` for size, then a guarded `get_artifact`), but bound the inline
   body by a **dedicated, smaller cap** `KDIVE_ARTIFACT_INLINE_MAX_BYTES`
   (default 64 KiB), not the 1 MiB `_MAX_SEARCHABLE_ARTIFACT_BYTES` fetch bound:
   `search_text` is willing to *fetch* 1 MiB but caps what it *returns* at 64 KiB
   (`MAX_MATCHES_JSON_CHARS`), so inlining a full 1 MiB object would be a far larger
   response than any other read tool and a heavy token payload for an LLM consumer.
   When `size_bytes` exceeds the inline cap, omit `content` (`content_omitted:
   artifact_too_large`) before any fetch and rely on the URI. Within the cap, fetch
   via `get_artifact(key, head.etag)` and re-verify `fetched.sensitivity is REDACTED`
   before the bytes reach the response (the same redaction gate `search_text`
   applies — a `sensitive`/quarantined object at a `redacted` row's key is
   rejected). Bytes are decoded UTF-8 with `errors="replace"` (a best-effort text
   view; the artifacts these serve — console logs, redacted dmesg — are UTF-8 text,
   and `download_uri` is authoritative for exact bytes). `data["content_truncated"]`
   is `"false"`; the size cap is a hard reject, not a silent clip, so a caller never
   mistakes a prefix for the whole object. `data["size_bytes"]` reports the object size.

2. **A presigned download URI** in `refs["download_uri"]`, minted via
   `store.presign_get(key, expires_in=KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS)` with a
   bounded default expiry (900s). This is the escape hatch for an artifact at or
   above the inline cap. The URL is a bearer capability scoped to the one
   `redacted` object key; the URI is minted from the same authorized key, so it
   cannot point at the `sensitive` sibling. The URI is minted **only after the
   object's own metadata sensitivity is confirmed `REDACTED`** via
   `head` (which now surfaces the class it already reads, `HeadResult.sensitivity`).
   This makes the URI path enforce the same object-metadata redaction gate the
   inline path applies — for every size, including artifacts above the inline cap
   that are never fetched — so a DB-row/object sensitivity drift (a row labelled
   `redacted` over a `sensitive` object) yields a not-found-shaped error on both
   paths rather than leaking via the URL.

Both are best-effort and independent of the not-found/authz envelope: a store
outage degrades content/URI but the metadata envelope (`refs.object`, `available`)
still returns. A store failure on the content/URI path is surfaced as a
`data["content_unavailable"]` reason, not a hard tool failure, so the metadata
contract `artifacts.get` already honors is unchanged.

Two new server-scoped config settings: `KDIVE_ARTIFACT_INLINE_MAX_BYTES`
(default 65536) bounding the inline body, and `KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS`
(default 900) bounding the presigned-GET expiry.

## Consequences

- A caller retrieves a redacted artifact end-to-end: inline for objects ≤1 MiB,
  or via the presigned URI for any size. The acceptance criterion is met by either
  path; both ship.
- The inline cap (`KDIVE_ARTIFACT_INLINE_MAX_BYTES`, 64 KiB) is set to
  `search_text`'s *return* scale, not its 1 MiB *fetch* scale. A larger console log
  returns metadata + URI, never a truncated body.
- The presigned URL is the only content path that bypasses the in-process redaction
  gate at fetch time. It is safe because the object it addresses is the redacted
  derivative; the URL is never minted for a `sensitive` key (authorization resolves
  the `redacted` row first).
- `artifacts.get`'s advertised output schema stays the flat `{"type":"object"}`
  (ADR-0113); the generated tool reference description changes (new `data`/`refs`
  fields documented in prose), so the generated docs are regenerated.
- No DB migration; no new object-store method (reuses `head`, `get_artifact`,
  `presign_get`). `HeadResult` gains a trailing optional `sensitivity` field
  (populated from metadata `head` already retrieves) so the URI path can gate on
  the object's class without fetching the body; the field defaults to `None`, so
  existing `HeadResult` construction sites are unaffected. Two new env settings
  (inline cap, download TTL).

## Considered & rejected

- **Inline-only (no URI).** Rejected: a 50 MiB console log would be unreachable,
  failing the acceptance criterion for large artifacts. The URI is the size escape.
- **URI-only (no inline).** Rejected: forces every caller through a second HTTP
  fetch + presigned-URL handling for a few-KiB log; the inline path is the common,
  one-call case.
- **Silently clipping oversized inline content to the cap.** Rejected: a caller
  cannot distinguish a clipped prefix from the whole object, and a redacted log's
  meaning can hinge on its tail (the panic). Oversized is a hard reject that points
  the caller at the URI, mirroring `search_text`'s `artifact_too_large`.

- **Reusing the 1 MiB `_MAX_SEARCHABLE_ARTIFACT_BYTES` as the inline cap.**
  Rejected: that constant bounds how much `search_text` *fetches and scans*, not
  what it *returns* (it caps its returned payload at 64 KiB). Inlining a full 1 MiB
  object into one response would dwarf every other read tool's payload and be a
  heavy token cost for an LLM consumer. The inline cap is a dedicated 64 KiB knob;
  anything larger uses the URI.

- **Treating `data["content"]` as a byte-faithful copy.** Rejected: it is a
  best-effort UTF-8 text view (`errors="replace"`). Redacted artifacts are text
  today, but the read surface is generic; `download_uri` serves the exact bytes, so
  `content` is documented as advisory rather than guaranteed byte-equal.
- **Re-running the redactor on fetched bytes here.** Rejected: the object at a
  `redacted` key is already the redactor's output (`_extract_redacted` at capture);
  re-redacting would be a no-op at best and risks diverging from the persisted
  form. The gate is the `sensitivity is REDACTED` check, identical to `search_text`.
- **Streaming the bytes through the MCP response for large objects.** Rejected:
  the envelope is "references, never log dumps" (ADR-0019); the presigned URI is
  the reference for bulk bytes, consistent with how vmcores are handed off.

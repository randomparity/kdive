# ADR-0140: End-to-end artifact retrieval through `artifacts.get` (#485)

- Status: Proposed
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

1. **Size-bounded inline content** in `data["content"]`. Reuse the `search_text`
   pattern exactly: `head` for size, reject over `_MAX_SEARCHABLE_ARTIFACT_BYTES`
   with `configuration_error` (`reason: artifact_too_large`) before any fetch,
   then `get_artifact(key, head.etag)` and re-verify `fetched.sensitivity is
   REDACTED` before the bytes reach the response (the same redaction gate
   `search_text` applies — a `sensitive`/quarantined object at a `redacted`
   row's key is rejected). Bytes are decoded UTF-8 with `errors="replace"` and
   returned whole (the artifacts these serve — console logs, redacted dmesg — are
   UTF-8 text). `data["content_truncated"]` is `"false"`; the size cap is a
   hard reject, not a silent clip, so a caller never mistakes a prefix for the
   whole object. `data["size_bytes"]` reports the object size.

2. **A presigned download URI** in `refs["download_uri"]`, minted via
   `store.presign_get(key, expires_in=KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS)` with a
   bounded default expiry (900s). This is the escape hatch for an artifact at or
   above the inline cap. The URL is a bearer capability scoped to the one
   `redacted` object key; because only `redacted` objects reach this path and a
   `redacted` object stores already-redacted bytes, the URL exposes no
   unredacted content. The URI is minted from the same authorized key, so it
   cannot point at the `sensitive` sibling.

Both are best-effort and independent of the not-found/authz envelope: a store
outage degrades content/URI but the metadata envelope (`refs.object`, `available`)
still returns. A store failure on the content/URI path is surfaced as a
`data["content_unavailable"]` reason, not a hard tool failure, so the metadata
contract `artifacts.get` already honors is unchanged.

`KDIVE_ARTIFACT_DOWNLOAD_TTL_SECONDS` is a new server-scoped config setting
(default 900) bounding the presigned-GET expiry.

## Consequences

- A caller retrieves a redacted artifact end-to-end: inline for objects ≤1 MiB,
  or via the presigned URI for any size. The acceptance criterion is met by either
  path; both ship.
- The 1 MiB inline cap is shared with `search_text` (`_MAX_SEARCHABLE_ARTIFACT_BYTES`)
  — one bound, one place. A larger console log returns metadata + URI, never a
  truncated body.
- The presigned URL is the only content path that bypasses the in-process redaction
  gate at fetch time. It is safe because the object it addresses is the redacted
  derivative; the URL is never minted for a `sensitive` key (authorization resolves
  the `redacted` row first).
- `artifacts.get`'s advertised output schema stays the flat `{"type":"object"}`
  (ADR-0113); the generated tool reference description changes (new `data`/`refs`
  fields documented in prose), so the generated docs are regenerated.
- No DB migration; no new object-store method (reuses `head`, `get_artifact`,
  `presign_get`); one new env setting.

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
- **Re-running the redactor on fetched bytes here.** Rejected: the object at a
  `redacted` key is already the redactor's output (`_extract_redacted` at capture);
  re-redacting would be a no-op at best and risks diverging from the persisted
  form. The gate is the `sensitivity is REDACTED` check, identical to `search_text`.
- **Streaming the bytes through the MCP response for large objects.** Rejected:
  the envelope is "references, never log dumps" (ADR-0019); the presigned URI is
  the reference for bulk bytes, consistent with how vmcores are handed off.

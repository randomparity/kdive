# 0395 — Surface the presigned-PUT footguns on the upload response

Status: Accepted

## Context

The external-build upload path has two presigned-PUT footguns that were invisible
to an agent reading only tool schemas and responses (#1338):

1. **Extra-header 403.** Each presigned URL is SigV4-signed over a fixed header
   set (`x-amz-checksum-sha256` + the `x-amz-meta-*` metadata). Any *extra*
   header the client sends — most commonly an implicit `Content-Type` from
   `curl --data-binary` — changes the signed request and the store answers
   `403 SignatureDoesNotMatch`. The correct invocation (`curl -T` with
   `Content-Type:` cleared) lived only in the `external-build-upload.md` resource
   doc, not in the tool response or the generated tool reference.
2. **Opaque checksum-bypass rejection.** A direct `put_object` that bypasses the
   presigned PUT stores the object *without* `x-amz-checksum-sha256`.
   `runs.complete_build` then rejected it with the generic "uploaded artifact
   disagrees with its manifest" — the same message a genuine checksum *mismatch*
   produces, so the agent could not tell "checksum absent (bypass)" from
   "checksum differs (wrong bytes)".

This is the sibling of ADR-0394 (#1336, the deadline contract): the same "state a
limit's full contract on the surface the agent actually reads" doctrine, applied
to the PUT ergonomics rather than the deadline.

## Decision

**1. Carry the guidance in the collection `data`, not only in a resource doc.**
`create_run_upload` / `create_system_upload` responses gain a
`data.upload_hint` string that states: send *only* `data.required_headers`; an
extra header (notably curl's implicit `Content-Type`) breaks the SigV4 signature
→ `403 SignatureDoesNotMatch`; prefer `curl -T <file> -H 'Content-Type:'`; and do
not bypass the presigned PUT with a direct `put_object` (it drops the signed
`x-amz-checksum-sha256` integrity binding). This is one collection-level field
(alongside `server_time` / `manifest_deadline` / `on_expiry` from ADR-0394), not
a per-item repeat.

**2. State it in the wrapper docstrings too, so the generated reference carries
it.** Both `@app.tool` docstrings state the extra-header 403 footgun; the run
docstring additionally names the concrete consequence of a bypass
(`runs.complete_build` rejects an object with no stored checksum), because the
run build-artifact path is the one that verifies the stored checksum.

**3. The `data.upload_hint` bypass caution is stated generically.** Only the run
build-artifact finalize (`validate_external_artifacts`) verifies the stored
checksum against the manifest; the system rootfs path does not. So the shared
response field states the bypass caution as loss of the integrity binding (true
for both owner kinds) and does not claim a specific downstream rejection; the
run-specific rejection is named only in the run tool's docstring, where it is
accurate.

**4. Split the finalize rejection into two distinct messages.**
`_validate_one_artifact` now raises a distinct, actionable message when
`head.checksum_sha256 is None` ("uploaded artifact has no stored SHA-256 checksum
(the upload bypassed the presigned PUT; a direct put_object must send the
x-amz-checksum-sha256 header)"), separate from the genuine size/checksum
*mismatch* case which keeps "uploaded artifact disagrees with its manifest". The
absent-checksum check runs first, so a bypass is always diagnosed as a bypass.
The chunked/reassembled branch is unchanged (a reassembled object legitimately
exposes only a composite checksum; its per-chunk pins already bound every byte).

**5. Extend the `presign_put` docstring.** It already documented the checksum
binding; it now also states that the URL is signed over exactly the returned
header set and that an extra header yields `403 SignatureDoesNotMatch`, so the
constraint travels with the store primitive.

## Consequences

- A tool-schema-only agent sees both footguns in the upload response and the
  generated tool reference, not just in a resource doc.
- A checksum-bypass finalize failure names its own cause, so the agent stops
  guessing between "absent" and "differs".
- The generated tool reference changes, so `just docs` runs and `docs-check`
  gates it.
- Response-contract and docs only; no schema change, no migration. The new field
  lives in `data` (not the `extra="forbid"` `ToolResponse` envelope), consistent
  with ADR-0394.

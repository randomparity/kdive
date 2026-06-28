# Issues 888 and 889 Upload Contract Design

## Problem

`artifacts.create_run_upload` returns presigned PUT URLs, but clients must also send signed
S3 headers from the response. The current response flattens those `x-amz-*` fields into
each upload item, and the tool/runbook wording does not make the header requirement
obvious. The same tool also replaces the owner upload manifest on each call, but the
collection response and runbook do not disclose that callers must redeclare every artifact
when correcting an upload.

## Contract

Each upload item must expose a structured `required_headers` object containing every HTTP
header that must accompany the PUT request. Existing flattened header fields may remain in
the item data so current clients do not lose information.

The collection response must disclose manifest replacement semantics with
`manifest_mode = "replace"` and `replaces_prior_manifest = true`. Documentation must tell
agents that a second upload declaration replaces the prior manifest and that the PUT must
include the returned required headers.

## Implementation

Add `required_headers` to `_upload_response()` item data while keeping the existing
flattened signed headers. Add manifest replacement fields to the collection `data`.
Update the `artifacts.create_run_upload` tool docstring and external upload runbook text.

## Testing

Add behavior coverage in `tests/mcp/lifecycle/test_create_upload_tool.py` that asserts the
structured required headers and replacement-mode fields are present. Add discovery/doc
coverage that asserts the registered tool description mentions required headers and
manifest replacement. Update the committed MCP resource copy of the runbook alongside the
operator-facing runbook.

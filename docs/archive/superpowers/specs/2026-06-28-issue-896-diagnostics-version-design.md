# Issue 896 Diagnostics Version Design

## Problem

Agents can run `ops.diagnostics` to inspect kdive dependencies, but the response does not
identify the kdive service build that produced the verdict. That makes black-box reports
harder to correlate with a deployed package or commit.

The repository already has `kdive.version.version_info()` and `kdive.version.full_version()`
for package version, commit, and release/dev state. The missing piece is projecting those
facts onto an agent-visible MCP response.

## Contract

`ops.diagnostics` must include top-level `data.service_version` on successful diagnostic
responses. The value must contain:

- `version`: package version string.
- `commit`: commit string when known, otherwise `null`.
- `is_release`: whether the running build is an exact release build.
- `full_version`: the display string already used by `kdive --version` and startup logs.

The field is intentionally top-level data, not a synthetic diagnostic check item. It
describes the responding kdive service rather than a probed dependency, and it must be
present even when individual diagnostic checks fail or error.

Authorization, audit behavior, and denied responses stay unchanged.

## Implementation

Add a small helper in `src/kdive/mcp/tools/ops/diagnostics.py` that reads
`kdive.version.version_info()` and `kdive.version.full_version()` and returns JSON-safe
service-version data. Include that helper in `_verdict()` data next to `has_failure` and
`has_error`.

## Testing

Add `tests/mcp/ops/test_diagnostics.py` coverage that monkeypatches the version module and
asserts a served operator response includes the deterministic `service_version` object.
Keep the denial tests unchanged so denied responses do not gain diagnostic metadata.

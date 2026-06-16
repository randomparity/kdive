# Tool-error detail surfacing — design spec (#450, ADR-0123)

- **Issue:** [#450](https://github.com/randomparity/kdive/issues/450) (work item B, error detail)
- **Epic:** [#449](https://github.com/randomparity/kdive/issues/449)
- **ADR:** [`0123`](../adr/0123-tool-error-detail-surfacing.md)
- **Design doc:** [`mcp-onboarding-error-ergonomics.md`](./mcp-onboarding-error-ergonomics.md) (work item B)

## Problem

The uniform tool-response envelope (`ToolResponse`, `src/kdive/mcp/responses.py`) carries
`error_category` but no human-readable reason. Two pieces of information the server already
computes are dropped on the way to the wire:

1. **The exception message.** `CategorizedError(message, …)` passes `message` to
   `Exception.__init__` (`src/kdive/domain/errors.py:64-81`), but
   `ToolResponse.failure_from_error` (`responses.py:193-210`) reads only `exc.details` and
   discards `str(exc)`. The admission service's private `_failure_from_error`
   (`admission.py:132-133`) does the same.

2. **The structured validation errors.** `ProvisioningProfile.parse()` attaches Pydantic's
   `exc.errors(...)` list to `details["errors"]` (`provisioning.py:280-287`), but
   `_safe_error_details` (duplicated in `responses.py:77-86` and `admission.py:120-129`) filters
   `data` to scalars — and `errors` is a list, so it is dropped.

Observed in MCP-surface testing: a `configuration_error` returned with empty `data` and no
message, leaving the caller unable to learn what was wrong. This is the foundation seam for the
epic — #451 (typed profile param) and #452 (transport bound) both surface their reasons through
the `detail`/`errors` carriers built here.

## Decision (from ADR-0123)

Add `detail: str | None = None` to `ToolResponse`, populated from the `CategorizedError`
message. Widen the data-detail filter to preserve one reserved nested
`errors: list[{loc, msg, type}]` key, bounded to 20 entries, each sanitized to scalars.
Consolidate the duplicated `_safe_error_details` to one helper. `AdmissionFailure` gains a
`detail` field threaded through the `provision.py` mapper.

The **no-leak guard is enforced at the seam, not the raise site**: `failure`/`failure_from_error`
hold a closed set of *suppressed categories* (`authorization_denied`, `not_found`) for which
`detail` is the fixed constant `"access denied"` / `"not found"` and `str(exc)` is ignored — so
no raise site (including `ProjectMembershipDenied`, whose message embeds the named project, and
the resolver/build-host `not_found` raisers, whose messages embed the object id/name) can leak
through `detail`.

## Approach

### Envelope field and constants (`responses.py`)

- `ToolResponse` gains `detail: str | None = None`. It is an additive wire field on the payload;
  the advertised output schema is unchanged (stays flat `{"type": "object"}` per ADR-0113 — the
  `build_app` sweep already flattens it). `detail` is **present-but-null** on success and on
  worker-plane (`from_job`) envelopes — it is not omitted when `None`. Because the advertised
  schema is a flat untyped object, a nullable field on every response is not a schema change; the
  consistency (every envelope has the key) is preferable to conditional omission.
- A module-level closed map names the suppressed categories and their constant detail:

  ```python
  _SUPPRESSED_DETAIL: dict[ErrorCategory, str] = {
      ErrorCategory.AUTHORIZATION_DENIED: "access denied",
      ErrorCategory.NOT_FOUND: "not found",
  }
  ```

- `failure()` gains an optional `detail: str | None = None` kwarg. A passed `detail` is run
  through the seam rule (`_seam_detail`): for a suppressed category the constant wins and any
  caller value is ignored; otherwise the caller value passes through.
- `failure_from_error()` derives `detail` from `str(exc)` via the same `_seam_detail` rule, so a
  `not_found`/`authorization_denied` `CategorizedError` is collapsed to the constant even though
  its message embeds the resource.

### Structured-error preservation (`_safe_error_details`)

A single helper lives in `responses.py` (the duplicate in `admission.py` is deleted and imports
the shared one). The scalar-only rule is unchanged for every key **except** one reserved key,
`errors`:

- When `details["errors"]` is a `list`, keep the first 20 entries. Each entry is reduced to a
  dict whose values are sanitized to scalars (the same float-finite / str/bool/int rule), and
  only the reserved sub-keys `loc`, `msg`, `type` are kept. `loc` is a Pydantic tuple of
  path segments → rendered to a `list` of scalars (segment ints kept, everything else `str()`).
- `input` is already stripped at the throw site
  (`exc.errors(include_url=False, include_input=False, include_context=False)`), so no submitted
  *value* echoes back. The helper does not read or forward `input` even if present. Note: `loc`
  *can* contain a caller-chosen key name — an `extra_forbidden` error puts the rejected extra
  key into `loc` (verified: `loc == ('SECRET_EXTRA_KEY',)` for an unknown key). For the profile
  surface this is a field path, not secret material, so it is intended; the invariant is "no
  submitted *value* echoes," not "no caller-supplied string at all." A test pins this so the
  distinction is explicit rather than assumed.
- All other `data` keys keep the scalar-only behavior (lists/dicts under any other key are still
  dropped).

`AdmissionFailure` carries the sanitized `data` (already does) plus a new `detail: str | None`
field. `_failure_from_error` in `admission.py` sets `detail` via the shared seam rule, and the
`provision.py` mapper (`_admission_response`) passes `result.detail` into `ToolResponse.failure`.

### No-leak audit (bounded)

The ADR requires a one-time audit of diagnostic-category raise sites for secret/path/hostname
interpolation, bounded to categories that reach `detail`. Findings:

- The secret-bearing `CONFIGURATION_ERROR` messages — ssh credential-ref resolution
  (`providers/shared/build_host/ssh_transport.py:118`), remote-libvirt TLS secret-ref resolution
  (`providers/remote_libvirt/transport.py:114`), object-store `presign_get` key
  (`store/objectstore.py:320`) — are all raised in the **provider/worker plane**. They reach the
  wire through the async worker's `failure_context` path (`jobs/worker.py:255`, redacted by
  `SecretRegistry`) and `ToolResponse.from_job`, **not** through `failure_from_error`/`detail`.
  They are therefore out of the `detail` audit scope; the worker keeps its own redaction.
- The synchronous diagnostic-category raise sites that *do* reach `failure_from_error` — chiefly
  `ProvisioningProfile.parse()` (`"invalid provisioning profile"`), admission sizing/quota, and
  the `_common`/`_runtime_resolution` config errors — carry author-controlled messages with no
  secret/path/hostname/object-key interpolation. No raise-site message edits are required for
  the `detail` egress in this change.

## Acceptance criteria → tests

1. A `configuration_error` from `ProvisioningProfile.parse()` carries a non-empty `detail`
   (`"invalid provisioning profile"`) and an `errors` list of `{loc, msg, type}` field paths.
   *Test:* `tests/mcp/core/test_responses.py` (envelope) + an admission test driving a malformed
   profile end-to-end through `provision.py`.
2. A non-member denial (`ProjectMembershipDenied`, message embeds the project) and a by-id
   `not_found` (resolver message embeds the object id) both carry the seam's constant `detail`
   with NO resource/project name. *Test:* `tests/mcp/core/test_responses.py` drives a
   `CategorizedError(not_found)` whose message embeds a name and asserts `detail == "not found"`
   and the name is absent from the whole serialized envelope; a middleware test asserts the
   `authorization_denied` envelope detail is the constant.
3. `errors` is bounded at 20 and each entry is scalar-sanitized; submitted `input`/`ctx` never
   echo. *Test:* feed a 25-entry `errors` list with a nested `input`/`ctx` and assert ≤20
   entries, only `{loc, msg, type}` sub-keys, scalars only, no `input`/`ctx`. A separate test
   pins that `loc` *may* carry a caller-chosen key name (the intended field-path behavior).
4. Advertised output schema unchanged (flat, ADR-0113). *Test:* the existing `build_app`
   flat-output-schema test stays green; add an assertion that `detail` does not appear as a
   distinct advertised output property.

## Out of scope

- The typed `profile` param and `systems.profile_examples` discovery tool (#451).
- The synchronous transport bound (#452).
- Any worker-plane message rewriting or a general redaction pass (none added; ADR-0123).
- No DB migration (`detail` is a wire-only field; no persisted column).

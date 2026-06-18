# ADR 0166 — Self-correcting upload-declaration rejections + upload-vocabulary discovery

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0048](0048-external-build-artifact-ingestion.md)
  (the external-upload build lane and its declaration shape),
  [ADR-0019](0019-tool-response-envelope.md) (the `data`/`detail` split in the response
  envelope), [ADR-0123](0123-tool-error-detail-surfacing.md) (the no-leak seam:
  `detail` is suppressed only for `not_found`/`authorization_denied`, never for
  `configuration_error`), [ADR-0117](0117-projects-list-whoami.md) (the auth-only read
  posture), [ADR-0124](0124-provisioning-profile-discoverability.md) /
  [ADR-0160](0160-buildhost-source-kind-discovery.md) (the `*_profile_examples`
  discovery pattern the new tool mirrors).
- **Spec:** [`../specs/2026-06-17-upload-artifact-error-detail.md`](../specs/2026-06-17-upload-artifact-error-detail.md)
- **Issue:** [#551](https://github.com/randomparity/kdive/issues/551)

## Context

The external-upload build lane (`artifacts.create_run_upload`, ADR-0048) is the only
build path that bypasses the broken ephemeral guest agent, yet a black-box MCP client
cannot use it. Two compounding gaps:

1. `_validate_artifact_declarations` rejects an unaccepted artifact name with
   `_config_error(object_id, data={"reason": "bad_artifact_declaration"})`
   (`uploads.py:107-113`). The accepted vocabulary
   (`{effective_config, kernel, initrd, vmlinux}` for runs; `{rootfs}` for systems) is
   not in the response, and the envelope's `detail` is `null`. A user who declares a
   bzImage as `bzImage`/`vmlinuz` (the names the kernel build tree uses) gets no signal
   that the accepted name is `kernel`. The sibling raises — missing key, non-string /
   non-int field, malformed chunk (`uploads.py:106,113,142,146`) — are likewise silent
   about which field failed.
2. The accepted set is undiscoverable *before* an upload attempt. The codebase already
   has `runs.profile_examples` / `systems.profile_examples` discovery tools (ADR-0124,
   ADR-0160) the reporter praised, but nothing advertises the upload vocabulary.

A black-box test verdict: "Fixing this alone would likely have let me complete the live
dhash_entries=1 reproduction, since I had a valid bzImage + vmlinux in hand."

## Decision

### 1. Enrich every `bad_artifact_declaration` raise

Replace the four bare `bad_artifact_declaration` raises with a shared helper that puts
the failing field, the offending value (when safe), and the accepted vocabulary into the
response. The structured facts go in `data` (machine-actionable, never suppressed for
`configuration_error`); a fixed-template human string goes in `detail`. Concretely:

```json
{ "reason": "bad_artifact_declaration",
  "field": "name",
  "value": "bzImage",
  "accepted_names": ["effective_config", "initrd", "kernel", "vmlinux"] }
```

- `field` distinguishes the four failure points: `name` (top-level name unaccepted or
  not a string), `sha256` / `size_bytes` (missing or wrong type), `chunks` (malformed
  chunk sub-structure).
- `value` is included **only** when `field == "name"` *and* the offending value is a
  `str` of ≤64 chars. A non-string name, an oversized string, or a chunk failure carries
  no `value`. The offending name is user-supplied, so echoing it back is the
  self-correcting affordance the issue asks for; the length+type guard bounds what we
  echo so no large/binary payload reaches the client (the issue's explicit constraint).
- `accepted_names` is `sorted(allowed)` and is always present — it is the server's own
  vocabulary, not user input.
- `detail` is a stable template naming the field and the accepted set; it does **not**
  interpolate the offending value (that lives in `data.value`), keeping `detail`
  low-cardinality. `configuration_error` is not a no-leak-suppressed category
  (ADR-0123), so both `data` and `detail` survive to the client.

The accepted vocabulary threads through from the existing
`_UploadOwnerSpec.allowed_names`, so a run rejection lists the build vocabulary and a
system rejection lists `{rootfs}` — each error advertises exactly the set that owner
accepts.

### 2. Add `artifacts.expected_uploads`

A static, auth-only, `read_only` discovery tool that returns the accepted vocabulary per
upload owner-kind before any upload attempt, mirroring `runs.profile_examples`:

- No DB, no resolver — the vocabulary is a module constant
  (`_BUILD_ARTIFACT_NAMES` / `{rootfs}`).
- Auth-only (ADR-0117): `current_context()` gates the transport as defence-in-depth;
  no platform/project gate, no audit — the projection is the public name vocabulary,
  which leaks nothing project-scoped.
- Returns a `ToolResponse.collection` with one item per owner-kind (`run`, `system`);
  each item's `data` carries `owner_kind`, `accepted_names` (sorted), `create_tool` (the
  literal `artifacts.create_run_upload` / `artifacts.create_system_upload` identifier),
  and a per-name `descriptions` map (e.g. `kernel` → "Boot kernel image (bzImage)";
  `vmlinux` → "Uncompressed kernel ELF with DWARF for debug").

## Alternatives considered

- **`runs.expected_artifacts` (the issue's suggested name).** Rejected in favour of
  `artifacts.expected_uploads`: the vocabulary is owner-kind-specific and applies to
  both run and system uploads, and the upload tools live in the `artifacts.*` namespace.
  A runs-only tool would silently omit the rootfs vocabulary and split the discovery
  surface from the tools it describes.
- **Put the accepted set only in `detail`, as a JSON string.** Rejected: `data` is the
  machine-readable channel (ADR-0019); a stringified JSON blob in `detail` is harder to
  parse and duplicates structure. We use `data` for facts and `detail` for the human
  template.
- **Echo the offending value unconditionally.** Rejected: the issue calls out not
  leaking large/binary payloads. We echo only a short string name.
- **A schema enum on the `artifacts` field.** Rejected for now: the declaration is a
  free `Mapping`; an input-schema enum is a larger FastMCP change than #551 needs, and a
  discovery tool plus a self-correcting error already make the set knowable.

## Consequences

- The upload lane becomes self-correcting from a black-box client: a wrong name names
  itself and lists the accepted set, and `artifacts.expected_uploads` lets a cold agent
  learn the set up front.
- No schema/DB change, no migration. The `data` keys are additive; existing callers
  that read `data.reason` are unaffected.
- New tool count: the doc guard (ADR-0047) requires `artifacts.expected_uploads` to be
  fully documented and mapped to a behavior test; the generated tool reference is
  regenerated.
- Reduces the reporter's D7 (digest base64-vs-hex) confusion: once a rejection names the
  failing `field`, a mis-encoded digest no longer collides with the *name*-rejection
  path. A hex digest is still a valid `str`, so it passes declaration validation and any
  encoding mismatch surfaces later at the store, not here; no digest-encoding validation
  is added in this change (out of scope per the spec).

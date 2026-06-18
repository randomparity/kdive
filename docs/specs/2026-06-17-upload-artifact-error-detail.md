# Spec — self-correcting `bad_artifact_declaration` + upload-vocabulary discovery

- **Date:** 2026-06-17
- **Issue:** [#551](https://github.com/randomparity/kdive/issues/551)
- **ADR:** [ADR-0166](../adr/0166-upload-artifact-error-detail.md)

## Problem

`artifacts.create_run_upload` is the only build lane that bypasses the broken
ephemeral guest agent, but it is unusable from a black-box MCP client. Two gaps
compound:

1. **Opaque rejection.** A declaration with an unaccepted artifact name returns
   `configuration_error` with `data == {"reason": "bad_artifact_declaration"}` and
   `detail == null` (`uploads.py:107-113`). The accepted set
   (`{"effective_config", "kernel", "initrd", "vmlinux"}` for runs; `{"rootfs"}` for
   systems) is nowhere in the response. A user who declares a boot kernel as `bzImage`
   or `vmlinuz` gets no hint that the accepted name is `kernel`. The other
   `bad_artifact_declaration` sites (missing key, non-string/-int fields, malformed
   chunk) are equally silent about which field failed.
2. **Undiscoverable vocabulary.** The accepted set is not knowable *before* an upload
   attempt. There is no read tool mirroring `runs.profile_examples` /
   `systems.profile_examples` that advertises it.

## Decision (summary; full rationale in ADR-0166)

1. **Enrich every `bad_artifact_declaration` raise.** Add the offending field and the
   accepted vocabulary to the structured `data` payload and a human-readable `detail`
   string. `CONFIGURATION_ERROR` is not a no-leak-suppressed category (ADR-0123), so
   both survive to the client.
2. **Add `artifacts.expected_uploads`** — a static, auth-only, read-only discovery tool
   that returns the accepted artifact-name vocabulary per upload owner-kind (run /
   system) with a one-line description per name, so the set is knowable up front.

## Detail shape

The structured `data` for a name/type/key rejection becomes:

```json
{
  "reason": "bad_artifact_declaration",
  "field": "name",
  "value": "bzImage",
  "accepted_names": ["effective_config", "initrd", "kernel", "vmlinux"]
}
```

- `field` — which declared field failed: `name`, `sha256`, `size_bytes`, or `chunks`.
  To name the missing key precisely, the implementation replaces the tuple-unpack
  `try/except KeyError` with explicit per-key membership checks, so a missing-key
  rejection reports the specific absent key in `field`.
- `value` — the offending **name** string, included only when `field == "name"` and the
  value is a short string (≤64 chars). Never echo a non-string, an oversized string, or
  a binary payload (defends the no-leak / no-payload-echo guard from the issue).
- `accepted_names` — `sorted(allowed)`, always present (it is the vocabulary, not user
  input).
- `detail` — a fixed-template string, e.g.
  `artifact declaration rejected: field 'name' must be one of effective_config, initrd, kernel, vmlinux`.
  The offending value is *not* interpolated into `detail` (it lives in `data.value`),
  keeping `detail` a stable, low-cardinality template.

The chunk-level raises (`uploads.py:142,146`) set `field` to `chunks` (the malformed
sub-structure), `accepted_names` to the owner's vocabulary, and no `value`.

## `artifacts.expected_uploads`

- Static (no DB, no resolver). The vocabulary is a module constant.
- Auth-only (ADR-0117): `current_context()` for transport defence-in-depth, no
  platform/project gate, no audit — it leaks nothing project-scoped.
- `read_only` annotation, `maturity: "implemented"`.
- Returns a `ToolResponse.collection`: one item per owner-kind (`run`, `system`),
  each item's `data` carrying `owner_kind`, `accepted_names` (sorted), `create_tool`
  (the literal upload tool name), and a per-name `descriptions` map.

## Acceptance criteria (from #551)

- Declaring an artifact with an unaccepted name returns `configuration_error` whose
  `data` names the rejected value and lists the accepted names, and whose `detail` is
  non-null.
- A correctly-named `kernel` (+`vmlinux`) upload of the same locally-built kernel
  succeeds end-to-end (existing happy-path test stays green; assert it explicitly).
- Tests cover: unaccepted name (asserts offending value + accepted set + non-null
  detail), missing required field, non-string name (value omitted, accepted set still
  present), oversized name (value omitted), malformed chunk, the happy path, and the
  `artifacts.expected_uploads` projection.

## Out of scope

- No schema/DB change, no migration.
- D7 (digest base64-vs-hex): subsumed — once the error names the failing `field`, a hex
  digest no longer collides with the name-rejection path; no separate digest validation
  is added here.

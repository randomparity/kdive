# ADR 0436 — Reject chunked (multipart) rootfs uploads for local-libvirt Systems at declaration

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) (the #743 install
  path whose plain-SHA-256 verify is the constraint this decision honors),
  [ADR-0048](0048-external-build-artifact-ingestion.md) (the agent-upload transport and the
  System-owned `rootfs` upload window), [ADR-0104](0104-chunked-external-upload-reassembly.md) (the
  chunked/multipart upload mechanism this scopes out of the System path).

## Context

`artifacts.create_system_upload` and `artifacts.create_run_upload` share one declaration
validator, `_validate_artifact_declarations`. It accepts either a single-PUT declaration (rejected
above the 5 GiB `SINGLE_PUT_MAX_BYTES` single-object cap) or a **chunked** one (a `chunks` array →
multipart reassembly, ADR-0104, total cap `KDIVE_MAX_UPLOAD_BYTES` = 50 GiB). Multipart reassembly
is wired only for `runs.complete_build`, not for the System rootfs commit, and the validator had no
per-owner gate. So a chunked `rootfs` declaration was accepted, minted per-chunk presigned PUTs, and
only failed **late** — after the agent uploaded potentially many GiB — when `_commit_uploaded_rootfs`
HEADed the never-written final object key and raised `CONFIGURATION_ERROR: "upload-kind rootfs was
never uploaded"` (#1503).

The failure is not merely poorly-timed; it is unfixable without reworking the integrity model. A
reassembled multipart object exposes only a **composite** checksum (`<base64>-<N>`,
checksum-of-checksums), not a plain SHA-256 of the body. The #743 install path
(`rootfs_upload_fetch.py`, ADR-0434) verifies `sha256(body) == head.checksum_sha256`, which a
composite checksum can never satisfy. So even if reassembly were wired for the System path, the
verify would always reject the result.

## Decision

Reject a chunked System-rootfs declaration **at declaration time** (Option A), with a clear,
self-correcting message, instead of accepting it and failing late at provision.

`_UploadOwnerSpec` gains an `allow_chunks: bool = True` field. `_SYSTEM_UPLOAD` sets it `False`
(`_RUN_UPLOAD` keeps the `True` default). `_validate_artifact_declarations` takes a keyword-only
`allow_chunks: bool = True`; when a declaration carries `chunks` and the owner forbids them, it
returns a `configuration_error` with `reason="chunking_not_supported"` and the detail *"System
rootfs must be a single PUT <= 5 GiB; chunked/multipart upload is not supported (omit chunks and
declare a single-PUT upload)"* — evaluated at the **top** of the chunk branch, before per-chunk
validation, so no part URL is ever minted and no manifest is persisted. The pre-existing single-PUT
size guard (rejecting a rootfs over 5 GiB) is unchanged and stays covered.

The RUN owner is untouched: `runs.complete_build` still accepts a well-formed chunked declaration.
The guard is owner-scoped, not global.

## Consequences

- A chunked System-rootfs declaration now fails fast at `create_system_upload` with an actionable
  message, instead of after a large upload with a misleading "never uploaded" provision error.
- No schema, no migration, no MCP-surface addition — one owner-spec field and one validator branch.
- A System rootfs is capped at the 5 GiB single-PUT ceiling. A debug rootfs larger than that is not
  supportable until the integrity model is reworked (Option B: composite-aware or post-reassembly
  whole-object verification), which is out of scope here and would be its own ADR.

## Considered & rejected

- **(B) Wire multipart reassembly + composite-aware verification for the System path.** Rejected as
  out of scope: it requires the #743 install-fetch to verify against a per-part/composite scheme or
  recompute a whole-object hash after reassembly, rather than the object's stored `checksum_sha256`.
  That reworks the ADR-0434 integrity anchor for a >5 GiB rootfs use case with no current demand.
- **Do nothing (leave the late failure).** Rejected: the failure wastes a multi-GiB upload and its
  message ("never uploaded") misdirects the agent, which did upload — just to chunk objects the
  install path cannot read.

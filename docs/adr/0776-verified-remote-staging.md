# 0776 — Verified remote staging

## Status

Accepted (2026-10-16)

## Context

Authority-owned remote provisioning requires mechanically inspected catalog root facts and
a matching image digest. Stage-volume previously captured only the optional kernel config.

## Decision

Stage-volume requires a private self-contained qcow2 snapshot, bounded SHA-256 hashing and
mechanical root inspection before fresh-only upload of those exact bytes. Commit matching
digest/root_spec on the unchanged public config-managed staged row after successful upload.
Inspection and mandatory metadata persistence fail the command; kernel config stays advisory.
Reconcile preserves facts for unchanged staged volume declarations and invalidates root authority
on repointing. Plain staged rows remain unverified. No CLI flags or schema migration are added.

## Consequences

Staging needs local space for one image copy. Existing targets are refused without overwrite.
Post-upload persistence failure leaves remote bytes requiring operator inspection/cleanup or a
fresh target; a retry cannot attest an existing volume. Catalog facts describe stage-time bytes,
not ongoing remote overwrite detection. Separate authority-store digest/file checks still apply.

## Considered & rejected

- Keep remote authority provisioning unsupported. judgment: fails the selected supported path.
- Hash and inspect the mutable source separately from upload. judgment: cannot bind the facts
  to bytes after source replacement; private snapshot is the smaller coherent boundary.
- Attest a preexisting remote volume. judgment: lookup presence is no byte-identity proof.
- Enable remote images.publish. verified: ADR-0759 reserves that distinct publication boundary;
  the operator-approved #3218 scope excludes it.

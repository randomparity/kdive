# 0727 — Explicit root uv selection for lifecycle provisioning

## Status

Accepted (2026-10-07), following explicit operator approval of this interface and validation rule.

## Context

Issue #3069 showed that the host role installed uv in `/usr/local/bin`, while
Rocky's sudo PATH hid it from the lifecycle installer. The role already exposes
`live_vm_host_uv_bin` for its other venv caller; installation must consume the same
selection without inheriting the operator's PATH or changing sudo configuration.

## Decision

The installer accepts optional `--uv PATH`. The host play supplies the existing
`live_vm_host_uv_bin`. Before installer mutation, explicit selection must resolve
to an absolute executable regular file whose canonical file and ancestors are
root-owned and not group/other-writable. The installer executes that canonical
path. Missing/unsuitable paths fail with a remediation message; selection never
falls back when explicitly supplied. `uv --version` must succeed and identify uv.

Omitting the option retains the existing PATH-based caller contract. It still
requires an absolute executable regular file; the version check runs before
mutation. This does not introduce operator PATH inheritance into provisioning.

## Consequences

No second installation or repository-wide PATH policy is needed. Root-controlled
symlinks may resolve to a trusted canonical executable. Root administrators and
the installed tool itself remain trusted; this is not binary-content attestation.
Existing direct callers remain supported. Tool isolation remains #3070.

## Considered & rejected

- **Add `/usr/local/bin` as a fixed fallback.** judgment: it ignores the existing
  inventory-selected executable and can silently pick a different installation.
- **Pass the operator PATH through sudo.** verified: issue #3069 explicitly forbids
  requiring operator PATH inheritance or sudo-policy weakening for provisioning.

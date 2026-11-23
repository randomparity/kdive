# 0847 — Preserve remote capture failure context

## Status

Proposed

## Context

Remote capture uses one volume-delete helper for required stale pre-deletion and best-effort
cleanup. Suppressing its libvirt errors makes a failed pre-delete look successful. DAC discovery
also discards parser exceptions and reports them as an unusable parsed ownership label.

## Decision

Translate libvirt deletion errors once in the existing helper, with the volume name and chained
cause. Required pre-delete propagates; reclaim warns; attach rollback warns about a secondary
categorized deletion failure and re-raises its original failure. Catch expected XML parser and
defusedxml rejection exceptions explicitly in DAC discovery, retaining their cause under a
configuration error with reason `dac_xml_parse_failed`. Keep parsed-label checks unchanged.

## Consequences

A retry cannot proceed over a stale volume whose deletion failed. Best-effort cleanup remains
observable without replacing the primary attach error on libvirt deletion failure. XML parse
failure and unusable ownership are distinguishable. No new public signature, dependency, host
configuration or privilege is introduced; unexpected programming errors are not hidden.

## Considered & rejected

- **Suppress deletion everywhere.** verified: at54f756e00, the private capture-boundary
  reproduction reports `DID NOT RAISE` when prepare encounters a libvirt deletion failure.
  The permanent prepare regression retains this contract.
- **Separate deletion helpers for each caller.** judgment: caller-owned propagation versus
  best-effort handling is simpler than duplicating the same libvirt translation.

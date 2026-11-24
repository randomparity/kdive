# 0856 — Image denial auditing is best effort

## Status

Accepted (2026-11-23)

## Context

`images.upload` and `images.delete` catch project member-overreach and audit before returning
their denial envelopes. In source `4752dd15af34`, `audit_project_denial` lets connection,
transaction and write failures escape. A read-only actual-upload harness with a viewer context
and an injected `PoolTimeout` reproduced that escape before any service/store call.
The existing `DenialAuditMiddleware._denied` already preserves denials after audit failure.

## Decision

Catch ordinary `Exception` around the image helper's complete audit transaction and connection.
Warn with fixed text and tool only; omit raw exception text, traceback, stack and explicit
request identity/arguments. Preserve global logging enrichment. Keep cancellation and other
`BaseException` propagation. Keep the original denied response and attempt no mutation.

## Consequences

An unavailable audit store cannot replace this member-overreach denial with a server error.
An exit failure can leave the audit commit outcome unknown; there is no retry or durable
recovery. Successful mutation audits, admin/platform policy, denial shapes and concealment
stay unchanged. The change uses the existing helper and adds no runtime abstraction.

## Considered & rejected

- **Keep failure propagation.** verified: the real upload/viewer/failing-audit harness on
  `4752dd15af34` raised `PoolTimeout` before returning its existing denied response. This
  preserves the inconsistency with the middleware's ordinary-exception policy.
- **Move response handling into middleware.** judgment: preserving image object IDs and
  concealment through a dispatch redesign adds scope beyond the shared helper defect.
- **Retry denial writes.** judgment: exit failures can have ambiguous commit results, and a
  recovery/deduplication protocol is unnecessary for preserving an already-denied operation.

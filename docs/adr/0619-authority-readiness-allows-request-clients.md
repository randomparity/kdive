# 0619 — Authority readiness allows request clients

## Status

Accepted (2026-09-06)

## Context

ADR-0606 made the authority host reject every configured denied identity that belonged to either
the authority owner group or the authority client group. ADR-0611 subsequently introduced the
worker-local authority route. Its fixed workers must traverse the authority request directory and
open its group-owned socket, so deployment deliberately adds them to the authority client group.
Treating that transport membership as authority ownership prevents the service from becoming ready
whenever the local route is enabled.

The client group grants traversal to the request socket and read access to the worker's protected
TLS files. Mutual TLS and the active worker-incarnation credential still authenticate every
request. Authority state, journals, provider configuration, mutation socket, and provider objects
remain under the distinct authority owner identity and group.

## Decision

This decision supersedes only ADR-0606's requirement that a denied local identity not belong to
the authority client group. Authority-host readiness permits a configured denied identity to be a
member of that request-only group.

Readiness continues to require every denied identity to exist and rejects uid 0, the authority
uid, and membership in the authority owner group. It continues to require the exact owner, group,
mode, and absence of POSIX ACLs on every protected directory. Deployment continues to prove that
fixed workers cannot reach the authority's private provider paths, mutation socket, configuration,
or provider objects; the reconciler remains outside the client group and cannot traverse the
request path.

## Consequences

The local authority route can be enabled without making readiness reject its intended clients.
Client-group membership alone conveys no provider mutation authority: a worker still needs its
installed TLS identity, a current incarnation credential, and an accepted closed operation.

## Considered & rejected

- judgment: Remove fixed workers from the client group: they could not reach the ADR-0611 AF_UNIX
  route.
- judgment: Stop checking denied identities: missing identities, root or authority aliases, and
  authority owner-group membership would no longer fail closed.
- judgment: Permit ACLs on private directories: request transport access does not require changing
  the authority's private filesystem boundary.

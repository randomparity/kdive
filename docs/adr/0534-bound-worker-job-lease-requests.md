# 0534 — Bound worker job lease requests

## Status

Accepted (2026-08-02)

## Context

ADR-0018 introduced leased job claims and heartbeats, and ADR-0533 made Postgres the enforcement
boundary for credential-bound worker transitions. The guarded claim function still accepted zero,
negative, and arbitrarily long intervals. A non-positive claim is immediately reclaimable while its
first provider operation continues, which permits concurrent non-idempotent work. An unbounded lease
also lets one request postpone reclaim without limit. Python defaults do not constrain a worker-role
connection invoking the security-definer functions directly.

## Decision

Every guarded job claim and heartbeat accepts one lease duration expressed as a PostgreSQL `interval`.
The duration must be greater than zero and no greater than one hour. The function captures
`clock_timestamp()` once at each invocation and uses that PostgreSQL value for both `heartbeat_at` and
`lease_expires_at`; it does not use the caller transaction's possibly older timestamp.

The limit applies independently to each claim or heartbeat invocation for one exact job attempt. A
successful later heartbeat may start another lease of at most one hour; this is not a cumulative
per-job runtime limit. A missing, non-positive, or over-one-hour interval raises SQLSTATE `22023`
before state, attempt, heartbeat, or lease data changes. The caller recovers by retrying the same claim
or heartbeat with a valid interval. The production worker continues to request five minutes.

The one-hour ceiling leaves twelve times the normal lease for specialized callers while bounding a
single compromised or misconfigured request. Credential, active-incarnation, holder, exact-attempt,
state, and dispatch-lane checks remain unchanged.

## Consequences

Callers cannot use an already-expired claim to make the same durable job concurrently dispatchable.
A transaction opened long before a claim or heartbeat cannot backdate its lease. A live credential
holder can continue extending work through bounded heartbeats, as required for long provider
operations; termination evidence, not lease age, remains the artifact-fence recovery authority.

Custom worker integrations that request zero, negative, or more than one hour now fail before mutation
and must retry with a valid duration. Operators diagnosing SQLSTATE `22023` should correct that one
invocation's lease interval; no job repair or attempt rollback is required.

## Considered & rejected

- **Validate only in Python.** A worker-role connection can invoke the security-definer functions
  directly, so application validation is not the enforcement boundary.
- **Require only a positive interval.** This prevents immediate reclaim but leaves one request able to
  postpone recovery without limit.
- **Use `now()`.** PostgreSQL binds it to transaction start, so a caller can unintentionally create an
  already-shortened or expired lease by invoking the function from an older transaction.
- **Impose a cumulative job runtime limit.** Long builds and provider operations legitimately need
  repeated heartbeats. Bounding each extension prevents malformed single requests without inventing a
  new job-duration policy.

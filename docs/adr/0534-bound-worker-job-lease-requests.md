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
The function first acquires and validates every blocking ownership lock: the active incarnation lock
for a claim, and both that lock and the exact running job-attempt row lock for a heartbeat. It then
captures `clock_timestamp()` once immediately before mutation, applies the interval once to compute a
candidate deadline, and validates that deadline against the same reference. The candidate must be
strictly after the reference and no later than the reference plus one hour. The validated candidate is
persisted as `lease_expires_at`, and the reference is persisted as `heartbeat_at`; the function does
not use the caller transaction's possibly older timestamp or a timestamp captured before lock waits.

The computed deadline is the enforcement target, not PostgreSQL's abstract ordering of intervals.
Intervals may contain month and day fields whose timestamp-addition result depends on the calendar
and session time zone, including daylight-saving transitions, even when interval comparison
normalizes them to a value within one hour. Applying the interval first evaluates those semantics at
the post-lock database timestamp before enforcing the elapsed bound.

The limit applies independently to each claim or heartbeat invocation for one exact job attempt. A
successful later heartbeat may start another deadline at most one elapsed hour after its reference;
this is not a cumulative per-job runtime limit. A missing interval or one whose computed deadline is
not in the allowed range raises SQLSTATE `22023` before state, attempt, heartbeat, or lease data
changes. The caller recovers by retrying the same claim or heartbeat with an interval whose computed
deadline is valid. The production worker continues to request five minutes.

The one-hour ceiling leaves twelve times the normal lease for specialized callers while bounding a
single compromised or misconfigured request. Credential, active-incarnation, holder, exact-attempt,
state, and dispatch-lane checks remain unchanged.

## Consequences

Callers cannot use an already-expired claim to make the same durable job concurrently dispatchable.
A transaction opened long before a claim or heartbeat cannot backdate its lease. A live credential
holder can continue extending work through bounded heartbeats, as required for long provider
operations. Contention on the incarnation or exact heartbeat job row delays the operation but does
not consume the requested lease before mutation. Termination evidence, not lease age, remains the
artifact-fence recovery authority.

Custom worker integrations whose interval computes an expired or over-one-hour deadline now fail
before mutation and must retry with a valid duration. Operators diagnosing SQLSTATE `22023` should
correct that one invocation's lease interval; no job repair or attempt rollback is required.

## Considered & rejected

- **Validate only in Python.** A worker-role connection can invoke the security-definer functions
  directly, so application validation is not the enforcement boundary.
- **Require only a positive interval.** This prevents immediate reclaim but leaves one request able to
  postpone recovery without limit.
- **Compare the interval directly with zero and one hour.** PostgreSQL normalizes month and day fields
  for interval ordering but applies them with calendar and time-zone semantics during timestamp
  addition. A normalized in-range interval can therefore compute an expired or over-limit deadline.
- **Use `now()`.** PostgreSQL binds it to transaction start, so a caller can unintentionally create an
  already-shortened or expired lease by invoking the function from an older transaction.
- **Capture the database clock before ownership locks.** Advisory and row-lock waits are unbounded;
  they could consume the validated lease and let the function persist an already-expired deadline.
- **Impose a cumulative job runtime limit.** Long builds and provider operations legitimately need
  repeated heartbeats. Bounding each extension prevents malformed single requests without inventing a
  new job-duration policy.

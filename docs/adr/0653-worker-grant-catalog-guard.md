# 0653 — Worker grant catalog guard

## Status

Accepted (2026-09-13)

- **Issue:** #2349

## Context

The #2347 inventory says which handler-test modules execute handler acts through `kdive_worker`,
but an executed test path cannot cover every declared database write. A future write can be
declared without a corresponding worker grant and fail only in production. The #2345 baseline is
a broader sweep artifact with a known image-catalog leak; it is not this guard's declaration or
authority boundary.

Direct table privileges are not the only lawful authority. ADR-0629 intentionally leaves
`remote_module_attempt_obligations` without a worker write grant and permits its constrained
write through `public.discharge_system_mutation_obligations(uuid)`, a SECURITY DEFINER function
with a worker EXECUTE grant.

## Decision

Version the existing #2347 `worker_role_inventory.json` to carry this guard's declared
worker-write coverage entries. The migrated-catalog test consumes that one inventory. Direct
entries require the matching `has_table_privilege` result. SECURITY DEFINER entries require an
existing `pg_proc.prosecdef` function and `has_function_privilege` EXECUTE result, without a
table grant requirement.

## Consequences

- Revokes and later grants are evaluated from the effective migrated catalog rather than SQL text.
- A direct write without its declared privilege, and a fenced write without its lawful function
  authority, fail the schema-test lane before delivery.
- The guard covers only worker-write entries declared in the #2347 inventory. It does not claim
  universal handler coverage, audit other roles, change production grants, or remediate the
  baseline's image-catalog leak.

## Considered & rejected

- **Consume the #2345 baseline directly.** verified: `tests/jobs/worker_write_baseline.json`
  records a distinct broader sweep and includes `image-build.image-catalog.insert` as `LEAK`; the
  campaign controller assigned #2349's declaration boundary to the #2347 inventory instead.
- **Require table grants for every declared write.** verified: ADR-0629's accepted decision says
  neither worker role gains a table privilege for obligation discharge; a table-only check would
  reject the intended least-privilege boundary.
- **Add a parallel grant manifest.** judgment: two write declarations invite drift while the
  existing inventory can version its single test-owned contract.

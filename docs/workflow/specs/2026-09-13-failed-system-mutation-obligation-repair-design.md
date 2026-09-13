# Failed-System mutation-obligation repair design

## Goal

Repair the open mutation obligations that terminal failed Systems otherwise retain permanently.

## Scope and constraints

The reconciler is the owner. It must use the existing worker/reconciler SECURITY DEFINER discharge
function, retain the current per-System advisory lock and teardown settle guard, and leave the
`torn_down` lane, ordinary teardown, external-boot behavior, privileges, schema, and MCP contracts
unchanged. The pass stays bounded at 100 candidates and returns discharged obligation rows.

## Design

The existing `repair_leaked_mutation_obligations` candidate query changes from one terminal state to
the two terminal states `torn_down` and `failed`. Its locked re-read remains necessary because a
teardown can appear after candidate selection. Once locked and still eligible, the existing
idempotent system-wide terminal-escape discharge marks only currently open mutation obligations.

The existing repair catalog position after abandoned-job recovery is retained. Failed Systems have
no new transition and no provider operation: this is durable obligation repair only.

## Failure behavior

One candidate discharge failure is logged and does not starve other candidates; a later pass retries
it. An active or recently terminal teardown continues to defer the candidate. A second pass after a
successful discharge returns zero and does not rewrite the immutable discharge evidence.

## Verification

Focused reconciler integration tests seed failed Systems with open obligations and prove discharge,
failed-only selection, teardown deferral, real reconciler-role execution, and idempotency. Existing
ready-System and torn-down-System tests remain the regression boundary.

## Alternatives

ADR-0652 records why a separate lane, a retained-owner filter, and a synthetic failed-to-torn-down
transition are not used.

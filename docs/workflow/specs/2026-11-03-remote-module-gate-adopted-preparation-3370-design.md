# Remote module attempt gate accepts an adopted preparation (#3370)

## Problem

`open_external_boot_remote_module_attempt` (migration 0146) opens a remote module attempt only
when the current generation has its own `materialize`/`result_committed` audit row. A retry
generation adopts the first generation's preparation and commits no such row, so the gate returns
NULL for every generation above 1 and the retried PREPARE ends `superseded`.

## Design

Migration `0187_remote_module_attempt_adopted_preparation.sql` replaces the function
(`CREATE OR REPLACE`, same signature, grants and fences). Only the audit `EXISTS` changes. The
row must match the current authority on system, activation, run, plan identity, provider kind and
authority instance, and its generation must be at most the current generation. The activation
`preparing`/materialization check and the head, lease, acknowledgement and successor checks are
untouched.

Chosen over writing an audit outcome at adoption: adoption commits nothing, so a new row would
record an event that did not happen, and it would need a change in the adopt path (excluded from
this issue). The identity match keeps the gate tied to the same activation and plan.

ADR-0585 gains an amendment recording this and replacing the "tracked as #3370" caveat in the
#3348 amendment's account of behavior.

## Failure model

- No committed preparation for the activation and plan, any generation: NULL (refused).
- Committed row under a different plan identity or authority instance: NULL.
- Committed row from a later generation than the caller's: NULL.
- Stale or superseded caller (successor generation, expired lease, wrong head): NULL, unchanged.
- Same-generation own row (the pre-0187 case): still admitted.
- Builds older than 0187: unchanged behavior; migration is forward-only.

## Tests

`tests/db/test_external_boot_authority_preparation_migration.py` moves the authority to
generation 2 over the generation-1 committed row and asserts the attempt opens, then asserts a
refusal for each of a different plan identity, a different authority instance and a later-
generation row. Verified red without the migration. Four migration-window tests gain the 0187 row.

## Open item

Live remote-lane proof (a Run boot whose first PREPARE fails and retries) needs a lab grant;
reported as open if not exercised.

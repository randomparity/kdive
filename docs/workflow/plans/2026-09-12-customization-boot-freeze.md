# Plan: fail fast on a terminal customization manager freeze

## Scope

Implement #2423 within the frozen `q2423-6f3a91c4` charter. The permitted production surface is
`customization_boot.py`; the only test surface is its focused unit module. Fedora/systemd repair
and all TCG deadline changes remain excluded.

## Steps

1. Add line-aware predicates requiring both full terminal manager lines. Keep the existing
   precedence: ok marker, explicit fail marker, existing genuine kernel faults, terminal manager
   pair, then pending.
2. Add focused tests for the complete pair, each partial line remaining pending, and successful
   marker precedence. Add an orchestration test that asserts `PROVISIONING_FAILURE`, console-tail
   preservation, teardown, and no sleep after the pair.
3. Run the focused module, then `just lint` and `just type`; run `just ci` before delivery.
4. Review the diff against ADR-0345 and the frozen failure model. Verify no timeout calculation,
   guest image, or external-system behavior changed.

## Contract checks

| Contract | Focused test | Task test |
|---|---|---|
| Observed terminal pair is failed | classifier pair test | applicable |
| Either line alone is not terminal | classifier partial-line parametrization | applicable |
| Success remains authoritative | classifier precedence test | applicable |
| Failure is immediate and categorized | orchestration/no-sleep test | applicable |
| Real Fedora/TCG boot | not applicable: approved upstream/hardware exclusion | not applicable |

## Rollback

Reverting the classifier predicate and its tests restores prior polling behavior without data
migration or external state cleanup.

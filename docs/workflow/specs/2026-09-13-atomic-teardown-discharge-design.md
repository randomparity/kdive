# Atomically finalize ordinary System teardown

Issue: [#2370](https://github.com/randomparity/kdive/issues/2370). Decision:
[ADR-0650](../../adr/0650-atomic-ordinary-teardown-terminal-commit.md).

## Problem

Ordinary teardown publishes `torn_down` before provider cleanup and mutation-obligation discharge.
An interruption can therefore leave `torn_down` beside an open obligation, permanently retaining
the module volumes because ordinary teardown is then idempotently short-circuited.

## Design

Add `SystemState.TEARING_DOWN = "tearing_down"` and a matching additive schema migration. Every
ordinary teardown entry state that previously led directly to `torn_down` now first enters
`tearing_down` under the existing System advisory lock. The state has no admission path back to a
provisionable state and only transitions to `torn_down`.

The handler resolves the provider after it commits the fence, then performs the provider teardown.
Only after that call succeeds does its final locked transaction call
`worker_discharge_system_mutation_obligations`, transition to `torn_down`, and audit the terminal
edge. Any exception rolls back the final transaction; the durable state remains `tearing_down` and
the existing job retry route remains available.

The state is non-terminal. The provision-result compensator treats it as a teardown fence and
reaps a domain a slow provision created after the fence committed. It is included in allocation
occupancy and the rootfs always-pin set, but excluded from console scheduling and all terminal-state
sets. Direct state consumers, generated CLI/reference artifacts, and tests are updated in the same
change.

## Success criteria

1. Ordinary teardown commits `torn_down` and mutation discharge atomically after provider success.
2. A provider or discharge failure leaves `tearing_down`, never a false terminal result.
3. A concurrent provision cannot retain a domain after the teardown fence is visible.
4. `tearing_down` is non-terminal, capacity-occupying, rootfs-pinning, and excluded from
   terminal/console state sets.
5. Existing external-boot and authority-owned terminal paths remain unchanged.
6. `just ci` is green after generated artifacts are refreshed.

## Scope

Included: the ordinary worker handler, shared System state model and database constraint, direct
classification consumers, generated references, focused integration/adversarial tests, ADR-0650.
Excluded: historical-row repair (ADR-0634), external-boot/authority-owned terminal behavior, role
grants, and changes to mutation-obligation semantics.

## Failure and concurrency model

`tearing_down` is committed before the external provider call, so slow provision commits observe a
non-provisionable state. The System advisory lock serializes each state transition and final
discharge. The provider call remains outside a database transaction; a crash there leaves the
fence, allowing the durable teardown job/reconciler to retry rather than declaring completion.
The final transaction is all-or-nothing: a discharge failure cannot expose `torn_down`.

## Validation

Focused tests prove the post-provider atomic terminal commit, rollback on injected discharge
failure, retained provider-failure state, and the existing concurrent provision race. State-set
tests prove classification. Regeneration plus `just ci` proves generated artifacts and repository
guardrails.

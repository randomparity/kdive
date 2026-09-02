# 0003 — The three external-boot agent contracts await their executor

## Status

Open
review-by: 2027-03-02

## Concern

`runs.release_external_boot`, `systems.resolve_external_boot_conflict`, and
`ops.resolve_recovery_orphan` are registered MCP tools that never perform the operation
they name. Each resolves its object, enforces its role, runs the System-wide external-boot
admission matrix, and then returns `configuration_error` with
`reason=recovery_executor_unavailable`. They declare ADR-0175 `partial` maturity with
`reason=degraded_stub`, and their wrapper docstrings say so in the prose an agent reads at
call time.

That is deliberate and was authorized: the operator amended issue #2117's completion
criterion on 2026-09-02 after an adversarial pass established that no truthful activation
transition exists on this branch. `ExternalBootActivationRepository` has no production
importer; `allocate_external_boot_authority` in `0122_external_boot_authority.sql` is gated
on `pg_has_role(session_user, 'kdive_worker', 'member')` and revoked from `kdive_server`,
the role the MCP server runs as; and `ExternalBootAuthorityMarkerV1`'s non-optional
`provider_kind` and `authority_instance` are on neither `ExternalBootActivation` nor
`ExternalBootReservation`. A tool that committed `active -> recovering` under those
conditions would open a one-way door: nothing on this branch can move an activation out of
`recovering`, and the matrix would then deny every operation on that System except
`systems.teardown`.

The debt is not the `configuration_error` — that response is truthful and disclosed. The
debt is that **the obligation to flip these three tools live is recorded only on #2117's
comment thread.** Issue #2118, which owns the executor, names none of the three tool names,
`recovery_executor_unavailable`, or the promotion anywhere in its body. An implementer
closing #2118 against its own acceptance criteria would land the handlers and leave three
registered tools still reporting their executor absent, with nothing in #2118 to catch it.

## Why deferred

The executor is out of scope for #2117 by the same amendment that authorized the stubs.
Activation lifecycle execution and reconciliation belong to #2118: the recovery job
handler, the worker claim path for authority-marked payloads, the `kdive_worker`-side
authority allocation, and the deadline that fails a stalled attempt forward. #2117 cannot
supply any of them without absorbing #2118, and #2118 declares itself blocked by #2117
because its handlers must enforce #2117's admission matrix. The amendment broke that cycle
by splitting the matrix from the executor, not by moving the executor.

This record exists because the split left an obligation with no home in the owning issue.

## Non-regression boundary

- All three tools must keep returning a failure envelope while the executor is absent. None
  may report success, and none may commit an activation transition. Two gates hold this:
  `tests/services/external_boot/test_recovery_requests.py` asserts the
  `external_boot_activations` row is unchanged field for field after all three run against a
  seeded `active` activation, and asserts the module's import closure reaches no
  activation-writing name.
- The `partial` maturity metadata and the docstring disclosure must stay in step with the
  behavior. `tests/mcp/lifecycle/test_external_boot_contracts.py` asserts both.
- The admission matrix must keep denying independently of these three tools. It is a guard
  and does not depend on the executor.

## What would resolve it

#2118 lands the external-boot recovery job handler and the worker claim path for
authority-marked payloads, then flips all three tools from the `configuration_error` branch
to the live transition — removing the `degraded_stub` maturity metadata and the docstring
disclosure with it, and adding the `idempotency_key` parameter and the absolute
`recovery_readiness_deadline` that the live contract needs and this one deliberately omits.

Done when calling `runs.release_external_boot` on an `active` activation returns a job
envelope whose recovery attempt a worker claims and completes, and when no registered tool
still returns `reason=recovery_executor_unavailable`.

## Provenance

target: src/kdive/services/external_boot/recovery_requests.py
target: src/kdive/mcp/tools/lifecycle/runs/registrar.py
target: src/kdive/mcp/tools/lifecycle/systems/registrar.py
target: src/kdive/mcp/tools/ops/security/breakglass.py
Found by the `$oathbind` scope audit on the #2117 branch on 2026-09-02 (finding F2), which
fetched #2118's body and confirmed the promotion obligation appears nowhere in it. The
obligation itself was recorded by the operator on #2117 the same day.
tracker: #2118

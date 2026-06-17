# ADR 0129 — `systems.teardown` requires project admin only

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

`systems.teardown` runs the three-check destructive-op gate
(`assert_destructive_allowed`, ADR-0006/0020/0038): a destructive op is allowed only when
**all three** independent checks pass — the allocation's `capability_scope.destructive_ops`
lists the op, the caller holds the required role on the allocation's project, and the
controlling profile opted the op in.

Driving the MCP surface end-to-end (#463, companion to #460/ADR-0128) surfaced a denial: a
demo token holding `admin` on project `demo` ran `systems.teardown` on a System in project
`demo` and got a bare `access denied`. `ops.force_teardown` (break-glass, `platform_admin`)
succeeded on the same System, so cleanup was possible — but the ordinary project-scoped
teardown was refused for a token that administers the System's own project.

Investigation found the cause is **structural, not a per-token RBAC mismatch**:

- Admission hard-codes `capability_scope={}` on every granted Allocation
  (`services/allocation/admission/core.py:461,606`).
- **No production code path ever writes `destructive_ops` into an Allocation's
  `capability_scope`.** Only tests populate it, via raw SQL `UPDATE`
  (`tests/mcp/lifecycle/test_systems_tools.py:1027`, `tests/integration/...`). The live-stack
  fixtures seeding it directly is what kept this gap out of CI.
- ADR-0020 explicitly deferred populating "`capability_scope`'s typed interior" to "the
  allocation issue that owns it" — that follow-up was never built.

Consequently the gate's **first check can never pass in production**, so `systems.teardown`
is denied for *every* caller on the normal MCP path — including a project admin tearing down
their own System. The demo admin's denial is this gap. The same dead first check sits under
`systems.reprovision`, `control.power` (off/cycle/reset), and `control.force_crash`, which
share `assert_destructive_allowed`; that wider gap is recorded as a follow-up (see
Consequences), not resolved here.

A secondary defect compounds it: the teardown handler catches `DestructiveOpDenied` but
discards `denied.missing`, returning a bare `ToolResponse.failure(..., AUTHORIZATION_DENIED)`.
The no-leak seam (ADR-0123) further fixes `detail` to the constant `"access denied"`, so the
caller cannot tell a role gap from a capability/opt-in gap. The tool's own description
("Requires admin and destructive-op opt-in") omits the `capability_scope` requirement.

## Decision

**1. `systems.teardown` requires project `admin` only.** Replace the three-check gate call in
`teardown_system` with a direct `require_role(ctx, allocation.project, Role.ADMIN)`. The
existing ownership checks (`system.project in ctx.projects`,
`allocation.project in ctx.projects`) stay. Teardown is the normal lifecycle terminus of a
caller's own granted System, so the two dropped layers add no defensible safety here:

- `capability_scope` is the *allocation-grant* layer — "this lease may perform these
  destructive ops." Requiring a pre-granted right to **end the lease itself** is circular, and
  the layer is un-grantable today regardless.
- `profile_opt_in` guards against driving a System into a *fragile runtime state* it was not
  configured for (the value `force_crash` gets from it). Teardown destroys the System; there
  is no "configured for teardown" notion, so the opt-in adds ceremony without protection.

`admin` (not `operator`) remains the bar: teardown destroys project resources, which is
administration, and the issue states an own-project admin should be permitted.
`force_crash`, `power`, and `reprovision` keep the full three-check gate unchanged. The
admin role check runs **before** the existing idempotent `torn_down` short-circuit
(`admin.py:250-256`), preserving today's authz-before-state order — so a non-admin caller
never receives the `torn_down` success envelope that would leak a System's terminal state and
project for a System they do not administer.

**2. Surface the denied checks on the envelope, and catch the role denial locally.** A denied
destructive op returns the failed check names in structured `data`
(`data["missing_checks"]`, e.g. `["capability_scope"]`). The check names are a **closed enum
of policy tokens** (`capability_scope`, `admin_role`, `operator_role`, `profile_opt_in`)
carrying no resource identifiers, so this does not violate the no-leak seam (ADR-0123) —
which suppresses `detail`, not `data`.

Teardown's new authority check, `require_role(ctx, allocation.project, Role.ADMIN)`, raises
`RoleDenied`, which the dispatch-boundary `DenialAuditMiddleware`
(`src/kdive/mcp/middleware.py:149-158`) would otherwise catch — auditing it as a
denial-shaped `record_denial` row and returning a bare `authorization_denied` envelope keyed
on the **tool name**. That path would drop `missing_checks`, regress the envelope's
`object_id` from `system_id` to `"systems.teardown"`, and silently move the teardown-denial
audit off the transition-shaped `_audit_destructive_denied` row that today's code (and the
other three gated ops) emit. So `teardown_system` **catches `RoleDenied` itself**, audits via
the existing `_audit_destructive_denied` helper (`transition=teardown:denied`,
`args.missing=["admin_role"]` — the same row shape as today), and returns the `system_id`-keyed
`authorization_denied` envelope with `data["missing_checks"]=["admin_role"]`. Because the
handler returns an envelope rather than re-raising, `DenialAuditMiddleware` never sees the
exception, so there is exactly one audit row (no double-write).

This answers the issue's "say *why*" ask. For the three still-gated ops the surfaced
`missing_checks` is **diagnostic, not remediable** — it will always include `capability_scope`
(structurally unpopulated; see Consequences) and the caller has no production path to grant
it, so the envelope's `suggested_next_actions` stays empty there. Likewise a non-admin
teardown denial offers no caller-actionable next step (the `ops.force_teardown` recovery path
needs `platform_admin`, which the denied caller by definition lacks), so its
`suggested_next_actions` is also empty; `missing_checks` is the explanatory signal, not a
remediation affordance.

**3. Correct the tool description** to "Requires admin on the System's project," matching the
new authority.

## Consequences

- A project admin can tear down their own project's Systems over the normal MCP path; the demo
  repro (#463) is unblocked without break-glass.
- `ops.force_teardown` is unchanged and remains the cross-project / non-admin break-glass path.
- Teardown no longer reads the provisioning profile or resolves a provider runtime (both were
  only used for the dropped opt-in), so `teardown_system` drops its `resolver` parameter and
  the dead `_teardown_opt_in` helper — flagged dead code is removed, not left as a shim.
- A denied destructive op (any of the four) now self-explains via `missing_checks`, ending the
  bare-`access denied` dead end that cost a multi-hour investigation. For the three still-gated
  ops this is an explanation, not an unblock: the agent learns *which* check failed but, until
  the follow-up below lands, cannot satisfy a missing `capability_scope`.
- **Follow-up (out of scope for #463):** `reprovision`, `power` (off/cycle/reset), and
  `force_crash` are still structurally denied on the normal path because nothing populates
  `capability_scope.destructive_ops`. Resolving that — either an allocation-request grant path
  or a per-op gate revision — is a separate design tracked in #465. This ADR does not decide it.
- The advertised output schema is unchanged (stays flat, ADR-0113); `missing_checks` rides the
  existing `data` payload.

## Considered & rejected

- **Populate `capability_scope.destructive_ops` at allocation-request time** (build the
  deferred allocation grant; keep all three checks). Most faithful to ADR-0006/0020 and would
  revive all four ops at once, but is the largest surface (new request-grant semantics, who
  decides the scope, `systems.profile_examples`/onboarding updates) and re-opens the awkward
  "must the agent pre-declare teardown intent when it requests the lease?" question. Deferred
  to the follow-up that owns the wider gate gap; teardown should not wait on it.
- **Admin + profile opt-in for teardown** (drop only the un-grantable `capability_scope`
  check). Keeps a defense-in-depth layer, but for teardown the opt-in protects nothing (see
  Decision §1) and forces every teardown-capable profile to list `teardown` in
  `destructive_ops`, adding config burden for no safety gain.
- **Observability only** (treat the gate as intended; surface `missing_checks`, fix the
  description, leave teardown deny-by-default). Lowest risk, but leaves "an own-project admin
  cannot tear down their own System" — contradicting the issue's "should be permitted" and
  mislabeling a dead tool as working. The observability part is adopted; the do-nothing-on-
  authority part is not.
- **Relax teardown to `operator`.** Rejected: teardown destroys project resources (more
  consequential than in-place reprovision, which is operator-gated as "iterating on your own
  System"); the issue names admin as the intended bar.

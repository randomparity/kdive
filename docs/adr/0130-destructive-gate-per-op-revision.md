# ADR 0130 — destructive gate drops the un-grantable `capability_scope` check

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

The destructive-op gate (`assert_destructive_allowed`, `src/kdive/security/authz/gate.py`,
ADR-0006/0020/0038) allows `systems.reprovision`, `control.power` (off/cycle/reset), and
`control.force_crash` only when **all three** independent checks pass: the allocation's
`capability_scope["destructive_ops"]` lists the op, the caller holds the required role on the
allocation's project (`admin`, or `operator` for reprovision per ADR-0038 §3), and the
controlling provisioning profile opted the op in.

Admission hard-codes `capability_scope={}` on every Allocation
(`services/allocation/admission/core.py:461,606`) and **no production code path writes
`destructive_ops`** — only tests do, via raw SQL `UPDATE`. ADR-0020 deferred populating
"`capability_scope`'s typed interior" to "the allocation issue that owns it"; that follow-up was
never built. So the gate's first check can never pass in production, and all three ops are
denied for every caller on the normal MCP path. ADR-0129 dropped `systems.teardown` to a single
admin-role check and named this wider gap as a follow-up tracked in #465; it also added
`data["missing_checks"]` so the gap is observable.

The remaining two checks are both satisfiable in production:

- **role** — RBAC on the System's project (real authorization the caller cannot grant itself).
- **profile opt-in** — `profile.provider.<runtime>.destructive_ops`, a deny-by-default list the
  agent supplies in the provisioning profile at `systems.provision` time (ADR-0028 §2). An
  absent or empty list refuses the op.

So the only dead check is `capability_scope`.

This decision resolves the #465 follow-up. See the spec
`../specs/destructive-gate-per-op-revision.md` and the gate invariant in `../specs/top-level-design.md`
("Destructive-op gate").

## Decision

**We will drop the `capability_scope` check from `assert_destructive_allowed`, making the gate a
two-check policy — required role and profile opt-in — and remove the now-dead `capability_scope`
column, model field, admission writes, and gate helper.**

1. `assert_destructive_allowed` evaluates only the role check and the profile opt-in. Its
   `missing` list (surfaced as `data["missing_checks"]`) now draws from the closed enum
   `{admin_role, operator_role, profile_opt_in}`; `capability_scope` is removed from the enum.
   Check order is role then opt-in. The `DestructiveOp` dataclass keeps `kind` and
   `profile_opt_in`; the gate's `_scope_permits` helper and `_DESTRUCTIVE_OPS_KEY` are deleted.

2. The `Allocation.capability_scope` field, the `allocations.capability_scope` column (dropped in
   migration `0036`), its entry in the repository `json_columns` set, and the two
   `capability_scope={}` literals in admission are removed. This is dead state (always `{}`, no
   production reader after step 1), removed rather than left as a shim (replace-don't-deprecate).

3. The three handlers keep their existing structure — they already resolve the profile opt-in,
   bind the role to the target System's project after the in-project check, catch
   `DestructiveOpDenied`, audit `transition=f"{op}:denied"` with `args.missing`, and return the
   `system_id`-keyed `authorization_denied` envelope with `data["missing_checks"]`. Only the set
   of checks the gate runs changes, so the audit-row shape and envelope are unchanged except that
   `capability_scope` can no longer appear in `missing`.

4. `suggested_next_actions` stays empty for these denials (ADR-0129 precedent). A missing role is
   not caller-remediable; a missing opt-in is remediable only by re-provisioning with an updated
   profile (multi-step, conditional), so it is documented in prose and the tool descriptions
   rather than advertised as a single-tool affordance.

The role factors are unchanged: `admin` for `power`/`force_crash`, `operator` for `reprovision`
(ADR-0037/0038). `systems.teardown` (ADR-0129), `control.power on`, and every non-destructive
path are untouched.

## Consequences

- The three ops are grantable on the normal MCP path: a caller with the required role and a
  System provisioned with the op in its profile `destructive_ops` succeeds without raw-SQL
  seeding. The #465 dead-tool gap closes for all four destructive ops.
- The gate is simpler (two checks) and both checks are real boundaries; the gate documentation
  and tests that asserted a three-check model are revised to two.
- The denial enum loses `capability_scope`; tests asserting it appears in `missing_checks` are
  updated, and the `_common.py` docstring enum is corrected.
- Migration `0036` drops `allocations.capability_scope` (forward-only, ADR-0015). The dropped
  data is always `{}` in production, so no data is lost. Tests that seeded `destructive_ops` via
  raw SQL no longer compile against the column and are rewritten to drive the live opt-in path.
- The deferred ADR-0020 allocation-grant layer is now explicitly **not** built; the typed
  interior of `capability_scope` is removed rather than populated. A future externally-granted
  capability model, if ever needed, starts from a clean slate with a new ADR.
- The advertised flat output schema (ADR-0113) is unchanged; `missing_checks` rides `data`.

## Alternatives considered

- **Allocation-request grant path** (build the deferred ADR-0006/0020 grant: add a
  `requested_destructive_ops` field to `allocations.request`, have admission write
  `capability_scope["destructive_ops"]`, keep all three checks). Most faithful to the original
  design and would populate the layer rather than delete it, but the field is **self-declared by
  the requester** — the check would verify only that the agent earlier said it might do the op,
  not an externally-imposed authorization, so it adds ceremony without a boundary beyond role +
  profile opt-in. It is also the larger surface (request schema, who-decides-the-scope policy,
  `systems.profile_examples`/onboarding updates) and re-opens the "pre-declare destructive intent
  at lease time" question ADR-0129 flagged. Rejected: cost without a security gain over the
  two-check gate.
- **Externally-granted capability model** (an admin pre-authorizes what a project's allocations
  may do, populating `capability_scope` from outside the requester). This would be a real
  boundary, but no user needs project-level destructive-op governance today; it is a speculative
  feature and a much larger surface. Deferred to a future ADR if a concrete need arises.
- **Keep `capability_scope`, auto-populate it at admission** (e.g. from the profile or role).
  Collapses the check into the other two — it would always agree with them — so it is redundant
  state to maintain. Removing it is cleaner than auto-filling it.
- **Admin + profile opt-in via the gate, but leave the column** (drop only the check, keep the
  field). Leaves dead state behind (`{}` forever, no reader), contradicting
  replace-don't-deprecate. Rejected for the same reason ADR-0129 removed its dead opt-in helper.
- **Observability only** (surface `missing_checks`, fix descriptions, leave the ops denied).
  ADR-0129 already adopted the observability half for the shared gate; doing only that here would
  leave three tools permanently dead, contradicting #465's intent to revive them.

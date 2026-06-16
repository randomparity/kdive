# Destructive-gate per-op revision — revive reprovision/power/force_crash (#465)

- **Issue:** #465 (spun out of #463 / ADR-0129)
- **ADR:** [0130](../adr/0130-destructive-gate-per-op-revision.md)
- **Status:** Draft

## Problem

The three-check destructive-op gate (`assert_destructive_allowed`,
`src/kdive/security/authz/gate.py`, ADR-0006/0020/0038) allows a destructive op only when
**all three** independent checks pass:

1. `capability_scope` — the allocation's `capability_scope["destructive_ops"]` lists the op.
2. role — the caller holds the required role (`admin`, or `operator` for reprovision).
3. `profile_opt_in` — the controlling provisioning profile opted the op in.

Admission hard-codes `capability_scope={}` on every Allocation
(`src/kdive/services/allocation/admission/core.py:461,606`) and **no production code path
ever writes `destructive_ops`** into it — only tests do, via raw SQL `UPDATE`. So check 1
can never pass in production, and `systems.reprovision`, `control.power`
(`off`/`cycle`/`reset`), and `control.force_crash` are denied for **every** caller on the
normal MCP path. Only the seeded `live_stack`/integration fixtures pass, which is why the gap
never failed CI.

ADR-0129 fixed `systems.teardown` by dropping it to a single `require_role(ADMIN)` and named
this wider gap as the follow-up tracked here. It also added `data["missing_checks"]` to denied
destructive ops, so the gap is observable.

## Goal

`systems.reprovision`, `control.power` (off/cycle/reset), and `control.force_crash` are
grantable on the normal MCP path — deny-by-default, but satisfiable by a caller who holds the
required role on the System's project **and** provisions the System with a profile that opts
the op in. No new self-declared allocation-grant field is introduced.

## Decision (summary; full rationale in ADR-0130)

Drop the structurally-dead `capability_scope` check from the gate. The gate becomes a
**two-check** policy: required role **and** profile opt-in — both independent, both satisfiable
in production. The `capability_scope` column, model field, admission writes, and the gate's
`_scope_permits` helper are removed (dead code: always `{}`, no production reader after this
change). This mirrors the ADR-0129 teardown precedent rather than building the deferred
allocation-grant path.

### Why not the allocation-request grant path

The alternative — add a `requested_destructive_ops` field to `allocations.request` and have
admission write `capability_scope["destructive_ops"]` — keeps all three checks but the field is
**self-declared by the requester**. The check would only verify "the agent previously said it
might do this," not an externally-imposed authorization, so it adds ceremony without a security
boundary beyond role + profile opt-in. It is also the larger surface (request schema, scope
policy, onboarding/`profile_examples` updates) and re-opens the "pre-declare destructive intent
at lease time" question ADR-0129 already flagged. Rejected; see ADR-0130.

## Affected behavior, after the change

| Op | Role factor | Other check | Grantable in production? |
|----|-------------|-------------|--------------------------|
| `control.power` off/cycle/reset | `admin` | profile opt-in (`power` in profile `destructive_ops`) | yes |
| `control.force_crash` | `admin` | profile opt-in (`force_crash`) | yes |
| `systems.reprovision` | `operator` | profile opt-in (`reprovision`) | yes |
| `systems.teardown` | `admin` | none (ADR-0129) | yes (unchanged) |
| `control.power on` | `operator` | none | yes (unchanged; not gated) |

`profile_opt_in` resolves from the System's provisioning profile
(`profile.provider.<runtime>.destructive_ops`, a list the agent supplies at `systems.provision`
time — ADR-0028 §2). It is deny-by-default: an absent or empty list refuses the op.

## Denial envelope and remediation

A denied op returns `authorization_denied` with `data["missing_checks"]` naming the failed
checks from a **closed policy enum**, now `{admin_role, operator_role, profile_opt_in}`
(`capability_scope` is removed from the enum). The token carries no resource identifier, so the
no-leak seam (ADR-0123) is untouched — it suppresses `detail`, not `data`.

`missing_checks` is the **diagnostic** signal; `suggested_next_actions` stays empty for these
denials (consistent with ADR-0129):

- A missing `admin_role`/`operator_role` offers no caller-actionable next step — the caller
  lacks the role and cannot grant it to itself.
- A missing `profile_opt_in` is remediable, but only by re-provisioning the System with a
  profile whose `destructive_ops` lists the op (via `systems.reprovision` if reprovision is
  itself opted in, else teardown + fresh `systems.provision`). That is multi-step and
  conditional, not a single literal next tool, so it is documented in prose and the tool
  descriptions rather than advertised as an affordance.

## Acceptance criteria

1. With `capability_scope` removed, a caller holding `admin` on the System's project and a
   System whose profile lists `force_crash` in `destructive_ops` can `control.force_crash` on
   the normal MCP path (no raw-SQL seeding). Same for `power` off/cycle/reset (admin) and
   `reprovision` (operator).
2. A caller missing the role is denied with `missing_checks=["admin_role"]` (or
   `["operator_role"]`), and the denial audit row keeps today's shape
   (`transition=f"{op}:denied"`, `args.missing=[...]`).
3. A caller with the role but a profile that does not opt the op in is denied with
   `missing_checks=["profile_opt_in"]`.
4. A caller missing both is denied with `missing_checks=["<role>_role", "profile_opt_in"]`
   in check order.
5. `capability_scope` no longer exists on the `Allocation` model, the `allocations` table
   (migration `0036`), the repository `json_columns` set, or admission's insert paths; the
   gate's `_scope_permits` helper and `_DESTRUCTIVE_OPS_KEY` are gone.
6. The `missing_checks` closed enum documented in `mcp/tools/_common.py` no longer lists
   `capability_scope`.
7. No test seeds `capability_scope` via raw SQL; tests drive the two live checks directly.
8. `teardown`, `power on`, and every non-destructive path are behaviorally unchanged.

## Out of scope

- Any externally-granted (admin-issued, not self-declared) capability model. No users need it
  today; building it would be a speculative governance feature.
- Changing the role factors (admin for power/force_crash, operator for reprovision) — ADR-0037/
  0038 settled those; this change does not reopen them.
- The advertised flat output schema (ADR-0113) is unchanged; `missing_checks` rides `data`.

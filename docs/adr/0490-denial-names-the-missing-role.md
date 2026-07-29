# ADR 0490 — A denial names the missing role in `data`, not in `detail`

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1642
- **Reaffirms:** [ADR-0123](0123-tool-error-detail-surfacing.md). Its `_SUPPRESSED_DETAIL` seam is
  unchanged — `src/kdive/domain/errors.py` is not modified by this ADR — and its decision text
  needs no amendment. An `authorization_denied` `detail` remains the bare constant
  `"access denied"`.
- **Extends:** [ADR-0129](0129-systems-teardown-admin-authority.md), which established that a
  **closed enum** of tokens in `data` is not a resource identifier and therefore survives the
  no-leak seam. That precedent is applied to a second, wider class of denial.
- **Resolves:** [ADR-0471](0471-denial-envelope-terminal-affordance.md)'s `## Deferred` section,
  which split this question out of #1596 as a security-posture decision deserving its own record.

## Context

ADR-0471 gave a denied caller one terminal affordance: `session.whoami`, which reports the grants
the caller **holds**. It closed half the gap and said so. The other half stayed open: nothing in
the envelope reports what the tool **requires**.

So an agent that gets denied can enumerate its own roles and still cannot name a blocker. Its
report to a human operator is "something was denied, and here is everything I hold" — leaving the
operator to work backwards from the tool name to the grant, which is exactly the lookup the
server already performed and then discarded.

The reason it was discarded is deliberate. `_SUPPRESSED_DETAIL`
(`src/kdive/domain/errors.py:63-69`, applied at `ToolResponse.failure`,
`src/kdive/mcp/responses.py:196`) rewrites *every* `AUTHORIZATION_DENIED` detail to a fixed
constant, so that no raise site — including `require_role`'s, whose message embeds a project
name, and `require_platform_role`'s, whose message embeds the caller's held role set — can leak
resource existence through `detail`. That map has been unchanged since ADR-0123 landed in
`397792328`.

The precedent pointing the other way is ADR-0129. Its destructive-op gate already surfaces a
closed enum of failed policy-check tokens (`admin_role`, `operator_role`, `profile_opt_in`) under
`data.missing_checks`, on the stated grounds that a closed enum is never a resource identifier.
Before this ADR that was the only grant-adjacent disclosure on the whole surface, reached from
three call sites.

### What is actually being disclosed

The role gating is already public. It is in the tool docstrings, in `docs/`, and in
`src/kdive/mcp/exposure.py`'s `_TOOL_SCOPES` table, which is in this repository.

That does not make the question trivial, and the honest framing matters. Documentation and a
wire-level oracle are not equivalent. A caller who can probe an endpoint and be told which grant
it needs can map the authorization surface tool by tool, without reading anything, including for
tools added after the docs were last consulted. Publishing a fact and answering questions about
it at runtime are different exposures.

The bound on that exposure is what makes it acceptable here: a **closed enum**. The caller learns
one of seven fixed tokens. It cannot learn a project name, an object id, a hostname, a principal,
or anything the vocabulary does not already contain, no matter how it probes. The disclosure is
finite, auditable, and knowable in advance — the same property ADR-0129 relied on.

## Decision

### 1. `data.missing_roles` carries a closed enum; `detail` stays suppressed

`ToolResponse.denied` gains a `missing_roles` argument. A denial that has a role to name emits:

```json
{"outcome": "denied",
 "error": {"category": "authorization_denied", "detail": "access denied"},
 "data": {"missing_roles": ["platform_admin"]}}
```

`detail` is byte-identical to what it was before this ADR, at every site. The no-leak seam is not
touched, not widened, and not conditioned on anything — which is why ADR-0123 needs no amendment
and its pinned test (`tests/domain/test_errors.py`,
`test_suppressed_detail_collapses_authorization_denied_to_constant`) still holds unmodified. That
test is this ADR's reaffirmation of ADR-0123, not a new one: a second copy asserting the same
collapse over a different input string would be redundant rather than stronger.

Rejected: **putting a structured reason in `detail`**. It would require amending
`_SUPPRESSED_DETAIL` to make `AUTHORIZATION_DENIED` conditionally pass-through, which reopens the
seam for every raise site at once — including ones whose messages embed a project name — to buy a
disclosure `data` already carries. `detail` is the human string; `data` is the machine-readable
channel, and this is machine-readable.

Rejected: **keeping the seam as-is and closing the issue.** It is the status quo, and its cost is
concrete: the operator still does the lookup by hand, and #1596's terminal affordance stays half
a fix.

### 2. The vocabulary is the live enforcement enums, not a parallel copy

`missing_roles` is typed `Role | PlatformRole` — the two enums
`require_role` and `require_platform_role` actually check — exported from `responses.py` as
`MissingRole`. It is not a `str`, and not a new `MissingRoleToken` enum mirroring them.

This follows ADR-0471's method: make the wrong value **unrepresentable** rather than
runtime-validated. A typo is a type error at the call site; a role that does not exist cannot be
constructed; and a role added to the enforcement enum is automatically in the vocabulary with no
second list to update. A parallel enum would have been a second answer to one question, free to
drift, which is the failure mode ADR-0483 decision 2 called out for the retryability table.

The two enums' value sets are disjoint (`admin` vs `platform_admin`), so one flat list is
unambiguous about which tier a token belongs to. That disjointness is now guarded rather than
assumed — a future `Role.PLATFORM_ADMIN` would make a wire token mean two things.

### 3. Absence, not an empty list, when no role would have helped

The key is **omitted** from `data` when there is no role to name. It is never present-but-empty.

This is a real distinction for the caller: `[]` reads as "a role, but we lost track of which",
whereas absence says "no grant you could ask for would have changed this answer" — which is a
different blocker and a different next step for the operator.

Three classes of denial have no role to name, and this ADR reports the finding rather than
inventing a value for them:

- **Non-member denials.** `require_role` raises the base `AuthorizationError` when the project is
  not granted at all, and the more specific `RoleDenied` only when the caller *is* a member
  ranking below the bar. Naming a role on the non-member arm would confirm that the named project
  exists and that membership — not rank — is the blocker. That is precisely the resource-existence
  leak ADR-0123 exists to prevent, and it is why `not_found` is byte-identical for an ungranted
  project. The split is not incidental: the member arm names the role, the non-member arm does
  not, and they are separate `except` clauses so the distinction is structural.
- **The ADR-0129 destructive gate.** `authz_denied` keeps speaking `missing_checks` and does not
  additionally emit `missing_roles`. Its gate is an any-of over factors of which a role is only
  one — `profile_opt_in` is not a grant at all — so a check token already answers the caller's
  question more precisely. Emitting both would give one denial two vocabularies free to disagree.
- **The dispatch-boundary middleware's membership arm.** `DenialAuditMiddleware` catches *after*
  `call_next`, not before dispatch; its `ProjectMembershipDenied` arm is a non-member denial and
  names no role, while its `RoleDenied` arm names `denial.required` like any other member
  over-reach. That distinction matters because the middleware is the funnel for the ~40
  `require_role` sites that do not catch locally — see Consequences.

### 4. A per-plane helper takes its role as a required argument

The 27 `ToolResponse.denied` construction sites include nine module-local `_denied` helpers. Where
every tool in a module gates on one role, the helper names that role itself. Where it varies —
`resources.drain` gates on `_drain_role(mode)`, `platform_admin` for `force_release` and
`platform_operator` for a passive cordon — the helper takes the role as a **required positional
argument**, and `images/_common.denied` takes it as required-but-nullable so that `None` at the
two non-member sites is a stated answer rather than an omission.

Required-ness is what earns these helpers their exemption from the role-less-site guard, so it is
itself guarded: a test asserts each forwarding helper's role parameter exists, carries no default,
and is actually forwarded into `missing_roles`. Without that, giving the parameter a default and
dropping it at one call site would produce a silently role-less denial with every other guard
green.

## Consequences

An unprivileged caller can determine the required role of a gated tool by calling it. That is the
disclosure this ADR accepts, bounded to seven tokens, and it is a deliberate widening of what a
denial tells a caller.

**How much of the surface that covers today is gated on #1635, and this ADR does not claim
otherwise.** Only five modules catch `RoleDenied` locally; the other ~40 `require_role` sites —
allocations, runs, systems, `ssh_access`, debug sessions, vmcore, artifacts, accounting — let it
propagate to `DenialAuditMiddleware`, which is why that arm now passes `denial.required`. But
#1635 established that this arm is **currently dead on every real dispatch path**: FastMCP builds
the middleware chain outside the `try` that converts a tool exception into `ToolError`, so the
middleware sees a `ToolError` with the real exception demoted to `__cause__` and neither `except`
clause fires. The change here is correct and additive, and it goes live when #1635 lands. Until
then the disclosure reaches only the locally-catching sites, and a `viewer` denied by
`systems.decommission` still learns nothing about what it needed.

The `data.missing_roles` key is additive on a failure envelope. No success envelope changes, no
`detail` changes, no `suggested_next_actions` change, and no client that ignores unknown `data`
keys is affected. No schema, no migration, no configuration setting, and no change to the tool
list or to any tool's arguments.

The `data` argument is not a back door. `denied` **raises** when `data` carries the
`missing_roles` key, because `data` is an unchecked dict and routing the key through it would put
a free-form string (`["superuser"]`) on the egress seam with no type ever seeing it. The typed
argument is the only way in — which is the make-it-unrepresentable method this ADR claims to
follow, applied at the seam rather than delegated to a test.

Three guards in `tests/guards/test_denial_envelope_actions.py` back that up. They are the
backstop; the runtime check above is the defence:

- Every `ToolResponse.denied` call under `src/kdive/` that names no missing role must be listed,
  with a count and the reason no role applies. A new denial site that simply forgets the role is a
  failing test, not a silently less useful envelope.
- No module outside `responses.py` may *name* the `missing_roles` key — resolving the exported
  `MISSING_ROLES_KEY` constant and any alias of it, not only the string literal. Matching literals
  alone was evadable with `data={MISSING_ROLES_KEY: ["superuser"]}`, using a name `responses.py`
  itself hands out.
- Each role-forwarding helper keeps its role parameter required and forwards it, per decision 4.

The role a denial reports is the role the gate **required**, not the full set that would satisfy
it. `platform_admin` implies `platform_auditor` (rbac.py `_PLATFORM_IMPLIES`), so a caller holding
`platform_admin` and denied on a `platform_auditor` gate is impossible; but a denial naming
`platform_auditor` does not restate that `platform_admin` would also serve. The implication is
documented in ADR-0043 §2 and reachable via `session.whoami`; restating it per-denial would make
the field a partial copy of the role model.

Not addressed here: making `missing_roles` a **required** argument of `ToolResponse.denied`, which
would be the stronger unrepresentable-by-construction form. It cannot be done in this change
because `mcp/middleware/denial_audit.py` is owned by #1635 and its membership arm legitimately
passes no role. The AST guard above is the interim enforcement and holds the same invariant one
layer out.

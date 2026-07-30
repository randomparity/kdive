# 0507 — A structural guard over `require_role`'s unenveloped non-member arm

## Status

Accepted (2026-07-30)

## Context

`require_role` raises from two sites and only one is owned by a boundary:

```python
def require_role(ctx: RequestContext, project: str, role: Role) -> None:
    if project not in ctx.projects:
        raise AuthorizationError(...)          # non-member — owned by nothing
    held = ctx.roles.get(project)
    if not role_satisfies(held, role):
        raise RoleDenied(...)                  # rank-below — owned by DenialAuditMiddleware
```

`middleware/denial_audit.py`'s `_DENIAL_TYPES` is `(RoleDenied, ProjectMembershipDenied)`. That
narrowing is deliberate — ADR-0062 §5 and ADR-0043 §4 require it, because a base-class catch
would also sweep in `require_platform_role` denials and `DestructiveOpDenied` and double-write
their audit rows. The cost is that a bare `AuthorizationError` escaping a handler reaches the
client as a raw `ToolError`, not an `authorization_denied` envelope: wrong exit code, no audit
row, no `missing_roles`, and a caller branching on `is_error` reads it as a transport fault.

That was #1661, on `accounting.report`. ADR-0493 fixed the one site and disclosed the general
case as a standing residual, in the present tense: nothing in `just ci` fails a new `require_role`
call on a caller-named project with no guard and no catch. It named the reason for deferring —
distinguishing a caller-named project from a row-resolved one is a dataflow property, not a grep.

Two things have changed since, both making the residual worse:

- **The failure mode became silent.** ADR-0493 §2 deleted `UsageTrackingMiddleware`'s
  `except AuthorizationError` arm as dead code. It was the one thing that incidentally recorded
  such a denial as anomalous. A new occurrence now produces no signal anywhere
  (`mcp/middleware/usage.py` records the post-deletion state).
- **The surface grew.** ADR-0493's evidence was a manual audit of 52 `require_role` sites. The
  audit was point-in-time; the surface has moved since, and an allowlist transcribed from it
  would exempt precisely the newest sites — the ones least likely to have been reviewed.

The existing guard does not cover this. `tests/guards/test_denial_envelope_actions.py`'s
`_DeniedCallVisitor` walks `ToolResponse.denied(...)` calls — envelope *construction*. A bare
`AuthorizationError` builds no envelope at all, so it is invisible there.
`tests/adversarial/test_auth_properties.py` property-tests `require_role`'s semantics but never
inspects a call site.

## Decision

### 1. The rule is universal, not scoped to caller-named projects

`tests/guards/test_require_role_membership_guard.py` requires **every** `require_role` call under
`src/kdive/` to be *membership-covered*: by the time control reaches it, `project in ctx.projects`
is already established, or the resulting `AuthorizationError` is caught and enveloped.

#1681 framed the hazard as a *caller-supplied* project identifier, on the reasoning that a
row-resolved one is safe. Re-deriving the surface against the tree showed that premise is wrong,
and that acting on it would have been the more dangerous design. A row fetched by id belongs to
whatever project owns it, which need not be one of the caller's — `require_role(ctx, run.project,
…)` on a run the caller cannot see raises the same bare `AuthorizationError`. What actually makes
the row-resolving tools safe is that they pair the fetch with an explicit membership check:

```python
run = await RUNS.get(conn, uid)
if run is None or run.project not in ctx.projects:
    return _not_found(run_id)
require_role(ctx, run.project, Role.CONTRIBUTOR)
```

That idiom is the dominant shape on this surface and it *is* one of the accepted mitigations. So
asking every site costs no allowlist entries over asking only the caller-named ones, and it closes
the row-resolved hazard too. Precision comes from the mitigations being real, not from narrowing
which sites are asked — which matters, because a guard that fires on legitimate tool calls gets
disabled by the next person who hits it.

Four ways to be covered:

1. the project argument is bound by iterating `ctx.projects` (every value it can take is a
   membership — how `projects_with_role` and the granted-set readers enumerate);
2. `require_project(ctx, …)` or a `… in ctx.projects` test precedes the call in the same function,
   **naming the same project expression** the `require_role` authorizes;
3. the call is in the body of a `try` whose handler catches the base `AuthorizationError`;
4. every call to the enclosing function *within its module* is itself covered by (2) or (3).

Rule 4 is one level of intra-module call graph, and it is what the deliberate raisers need:
`accounting/reports.py`'s `_resolve_granted_set` raises on purpose and its sole caller wraps it
(ADR-0493 §1), and `raw_fetch.py`'s `_resolve_key` is called only after its caller has checked
`run.project not in ctx.projects`. It requires at least one call site, so a function with none
stays uncovered rather than passing vacuously.

Rule 2 compares unparsed source, so it holds only for a syntactically identical expression:
`require_project(ctx, other)` above `require_role(ctx, project, …)`, or a `run.debuginfo_ref not
in ctx.projects` guarding a `require_role(ctx, run.project, …)`, establishes nothing about the
project actually being authorized and does not clear the site. Text equality is a weaker
relation than "the same value", but it is the right direction of weak: it can refuse a site that
is in fact safe (which the allowlist absorbs), and it cannot accept one that is unsafe because the
check names a different object. Rule 4 deliberately drops the expression match, because the
caller's binding is a different name in a different scope — which is why it compensates by
demanding that *every* call site be covered.

`except RoleDenied` does **not** satisfy rule 3. `RoleDenied` is the arm a boundary already owns;
catching only it leaves the non-member arm still propagating. Accepting it would have made the
guard green on the exact shape it exists to catch.

### 2. `require_platform_role` is guarded for envelopment, not membership

It carries no project, so membership is not the question — but it raises the same bare
`AuthorizationError`. `denial_audit.py` states the invariant that every non-owned
`AuthorizationError` "is audited by its own handler and must pass through here untouched"
(ADR-0043 §4). Nothing checked that. All 32 call sites already satisfy it with a local
`try/except AuthorizationError`, so the second test pins an invariant the code already holds and
its allowlist is empty.

### 3. The allowlist carries a per-entry reason, and is checked for staleness

`_UNCOVERED_REQUIRE_ROLE` is keyed by `<module>::<function>` — not by line, so an unrelated edit
above the call does not churn it — and maps to prose stating the cross-frame fact that makes the
site safe. This mirrors `_ROLELESS_DENIALS` in `test_denial_envelope_actions.py`. A new entry is a
deliberate diff reviewed as a claim about reachability, so "I forgot the membership check" cannot
ship as a one-line allowlist bump. Both tests also assert the allowlist is not *stale*: an entry
that no longer names an uncovered site fails, so an entry cannot outlive the hazard it excused.

Re-derived against the tree rather than transcribed from #1681's audit, the allowlist has **one**
entry: `services/investigations/lifecycle.py::_require_admin_for_force`, whose `project` arrives
two frames up.

### 4. A non-vacuity test

`test_the_guard_sees_the_authorization_surface` asserts the walk finds at least 40 `require_role`
and 20 `require_platform_role` sites. A rename, a package move, or a broken `_SRC_ROOT` would
otherwise turn every other assertion in the module into a no-op that still reports green — the
failure mode where a guard that cannot fail looks identical to a guard that passes.

## Consequences

**What this analysis cannot resolve, stated plainly.** It is intra-procedural plus one level of
intra-module call graph, over the AST. It cannot follow a project argument across two frames,
across modules, or through a container; it does not know that a repository `get` is
membership-scoped; and it reasons about lexical position, not reachability, so a mitigation on a
branch that does not dominate the call still counts. ADR-0493 deferred this guard because the
provenance question is dataflow rather than grep, and that is still true — what makes the guard
tractable is that it does not answer the provenance question. It asks for a mitigation at every
site and lets the allowlist absorb the cases where the answer lives in another frame. The
allowlist is therefore not an exemption list but the written-down boundary of the analysis, one
prose entry per site.

**The residual is one entry, and it is disclosed rather than closed.** Cross-frame provenance is
asserted in prose there, not proved. A future refactor that changes `close_investigation`'s
membership check would not redden this guard. Narrowing that requires interprocedural dataflow
across modules, which is a different tool than an AST walk in a test.

**A new tool that gates on a project must now carry its membership check or say why not.** The
existing surface already does — the guard lands green with a single allowlist entry, so it bites
only on drift, which is what makes it survivable. The cost lands on new code: a `require_role` on
a project with no established membership fails `just ci` with the site named and the two
mitigations spelled out in the failure message.

**Mutation-verified, not assumed.** Seven mutations were applied and confirmed to redden the
guard, then reverted: #1661's pre-fix shape in `accounting/report` (the required one — it names
`_resolve_granted_set`); dropping a row membership check; narrowing a catch from
`AuthorizationError` to `RoleDenied`; dropping a `require_project` pre-guard; unwrapping a
`require_platform_role`; and the two wrong-project shapes rule 2's expression match exists for —
a `require_project` on a different project, and a membership check on a different attribute of the
row. Each named the offending site; the clean tree is green. A guard that cannot be made to fail
is indistinguishable from one that passes, which is what
`test_the_guard_sees_the_authorization_surface` and this exercise together rule out.

**ADR-0493's residual paragraph is amended, not left standing.** It asserted in the present tense
that nothing in `just ci` fails this shape. That is now false, and an un-retracted disclosure is
worse than none — a future reader would re-derive a guard that exists.

No schema, no migration, no configuration setting, no runtime code change, no change to the tool
list, and no change to any tool's arguments or output schema. This change adds one test module and
amends one ADR.

## Considered & rejected

- **Extend `tests/guards/test_denial_envelope_actions.py`.** #1681 proposed it and it is the
  established location. Rejected: that module's subject is envelope *construction* — every visitor
  and both allowlists are about `ToolResponse.denied(...)` calls. This guard's subject is a raiser
  that constructs no envelope, so it would share a filename and nothing else. A concurrent change
  was also amending that file, and a new module keeps the two diffs disjoint.
- **Widen `_DENIAL_TYPES` to catch the base `AuthorizationError` at the boundary.** This makes the
  defect structurally impossible rather than merely detected, which is the better shape in
  general. Rejected here because ADR-0062 §5 and ADR-0043 §4 narrowed it *deliberately*: the base
  class also covers `require_platform_role` and `DestructiveOpDenied`, both of which their own
  handlers already envelope and audit, so widening double-writes their audit rows. Reversing two
  ADRs' central decision to close a hole a test can close is the wrong trade, and it would change
  runtime behaviour on paths #1681 does not name.
- **Scope the guard to caller-named projects only, per the issue.** Rejected on evidence: the
  premise that a row-resolved project is safe does not hold (decision 1), and the row-resolved
  sites pass the universal rule anyway. Scoping would have been more analysis for less coverage,
  and would have encoded a false safety claim into the guard's own structure.
- **Seed the allowlist from #1681's 52-site audit.** Rejected: the surface moved after the audit,
  so a transcribed list would silently exempt the newest sites — exactly the ones least likely to
  have been reviewed. The allowlist was re-derived by running the analysis against the tree, which
  is also the only method that stays correct the next time someone regenerates it.
- **Report unresolvable sites as "unknown" and pass them.** The cheaper way to a green start.
  Rejected: an analysis that silently passes what it cannot understand fails open, and the sites
  it cannot understand are the ones most worth a human's attention. Unresolvable sites fail and
  require a prose entry.
- **Runtime enforcement — assert membership inside `require_role` itself.** `require_role` cannot
  distinguish "the caller named this project" from "a row resolved to it", which is the whole
  difficulty; and the non-member arm is not a bug to assert away — it is a real authorization
  outcome that needs an envelope, not a crash.
- **A `ruff` custom rule or a `grep` in `check-records.sh`.** No lint rule expresses "a membership
  check precedes this call in the enclosing function", and a grep cannot see scope at all. The
  repo's established answer to a structural invariant is an AST walk under `tests/guards/`.

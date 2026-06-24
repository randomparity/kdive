# External upload as the default build path; a `contributor` role

- **Date:** 2026-06-24
- **ADR:** [ADR-0234](../../adr/0234-external-build-default-and-contributor-role.md)
- **Epic:** #771 (subs #765–#770)

This spec details the change implemented in this PR — the **`contributor` project role** (#767) —
and records the contracts the sibling PRs (#766 format unify, #768 retention, #769 advisory)
build on. ADR-0234 holds the decision rationale; this spec is the build sheet.

## In scope (this PR)

Insert a `contributor` project role and re-gate the crash-investigation loop down to it. No
migration (roles are string claims; rank integers are not persisted). No new tool.

## Role model

`Role` becomes a four-level total rank:

```
viewer (0) < contributor (1) < operator (2) < admin (3)
```

Two rank tables hold the integers and both must change together:

- `kdive.security.authz.rbac._RANK`
- `kdive.mcp.exposure._ROLE_RANK`

`roles_from_claims` needs no change — it already maps any `Role(value)`. A token carrying
`"contributor"` is parsed; an unknown value still fails closed.

## Tool → role mapping

Drop from `operator` to `contributor` (the lowest role that can run a full loop):

| Enforcement site | Tool(s) |
|------------------|---------|
| `services/runs/admission.py:_resolve_targets` (~272) | `runs.create` (bound) |
| `services/runs/admission.py` unbound create (~661) | `runs.create` (unbound) |
| `services/runs/bind.py` (~125) | `runs.bind` |
| `mcp/tools/lifecycle/runs/server_build.py` (~73) | `runs.build` |
| `mcp/tools/lifecycle/runs/complete_build.py` (~170) | `runs.complete_build` |
| `mcp/tools/lifecycle/runs/steps.py` (~47, ~80) | `runs.install`, `runs.boot` |
| `mcp/tools/lifecycle/runs/cancel.py` (~56) | `runs.cancel` |
| `mcp/tools/catalog/artifacts/uploads.py:_create_upload` (~406) | `artifacts.create_run_upload` **only** (see split below) |
| `mcp/tools/debug/sessions_lifecycle.py` (~346) | `debug.start_session` |
| `mcp/tools/debug/session_context.py` (~58) | `debug.continue`/`interrupt`/`set_breakpoint`/`clear_breakpoint`/`read_memory`/`read_registers`/`end_session` |
| postmortem seam `vmcore.py:_with_postmortem_crash_port` (~186) | `postmortem.crash`, `postmortem.triage` — **tightened** from `VIEWER` (runtime) / `OPERATOR` (exposure) to `CONTRIBUTOR`; arbitrary crash-command execution leaves pure `viewer` |
| `mcp/tools/lifecycle/vmcore.py` (~272) | `vmcore.fetch` |
| `mcp/tools/lifecycle/allocations/request.py` (~101) | `allocations.request` |
| `mcp/tools/lifecycle/allocations/lifecycle.py` (~40, ~81) | `allocations.release`, `allocations.renew` |
| `mcp/tools/catalog/investigations.py` (~217, ~296, + link/unlink/set seam) | `investigations.open`/`close`/`link`/`unlink`/`set` |

Stay at `operator`:

| Enforcement site | Tool(s) |
|------------------|---------|
| `services/systems/admission.py` (~414, ~614) | `systems.provision`/`provision_defined`/`reprovision` + `systems.define` |
| `mcp/tools/catalog/artifacts/uploads.py:_create_upload` (~406) | `artifacts.create_system_upload` |
| `mcp/tools/ops/images/upload.py` (~70), `images/delete.py` (~50) | `images.upload`, `images.delete` |
| control-power seam | `control.power` |

Stay at `admin`: `systems.teardown`, `control.force_crash`, `accounting.set_budget`/`set_quota`.

### Two gates per tool for the runtime-resolved tools

`runs.build`, `runs.complete_build`, and `vmcore.fetch` are gated **twice**: once at the
`with_runtime_for_run`/`with_runtime_for_system` registrar wrapper (`required_role=` kwarg, which
runs *before* the handler) and once inside the handler. Both must drop to `contributor` — the
registrar gate alone would deny a contributor even with the handler dropped:

- `runs/registrar.py` (~318 build, ~358 complete_build) — `required_role=Role.CONTRIBUTOR`
- `vmcore.py:fetch_vmcore` (~132) + `_fetch_vmcore` (~272) — both `CONTRIBUTOR`

### The shared upload seam

`_create_upload` serves both `create_run_upload` and `create_system_upload` through a
`_UploadOwnerSpec`. The single `require_role(ctx, project, Role.OPERATOR)` becomes a per-spec
required role: the spec carries `required_role` (run-spec → `Role.CONTRIBUTOR`, system-spec →
`Role.OPERATOR`), and the seam enforces `spec.required_role`. This is the one site where the gate
is data-driven rather than a constant.

## Exposure classifier

`kdive.mcp.exposure` gains:

- `ExposureScope.PROJECT_CONTRIBUTOR = "project_contributor"`
- `_PROJECT_SCOPE[PROJECT_CONTRIBUTOR] = Role.CONTRIBUTOR`
- `_ROLE_RANK` extended with `Role.CONTRIBUTOR: 1` (and operator→2, admin→3)
- `_CONTRIBUTOR = frozenset({ExposureScope.PROJECT_CONTRIBUTOR})`
- the re-gated tools in `_TOOL_SCOPES` move from `_OPERATOR` to `_CONTRIBUTOR`

`scope_satisfied`/`tool_visible` already work off `_ROLE_RANK`, so a contributor sees viewer +
contributor tools and an operator still sees everything ≤ operator. The completeness guard in
`tests/mcp/core/test_app.py` is unaffected (same tool set, different scope).

A classification must stay ≤ the handler's real `require_role`; since each re-gated tool's runtime
gate also drops to `contributor`, the invariant holds.

## Tests

Behavior-level, at the boundary:

- `rbac`: a `contributor` token satisfies `require_role(..., CONTRIBUTOR)` and `..., VIEWER`, and
  is `RoleDenied` for `..., OPERATOR`/`..., ADMIN`; `operator`/`admin` still satisfy
  `CONTRIBUTOR` (rank superset); `roles_from_claims` parses `"contributor"`.
- exposure: a contributor-only ctx sees the re-gated loop tools + all viewer tools, and does
  **not** see `systems.define`, `images.upload`, `create_system_upload`, `control.power`,
  `systems.teardown`.
- the shared upload seam: a contributor may `create_run_upload` but is denied
  `create_system_upload`; an operator may do both.
- one end-to-end-ish gate test per re-gated family (runs lifecycle, debug, allocations,
  investigations, vmcore, postmortem) asserting `contributor` is admitted and `viewer` is denied.

Break-then-fix each: confirm the test fails with the old `OPERATOR` constant before the drop.

## Downstream contracts (not built here)

- **#766 format unify:** one combined `kernel` tar (`boot/vmlinuz` + `lib/modules/<ver>/`) for
  both providers; local extracts `boot/vmlinuz` host-side for the libvirt `<kernel>` element;
  `modules_ref` removed.
- **#768 retention (migration 0048):** `gc_investigation_artifacts` reconciler repair on close +
  a TTL backstop on uploaded build artifacts; console evidence retention reconciled with #761.
- **#769 advisory:** `artifacts.expected_uploads` states the unified format contract per name.

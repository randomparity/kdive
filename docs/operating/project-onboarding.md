# Onboarding a project

A KDIVE *project* is the tenant boundary for budgets, quotas, allocations, and the
audit trail. There is **no projects table and no "create project" step**: a project
is derived from a verified OIDC token's `projects` and `roles` claims
([Safety and RBAC](../guide/safety-and-rbac.md)). The only persisted per-project
state is two rows keyed by the project name:

- a **budget** row (`budgets`) — the spend ceiling `limit_kcu`;
- a **quota** row (`quotas`) — the concurrency caps and pending-queue cap.

Onboarding a project in production therefore means: mint an admin-scoped token for
the project, then set its budget and quota with the audited admin tools. Until both
rows exist, `allocations.request` for that project is rejected by admission control.

## 1. Mint an admin token for the project

The operator establishing a project's policy needs a token whose claims grant
`admin` on that project (`admin` is the role `accounting.set_budget` /
`accounting.set_quota` gate on):

- `projects` includes the project name, e.g. `["acme"]`;
- `roles` maps the project to `admin`, e.g. `{"acme": "admin"}`.

How you mint this token is your IdP's concern. In the bundled mock-OIDC dev setup,
`kdivectl login` and the live-stack harness mint such tokens; in production your IdP
asserts the `projects` and `roles` claims. The per-project `admin` role is distinct
from the platform tier (`platform_admin` and friends) — a platform role does **not**
grant project-scoped accounting writes, and project `admin` does not grant
cross-project authority.

## 2. Set the budget and quota

Call the two admin tools with the admin token through any MCP client (an agent
session, a scripted FastMCP client, or Claude Code). Both writes are role-gated
(`require_role(..., admin)`) and audited, and both are idempotent upserts — re-running
them updates the ceilings in place, and re-setting the budget preserves the already
recorded `spent_kcu`.

- `accounting.set_budget` — `{"project": "acme", "limit_kcu": "1000000"}`
- `accounting.set_quota` — `{"project": "acme", "max_concurrent_allocations": 4,
  "max_concurrent_systems": 4, "max_pending_allocations": 0}`

See the [accounting tool reference](../guide/reference/accounting.md) for the full
parameter list. Confirm the result with the read-only `accounting.usage_project`
(`kdivectl ledger show --project acme`).

> **`kdivectl` cannot set budget or quota today.** The operator CLI's `tool call`
> passthrough is fail-closed read-only, and `set_budget` / `set_quota` are mutating
> tools with no curated break-glass verb, so they are unreachable from `kdivectl`.
> Onboard a project from an MCP client that holds the project-`admin` token. See the
> [kdivectl runbook](runbooks/kdivectl.md).

## Relationship to `seed-demo`

`python -m kdive seed-demo` writes the same `budgets` and `quotas` rows (and registers
the local libvirt resource) for a demo project. It is a **bootstrap convenience, not
the production path**: it runs as an installed-package CLI at deploy time, before any
request, so it has no OIDC token and no request context. It therefore writes the rows
with raw idempotent `INSERT`s instead of calling `accounting.set_budget` /
`accounting.set_quota`, which means those writes are **not role-gated and leave no
audit row**.

The end state is identical row content, so a demo project seeded this way behaves the
same at run time. Use `seed-demo` for local stacks and demos
([Local stack administration](local-stack.md)); onboard real tenants with the audited
admin tools above so every policy change is attributable.

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

How you mint this token is your IdP's concern: in production your IdP asserts the
`projects` and `roles` claims. In the bundled mock-OIDC dev setup, project-role tokens
are minted programmatically against the issuer (the live-stack harness's `mint_token`
does this) — `kdivectl login` covers only the platform-role axis, not the per-project
role, so it cannot mint a project-`admin` token. The per-project `admin` role is
distinct from the platform tier (`platform_admin` and friends) — a platform role does
**not** grant project-scoped accounting writes, and project `admin` does not grant
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
parameter list. Confirm the result with the read-only `accounting.usage`
(`kdivectl accounting usage --project acme`).

With `kdivectl`, set the budget through the mutating tool passthrough and the quota
through its generated verb. Both use the same server-side project permissions:

```sh
kdivectl tool call accounting.set_budget --allow-mutating \
  --json '{"project":"acme","limit_kcu":"1000000"}'
kdivectl accounting set-quota --project acme \
  --max-concurrent-allocations 4 --max-concurrent-systems 4 --max-pending-allocations 0
```

See the [kdivectl runbook](runbooks/kdivectl.md) for authentication and argument discovery.

## Remote-libvirt demo helper

For a remote-libvirt demo, `just setup-remote-libvirt HOST USER URI` combines the connection
preflight with audited budget and quota writes. Run it from a checkout with its project venv,
or set `KDIVE_PYTHON` to an interpreter with KDIVE's dependencies installed:

```sh
export KDIVE_MCP_BASE=http://127.0.0.1:8000/mcp
just setup-remote-libvirt HOST USER qemu+tls://HOST/system
```

The endpoint must end in `/mcp` and be reachable from this shell; use the
[Helm runbook's port-forward](runbooks/kubernetes-deploy.md#5-reach-the-mcp-endpoint)
for a cluster-local server.
Supply a project-admin `KDIVE_TOKEN` for your deployment. When it is absent, the helper invokes
[`scripts/demo-token.sh`](../../scripts/demo-token.sh) against the in-cluster mock issuer
(the script documents namespace, release-name, and context overrides). `KDIVE_PROJECT`
defaults to `demo`; set it to match the token's project. The helper sets accounting policy; it does not register
libvirt hosts or verify a guest lifecycle.

## Local-libvirt demo helper

The [local setup](../../examples/local-libvirt/README.md) funds the demo project during bring-up.
For a standalone accounting/preflight step, `scripts/operations/setup-local-libvirt.sh` defaults
to the token-less `seed-project` path below. Its audited mode requires `KDIVE_SETUP_AUDITED=1`,
a reachable `KDIVE_MCP_BASE` ending in `/mcp`, and a project-admin `KDIVE_TOKEN`.
`KDIVE_PROJECT` selects the project (default `demo`); `KDIVE_LIMIT_KCU`, `KDIVE_MAX_ALLOC`, and
`KDIVE_MAX_SYS` set its budget and limits. Like the remote helper, it uses the checkout venv
unless `KDIVE_PYTHON` selects another installed interpreter. It does not start a worker.

## Relationship to `seed-project`

`python -m kdive seed-project` writes the same `budgets` and `quotas` rows (and registers
the local libvirt resource) for a project. It is the **token-less bootstrap path, not
the audited production path**: it runs as an installed-package CLI at deploy time, before
any request, so it has no OIDC token and no request context. It therefore writes the rows
with raw idempotent `INSERT`s instead of calling `accounting.set_budget` /
`accounting.set_quota`, which means those writes are **not role-gated and leave no
audit row**.

The end state is identical row content, so a project seeded this way behaves the
same at run time. Use `seed-project` for local stacks and demos
([live-stack onboarding](runbooks/live-stack.md#fund-the-demo-project--just-onboard));
onboard real tenants with the audited admin tools above so every policy change is attributable.

> Renamed from `seed-demo` in #669. Accepted ADRs and archived plans that predate the
> rename still refer to `seed-demo`; the command is now `seed-project`.

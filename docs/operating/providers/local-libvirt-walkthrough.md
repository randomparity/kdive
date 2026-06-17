# local-libvirt walkthrough

End-to-end setup for the local-libvirt provider, where the KDIVE worker drives QEMU/KVM
guests on its own host. For the provider's prerequisites and config see
[the local-libvirt provider reference](local-libvirt.md); this page is the linear path from a
prepared host to a verified run.

> **Deployment:** the KDIVE app processes run as **host services** on the libvirt host (see
> [systemd](../systemd.md)), so the worker has native `/dev/kvm` and libvirt access. The
> app-tier-only [docker-compose](../docker-compose.md) worker has no KVM/libvirt access and
> cannot drive this provider — use compose only for the backends (Postgres, MinIO, OIDC).

## 1. Prepare

Run the read-only preflight; fix anything it reports before continuing:

```bash
just check-local-libvirt
```

## 2. Install

Bring up the backends, then run the three KDIVE processes as host services:

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

Install and start the host services as described in [systemd](../systemd.md), then apply the
schema with `python -m kdive migrate`. See [Local stack administration](../local-stack.md)
for the package-on-host layout.

## 3. Onboard the project

A fresh database has no quota or budget, so the first `allocations.request` would dead-end on
`quota_exceeded`. Seed the demo project's budget and quota. Run this from the repo checkout
with the project venv active (or set `KDIVE_PYTHON=/opt/kdive/.venv/bin/python`), so `kdive`
resolves:

```bash
just setup-local-libvirt
```

By default this runs `python -m kdive seed-demo`, which writes the budget/quota rows with no
token. To onboard through the audited, role-gated admin tools instead (the production-style
path), set `KDIVE_SETUP_AUDITED=1` and supply a project-`admin` token in `KDIVE_TOKEN` — this
path needs an OIDC issuer configured to assert the project-`admin` claims, and the local
script does **not** mint a token for you (unlike the remote one):

```bash
KDIVE_SETUP_AUDITED=1 \
  KDIVE_MCP_BASE=http://localhost:8000/mcp \
  KDIVE_TOKEN="$(your-issuer-mint-command)" \
  just setup-local-libvirt
```

See [Project onboarding](../project-onboarding.md) for the audited-onboarding rationale and
why `kdivectl` cannot perform these writes.

## 4. Test the lifecycle

With the project onboarded, request an allocation and drive a System through its lifecycle:

```bash
# allocations.request → provision → build → boot → verify → teardown → release
```

Issue these as MCP tool calls from an agent session or a scripted client. For the deep
build→boot→debug steps and the canonical dcache `dhash_entries` verification, follow the
[four-method live run](../runbooks/four-method-live-run.md) and
[live stack](../runbooks/live-stack.md) runbooks. A successful run reaches a ready System via
`provision` (minimum) and ideally completes teardown and release.

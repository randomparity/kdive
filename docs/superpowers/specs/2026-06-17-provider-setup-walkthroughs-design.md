# Provider setup walkthroughs (#497)

**Date:** 2026-06-17
**Issue:** [#497](https://github.com/randomparity/kdive/issues/497) — *Fresh demo seeds no
project quota/budget: first `allocations.request` hard-denies `quota_exceeded`*
**Branch:** `fix/provider-setup-walkthroughs-497`

## Problem

A freshly installed demo seeds **no** project quota and **no** budget. The first
`allocations.request` — the entry point of the whole System lifecycle — hard-denies with
`quota_exceeded` even though zero allocations are active, because admission control treats an
absent `quotas`/`budgets` row as a zero cap. The demo is unusable for its advertised purpose
until an operator manually runs `accounting.set_quota` + `accounting.set_budget`.

The issue offers two remediation paths: (a) seed defaults at install via a post-install Job,
or (b) document the required operator step in the deploy `NOTES.txt` and first-setup docs.

## Scope

This work delivers the **documentation arm** (path b), narrowed to docs + helper scripts. It
does **not** implement the seed-Job code fix (path a); that remains available as a future
change and the issue can stay open or be closed by docs at the maintainer's discretion.

Concretely:

1. Two task-oriented **walkthrough documents**, one per provider configuration, each taking
   an operator linearly from a bare host through a verified full lifecycle so the first
   `allocations.request` never dead-ends.
2. Two **reference helper scripts** (one per provider) that run the #497 onboarding commands
   (`accounting.set_quota` + `accounting.set_budget`) through an authenticated MCP client.

**Out of scope:** the seed-Job code fix; any change to admission-control behaviour; the chart
`NOTES.txt` (explicitly deferred — see Decisions); new lifecycle automation (the existing
live-stack/spine drivers and runbooks already cover that).

## Design principles

- **Link, don't restate.** The canonical mechanics already live in `project-onboarding.md`
  (the budget/quota step and the `kdivectl`-can't-write caveat), `providers/*.md` (provider
  prerequisites), the accounting/config reference, and the live-run runbooks. The walkthroughs
  reference these rather than duplicate them, so the prose can't drift out of sync.
- **One natural deployment per provider**, stated explicitly in each doc (the deployment mode
  is largely orthogonal to the provider axis, so a walkthrough must commit to one concrete
  path to stay linear).
- **Reuse proven mechanisms.** Token mint and MCP tool-call already exist
  (`scripts/demo-token.sh`, `scripts/coverage_campaign/drive.py`'s
  `LiveStackClient.over_http().call_tool()`). The scripts compose them; they do not invent a
  new client.

## Component 1 — Walkthrough documents

Two new files under the existing provider docs directory:

- `docs/operating/providers/local-libvirt-walkthrough.md` — deployment: **docker-compose**,
  single host (lightest turnkey path that still containerizes the worker).
- `docs/operating/providers/remote-libvirt-walkthrough.md` — deployment: **Helm/k8s control
  plane driving a separate TLS target host** (the #497 reproduction context and the real
  two-host validation setup).

Each follows the same four-stage skeleton:

1. **Prepare** — host prerequisites and the existing read-only preflight
   (`just check-local-libvirt` / `just check-remote-libvirt HOST USER URI`). The remote doc
   points at `docs/operating/runbooks/remote-libvirt-host-setup.md` for the target host's PKI,
   `virtproxyd`, firewall ACL, and guest image.
2. **Install** — bring up the control plane (compose `up` / `helm install ... -f
   values-demo.yaml --wait`) and attach the provider.
3. **Onboard the project (the #497 fix)** — run the new helper script: mint a
   project-`admin` token, then `accounting.set_quota` + `accounting.set_budget`. Framed
   explicitly as the step that turns the `quota_exceeded` dead-end into a granted request;
   links to `project-onboarding.md` for the production (audited, non-demo) path.
4. **Test — full lifecycle to teardown** — `allocations.request` (now granted) → provision →
   build → boot → a concrete verify step (the canonical dcache `dhash_entries=1` demo) →
   teardown → release. Presented as documented MCP commands, leaning on
   `four-method-live-run.md` and the live-stack runbooks for the deep build/boot/debug steps
   rather than re-documenting them.

The existing `providers/local-libvirt.md` and `providers/remote-libvirt.md` reference docs
each gain a one-line pointer near the top: *"Setting up from scratch? See the
[walkthrough](...)."*

## Component 2 — Reference helper scripts

Two scripts alongside the existing `scripts/check-*-libvirt.sh`:

- `scripts/setup-local-libvirt.sh`
- `scripts/setup-remote-libvirt.sh`

Each:

1. Runs the matching provider preflight (`check-*-libvirt.sh`) and aborts on failure.
2. Mints a project-`admin` token **inside the deployment network** so the token's `iss`
   matches what the server validates against (compose: `docker compose exec server …`; k8s:
   reuse `scripts/demo-token.sh`). Minting from the host published port stamps the wrong
   issuer and the server 401s.
3. Calls `accounting.set_quota` then `accounting.set_budget` for the demo project through a
   minimal inline `python -c` FastMCP client (bearer = the minted token).
4. Confirms with the read-only `accounting.usage_project` and prints the result.

Properties: `set -euo pipefail`; `shellcheck`- and `shfmt`-clean; idempotent (both accounting
tools are upserts, so re-running updates ceilings in place and preserves recorded `spent_kcu`);
sane defaults for project name and ceilings with env-var overrides; a usage/`--help` header
explaining the demo-only nature (the bundled issuer mints a valid token for any caller).

These two scripts are the "reference helper script that includes the commands referenced
#497."

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Deployment per provider | local→docker-compose, remote→Helm/k8s + TLS target | Most natural turnkey path for each; remote matches the #497 context. |
| Test depth | Full lifecycle to teardown | Matches the project's "functional test drives capability" principle; proves the provider end-to-end. |
| Script count/scope | One per provider, onboarding-focused | The #497 fix is the onboarding step; the lifecycle stays as documented commands per doc. |
| Script language | Shell (`.sh`) with inline `python -c` MCP client | Consistent with `check-*-libvirt.sh` / `demo-token.sh`; avoids pulling the `tests/` harness into `scripts/`. |
| Chart `NOTES.txt` pointer | Deferred (docs + scripts only) | Keeps the change focused; can be added later. |

## Verification

- `shellcheck` + `shfmt -d` clean on both new scripts.
- Documentation link/path checks pass (`scripts/check-doc-links.sh`,
  `scripts/check-doc-paths.sh`) so the walkthroughs' internal links resolve.
- Manual: on a live demo, the onboarding script flips the first `allocations.request` from
  `quota_exceeded` to granted (the #497 acceptance condition). The full lifecycle is exercised
  by the existing live-run runbooks the walkthroughs reference.

## Risks

- **Issuer-match trap.** A token minted from the host port carries the wrong `iss` and 401s.
  Mitigated by minting inside the deployment network in both scripts (the same trap
  `demo-token.sh` already encodes).
- **Doc drift.** Walkthroughs that restate reference mechanics would drift. Mitigated by the
  link-don't-restate principle.
- **Demo-only token.** The bundled mock issuer mints a valid token for any caller; the scripts
  must carry a prominent demo-only warning so they are never pointed at a real deployment.

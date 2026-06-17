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
   an operator linearly from host prerequisites through verified onboarding (so the first
   `allocations.request` is granted, not dead-ended) and as much of the lifecycle as the
   deployment allows — at minimum a ready System via `provision`, ideally through teardown.
   The remote walkthrough's separate TLS target host is a named prerequisite (the host-setup
   runbook), not part of a "bare host" start.
2. Two **reference helper scripts** (one per provider) that perform the #497 onboarding —
   `accounting.set_quota` + `accounting.set_budget` — through an authenticated MCP client on
   the Helm/remote path, and (for local) via `seed-demo` by default with the audited MCP path
   available when a claims-asserting issuer is supplied.

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

- `docs/operating/providers/local-libvirt-walkthrough.md` — deployment: **KDIVE app
  processes as host services on the libvirt host** (the systemd units under
  `deploy/systemd/`, backed by external/compose Postgres + MinIO + OIDC). The local-libvirt
  provider drives QEMU/KVM guests on the *worker's own host*, so the worker must run with
  native `/dev/kvm` and libvirt-socket access — i.e. as a host process, **not** in the
  app-tier-only docker-compose container, whose `worker` has no KVM/libvirt access
  (`docker-compose.yml:159-185`; `deploy/compose/README.md:11` calls it "app tier… not a
  production deployment"). Compose remains the way to bring up the *backends*.
- `docs/operating/providers/remote-libvirt-walkthrough.md` — deployment: **Helm/k8s control
  plane driving a separate TLS target host** (the #497 reproduction context and the real
  two-host validation setup). Here the worker needs no local KVM — the guest runs on the
  separate TLS target host, so a containerized/k8s worker is fine.

Each follows the same four-stage skeleton:

1. **Prepare** — host prerequisites and the existing read-only preflight
   (`just check-local-libvirt` / `just check-remote-libvirt HOST USER URI`). The remote doc
   points at `docs/operating/runbooks/remote-libvirt-host-setup.md` for the target host's PKI,
   `virtproxyd`, firewall ACL, and guest image.
2. **Install** — bring up the control plane (compose `up` / `helm install ... -f
   values-demo.yaml --wait`) and attach the provider.
3. **Onboard the project** — run the new helper script: mint a project-`admin` token, then
   `accounting.set_quota` + `accounting.set_budget`, so the first `allocations.request` is
   granted instead of dead-ending on `quota_exceeded`. The framing differs by deployment, and
   each doc must state which applies:
   - **Helm/remote:** this is the literal #497 fix — the chart seeds build-configs but **not**
     quota/budget, so without this step the demo dead-ends (the exact bug #497 reports).
   - **Local/host:** the documented bootstrap `python -m kdive seed-demo` *already* writes the
     quota/budget rows (`local-stack.md:60`) with no token, so the dead-end does not occur on
     this path. The local doc **leads with `seed-demo`** as the actual onboarding step. The
     helper script is the **audited** equivalent — it routes the same writes through the
     role-gated, attributed admin tools instead of `seed-demo`'s raw INSERTs — but it requires
     an OIDC issuer that asserts the project-`admin` claims (see the token-mint constraint
     below), so the doc presents it as the production-style path against a claims-asserting
     issuer, linking to `project-onboarding.md` for the full audited-onboarding rationale.
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

For `setup-local-libvirt.sh` the onboarding action is runnable on the default local demo:
after preflight it invokes `python -m kdive seed-demo` (token-less; the path that actually
works against the stock compose issuer) and takes the audited mint+MCP path (steps 2-4 below)
only when a claims-asserting issuer is supplied (e.g. `KDIVE_OIDC_ISSUER` set to a configured
issuer plus a flag). `setup-remote-libvirt.sh` targets the chart-configured Helm-demo issuer,
so it always takes the mint+MCP path:
2. Mints a project-`admin` token. **This requires an OIDC issuer configured to assert the
   project-`admin` claims** (`roles:{<project>:admin}`, `aud:kdive`) — a bare
   `mock-oauth2-server` mints default claims that the role gate rejects. The **Helm demo**
   issuer is configured this way by the chart's `demo/oidc.yaml`, so the remote script mints
   *in-cluster* by reusing `scripts/demo-token.sh` (which `kubectl exec`s into the server pod).
   The **stock compose issuer is *not* configured**, so the local script either targets an
   issuer the operator has configured with the demo claim set the same way the chart does, or
   the operator uses `seed-demo` (no token) for onboarding and the script is skipped. Whatever
   issuer is used, mint it under the same name the server validates against
   (`KDIVE_OIDC_ISSUER`) — mint from the host for a host-process server, in-cluster for a k8s
   server — so the token's `iss` matches and the server does not 401.
3. Calls `accounting.set_quota` then `accounting.set_budget` for the demo project through a
   minimal inline `python -c` FastMCP client (bearer = the minted token) against the server's
   MCP endpoint. The endpoint base URL **must end in `/mcp`**. On the host/local path the
   server is directly reachable (e.g. `http://localhost:8000/mcp`); on k8s the server is
   ClusterIP-only, so the script requires a `kubectl port-forward svc/<release>-server 8000:8000`
   (or a NodePort/ingress) and targets `http://127.0.0.1:8000/mcp`. The remote script therefore
   runs from a vantage point with three reachabilities: kube API access (for the in-cluster
   mint), the forwarded MCP endpoint (for the calls), and ssh/virsh to the target host (for the
   preflight). The doc states these up front.
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
| Deployment per provider | local→host services (systemd venv) on the libvirt host, remote→Helm/k8s + TLS target | local-libvirt needs the worker to have native KVM/libvirt, which the app-tier-only compose worker lacks; remote drives a separate host so a containerized worker is fine and matches the #497 context. |
| Test depth | Documented through teardown; verified minimum = ready System via `provision` | Matches "functional test drives capability"; the full build→boot→verify→teardown path is documented, while what must be executed before publishing is at least `provision` (full path needs hardware for the remote target — see Verification). |
| Script count/scope | One per provider, onboarding-focused | Literal #497 fix on the Helm/remote path; the audited alternative to `seed-demo` on the local path. The lifecycle stays as documented commands per doc. |
| Script language | Shell (`.sh`) with inline `python -c` MCP client | Consistent with `check-*-libvirt.sh` / `demo-token.sh`; avoids pulling the `tests/` harness into `scripts/`. |
| Chart `NOTES.txt` pointer | Deferred (docs + scripts only) | Keeps the change focused; can be added later. |

## Verification

- `shellcheck` + `shfmt -d` clean on both new scripts.
- Documentation link/path checks pass (`scripts/check-doc-links.sh`,
  `scripts/check-doc-paths.sh`) so the walkthroughs' internal links resolve.
- **Each walkthrough is walked verbatim on its committed deployment before publishing** — not
  delegated to other runbooks. The doc's central claim (an operator can follow it linearly to
  a running System) is unfalsifiable unless it is actually executed once on the target
  deployment. Acceptance per doc:
  - the onboarding step flips the first `allocations.request` from `quota_exceeded` to granted
    (the #497 acceptance condition); **and**
  - the lifecycle reaches at least a **ready System via `provision`** on that deployment —
    proving the deployment can actually drive the provider, the specific gap that the
    docker-compose choice would have hidden. Reaching teardown is the goal; `provision` is the
    minimum gate.
- If the full build→boot→verify cannot be run during this change (it needs real hardware for
  the remote target), the doc says so explicitly and records what was and was not executed,
  rather than implying an unrun path works.

## Risks

- **Issuer-match trap.** A token whose `iss` does not match what the server validates
  (`KDIVE_OIDC_ISSUER`) 401s. Mitigated by minting the token under the issuer's canonical name
  — from the host for a host-process server, in-cluster (`demo-token.sh`) for a k8s server —
  not by a fixed "inside the network" rule (the host-process server reaches its issuer on a
  host-published address).
- **Issuer must assert admin claims.** `set_quota`/`set_budget` are role-gated, so the helper
  script needs a token carrying `roles:{<project>:admin}`. The bundled compose `mock-oauth2-server`
  ships unconfigured and mints no such claim; only the Helm chart's `demo/oidc.yaml` configures
  it. The local doc therefore relies on `seed-demo` (token-less) for onboarding and treats the
  audited script as conditional on a claims-asserting issuer.
- **Doc drift.** Walkthroughs that restate reference mechanics would drift. Mitigated by the
  link-don't-restate principle.
- **Demo-only token.** The bundled mock issuer mints a valid token for any caller; the scripts
  must carry a prominent demo-only warning so they are never pointed at a real deployment.
- **Local deployment needs host KVM.** The local-libvirt walkthrough only works where the
  worker runs as a host process with `/dev/kvm` and libvirt access; the app-tier-only
  docker-compose worker cannot drive the provider. The doc states this prerequisite up front
  (host services via `deploy/systemd/`, backends external/compose) so an operator does not try
  the lifecycle in a worker container that will fail at `provision`.

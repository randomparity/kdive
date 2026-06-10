# ADR 0091 — `doctor` / diagnostics model (M2.3)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0089](0089-operator-cli-mcp-client.md) (`kdivectl`
  as an authenticated MCP client — `doctor` is a curated verb on it), the M1.3 platform-role
  gate (`mcp/tools/ops/`) the diagnostics tool is authz-gated behind, the M2 in-guest
  exec/presigned-URL seam (#202) the egress probe execs through, and
  [ADR-0006](0006-oidc-rbac-attribution.md) (the `(principal, operator-cli)` audit attribution
  every diagnostics call records under).
- **Spec:** [`../superpowers/specs/2026-06-10-m23-observability-doctor-design.md`](../superpowers/specs/2026-06-10-m23-observability-doctor-design.md)
- **Milestone:** M2.3

## Context

The faults that cost the most in M2 were *undiagnosed reachability* failures — a provider TLS
chain, the gdbstub-port ACL, a secret-ref that did not resolve, and a guest→object-store
egress path silently dropped by an unrelated host `FORWARD` policy. Each surfaced only as a
downstream job failure with no pointer to the cause. M2.3 adds a `doctor` preflight that probes
these four contracts and names the **exact fix**.

The checks do not share a vantage point. `kdivectl` runs on an operator laptop; it cannot
observe the guest-bridge→object-store hop or the worker→hypervisor TLS chain from there. A
preflight that probes only from the operator's network would false-green on exactly the M2
fault class.

## Decision

1. **`doctor` is a server-side diagnostics tool surfaced as a `kdivectl` verb, not a
   client-side prober.** `kdivectl doctor` calls an authenticated diagnostics MCP tool; the
   deployment runs each probe **from its correct vantage** and returns one coherent verdict.
   A client-side-only model was rejected (it cannot see the egress or worker-vantage paths);
   a client/server hybrid was rejected as two code paths and a result-merge problem for no
   coverage gain over running everything server-side.

2. **A `Check` framework with an explicit vantage and a mandatory fix.** A `Check` is `id`,
   `vantage`, and `run() -> CheckResult{status, detail, fix}`. `fix` is the exact remediation
   string; a check that cannot name the fix is not done — naming the fix is the whole point of
   the milestone, not a nicety. The four checks and their vantages:
   - `secret_ref` — **server** vantage — every configured secret ref resolves in the backend.
   - `provider_tls` — **worker job** vantage — the provider connection's TLS chain validates
     against the configured CA.
   - `gdbstub_acl` — **worker job** vantage — the gdbstub TCP port is reachable from the
     debug-client host.
   - `guest_egress` — **ephemeral-guest** vantage — a guest on the provider bridge can reach
     object-store.

3. **The egress check provisions an ephemeral probe guest.** `doctor` is a preflight and may
   run with zero workload guests, so `guest_egress` provisions a tiny short-lived guest on the
   target provider, execs a presigned `HEAD`/`PUT` against object-store **from inside the
   guest** (the exact hop the M2 `FORWARD DROP` broke), and tears it down. A worker-host proxy
   was rejected: the worker host may take a different path and pass while the guest path is
   still broken — a false-green on the one fault this check exists for. A bring-your-own-
   allocation model was rejected as not a true cold preflight (it cannot run with zero
   allocations). The cost — provisioning a guest is heavyweight and needs a bootable image — is
   accepted because catching this fault class is the milestone's highest-payoff outcome.

4. **Same auth boundary as every tool.** The diagnostics tool is authz-gated to
   `platform_operator` (the M2.2 operator boundary), and every invocation is audited under
   `(principal, operator-cli)` (ADR-0006). `doctor` is an operator preflight, not an agent
   capability: it is not on the agent-facing tool path and runs with no raw DB credentials.

5. **`kdivectl doctor` exits nonzero on any failing check** and renders per-check
   `status`/`detail`/`fix`, so it is usable in a deployment/CI gate, not only interactively.
   The verdict carries each probe's individual result as independently-checkable evidence —
   `doctor` is built in this same band and cannot be its own sole oracle for the band gate.

## Consequences

- A new `kdive/diagnostics/` package (the framework + the four checks) and a new
  `mcp/tools/ops/diagnostics.py` tool; the egress check adds a worker job that provisions and
  reaps a probe guest. No change to the provider seam or the agent-facing tool surface.
- The probe guest needs a minimal bootable image on the target provider; on local-libvirt this
  reuses the existing fixture image, and the M2.4 image-lifecycle work makes the per-provider
  probe image first-class.
- Each check is tested against a seeded-broken and a seeded-healthy fixture, asserting status
  **and** the exact `fix` string; the milestone exit test (spec issue 8) seeds all four faults
  and asserts `doctor` names each fix — the failure is asserted, not assumed.
- `doctor` is the consumer that justifies ADR-0089's note that operator-CLI calls arriving
  under the agent `client_id` should be flagged; that flag is a `secret_ref`-adjacent
  configuration check candidate but is not in the four-check exit scope.

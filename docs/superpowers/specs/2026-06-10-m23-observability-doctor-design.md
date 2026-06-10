# M2.3 — Observability & doctor — Design

**Status:** proposed · **Date:** 2026-06-10 · **Milestone band:** M2.x productionization
**Owner:** David Christensen
**ADRs:** ADR-0090 (OpenTelemetry adoption, log-signal migration & service health),
ADR-0091 (doctor / diagnostics model)
**Band parent:** `docs/superpowers/specs/2026-06-10-m2x-productionization-band-design.md`

## Context

Driving M2 (remote libvirt) end-to-end on real hardware showed kdive is not yet
operable by anyone but its author. Two day-2 gaps dominated the lost time:

1. **No operational visibility.** The platform emits structured JSON logs
   (ADR-0014) but no metrics and no traces. A wedged worker, a slow tool, or a
   reconcile loop falling behind is invisible until a human reads logs by hand.
   The three processes (`server`, `worker`, `reconciler`) expose no service health
   surface, so a deployment cannot tell "process up" from "process able to do
   work" — the M2.1 compose/Helm reference has nothing to probe.
2. **Silent contract violations.** The faults that cost the most in M2 were
   *undiagnosed reachability* failures: a provider TLS chain, the gdbstub-port
   ACL, a secret-ref that did not resolve, and a guest→object-store egress path
   silently dropped by an unrelated `FORWARD` policy. Each surfaced only as a
   downstream job failure with no pointer to the real cause.

M2.3 closes both gaps. It is provider-agnostic and acts on the **service**, not a
provider — it runs against a venv deployment exactly as against the M2.1 image.

## Decision

Deliver two tracks that share only the `KDIVE_*` config seam (ADR-0087):

- **Telemetry & health** — adopt OpenTelemetry as the signal spine (logs, metrics,
  traces), instrument all three processes, and add service health/readiness
  endpoints the M2.1 deployment can probe.
- **Diagnostics & doctor** — a server-side diagnostics tool that runs the four
  contract probes each from its correct vantage, surfaced as `kdivectl doctor`,
  which names the **exact fix** for each failing check.

### Telemetry & health

**All-in on OpenTelemetry as the signal spine.** Metrics and traces are net-new;
logs migrate onto the OTel log pipeline. One `LoggerProvider` per process; the
existing `logging.getLogger(...)` call sites are bridged in unchanged via
`opentelemetry.instrumentation.logging.LoggingHandler`, so there is no call-site
churn. Trace context (`trace_id` / `span_id`) is attached to log records
**natively** by OTel under an active span — the hand-rolled context injection of
earlier drafts is not needed.

**Dual log export, stdout is the floor.** The log pipeline carries two exporters:

- a **stdout exporter that preserves ADR-0014's JSON field schema** — always on.
  Under Kubernetes the kubelet scrapes it; under systemd journald captures it; in
  a bare venv it is on the terminal. stdout is the local-capture floor for *every*
  deployment shape, and it does **not** depend on the pre-stable `_logs` API.
- an **OTLP exporter** for active cross-host push to a collector —
  **configurable, default-off**. stdout-only is a complete, correct deployment;
  enabling `KDIVE_OTEL_*` adds central aggregation. This keeps the venv/systemd
  consumption model (how M2 was actually run) first-class without forcing a
  collector on anyone.

**`bind_context` survives** as kdive *domain* context (`request_id`, `job_id`,
`principal`, `object_id`, `transition`), carried as OTel log attributes. It is
orthogonal to trace context and still the primary correlation key for a single
request/job across processes.

**Redaction runs before export, on the pipeline, for both exporters.**
`SecretRedactionFilter` currently sits on the stdlib path. The moment a record can
leave the host over OTLP, redaction must run pre-export — shipping an unredacted
secret to an external collector is worse than a noisy local log. This is an
explicit ADR-0090 invariant and a dedicated test: a record containing a registered
secret is redacted in **both** the stdout and the OTLP output.

**Metrics & traces.** Per-process `service.name` resource attribute. Server: a
span per MCP request (hooked into existing `mcp/middleware.py`) and RED metrics
(rate / errors / duration) per tool. Worker: span per job, job-duration and
queue-depth metrics. Reconciler: span per pass, reconcile-lag metric. Metrics
export over OTLP (default-off, same switch as logs); a `/metrics` endpoint also
exposes them for scrape (below) so a Prometheus-style pull works without a
collector.

**Service health endpoints on all three processes.** The server is already an
HTTP app; the **worker and reconciler each bind a minimal auxiliary HTTP listener**
on a side port. All three expose:

- `/livez` — process is up and its event loop is turning (liveness).
- `/readyz` — the process can do work: a **shared backend-health probe**
  (Postgres `SELECT 1`, MinIO bucket `HEAD`, OIDC discovery reachable) reports
  ready only when every dependency that process needs is reachable. A backend down
  ⇒ `/readyz` not-ready.
- `/metrics` — scrape surface for the process's metrics.

The shared backend-health module is one implementation consumed by all three
`/readyz` handlers — not three copies. Compose and Helm (M2.1 artifacts) wire
liveness/readiness probes and the scrape annotations to these endpoints.

### Diagnostics & doctor

**A server-side diagnostics tool, surfaced as `kdivectl doctor`.** The four checks
live at different vantage points; `kdivectl` on an operator laptop cannot observe
the guest→object-store path or the worker→hypervisor TLS chain directly. So
`doctor` does **not** probe from the operator's network — it calls an
authenticated diagnostics MCP tool, and the deployment runs each probe from the
correct vantage and returns one coherent verdict.

**Check framework** (`kdive/diagnostics/`, new). A `Check` is `id`, `vantage`, and
`run() -> CheckResult{status, detail, fix}`, where `fix` is the exact remediation
string (the band's "names the exact fix" requirement — a check that cannot name
the fix is not done). The four checks:

| Check | Vantage | What it probes | Example fix string |
|-------|---------|----------------|--------------------|
| `provider_tls` | worker (job) | the provider connection's TLS chain validates against the configured CA | "virtproxyd cert not signed by configured CA `<path>`; reissue or set `KDIVE_PROVIDER_CA`" |
| `gdbstub_acl` | worker (job) | the gdbstub TCP port is reachable from the debug-client host | "gdbstub port `<n>` on `<host>` unreachable; open the host firewall / ACL for it" |
| `secret_ref` | server | every configured secret ref resolves in the secret backend | "secret ref `<name>` does not resolve under `KDIVE_SECRETS_DIR`; create the file-ref or fix the path" |
| `guest_egress` | ephemeral guest | a guest on the provider bridge can reach object-store | "guest bridge → object-store blocked (likely host `FORWARD` DROP); allow the guest subnet → MinIO" |

**The egress check provisions an ephemeral probe guest.** `doctor` is a preflight —
it may run with zero workload guests. The `guest_egress` check provisions a tiny
short-lived probe guest on the target provider, execs a presigned `HEAD`/`PUT`
against object-store **from inside the guest** (the exact guest-bridge→object-store
hop the M2 `FORWARD DROP` broke), and tears it down. A worker-host proxy was
rejected: the worker host may take a different network path and pass while the real
guest path is still broken — false-green on the one fault this check exists for.

**Authentication is the same boundary as every tool.** The diagnostics tool is
authz-gated to `platform_operator` (same as the M2.2 admin surface), and every
invocation is audited under `(principal, operator-cli)`. `doctor` is an operator
preflight, not an agent capability — it does not run with raw DB credentials and is
not exposed on the agent-facing tool path.

**`kdivectl doctor` verb** calls the tool, renders the verdict as a table (per
check: status, detail, fix), and exits nonzero if any check fails — so it is usable
in a deployment gate / CI step, not only interactively.

## Components & isolation

- `kdive/observability/` (new) — OTel facade: provider init, exporter wiring
  (stdout JSON + OTLP), the `KDIVE_OTEL_*` config binding, the redaction-on-export
  hook. **Isolates the pre-stable `_logs` SDK API** behind one module so an
  upstream API shift is a single-file change. *Depends on:* `kdive.config`,
  `kdive.security.secrets`.
- `kdive/health/` (new) — the shared backend-health probe + the `/livez` `/readyz`
  `/metrics` handlers and the minimal aux HTTP listener used by worker/reconciler.
  *Depends on:* the DB / object-store / OIDC clients, `kdive/observability`.
- `kdive/diagnostics/` (new) — the `Check` framework, the four checks, and the
  aggregating diagnostics service. *Depends on:* providers (TLS / gdbstub probes),
  the guest-agent exec seam (M2 #202), secret registry, object-store client.
- `mcp/tools/ops/diagnostics.py` (new) — the authz-gated diagnostics MCP tool.
  *Depends on:* `kdive/diagnostics`, the M1.3 platform-role gate.
- `cli/commands/` — the `doctor` verb (read/verdict rendering, exit code).
  *Depends on:* the diagnostics tool over the authenticated transport.

Each unit answers cleanly: what it does, how it is used, what it depends on. The
telemetry track and the doctor track share no code, only the `KDIVE_*` config seam.

## Decomposition (epic + 8 sub-issues)

**Telemetry track**

1. **OTel signal foundation** — SDK + exporters (stdout JSON preserving the
   ADR-0014 schema + OTLP, default-off), `LoggingHandler` bridge, native trace
   correlation, redaction-on-export, the `KDIVE_OTEL_*` config binding, per-process
   `service.name`. The `kdive/observability/` facade. *Blocks 2–4.* (ADR-0090)
2. **Server telemetry + health** — request spans, per-tool RED metrics, `/livez`
   `/readyz` `/metrics` on the server app, the shared backend-health probe.
3. **Worker/reconciler telemetry + aux health listener** — the minimal aux HTTP
   listener, job/reconcile spans + metrics, `/readyz` via the shared probe.
   *Shares the backend-health module with 2.* *Depends on 1.*
4. **Deployment probe + scrape wiring** — compose/Helm liveness/readiness/scrape
   config for all three processes; the generated config reference gains the
   `KDIVE_OTEL_*` keys. *Depends on 2 + 3.*

**Doctor track** *(independent of 1–4; depends only on M2.2 CLI + M2 #202 exec
seam, both merged)*

5. **Diagnostics framework + server/worker-vantage probes + the MCP tool** —
   the `Check`/`CheckResult` abstraction, `secret_ref` (server), `provider_tls`
   and `gdbstub_acl` (worker jobs), and the authz-gated aggregating diagnostics
   tool. (ADR-0091)
6. **Ephemeral-probe-guest egress check** — the `guest_egress` check: provision a
   probe guest, exec the presigned HEAD/PUT from inside, tear down. *Depends on 5's
   framework.* (heaviest issue)
7. **`kdivectl doctor` verb** — calls the tool, renders the verdict, sets the exit
   code. *Depends on 5.*
8. **Fault-seeding exit-criterion proof + operator runbook** — seed each of the
   four faults, assert `doctor` names the exact fix; assert `/readyz` goes
   not-ready with a backend down; the operator runbook. Mirrors the M2.2
   boundary-test pattern. *Depends on all.*

## Testing & exit criteria

**Testing.** Telemetry: unit-assert that a record with a registered secret is
redacted in **both** stdout and OTLP output; that a span emitted under a request
carries the `trace_id` onto its log records; that `/readyz` flips not-ready when a
stubbed backend probe fails. Diagnostics: each check tested against a seeded-broken
and a seeded-healthy fixture, asserting status **and** the exact `fix` string
(behavior, not implementation). The egress check is exercised against the live
remote stack (operator-run), since it provisions a real guest.

**Per-milestone exit criteria (band-aligned).**

- `doctor` flags each of the four seeded faults — broken TLS chain, closed gdb
  ACL, missing secret ref, blocked guest→object-store egress — **with the exact
  fix** (issue 8 asserts this, it is not assumed).
- `/readyz` reports not-ready when a backend is down, on all three processes.
- The three processes emit metrics and traces over OTLP when `KDIVE_OTEL_*` is set,
  and JSON logs to stdout always; no secret appears in either sink.

These feed the band gate (M3-entry signal): an operator-not-the-author runs
`doctor` on a fresh two-host setup and the record carries each probe's individual
result as independently-checkable evidence — `doctor` is built in this same band,
so it cannot be its own sole oracle.

## Consequences

- **ADR-0014 is amended, not discarded.** The JSON-on-stdout contract, the field
  schema, and `bind_context` all survive; the transport becomes the OTel log
  pipeline and trace correlation becomes native. ADR-0090 records the amendment.
- **New dependencies:** `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc` (and/or `-http`),
  `opentelemetry-instrumentation-logging`. Pinned exact versions; the `_logs`
  SDK API is pre-stable, so the `kdive/observability/` facade isolates it and the
  stdout floor never depends on it.
- The provider seam and the agent-facing MCP tool surface are unchanged.
  Diagnostics is an operator surface alongside the M2.2 admin CLI, on the same
  service layer and authz boundary.
- No renumbering of M3/M4/M5; M2.3 sits in the M2.x band per the parent spec.

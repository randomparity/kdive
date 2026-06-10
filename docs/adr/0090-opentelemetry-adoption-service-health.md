# ADR 0090 — OpenTelemetry adoption, log-signal migration & service health (M2.3)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Amends (does not discard):** [ADR-0014](0014-structured-logging.md) (the JSON-on-stdout
  structured-logging contract, its field schema, and `bind_context` — all preserved; what
  changes is the transport, now the OTel log pipeline, and trace correlation, now native).
- **Builds on:** [ADR-0087](0087-config-registry.md) (the `KDIVE_*` registry the
  `KDIVE_OTEL_*` keys extend), [ADR-0088](0088-deployment-packaging.md) (the M2.1 image /
  compose / Helm reference whose probes target the new health endpoints).
- **Spec:** [`../superpowers/specs/2026-06-10-m23-observability-doctor-design.md`](../superpowers/specs/2026-06-10-m23-observability-doctor-design.md)
- **Milestone:** M2.3

## Context

kdive emits structured JSON logs (ADR-0014) but no metrics and no traces, and its three
processes (`server`, `worker`, `reconciler`) expose no service-health surface. Driving M2 on
real hardware made both gaps costly: a wedged worker or a reconcile loop falling behind is
invisible, and the M2.1 deployment reference has nothing to probe for "able to do work" versus
"process up." ADR-0014 deliberately took **no third-party dependency**; metrics and traces
cannot be added without reversing that for at least the telemetry signals.

Three codebase facts constrain the choice:

- Logging is centralized in `src/kdive/log.py` with a fixed field schema and a
  `SecretRedactionFilter` on the emit path; many call sites use the plain `logging` API.
- The worker (`jobs/worker.py`) and reconciler (`reconciler/loop.py`) are asyncio loop
  processes with **no HTTP server**; only the FastMCP `server` is HTTP today.
- The platform's flagship M2.3 feature, `doctor`, exists because *reachability silently
  breaks*. A telemetry pipeline that goes dark on unreachability would share that failure mode.

## Decision

1. **Adopt OpenTelemetry as the single signal spine — logs, metrics, and traces.** Metrics
   and traces are net-new. Logs **migrate onto the OTel log pipeline**: one `LoggerProvider`
   per process, with existing `logging.getLogger(...)` call sites bridged in unchanged via
   `opentelemetry.instrumentation.logging.LoggingHandler` (no call-site churn). Trace context
   (`trace_id`/`span_id`) attaches to log records **natively** under an active span; no
   hand-rolled context injection.

2. **Dual log export; stdout is the floor, OTLP is opt-in.** The log pipeline carries two
   exporters: a **stdout exporter that preserves ADR-0014's exact JSON field schema** (always
   on — kubelet scrapes it under k8s, journald captures it under systemd, it is on the
   terminal in a bare venv), and an **OTLP exporter for cross-host push, default-off**,
   enabled by `KDIVE_OTEL_*`. stdout-only is a complete, correct deployment. This keeps the
   venv/systemd consumption model (how M2 was run) first-class and ensures the observability
   pipeline does **not** share the unreachability failure mode `doctor` diagnoses. Metrics and
   traces export over OTLP under the same switch; a `/metrics` scrape endpoint also exposes
   metrics so a pull-based collector works without OTLP.

3. **`bind_context` survives as domain context.** `request_id`, `job_id`, `principal`,
   `object_id`, `transition` are carried as OTel log attributes — orthogonal to trace context
   and still the primary key for correlating a request/job across processes.

4. **Redaction runs before export, on the pipeline, for every exporter.** The moment a record
   can leave the host over OTLP, `SecretRedactionFilter` must run pre-export. A record holding
   a registered secret is redacted in **both** stdout and OTLP output — an explicit invariant
   with a dedicated test. Shipping an unredacted secret to an external collector is a worse
   failure than a noisy local log, so this is not left to the existing stdlib-path filter
   placement; it is asserted on the OTel pipeline.

5. **Service health on all three processes via a minimal aux HTTP listener.** The server is
   already HTTP; the worker and reconciler each bind a small auxiliary HTTP listener on a side
   port. All three expose `/livez` (process up, loop turning), `/readyz` (a **shared**
   backend-health probe — Postgres `SELECT 1`, MinIO bucket `HEAD`, OIDC discovery reachable —
   not-ready when any needed dependency is down), and `/metrics`. One backend-health
   implementation is consumed by all three `/readyz` handlers. A heartbeat-file/exec-probe and
   a push-only/process-alive model were both rejected: the aux listener gives a uniform probe
   model across processes and a real "not-ready when a backend is down" signal that a
   process-alive check cannot.

6. **Isolate the pre-stable logs SDK behind a facade.** The Python OTel **logs signal is still
   under the `_logs` underscore namespace** (`opentelemetry.sdk._logs`) — the data model is
   stable but the SDK API is not yet promoted, unlike metrics/traces which are GA. All OTel
   wiring lives in `kdive/observability/`, so an upstream API shift is a single-file change.
   Combined with decision 2, the stdout floor does not depend on the `_logs` API at all, so
   logging is never hostage to a pre-stable surface.

## Consequences

- **New pinned dependencies:** `opentelemetry-api`, `opentelemetry-sdk`,
  `opentelemetry-exporter-otlp-proto-grpc` (and/or `-http`),
  `opentelemetry-instrumentation-logging`. ADR-0014's no-dependency stance is reversed for
  telemetry only; the stdout JSON path remains stdlib.
- The exact stdout-exporter mechanism that reproduces ADR-0014's JSON schema (a custom console
  log exporter vs. keeping the stdlib formatter on the console handler and bridging only the
  OTLP side) is an implementation choice settled in the foundation issue; both preserve the
  field contract and the existing log tests.
- The worker/reconciler gain a small HTTP surface they did not have; it is health/metrics only,
  bound on a side port, not an API.
- The M2.1 compose/Helm reference wires liveness/readiness/scrape to these endpoints; the
  generated config reference gains the `KDIVE_OTEL_*` keys.
- The provider seam and the agent-facing MCP tool surface are unchanged.

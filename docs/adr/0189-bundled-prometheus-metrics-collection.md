# ADR 0189 — Bundled, opt-in Prometheus to collect the aux `/metrics`

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0088](0088-deployment-packaging.md) (the compose +
  Helm reference this extends), [ADR-0090](0090-opentelemetry-adoption-service-health.md) (the
  per-process aux `/livez` `/readyz` `/metrics` listener whose metrics this collects).
- **Spec:** [`../design/bundled-prometheus-metrics-collection.md`](../design/bundled-prometheus-metrics-collection.md)
- **Related:** #600 (this), #601 (enrich the emitted series — out of scope here).

## Context

Each process exposes Prometheus metrics on its aux listener (ADR-0090 §5): `/metrics` on a
per-process port (server `9464`, worker `9465`, reconciler `9466`, bound via
`KDIVE_HEALTH_BIND_ADDR`). The Helm chart already stamps annotation-based scrape hints
(`prometheus.io/scrape`/`path`/`port`) on all three Deployments' pod templates
(`kdive.scrapeAnnotations`), and the compose binds each aux listener `0.0.0.0:<port>` on the
compose network. **Nothing scrapes them.** The series (`kdive_job_duration`,
`kdive_job_queue_depth`, `kdive_mcp_requests`, …) are produced and immediately discarded; the
only way to read them is an ad-hoc `kubectl port-forward … /metrics` or `docker compose exec`.

The aux listener is unauthenticated by design — its access boundary is the network namespace
(no Service fronts it in k8s; the port is not published in compose). Any collector must
therefore live **inside** that boundary (in-cluster / on the compose network) and must not
re-expose the raw `/metrics` off it.

## Decision

Ship an **opt-in, off-by-default** Prometheus on both deployment paths, scraping all three
components. No change to the app's metrics emission.

### Kubernetes (Helm)

1. A new `bundledObservability` value (default `false`), independent of `bundledBackends` —
   the app pods it scrapes exist on both the bundled and external-backend paths. When true the
   chart renders, under templates `demo/prometheus.yaml`:
   - a **Deployment** running `prom/prometheus:v3.12.0` with a mounted scrape config;
   - a **ConfigMap** holding `prometheus.yml` — a single `kubernetes_sd_configs` job with
     `role: pod`, scoped to the release namespace (`namespaces.names: [<release ns>]`), and the
     standard `prometheus.io/scrape` annotation relabeling (keep iff `scrape=true`, take `path`
     and `host:port` from the `path`/`port` annotations);
   - a **ServiceAccount** plus a namespaced **Role**/**RoleBinding** granting only
     `get/list/watch` on `pods` in the release namespace (pod-role SD needs no more);
   - a **ClusterIP Service** on `9090` so an operator reaches the UI by `kubectl port-forward`.
   Storage is `emptyDir` with a short retention (`--storage.tsdb.retention.time`, default
   `6h`), matching the bundled Postgres/MinIO ephemeral posture. A PVC is documented for real
   use but not templated (consistent with the demo backends).
2. **BYO / operator clusters** are documented, not templated: a cluster already running the
   Prometheus Operator should write a `PodMonitor` selecting the kdive pods (the annotations
   are equivalent metadata); a cluster with an existing Prometheus already gets the targets
   from the annotations the chart emits. The README carries a copy-pasteable `PodMonitor`.

### Compose

A `prometheus` service gated behind the `obs` compose **profile** (so the turnkey
`docker compose up` graph is unchanged), running `prom/prometheus:v3.12.0` with a committed
`deploy/compose/prometheus.yml` static-target config scraping `server:9464` / `worker:9465` /
`reconciler:9466` on the compose network. Its UI port `9090` **is** published to the host
(the UI is the point); the scraped aux ports stay unpublished. Brought up with
`docker compose --profile obs up -d prometheus`.

## Consequences

- The demo/dev paths gain durable-for-the-session metrics; an operator can confirm targets
  are `up` and query `kdive_*` without port-forward gymnastics.
- Off by default keeps production installs BYO and the turnkey compose graph minimal.
- The collector lives inside the network boundary and never re-exposes raw `/metrics` off it —
  in k8s the Prometheus Service is `9090`-only (the chart's existing "no Service fronts the aux
  port" invariant is preserved); in compose only `9090` is published.
- First RBAC in the chart. It is namespaced (Role, not ClusterRole) and minimal
  (`get/list/watch pods`), so the blast radius of the new ServiceAccount is one namespace.
- emptyDir + short retention means a Prometheus pod restart drops history — acceptable for the
  demo posture, documented, and overridable by the BYO path.

## Considered & rejected

- **A templated PodMonitor behind a value.** A `PodMonitor` is meaningless without the
  Prometheus Operator CRDs installed; rendering one unconditionally breaks `helm install` on a
  stock cluster (unknown kind). Documenting it for operator clusters avoids a CRD dependency
  while still serving them. Could be revisited as a third value if demand appears.
- **Gating Prometheus on `bundledBackends`/`demoAcknowledged`.** The metrics targets are the
  app pods, which exist on the external path too; coupling collection to the demo backends
  would deny the external path a bundled scraper for no security reason (Prometheus exposes no
  tokens). Kept orthogonal.
- **A ClusterRole for pod discovery.** Cluster-wide pod read is broader than needed; the SD
  job is namespace-scoped, so a namespaced Role suffices and is tighter.
- **Publishing the raw aux `/metrics` (NodePort/Ingress) so an external Prometheus scrapes
  directly.** Re-exposes an unauthenticated endpoint off the network boundary — exactly what
  ADR-0090 forbids. BYO scrapers run in-cluster.
- **Bundling Grafana/dashboards.** Out of scope (#600 is collection only); tracked separately.
- **PVC-backed storage by default.** Inconsistent with the bundled Postgres/MinIO emptyDir
  posture and would silently allocate a volume on a throwaway demo. Documented for real use.

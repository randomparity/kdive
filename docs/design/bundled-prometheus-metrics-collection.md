# Spec — Bundled, opt-in Prometheus to collect the aux `/metrics` (#600)

- **ADR:** [ADR-0189](../adr/0189-bundled-prometheus-metrics-collection.md)
- **Status:** Accepted
- **Issue:** #600 (collection); #601 enriches the emitted series (out of scope here).

## Problem

Every process emits Prometheus metrics on its aux listener (ADR-0090 §5): `/metrics` on a
per-process port — server `9464`, worker `9465`, reconciler `9466` (`KDIVE_HEALTH_BIND_ADDR`).
The Helm chart stamps `prometheus.io/scrape`/`path`/`port` annotations on all three
Deployments (`kdive.scrapeAnnotations`); the compose binds each aux listener on the compose
network. **Nothing scrapes them**, so the series exist only for the lifetime of an ad-hoc
`kubectl port-forward`/`docker compose exec`. This issue stands up a collector on both paths.

The aux listener is unauthenticated; its boundary is the network namespace (no Service fronts
it in k8s; the port is unpublished in compose). A collector must live inside that boundary and
must not re-expose raw `/metrics` off it.

## Goals / non-goals

- **Goal:** opt-in, off-by-default Prometheus on Helm and compose; all three components show as
  healthy `up` targets with `kdive_*` series present; BYO path documented; a runbook note.
- **Non-goal:** Grafana/dashboards (#601-adjacent, tracked separately); changing the app's
  metrics emission; production durability (emptyDir/short retention is the demo posture).

## Design

### Helm (`bundledObservability`, default `false`)

A new top-level value `bundledObservability` (independent of `bundledBackends` — the scrape
targets are the app pods, present on both paths). When true, `templates/demo/prometheus.yaml`
renders five objects, all guarded by `{{- if .Values.bundledObservability }}`:

1. **ServiceAccount** `<fullname>-prometheus`.
2. **Role** (namespaced) granting `get`/`list`/`watch` on `pods` — all a pod-role
   `kubernetes_sd_configs` needs — and a **RoleBinding** to the ServiceAccount.
3. **ConfigMap** `<fullname>-prometheus-config` holding `prometheus.yml`:
   - global `scrape_interval` (`{{ .Values.observability.scrapeInterval }}`, default `15s`);
   - one job `kdive-pods`, `kubernetes_sd_configs: [{role: pod, namespaces: {names:
     [{{ .Release.Namespace }}]}}]`;
   - relabel rules: keep iff `__meta_kubernetes_pod_annotation_prometheus_io_scrape == true`;
     `__metrics_path__` from the `prometheus.io/path` annotation; rewrite `__address__` to
     `<pod-ip>:<prometheus.io/port>`; map `pod`/`namespace`/`app.kubernetes.io/name` to labels.
4. **Deployment** `<fullname>-prometheus` running `{{ .Values.observability.image }}`
   (default `prom/prometheus:v3.12.0`), `serviceAccountName` set, args
   `--config.file=/etc/prometheus/prometheus.yml`,
   `--storage.tsdb.path=/prometheus`,
   `--storage.tsdb.retention.time={{ .Values.observability.retention }}` (default `6h`),
   `--web.enable-lifecycle`. The config ConfigMap mounts at `/etc/prometheus`; storage is an
   `emptyDir` at `/prometheus`. A `checksum/config` pod annotation hashes the rendered
   ConfigMap so a scrape-config change rolls the pod (mirrors `kdive.configChecksum`).
5. **Service** `<fullname>-prometheus`, `ClusterIP`, port `9090` → reach the UI by
   `kubectl port-forward svc/<fullname>-prometheus 9090:9090`.

Storage/retention default to the demo posture; the README documents swapping the emptyDir for
a PVC and lengthening retention for real use.

**BYO (documented, not templated):** a cluster running the Prometheus Operator writes a
`PodMonitor` selecting `app.kubernetes.io/name: kdive` (the README carries the manifest); a
cluster with an existing annotation-discovery Prometheus already gets the targets from the
annotations the chart emits.

### Compose (`obs` profile)

A `prometheus` service in `docker-compose.yml`, `profiles: ["obs"]` (so the turnkey graph is
unchanged), running `prom/prometheus:v3.12.0`, mounting a committed
`deploy/compose/prometheus.yml` (read-only) with three static targets — `server:9464`,
`worker:9465`, `reconciler:9466` — on the compose network. Port `9090` **is** published to the
host (the UI is the point); the scraped aux ports stay unpublished. Brought up with
`docker compose --profile obs up -d prometheus`.

## Acceptance criteria → verification

- `bundledObservability=true` renders Prometheus Deployment/ConfigMap/SA/Role/RoleBinding/
  Service, scrape config references all three aux ports via annotation relabeling, Service is
  ClusterIP `9090` — `tests/helm/test_helm_render.py`.
- `bundledObservability=false` (default) renders none of them; deployment count unchanged —
  `tests/helm/test_helm_render.py`.
- The compose `prometheus` service parses, is on the `obs` profile, scrapes the three aux
  ports, publishes `9090`, and the static config is valid — `tests/compose/test_compose_config.py`.
- BYO path + runbook note documented — chart README + `kubernetes-deploy.md` runbook + compose
  README.
- No change to app metrics emission — no `src/kdive/**` edits.

## Edge cases / failure modes

- **Operator absent + PodMonitor:** not templated, so a stock `helm install` never references an
  unknown CRD kind. (Rejected templating it; see ADR.)
- **RBAC minimization:** namespaced Role, `pods` `get/list/watch` only; SD scoped to the release
  namespace so it cannot read pods elsewhere.
- **`docker compose config` includes profile services:** the structural test sees the
  `prometheus` service even though it is profile-gated, so it can assert on it directly; the
  turnkey-ordering tests are unaffected because the app services do not `depends_on` it.
- **emptyDir restart drops history:** documented; acceptable for the demo posture; BYO/PVC for
  durability.
- **Aux port never re-exposed:** k8s Prometheus Service is `9090`-only; compose publishes only
  `9090`. The existing "no Service fronts the aux port" / "aux port not published" tests still
  hold.

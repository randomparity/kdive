# Bundled Prometheus Metrics Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up an opt-in, off-by-default Prometheus on both the Helm and compose deployment paths so the per-process aux `/metrics` (ADR-0090 §5) is actually collected.

**Architecture:** Helm gains a `bundledObservability` value that renders a namespaced-RBAC + ConfigMap (annotation-discovery scrape config) + Deployment + ClusterIP-9090 Service under `templates/demo/prometheus.yaml`. Compose gains a `prometheus` service behind the `obs` profile with a committed static-target `deploy/compose/prometheus.yml`. Tests parse the rendered/committed `prometheus.yml` *content* (not just the wrapping manifest) so a valid-but-wrong scrape config fails CI. No `src/kdive/**` change.

**Tech Stack:** Helm (Go templates), Docker Compose, Prometheus `prom/prometheus:v3.12.0`, pytest (shells out to real `helm`/`docker compose`), PyYAML.

## Global Constraints

- ADR: [ADR-0189](../../adr/0189-bundled-prometheus-metrics-collection.md); spec: [`docs/design/bundled-prometheus-metrics-collection.md`](../../design/bundled-prometheus-metrics-collection.md).
- Aux ports are the contract: server `9464`, worker `9465`, reconciler `9466` (`_AUX_PORTS`).
- Prometheus image pinned `prom/prometheus:v3.12.0` (no `:latest`); every image in this repo is tag-pinned.
- Helm objects all guarded `{{- if .Values.bundledObservability }}`; default `false`. Independent of `bundledBackends`/`demoAcknowledged`.
- k8s RBAC is **namespaced** (Role, not ClusterRole): `get`/`list`/`watch` on `pods` only, SD scoped to `{{ .Release.Namespace }}`.
- Never re-expose the aux `/metrics` off the network boundary: k8s Prometheus Service is `9090`-only; compose publishes only `9090`.
- Doc style: plain/factual; **Milestone** not "Sprint"; no "critical/robust/comprehensive/elegant".
- Guardrails before each commit: `just lint` (touches no py unless tests), `just type`, the touched test module, plus the doc guards (`just docs-links`, `just check-mermaid`) when docs change. Full `just`-equivalent CI recipes run once before push (see step 7).
- Commit trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Helm values for `bundledObservability`

**Files:**
- Modify: `deploy/helm/kdive/values.yaml`

**Interfaces:**
- Produces: `.Values.bundledObservability` (bool, default false); `.Values.observability.{image,retention,scrapeInterval,uiPort}` consumed by Task 2's template.

- [ ] **Step 1:** Add the values block after the `bundledBackends`/`demoAcknowledged` block, with a comment explaining it is independent of `bundledBackends`, off by default, emptyDir/short-retention demo posture:

```yaml
# bundledObservability deploys an in-cluster Prometheus that scrapes all three components'
# aux /metrics (ADR-0090 §5) via the prometheus.io/scrape annotations the chart already emits.
# Independent of bundledBackends (the scrape targets are the app pods, present on both paths)
# and off by default (production installs are BYO — see the chart README). It runs on emptyDir
# with short retention (demo posture); a Prometheus pod restart drops history. Reach the UI with
# `kubectl port-forward svc/<release>-kdive-prometheus 9090:9090`. ADR-0189.
bundledObservability: false
observability:
  image: prom/prometheus:v3.12.0
  retention: 6h
  scrapeInterval: 15s
  uiPort: 9090
```

- [ ] **Step 2:** `helm lint deploy/helm/kdive` — Expected: 0 chart(s) failed (no template references it yet, so values-only is inert).
- [ ] **Step 3:** Commit `feat(helm): add bundledObservability values (off by default)`.

---

### Task 2: Helm Prometheus manifests + render/content tests

**Files:**
- Create: `deploy/helm/kdive/templates/demo/prometheus-config.yaml` (the scrape-config ConfigMap, in its own file)
- Create: `deploy/helm/kdive/templates/demo/prometheus.yaml` (SA/Role/RoleBinding/Deployment/Service)
- Test: `tests/helm/test_helm_render.py` (append)

**Interfaces:**
- Consumes: `.Values.bundledObservability`, `.Values.observability.*` (Task 1); `kdive.fullname`, `kdive.labels` helpers.
- Produces: ServiceAccount/Role/RoleBinding/Deployment/Service named `<fullname>-prometheus` (`prometheus.yaml`) and a `<fullname>-prometheus-config` ConfigMap (`prometheus-config.yaml`).

**Why two files:** the Deployment hashes the ConfigMap into a `checksum/config` pod annotation so a scrape-config edit rolls the pod (mirrors `kdive.configChecksum`, which hashes `configmap.yaml` from the deployment files). The hash uses `include (print $.Template.BasePath "/demo/prometheus-config.yaml") .` — it MUST point at a *different* file than the one it lives in, or the include recurses into itself and `helm template` aborts. So the ConfigMap lives in its own `prometheus-config.yaml`.

- [ ] **Step 1: Write the failing tests.** Append to `tests/helm/test_helm_render.py` — helpers that re-template with `bundledObservability=true` and parse the ConfigMap's `prometheus.yml` as YAML:

```python
def _obs_docs(*set_args: str) -> list[dict[str, Any]]:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y", "bundledObservability=true", *set_args)
    assert res.returncode == 0, res.stderr
    return [d for d in yaml.safe_load_all(res.stdout) if isinstance(d, dict)]


def _obs_kind(docs: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(d for d in docs if d.get("kind") == kind and "prometheus" in d["metadata"]["name"])


def _scrape_config(docs: list[dict[str, Any]]) -> dict[str, Any]:
    cm = next(
        d for d in docs
        if d.get("kind") == "ConfigMap" and d["metadata"]["name"].endswith("-prometheus-config")
    )
    return yaml.safe_load(cm["data"]["prometheus.yml"])


def test_observability_off_by_default_renders_no_prometheus() -> None:
    res = _template("config.KDIVE_DATABASE_URL=postgresql://x/y")
    assert res.returncode == 0, res.stderr
    assert "-prometheus" not in res.stdout


def test_observability_renders_rbac_deployment_and_clusterip_service() -> None:
    docs = _obs_docs()
    assert _obs_kind(docs, "ServiceAccount")
    role = _obs_kind(docs, "Role")
    rules = role["rules"][0]
    assert rules["resources"] == ["pods"]
    assert sorted(rules["verbs"]) == ["get", "list", "watch"]
    assert _obs_kind(docs, "RoleBinding")
    svc = _obs_kind(docs, "Service")
    assert svc["spec"]["type"] == "ClusterIP"
    assert {p["port"] for p in svc["spec"]["ports"]} == {9090}
    dep = _obs_kind(docs, "Deployment")
    assert dep["spec"]["template"]["spec"]["serviceAccountName"].endswith("-prometheus")


def test_observability_scrape_config_uses_annotation_relabeling() -> None:
    cfg = _scrape_config(_obs_docs())
    job = cfg["scrape_configs"][0]
    sd = job["kubernetes_sd_configs"][0]
    assert sd["role"] == "pod"
    assert sd["namespaces"]["names"] == ["default"]  # helm template's default release ns
    rels = job["relabel_configs"]
    keep = next(r for r in rels if r.get("action") == "keep")
    assert keep["source_labels"] == ["__meta_kubernetes_pod_annotation_prometheus_io_scrape"]
    assert keep["regex"] == "true"
    paths = [r for r in rels if r.get("target_label") == "__metrics_path__"]
    assert paths and paths[0]["source_labels"] == ["__meta_kubernetes_pod_annotation_prometheus_io_path"]
    addr = next(r for r in rels if r.get("target_label") == "__address__")
    assert "__meta_kubernetes_pod_annotation_prometheus_io_port" in addr["source_labels"]
    assert "__meta_kubernetes_pod_ip" in addr["source_labels"]


def test_observability_independent_of_bundled_backends() -> None:
    # Renders on the external path (no bundledBackends) — the targets are the app pods.
    docs = _obs_docs()
    assert _obs_kind(docs, "Deployment")
    assert "mock-oauth2-server" not in yaml.safe_dump_all(docs)
```

- [ ] **Step 2: Run to verify they fail.** Run: `uv run python -m pytest tests/helm/test_helm_render.py -k observability -q` — Expected: FAIL (no `-prometheus` docs; `next()` StopIteration).
- [ ] **Step 3a: Write the scrape-config ConfigMap** in its own file `deploy/helm/kdive/templates/demo/prometheus-config.yaml` (full content):

```yaml
{{- if .Values.bundledObservability }}
# Scrape config for the opt-in Prometheus (ADR-0189), in its own file so the Deployment in
# prometheus.yaml can hash it into a checksum/config annotation without a self-referential
# include. Annotation discovery: keep pods with prometheus.io/scrape=true, take the metrics path
# and port from the prometheus.io/path & prometheus.io/port annotations the chart already stamps.
apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus-config
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
data:
  prometheus.yml: |
    global:
      scrape_interval: {{ .Values.observability.scrapeInterval }}
    scrape_configs:
      - job_name: kdive-pods
        kubernetes_sd_configs:
          - role: pod
            namespaces:
              names: [{{ .Release.Namespace }}]
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
            action: keep
            regex: "true"
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
            action: replace
            target_label: __metrics_path__
            regex: (.+)
          - source_labels: [__meta_kubernetes_pod_ip, __meta_kubernetes_pod_annotation_prometheus_io_port]
            action: replace
            target_label: __address__
            regex: (.+);(.+)
            replacement: $1:$2
          - source_labels: [__meta_kubernetes_pod_label_app_kubernetes_io_name]
            target_label: app
          - source_labels: [__meta_kubernetes_namespace]
            target_label: namespace
          - source_labels: [__meta_kubernetes_pod_name]
            target_label: pod
{{- end }}
```

- [ ] **Step 3b: Write the RBAC + Deployment + Service** in `deploy/helm/kdive/templates/demo/prometheus.yaml` (full content):

```yaml
{{- if .Values.bundledObservability }}
# Opt-in in-cluster Prometheus (ADR-0189): scrapes the per-process aux /metrics (ADR-0090 §5)
# via the prometheus.io/scrape annotations the chart stamps on every component pod. emptyDir +
# short retention (demo posture); ClusterIP only — reach the UI with `kubectl port-forward`.
# The scrape-config ConfigMap is in demo/prometheus-config.yaml (separate file: the checksum
# annotation below hashes it, which would recurse if it lived here).
apiVersion: v1
kind: ServiceAccount
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
---
# Namespaced pod-read for kubernetes_sd_configs (role: pod). A Role (not ClusterRole) keeps the
# blast radius to the release namespace, which is also where the SD job is scoped.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: {{ include "kdive.fullname" . }}-prometheus
subjects:
  - kind: ServiceAccount
    name: {{ include "kdive.fullname" . }}-prometheus
    namespace: {{ .Release.Namespace }}
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: {{ include "kdive.fullname" . }}-prometheus
  template:
    metadata:
      labels:
        app: {{ include "kdive.fullname" . }}-prometheus
        {{- include "kdive.labels" . | nindent 8 }}
      annotations:
        checksum/config: {{ include (print $.Template.BasePath "/demo/prometheus-config.yaml") . | sha256sum }}
    spec:
      serviceAccountName: {{ include "kdive.fullname" . }}-prometheus
      containers:
        - name: prometheus
          image: {{ .Values.observability.image }}
          args:
            - --config.file=/etc/prometheus/prometheus.yml
            - --storage.tsdb.path=/prometheus
            - --storage.tsdb.retention.time={{ .Values.observability.retention }}
            - --web.enable-lifecycle
          ports:
            - containerPort: 9090
          volumeMounts:
            - name: config
              mountPath: /etc/prometheus
              readOnly: true
            - name: data
              mountPath: /prometheus
      volumes:
        - name: config
          configMap:
            name: {{ include "kdive.fullname" . }}-prometheus-config
        - name: data
          emptyDir: {}
---
apiVersion: v1
kind: Service
metadata:
  name: {{ include "kdive.fullname" . }}-prometheus
  labels:
    {{- include "kdive.labels" . | nindent 4 }}
spec:
  type: ClusterIP
  selector:
    app: {{ include "kdive.fullname" . }}-prometheus
  ports:
    - port: {{ .Values.observability.uiPort }}
      targetPort: 9090
{{- end }}
```

- [ ] **Step 4: Run to verify pass.** Run: `uv run python -m pytest tests/helm/test_helm_render.py -q` — Expected: PASS (new + the existing 6-deployment / external-3 tests still pass, since observability is off in them).
- [ ] **Step 5:** `helm lint deploy/helm/kdive` — Expected: 0 chart(s) failed.
- [ ] **Step 6:** Commit `feat(helm): render opt-in Prometheus with namespaced pod-SD scrape config`.

---

### Task 3: Compose `prometheus` service + static config + content tests

**Files:**
- Modify: `docker-compose.yml`
- Create: `deploy/compose/prometheus.yml`
- Test: `tests/compose/test_compose_config.py` (append)

**Interfaces:**
- Consumes: the `server`/`worker`/`reconciler` services and their aux ports.
- Produces: a `prometheus` service (profile `obs`) and the committed scrape config.

- [ ] **Step 1: Write the failing tests.** Append to `tests/compose/test_compose_config.py`. NOTE: `docker compose config` OMITS profile-gated services unless the profile is active (verified on Compose v5.x), so the existing `_services()` (no `--profile`) would never see `prometheus` — these tests render with the `obs` profile enabled via a dedicated helper:

```python
import yaml  # add to imports at top of file

_PROM_CONFIG = Path(__file__).resolve().parents[2] / "deploy" / "compose" / "prometheus.yml"


def _services_with_obs_profile() -> dict[str, Any]:
    # `docker compose config` drops profile-gated services unless the profile is active, so
    # render with `--profile obs` to make the prometheus service appear in the model.
    res = subprocess.run(
        ["docker", "compose", "-f", str(_COMPOSE_FILE), "--profile", "obs",
         "config", "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, f"compose config invalid: {res.stderr}"
    return json.loads(res.stdout)["services"]


def test_prometheus_service_is_obs_profile_only() -> None:
    # Present when the profile is active...
    assert "prometheus" in _services_with_obs_profile()
    # ...and absent from the default (no-profile) model, so the turnkey graph is unchanged.
    assert "prometheus" not in _services()
    assert _services_with_obs_profile()["prometheus"]["profiles"] == ["obs"]


def test_prometheus_publishes_ui_but_not_the_scraped_ports() -> None:
    prom = _services_with_obs_profile()["prometheus"]
    published = {str(p.get("published")) for p in prom.get("ports", [])}
    assert "9090" in published
    for aux in _AUX_PORTS.values():
        assert str(aux) not in published


def test_prometheus_static_config_targets_every_aux_port() -> None:
    cfg = yaml.safe_load(_PROM_CONFIG.read_text())
    targets: set[str] = set()
    for job in cfg["scrape_configs"]:
        for sc in job["static_configs"]:
            targets.update(sc["targets"])
    assert targets == {f"{svc}:{port}" for svc, port in _AUX_PORTS.items()}
```

- [ ] **Step 2: Run to verify they fail.** Run: `uv run python -m pytest tests/compose/test_compose_config.py -k prometheus -q` — Expected: FAIL (`KeyError: 'prometheus'` from `_services_with_obs_profile` / missing `prometheus.yml` for the static-config test).
- [ ] **Step 3: Create the scrape config** `deploy/compose/prometheus.yml`:

```yaml
# Static-target scrape config for the reference compose's opt-in Prometheus (ADR-0189).
# Targets each process's aux /metrics (ADR-0090 §5) by compose service name on the compose
# network; those ports are never published to the host. Brought up with the `obs` profile:
#   docker compose --profile obs up -d prometheus
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: kdive
    static_configs:
      - targets:
          - server:9464
          - worker:9465
          - reconciler:9466
```

- [ ] **Step 4: Add the compose service.** Append to `docker-compose.yml` (before the `volumes:` block), and document the obs profile in a comment:

```yaml
  # Opt-in metrics collection (ADR-0189). Behind the `obs` profile so the turnkey
  # `docker compose up` graph is unchanged. Scrapes each process's aux /metrics over the
  # compose network (those aux ports stay unpublished); only the UI is published to the host.
  #   docker compose --profile obs up -d prometheus    # then open http://localhost:9090
  # TSDB is ephemeral container-local (no named volume) — matches the demo posture; a
  # `docker compose down` drops history. Only the config is mounted (read-only).
  prometheus:
    image: prom/prometheus:v3.12.0
    profiles: ["obs"]
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=6h
    volumes:
      - ./deploy/compose/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    ports:
      - "9090:9090"
```

- [ ] **Step 5: Run to verify pass.** Run: `uv run python -m pytest tests/compose/test_compose_config.py -q` — Expected: PASS (new + the existing turnkey-ordering tests, which are unaffected because nothing `depends_on` prometheus).
- [ ] **Step 6:** Commit `feat(compose): add obs-profile Prometheus scraping the aux metrics`.

---

### Task 4: Optional `promtool check config` semantic gate

**Files:**
- Test: `tests/helm/test_helm_render.py` and `tests/compose/test_compose_config.py` (append one test each)

**Interfaces:**
- Consumes: rendered Helm ConfigMap `prometheus.yml` (Task 2), committed compose `prometheus.yml` (Task 3).

- [ ] **Step 1: Write the tests** (gated on a `promtool` binary, skip cleanly when absent — like the `helm` gate). Helm side:

```python
def test_observability_scrape_config_passes_promtool() -> None:
    if shutil.which("promtool") is None:
        pytest.skip("promtool not installed")
    cm_yaml = _scrape_config(_obs_docs())
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=True) as fh:
        yaml.safe_dump(cm_yaml, fh)
        fh.flush()
        res = subprocess.run(["promtool", "check", "config", fh.name],
                             capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
```

Compose side:

```python
def test_prometheus_static_config_passes_promtool() -> None:
    if shutil.which("promtool") is None:
        pytest.skip("promtool not installed")
    res = subprocess.run(["promtool", "check", "config", str(_PROM_CONFIG)],
                         capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr
```

- [ ] **Step 2: Run.** Run: `uv run python -m pytest tests/helm/test_helm_render.py tests/compose/test_compose_config.py -k promtool -q` — Expected: PASS or SKIP (skip if promtool absent locally; if present, must pass — fix the config if it does not).
- [ ] **Step 3:** Commit `test: add optional promtool semantic check for both scrape configs`.

---

### Task 5: Documentation — chart README, compose README, runbook

**Files:**
- Modify: `deploy/helm/kdive/README.md`
- Modify: `deploy/compose/README.md`
- Modify: `docs/operating/runbooks/kubernetes-deploy.md`

**Interfaces:** none (docs only).

- [ ] **Step 1:** Chart README — add an "Observability (opt-in)" section: enable with `--set bundledObservability=true`; reach the UI with `kubectl port-forward svc/<release>-kdive-prometheus 9090:9090`; confirm targets at `http://localhost:9090/targets` (the live not-CI-covered check); emptyDir/retention defaults and how to override for real use; a copy-pasteable `PodMonitor` for Operator clusters and a note that an existing annotation-discovery Prometheus already gets the targets.
- [ ] **Step 2:** Compose README — add an "Observability (opt-in)" section: `docker compose --profile obs up -d prometheus`, open `http://localhost:9090`, confirm targets, note ephemeral storage.
- [ ] **Step 3:** Runbook `kubernetes-deploy.md` — add a short "Collect metrics" note: enable the value, port-forward, confirm all three components are `up` targets and `kdive_*` series are present (the live verification the render tests cannot do).
- [ ] **Step 4:** Run `just docs-links` and `just check-mermaid` — Expected: both pass.
- [ ] **Step 5:** Commit `docs: document opt-in Prometheus (helm + compose + runbook, BYO path)`.

---

## Self-Review

- **Spec coverage:** Helm bundledObservability + 5 objects (Tasks 1-2), namespaced RBAC (Task 2), annotation relabeling scoped to release ns (Task 2), ClusterIP-9090 (Task 2), compose obs-profile service + static config + published 9090 only (Task 3), content-level assertions closing the render-vs-scrape gap (Tasks 2-3), optional promtool (Task 4), BYO + runbook note + ephemeral-storage docs (Task 5), no `src/kdive/**` change (none of the tasks touch it). All spec sections map to a task.
- **Placeholder scan:** every code/template/YAML step shows full content; no TBD/"handle edge cases".
- **Type/name consistency:** `<fullname>-prometheus` (objects) and `<fullname>-prometheus-config` (ConfigMap) used consistently across Task 2 template and tests; `_AUX_PORTS` reused from both existing test modules; `observability.{image,retention,scrapeInterval,uiPort}` defined in Task 1 and consumed in Task 2.

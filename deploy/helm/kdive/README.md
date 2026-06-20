# kdive Helm chart

Deploys the three kdive processes — server, worker, reconciler — plus a migrate
one-shot Job, against operator-provided Postgres/MinIO/OIDC backends. Implements
ADR-0088 (deployment & packaging).

This README is the value/flag reference. For an end-to-end bring-up — building and
pushing the image, standing up backends, reaching the MCP endpoint, and verifying —
follow [`docs/operating/runbooks/kubernetes-deploy.md`](../../../docs/operating/runbooks/kubernetes-deploy.md).

## Install (external backends, production)

> **Installing from a source checkout?** The chart's default image tag is `appVersion`,
> which tracks the *next unreleased* version (ADR-0041) and has no published image until
> that version is cut — a bare install would `ImagePullBackOff`. From a checkout, pin the
> rolling image: add `--set image.tag=edge` **and `--set image.pullPolicy=Always`**. `:edge` is
> mutable (overwritten on every push to `main`), so with the chart default `IfNotPresent` a node
> that cached an older `edge` keeps serving stale code across `helm install`/`upgrade`; `Always`
> forces a re-pull (`values-demo.yaml` sets it for you). A bare `appVersion` default is correct
> only when you install a cut release / published chart.

```sh
helm install kdive deploy/helm/kdive \
  --set config.KDIVE_DATABASE_URL='postgresql://<user>:<password>@<host>:5432/kdive' \
  --set config.KDIVE_OIDC_ISSUER=https://idp.example/realms/kdive \
  --set config.KDIVE_OIDC_JWKS_URI=https://idp.example/realms/kdive/protocol/openid-connect/certs \
  --set config.KDIVE_S3_ENDPOINT_URL=https://s3.example
```

The migrate Job runs as a `pre-install`/`pre-upgrade` hook — the external backend
already exists, so migrations apply before the app rollout. Migrations must be
backward-compatible (expand-contract); the runner is forward-only (ADR-0015), so
rollback is image-only and the prior image must tolerate the newer schema.

## Upgrade

**Do not upgrade with bare `helm upgrade --reuse-values`.** `--reuse-values` carries the
previous release's merged values and *ignores the fresh `values.yaml` defaults*, so any
config default added in a newer chart (e.g. `config.KDIVE_LOCAL_LIBVIRT_ENABLED: "false"`,
ADR-0127) never reaches an already-installed release — the new image runs without it. Capture
your current values and merge fresh defaults instead:

```sh
helm get values kdive -o yaml > kdive-values.yaml   # your overrides only (no chart defaults)
helm upgrade kdive deploy/helm/kdive -f kdive-values.yaml \
  --set image.tag=<new-tag>
```

Passing the captured file with `-f` preserves your overrides **and** layers the new chart's
`values.yaml` defaults on top, so a new config default is not silently dropped. As a backstop
the chart renders `KDIVE_LOCAL_LIBVIRT_ENABLED` from a defensive `default "false"`, so even a
bare `--reuse-values` no longer reintroduces the local-libvirt reaper crash-loop — but
`-f kdive-values.yaml` is the general fix for *any* future config-default drift, so prefer it.

A `helm upgrade` that changes a `config.*` value now rolls the server/worker/reconciler
Deployments automatically (a `checksum/config` pod annotation, ADR-0134) — no manual
`kubectl rollout restart` is needed. The bundled Postgres/MinIO backends carry no such
annotation, so a config change never rolls their `emptyDir` pods.

## Bundled backends (demo only)

`bundledBackends=true` (co-set with `demoAcknowledged=true`) stands up first-party Postgres,
MinIO, and a mock-OIDC issuer as in-chart Deployments on `emptyDir`: **a pod restart drops all
state by design.** The issuer mints valid `aud=kdive` tokens for any caller, so the chart
forces `service.type=ClusterIP` on this path — reach MCP with `kubectl port-forward`, never
expose it.

```sh
helm install kdive deploy/helm/kdive -f deploy/helm/kdive/values-demo.yaml
helm test kdive    # mints a token, asserts tools/list returns tools
```

`values-demo.yaml` pins `image.tag=edge` (the rolling published image); without a published
image the demo cannot pull. The demo migrate Job runs `post-install` behind a DB-readiness
init container.

### Single object store (remote-libvirt & external uploads)

A deployment has **one** object store, and three parties use it over presigned URLs: the
in-cluster worker, an external uploader (`runs.complete_build`), and the remote-libvirt guest
(`install` fetch + `kdump` capture). The bundled MinIO defaults to **ClusterIP** with
`KDIVE_S3_ENDPOINT_URL=http://<release>-minio:9000` — in-cluster only, so `host_dump` capture and
`introspect.from_vmcore` work but external uploads and remote-libvirt `install`/`kdump` capture do
not. To use the bundled store off-cluster, expose it and point the endpoint at an address all three
parties resolve to the same store:

```sh
helm get values kdive -o yaml > kdive-values.yaml
helm upgrade kdive deploy/helm/kdive \
  -f deploy/helm/kdive/values-demo.yaml -f kdive-values.yaml \
  --set demo.minio.service.type=NodePort --set demo.minio.service.nodePort=30900 \
  --set config.KDIVE_S3_ENDPOINT_URL=http://<node-ip>:30900
```

(Capture-and-`-f`, not `--reuse-values` — see [Upgrade](#upgrade). Later `-f` files and
`--set` flags win, so your captured overrides and the new endpoint layer on top of the demo
defaults.)

`config.KDIVE_S3_ENDPOINT_URL` now overrides the bundled default in both modes (it previously could
not). The cluster network/firewall must permit the chosen NodePort; a cluster that only admits
`:6443` needs a node firewall change or an external S3 all parties reach. See the
[Kubernetes deploy runbook §7](../../../docs/operating/runbooks/kubernetes-deploy.md) for the full
topology (with a diagram).

Exposing the store opens a companion NetworkPolicy on :9000 from `demo.minio.service.sourceRanges`
(default `0.0.0.0/0`). That allowlist does **not** restrict by client IP: under the Service's
default `externalTrafficPolicy: Cluster`, external traffic is SNAT'd to a node IP before the
NetworkPolicy controller sees it, so the rule matches iff the node IP is in range — effectively
all-or-nothing on the node/pod CIDR. The bundled store is fronted by static demo credentials, so
restrict real access at your LoadBalancer or network firewall; treat `sourceRanges` as a coarse
node-CIDR gate, not per-client access control.

Every token the bundled issuer mints carries the claim set in `demo.oidc.claims`,
defaulting to `admin` on project `demo` plus all three platform roles (`platform_admin`,
`platform_operator`, `platform_auditor`) — a full RBAC grant, so a stock demo deploy can
exercise the whole authz surface. `aud` is pinned to `["kdive"]` by the chart and cannot
be overridden. To test a denial per session, mint a narrowed token with
`scripts/demo-token.sh --role viewer` (or `--role operator`) — the chart also registers
`client_id: kdive-demo-<role>` issuer variants that carry only that project role and no
platform roles. To change the *default* grant deploy-wide, narrow it with
`--set demo.oidc.claims.roles.demo=viewer` or drop `platform_roles`. The grant only
authorizes operations on a project with a budget/quota row; the demo seeds project `demo`
via `kdive seed-demo`, so if you change the project name, seed it
(`kdive seed-demo --project <name>`). This is demo-only — the issuer mints a valid token
for any caller and must never front a real RBAC boundary.

## Health probes & scrape (ADR-0090 §5)

Every Deployment wires `livenessProbe` → `/livez` and `readinessProbe` → `/readyz` on
the process's aux port (`server` 9464, `worker` 9465, `reconciler` 9466), and carries
`prometheus.io/scrape` pod annotations pointing a pull-based collector at `/metrics` on
that port. Liveness tracks the loop being alive, readiness tracks the process's own
backend set — a failing `/readyz` (a backend down) withdraws/gates the pod but does
**not** trip liveness, so a live-but-not-ready pod is never killed.

The aux listener binds `0.0.0.0:<port>` *inside* the pod (set per Deployment via
`KDIVE_HEALTH_BIND_ADDR`, overriding the loopback registry default) so the node kubelet
and the scrape can reach it. **No Service fronts the aux port** — only the server's MCP
`8000` is exposed — so the unauthenticated `/readyz`/`/metrics` stay pod-local. The
network boundary is their access control; scope it with a NetworkPolicy if your scrape
source is not pod-local.

The MCP `8000` Service defaults to `ClusterIP` (`kubectl port-forward` to reach it). Set
`service.type=NodePort` — optionally pinning `service.nodePort` — or front it with an
Ingress/LoadBalancer to expose it outside the cluster.

### Bundled Prometheus (opt-in — ADR-0189)

Nothing scrapes the `/metrics` above by default. Set `bundledObservability=true` to deploy an
in-cluster Prometheus that discovers all three components via the `prometheus.io/scrape`
annotations and collects them:

```sh
helm upgrade --install kdive deploy/helm/kdive ... --set bundledObservability=true
```

It is independent of `bundledBackends` (the scrape targets are the app pods, present on both the
demo and external-backend paths) and **off by default** — production installs are BYO (below).
It renders a `ServiceAccount`, a **namespaced** `Role`/`RoleBinding` (only `get`/`list`/`watch`
on `pods`, scoped to the release namespace), the scrape-config `ConfigMap`, the Prometheus
`Deployment`, and a `ClusterIP` Service on `9090`. Reach the UI and confirm targets:

```sh
kubectl port-forward svc/<release>-kdive-prometheus 9090:9090
# open http://localhost:9090/targets — all three components (server/worker/reconciler) should be UP
# then query e.g. kdive_job_queue_depth to confirm kdive_* series are present
```

Defaults match the bundled demo posture: `emptyDir` storage (a Prometheus pod restart drops
history) and short retention. Override `observability.retention`, `observability.scrapeInterval`,
and `observability.image` as needed; for durable storage swap the `emptyDir` for a PVC (edit
`templates/demo/prometheus.yaml`). The Prometheus Service is `9090`-only — the unauthenticated
aux `/metrics` is never re-exposed off the cluster (keep it that way; do not NodePort/Ingress it).

**BYO Prometheus (production / operator clusters).** Two paths, both relying on the
`prometheus.io/*` annotations the chart already emits — leave `bundledObservability=false`:

- An existing **annotation-discovery** Prometheus (the common `kubernetes_sd_configs` pod-role
  setup) already picks up the kdive pods; no chart change needed.
- A cluster running the **Prometheus Operator** — add a `PodMonitor` (not templated here, to
  avoid a CRD dependency on stock clusters). Each process exposes a different aux port and the
  port is not a named container port, so target each by number with one endpoint per port:

  ```yaml
  apiVersion: monitoring.coreos.com/v1
  kind: PodMonitor
  metadata:
    name: kdive
  spec:
    selector:
      matchLabels:
        app.kubernetes.io/name: kdive
    podMetricsEndpoints:
      - targetPort: 9464   # server
        path: /metrics
      - targetPort: 9465   # worker
        path: /metrics
      - targetPort: 9466   # reconciler
        path: /metrics
  ```

  (Each endpoint targets every selected pod, so the two ports a given pod does not listen on
  show as down — harmless. To avoid that, reuse the bundled chart's annotation-relabeling job
  from `templates/demo/prometheus-config.yaml` as an Operator `additionalScrapeConfigs` instead.)

## Secrets

`config.*` renders into a plain ConfigMap, so it is for **non-secret** configuration
(endpoints, bucket, region, OIDC issuer). Do not put a database DSN with an embedded
password or S3 secret keys into `config.*` in production.

### File-ref secrets (`secrets.secretName`)

The file-ref secret backend (ADR-0027/ADR-0088 decision 3) resolves credentials from
files under `KDIVE_SECRETS_ROOT` — remote-libvirt TLS client cert/key/CA refs in
`systems.toml` and debug-session secrets. Create a Secret whose keys are the credential
filenames, then point `secrets.secretName` at it:

```sh
kubectl create secret generic kdive-remote-tls \
  --from-file=clientcert.pem=client.pem \
  --from-file=clientkey.pem=clientkey.pem \
  --from-file=cacert.pem=ca.pem

cat >systems.toml <<'EOF'
schema_version = 2

[[image]]
provider = "remote-libvirt"
name = "fedora-kdive-ready"
arch = "x86_64"
format = "qcow2"
root_device = "/dev/vda"
visibility = "private"
[image.source]
kind = "staged"
volume = "fedora-kdive-ready.qcow2"

[[remote_libvirt]]
name = "lab-remote"
uri = "qemu+tls://host.example/system"
gdb_addr = "192.0.2.20"
gdbstub_range = "47000:47099"
client_cert_ref = "clientcert.pem"
client_key_ref = "clientkey.pem" # pragma: allowlist secret
ca_cert_ref = "cacert.pem"
base_image = "fedora-kdive-ready"
cost_class = "remote"
concurrent_allocation_cap = 4
EOF
kubectl create configmap kdive-systems --from-file=systems.toml=systems.toml

helm install kdive deploy/helm/kdive \
  --set secrets.secretName=kdive-remote-tls \
  --set systems.configMapName=kdive-systems \
  --set config.KDIVE_REMOTE_LIBVIRT_STORAGE_POOL=default \
  --set config.KDIVE_REMOTE_LIBVIRT_NETWORK=default \
  --set config.KDIVE_REMOTE_LIBVIRT_MACHINE=pc \
  --set config.KDIVE_DATABASE_URL=... --set config.KDIVE_OIDC_ISSUER=...
```

The chart mounts the Secret **read-only** (`defaultMode 0440`) at `secrets.mountPath`
(default `/etc/kdive/secrets`) on the server, worker, and reconciler, and sets
`KDIVE_SECRETS_ROOT` to that path. Refs are resolved **relative to the root**, so a
bare key name like `clientcert.pem` is enough — the Kubernetes Secret volume's `..data`
symlink indirection resolves correctly, and a ref escaping the root is rejected. Leaving
`secrets.secretName` empty mounts nothing.

When `systems.configMapName` is set, the chart mounts that ConfigMap read-only at
`systems.mountPath` (default `/etc/kdive/systems`) on migrate, server, worker, and
reconciler, and sets `KDIVE_SYSTEMS_TOML` to the mounted `systems.fileName`. Use
`config.*` only for the remaining remote-libvirt host-topology env vars that are not
inventory identity: `KDIVE_REMOTE_LIBVIRT_STORAGE_POOL`, `KDIVE_REMOTE_LIBVIRT_NETWORK`,
and `KDIVE_REMOTE_LIBVIRT_MACHINE`.

For S3, prefer IRSA/workload identity, or a managed Secret you `envFrom` onto the pods.
The fixed `demoCredentials` are non-secret by design: the demo data they guard is
throwaway `emptyDir` state.

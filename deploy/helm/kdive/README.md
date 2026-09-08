# kdive Helm chart

Deploys four long-running Kubernetes workloads — server, worker, reconciler, lifecycle witness —
plus a migrate one-shot Job, against operator-provided Postgres/MinIO/OIDC backends. Implements
ADR-0088 (deployment & packaging).

This README is the value/flag reference. For an end-to-end bring-up — building and
pushing the image, standing up backends, reaching the MCP endpoint, and verifying —
follow [`docs/operating/runbooks/kubernetes-deploy.md`](../../../docs/operating/runbooks/kubernetes-deploy.md).

## Install (external backends, production)

Use the [deployment runbook](../../../docs/operating/runbooks/kubernetes-deploy.md) for image
selection, backend checks, required Secrets, installation, and verification. The complete
value definitions and defaults are in [`values.yaml`](values.yaml); runtime environment settings
are in the [configuration reference](../../../docs/guide/reference/config.md).

The external artifact bucket requires bucket-wide versioning `Enabled`, MFA Delete off, and no
prefix/folder exclusions. Runtime permissions include `s3:GetObjectVersion`,
`s3:GetBucketVersioning`, `s3:ListBucketVersions`, and `s3:DeleteObjectVersion` in addition to
ordinary object access. Follow the [object-store preflight](../../../docs/operating/install.md#object-store-preflight)
to prove exact-version reads before starting KDIVE.

## Upgrade

**Worker-fence releases use the [staged worker-fence upgrade procedure](
../../../docs/operating/runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade).**
It captures live replica counts, proves every KDIVE workload is stopped for migration, and restores
workers only after the target-image witness is ready. Do not use a rolling upgrade or an old image
after the migration.

**Capture publication protocol 4 is build-new-only.** Install this release with a new empty
Postgres database and a new versioned object-store bucket or namespace. Migration 0113 refuses
existing worker, job, capture-operation, or artifact rows. No Helm cutover, rollback, migration,
preservation, inspection, or cleanup path exists for protocol-3 state or objects.

On the fresh installation, each worker proves the store's conditional-create behavior before it
becomes ready. A failed probe prevents job claims; correct the bucket's versioning or
conditional-create behavior and restart the worker.

On a fresh bundled install, Helm renders the worker StatefulSet at zero replicas. The post-install
migration runs first; a later post-install scaler Job, authorized only to patch that StatefulSet's
namespaced scale resource and its own exact RoleBinding, restores the configured count and then
empties that binding's subjects. A cleanup failure fails and retains the hook Job, whose log prints
the exact namespaced recovery command. A failed migration therefore creates no worker Pod. Upgrades
retain normal replicas until the pre-upgrade migration boundary, and the cutover wrapper performs
the required stop first.

### Worker capacity: one in-flight job per dispatch lane (ADR-0550)

A worker replica now runs **one in-flight job per accepted dispatch lane — two by default**, where
it previously ran one in total. `KDIVE_WORKER_ACCEPTED_LANES` defaults to `default,state-fenced`:
the `state-fenced` lane carries `restore`, `reprovision`, and `snapshot`, whose jobs fence a System
or Snapshot from the moment they are enqueued, so they get a claim loop that unrelated long work
cannot block.

Two consequences for sizing, neither requiring operator action to take effect:

- **CPU, memory, and database connections per replica rise on upgrade.** A replica sized against
  the old one-job-per-process behavior is now under-provisioned. `worker.replicas` is unchanged, so
  this is a capacity note rather than a migration step — but review requests and limits before a
  busy upgrade.
- **Idle cost scales with the lane count too.** Each lane polls independently every poll interval
  whether or not work exists, and the `state-fenced` lane is idle most of the time, so a fleet pays
  one extra empty-queue poll per replica per interval.

Setting `KDIVE_WORKER_ACCEPTED_LANES` to a single lane restores the old footprint. It also starves
every omitted lane: those jobs are never claimed by this worker, and if no deployed worker accepts
the lane they are never claimed at all — the System or Snapshot they fence stays fenced, and
nothing sweeps it, because the abandoned-job repair reaps only `running` rows. A worker that omits
a routed lane logs a warning naming it at startup.

For value changes on an upgrade-compatible release, follow the runbook's
[config-default handling](../../../docs/operating/runbooks/kubernetes-deploy.md#upgrading-a-release-config-default-drift--adr-0134).
Apply captured overrides to the new defaults; do not use bare `--reuse-values`.

A `helm upgrade` that changes a shared `config.*` value rolls server, worker, and reconciler
workloads automatically (a `checksum/config` pod annotation, ADR-0134) — no manual
`kubectl rollout restart` is needed. The bundled Postgres/MinIO backends carry no such
annotation, so a config change never rolls their `emptyDir` pods. The lifecycle witness consumes
only explicit authority settings; changing its database Secret ref rolls that workload alone.

### Worker storage and capacity

Each worker StatefulSet replica has its own build and install PVCs (ADR-0514).
`worker.replicas` defaults to 2; `worker.persistence.build.size` and
`worker.persistence.install.size` are **per replica**. The default requests
2 × (10Gi + 5Gi) = 30Gi. Size replica count and storage together.

These volumes are disposable scratch on this chart's remote-provider path. Postgres holds
state of record and the object store holds durable artifacts. Discarding scratch requires
rebuilding or fetching it again. Deployment-era release-scoped claims are not adopted by the
StatefulSet's per-ordinal `volumeClaimTemplates`.

`volumeClaimTemplates` is immutable, so changing a persistence size through an ordinary
`helm upgrade` fails. Replacing the worker controller requires planned maintenance that
preserves the [stop-old-first worker authority boundary](
../../../docs/operating/runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade); do not
bypass it with force deletion.

Scaling down or uninstalling deletes the departing replicas' claims:
`persistentVolumeClaimRetentionPolicy` is `Delete`/`Delete`. The chart requires Kubernetes
`>=1.27` for that retention policy. This deletion is safe only under the scratch assumption.

> The scratch claim does **not** hold on the local-libvirt path, where the staged
> `kernel`/`initrd` are the domain XML's direct-boot files and durable for the System's
> lifetime. This chart cannot reach that path — it sets `KDIVE_LOCAL_LIBVIRT_ENABLED: "false"`
> and mounts neither a libvirt socket nor `/dev/kvm`. If you wire one up anyway, scaling the
> worker down un-boots the Systems installed by the departing ordinals until you reinstall them.

## Bundled backends (demo only)

`bundledBackends=true` (co-set with `demoAcknowledged=true`) stands up first-party Postgres,
MinIO, and a mock-OIDC issuer as in-chart Deployments on `emptyDir`: **a pod restart drops all
state by design.** The issuer mints valid `aud=kdive` tokens for any caller, so the chart
forces `service.type=ClusterIP` on this path — reach MCP with `kubectl port-forward`, never
expose it.

The demo also requires the [worker credential-broker Secrets](
../../../docs/operating/runbooks/kubernetes-deploy.md#worker-credential-broker-secrets).
The chart mounts these authority credentials but does not generate them.

Follow the [bundled demo install procedure](
../../../docs/operating/runbooks/kubernetes-deploy.md#bundled-demo-install).
Fresh bundled installs must omit `--wait`: the post-install migration creates the database
roles before app readiness can pass. Upgrades use a pre-upgrade migration hook.

`values-demo.yaml` pins `image.tag=edge` (the rolling published image); without a published
image the demo cannot pull. The demo migrate Job runs `post-install` behind a DB-readiness
init container.

The bundled `minio-init` Job creates the configured bucket, enables bucket-wide versioning, then
fails closed unless MinIO reports `Enabled`, MFA Delete off, and no prefix/folder exclusions. The
server, worker, and reconciler Pods each repeat that policy check in an `mc` init container. Until
the bucket exists and passes, Kubernetes may restart the init container, but it cannot start the
app container. External-backend workloads omit this MinIO-specific barrier and use the runtime S3
validation described above.

### Single object store (remote-libvirt & external uploads)

A deployment has **one** object store, and three parties use it over presigned URLs: the
in-cluster worker, an external uploader (`runs.complete_build`), and the remote-libvirt guest
(`install` fetch + `kdump` capture). The bundled MinIO defaults to **ClusterIP** with
`KDIVE_S3_ENDPOINT_URL=http://<release>-kdive-minio:9000` — in-cluster only, so `host_dump` capture and
`introspect.from_vmcore` work but external uploads and remote-libvirt `install`/`kdump` capture do
not. `config.KDIVE_S3_ENDPOINT_URL` overrides that default in both modes. Follow the
[object-store exposure procedure](../../../docs/operating/runbooks/kubernetes-deploy.md#the-bundled-demos-object-store-is-in-cluster-only--expose-it-for-remote-libvirt)
to configure an endpoint reachable by all three consumers and verify the network route.

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
via `kdive seed-project`, so if you change the project name, seed it
(`kdive seed-project --project <name>`). This is demo-only — the issuer mints a valid token
for any caller and must never front a real RBAC boundary.

## Health probes & scrape (ADR-0090 §5)

Every long-running workload wires `livenessProbe` → `/livez` and `readinessProbe` → `/readyz` on
the process's aux port (`server` 9464, `worker` 9465, `reconciler` 9466, and
`lifecycle-witness` 9467), and carries
`prometheus.io/scrape` pod annotations pointing a pull-based collector at `/metrics` on
that port. Liveness tracks the loop being alive, readiness tracks the process's own
backend set — a failing `/readyz` (a backend down) withdraws/gates the pod but does
**not** trip liveness, so a live-but-not-ready pod is never killed.

The aux listener binds `0.0.0.0:<port>` *inside* the pod (set per workload via
`KDIVE_HEALTH_BIND_ADDR`, overriding the loopback registry default) so the node kubelet
and the scrape can reach it. **No Service fronts the aux port** — only the server's MCP
`8000` is exposed — so the unauthenticated `/readyz`/`/metrics` stay pod-local. The
network boundary is their access control; scope it with a NetworkPolicy if your scrape
source is not pod-local.

The MCP `8000` Service defaults to `ClusterIP` (`kubectl port-forward` to reach it). Set
`service.type=NodePort` — optionally pinning `service.nodePort` — or front it with an
Ingress/LoadBalancer to expose it outside the cluster.

### Bundled Prometheus (opt-in — ADR-0189)

Set `bundledObservability=true` to deploy Prometheus, which discovers all four components
through their `prometheus.io/scrape` annotations. It is off by default and independent of
`bundledBackends`. The chart creates namespaced Pod-discovery RBAC, a scrape ConfigMap,
a Deployment, and a ClusterIP Service on port 9090. Follow the runbook's
[metrics verification](../../../docs/operating/runbooks/kubernetes-deploy.md#collect-metrics-opt-in--adr-0189)
to confirm target health and series collection.

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
      - targetPort: 9467   # lifecycle witness
        path: /metrics
  ```

  (Each endpoint targets every selected pod, so the three ports a given pod does not listen on
  show as down — harmless. To avoid that, reuse the bundled chart's annotation-relabeling job
  from `templates/demo/prometheus-config.yaml` as an Operator `additionalScrapeConfigs` instead.)

## Secrets

`config.*` renders into a plain ConfigMap, so it is for **non-secret** configuration
(endpoints, bucket, region, OIDC issuer). Do not put a database DSN with an embedded
password or S3 secret keys into `config.*` in production.

### Database credentials

Create login members for the four non-login capability roles (`kdive_server`, `kdive_worker`,
`kdive_reconciler`, and `kdive_lifecycle_witness`) and a separate migration owner. The migration
owner needs the schema and role authority required by migrations; no runtime login may inherit it.
On a new database, pre-create the exact capability roles and memberships or perform the equivalent
two-stage migration and membership grant before allowing runtime Pods to start.

The [deployment runbook](../../../docs/operating/runbooks/kubernetes-deploy.md#4-install-the-chart)
owns Secret creation. One Secret with five distinct keys is supported.

Point `databaseCredentials.migration`, `.server`, `.worker`, `.reconciler`, and
`.lifecycleWitness` at their respective Secret names and keys. The chart rejects missing refs and
any detectable reuse of the migration ref. The migration Job receives only the migration ref;
runtime Pods receive only their process ref, with the lifecycle-witness ref confined to the
dedicated lifecycle-witness workload. Ref changes roll only affected runtime workloads.

### File-ref secrets (`secrets.secretName`)

The file-ref secret backend (ADR-0027/ADR-0088 decision 3) resolves credentials from
files under `KDIVE_SECRETS_ROOT` — remote-libvirt TLS client cert/key/CA refs in
`systems.toml` and debug-session secrets. Set `secrets.secretName` to a pre-existing Secret
whose keys match those refs. The [remote host registration guide](
../../../docs/operating/runbooks/remote-libvirt-host-setup.md#3-register-remote-libvirt-on-the-deployment)
owns inventory and TLS mapping; the [deployment runbook](
../../../docs/operating/runbooks/kubernetes-deploy.md#3-create-the-file-ref-secret-if-using-remote-libvirt-or-debug-session-secrets)
owns Secret installation.

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
The lifecycle witness is a dedicated Deployment and process with its own database credential,
envelope key, TLS private key, and service account. Its service account has `get` and `patch`
permission only for the configured worker Pod names. The reconciler and server have no Pod
authority or witness secrets. Worker
holders include the downward-API Pod UID, and the witness records a specific terminal Pod
incarnation before removing its finalizer. Scaling `worker.replicas`
does not shrink the Role's bounded `resourceNames`: `worker.deathVerificationOrdinalCeiling`
defaults to 32 and is recovery history. Raise it before scaling beyond it, and never lower it after
an ordinal has run. No cluster-wide Pod permission is required.

### Upgrading worker-fence authority

For an existing release, use the [staged worker-fence upgrade procedure](
../../../docs/operating/runbooks/kubernetes-deploy.md#staged-worker-fence-upgrade).
It is the only supported path for migration, credential handling, witness readiness, and worker
restore. Do not force-delete Pods, remove finalizers manually, or use database-owner access to
bypass the witness: such bypasses retain pins rather than releasing them.

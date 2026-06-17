# ADR 0134 — Chart upgrade correctness: config-checksum rollout + config-default drift

- **Status:** Accepted
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Two `helm upgrade` correctness gaps on the kdive chart surfaced on the `kdive-demo`
deployment (#469, #470). Both refine ADR-0088 (deployment & packaging) and follow
ADR-0127 (#459), which added the `KDIVE_LOCAL_LIBVIRT_ENABLED` config key defaulting to
`"false"` in the chart so a remote-libvirt-only k8s deploy stops the local-libvirt reaper
flood out of the box.

**(#470) A `config.*` change does not restart the consuming pods.** The server, worker,
and reconciler read `config.*` once at process start via `envFrom` of the
`<release>-kdive-config` ConfigMap. The Deployment pod templates carry no annotation that
varies with the ConfigMap's contents, so a `helm upgrade` that changes only a `config.*`
value updates the ConfigMap but leaves every pod template byte-identical. Helm reports
`STATUS: deployed`, `--wait` returns immediately (no pod template changed), and the
running pods keep the stale env until something else rolls them. The operator must run a
manual `kubectl rollout restart` — and the broad `-l app.kubernetes.io/name=kdive`
selector also hits the emptyDir postgres/minio backends, wiping demo data. Silent footgun:
config changes, helm says success, nothing actually changes.

**(#469) `helm upgrade --reuse-values` drops new chart-default config keys.**
`--reuse-values` carries the previous release's merged values and ignores the fresh
`values.yaml` defaults, so a config default added in a later chart never reaches an
already-installed release. Upgrading `kdive-demo` with bare `--reuse-values` left
`KDIVE_LOCAL_LIBVIRT_ENABLED` unset, and the reconciler kept crash-looping the every-30s
`libvirt-sock: No such file` reaper failure on the new image — the ADR-0127 code fix
shipped, but the value never applied. Every existing operator who upgrades with bare
`--reuse-values` keeps the crash-loop; the chart default only helps fresh installs.

## Decision

### 1. Config-checksum pod annotation (#470)

Add a `checksum/config` annotation (the standard Helm pattern) to the **pod template** of
`deployment-{server,worker,reconciler}.yaml`:

```yaml
annotations:
  checksum/config: {{ include (print $.Template.BasePath "/configmap.yaml") . | sha256sum }}
```

A change to any `config.*` value changes the rendered ConfigMap, which changes the
checksum, which changes the pod template — so `helm upgrade` rolls exactly the three app
Deployments that consume the ConfigMap. The annotation lives only on those three pod
templates, so postgres/minio (which do not consume `<release>-kdive-config`) are untouched.

The checksum hashes only the **chart-rendered** `configmap.yaml`. The optional `systems`
and `fixtures` ConfigMaps are operator-authored, referenced by name, and not rendered by
this chart — their content is not visible to `helm template`, so it cannot be hashed here
and is out of scope. (A change to `systems.toml` is already picked up at runtime by the
fail-open reconciler, ADR-0121; a change to the fixtures catalog needs an operator
rollout, unchanged by this ADR.)

### 2. Defensive `KDIVE_LOCAL_LIBVIRT_ENABLED` default + reuse-values upgrade docs (#469)

Combine three complementary, low-risk measures:

- **Chart robustness.** Render `KDIVE_LOCAL_LIBVIRT_ENABLED` from a defensive default so
  the key is `"false"` even when it is absent from a reused value-set. The `configmap.yaml`
  `range` over `.Values.config` only emits keys present in the merged values; excluding
  this one key from the range and emitting it explicitly alongside the existing
  computed-key block (`KDIVE_DATABASE_URL` etc., shared by both the bundled and external
  paths) with `.Values.config.KDIVE_LOCAL_LIBVIRT_ENABLED | default "false"` guarantees the
  rendered ConfigMap always carries it on **both** paths. An operator who genuinely runs a
  local libvirt socket still sets it `true` explicitly; the default only closes the
  reaper-flood footgun. The value is path-independent, so it renders identically whether or
  not `bundledBackends` is set.

- **Upgrade docs.** The bare `--reuse-values` invocations in the chart README and the
  Kubernetes deploy runbook are the trap. Replace them with the value-capture pattern
  (`helm get values kdive -o yaml > kdive-values.yaml`, then `helm upgrade … -f
  kdive-values.yaml`) which preserves operator overrides **and** merges fresh chart
  defaults. Add a dedicated "Upgrade" section that calls out config-default drift.

- **NOTES.txt warning.** The chart cannot determine whether a node runs `libvirtd` — that
  is host state, not a chart value — so NOTES does not assert a libvirt-presence fact.
  Instead, on the non-bundled path, NOTES carries a short post-install/upgrade reminder
  pointing at the value-capture upgrade procedure and warning that a bare `--reuse-values`
  drops new config defaults. This is unconditional guidance (it fires on every external
  deploy), not a per-key conditional, because NOTES renders only on `helm install`/`upgrade`
  (not `helm template`) and so cannot be exercised by the chart-render test suite; the
  defensive default above is the load-bearing, testable fix and NOTES is advisory backup.

### 3. Chart version bump

Bump `Chart.yaml` `version` `0.3.0 → 0.4.0` (chart behavior changed: pods now roll on a
config change, and the rendered ConfigMap always carries the local-libvirt key).
`appVersion` is unchanged — it tracks the pyproject version via `chart-version-check` and
must not be touched here.

## Consequences

- A `helm upgrade` that changes a `config.*` value now rolls server/worker/reconciler
  automatically; the manual `rollout restart` note in the runbook is removed.
- The config-checksum changes on **every** `config.*` change (including image-tag-only
  upgrades that touch nothing in the ConfigMap leave it unchanged — only ConfigMap content
  drives the roll). Postgres/minio never carry the annotation, so demo data is preserved.
- A reused value-set missing `KDIVE_LOCAL_LIBVIRT_ENABLED` now renders `"false"`, so an
  upgrade with bare `--reuse-values` no longer reintroduces the reaper crash-loop. The
  documented value-capture path is the recommended procedure regardless, because it is the
  general fix for *any* future config-default drift (this defensive default only protects
  the one key known to footgun today).
- One additional chart-render test file asserts: the checksum annotation is present on the
  three app pod templates and varies with a `config.*` change; postgres/minio carry no
  such annotation; the local-libvirt key renders `"false"` when absent from the value-set
  (on **both** the external and bundled paths) and honors an explicit `true`. NOTES is not
  asserted (it does not render under `helm template`).

## Alternatives considered

- **Checksum every consumed ConfigMap (systems/fixtures too).** Their content is not
  visible to `helm template` (operator-authored, referenced by name), so it cannot be
  hashed in the chart. Rejected as unimplementable here; runtime reconcile already covers
  `systems.toml`.
- **`--recreate-pods` / podAnnotations timestamp.** A timestamp annotation rolls pods on
  *every* upgrade including no-op ones, churning the cluster. The content checksum rolls
  only on a real config change. Rejected for the checksum.
- **Docs-only for #469 (no defensive default).** The value-capture upgrade path is the
  general fix, but it relies on every operator following it; the defensive default closes
  the one known crash-loop footgun even on a bare `--reuse-values`. Kept both: the default
  is a backstop, the docs are the general procedure.
- **Make the reaper tolerate a missing socket.** Already rejected in ADR-0127 (masks a
  real failure on a host that does run libvirt). Unchanged here.
